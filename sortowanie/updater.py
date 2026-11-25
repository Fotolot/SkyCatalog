"""Aktualizacja wersji przenośnej z GitHub Releases.

1. ``check_latest()`` – pyta https://api.github.com/repos/OWNER/REPO/releases/latest i porównuje
   ``tag_name`` z wersją programu. Błędy sieci/API zwracają None – nigdy nie blokują startu.
2. ``download()`` + ``extract()`` – pobiera ZIP do folderu tymczasowego i rozpakowuje go
   (z ochroną przed ścieżkami spoza folderu).
3. ``launch_update_script()`` – zapisuje i uruchamia skrypt pomocniczy ``.cmd``, po czym program
   się zamyka. Skrypt czeka na zamknięcie procesu, robi kopię zapasową bieżącej wersji w
   ``_backup``, kopiuje nowe pliki do folderu programu (z pominięciem danych użytkownika),
   a w razie błędu przywraca kopię. Na końcu uruchamia program ponownie.

Dane użytkownika SkyCatalog nie leżą w folderze programu – są w:
  %APPDATA%\\SkyCatalog       ustawienia.json, cache_osm.json, decyzje_lokalizacji.json,
                              historia_sortowania.json
  %LOCALAPPDATA%\\SkyCatalog  katalog.sqlite (+ -wal, -shm), miniatury\\, przegladarka\\ (cache mapy)
Aktualizacja nie zagląda do tych folderów. Dodatkowo – gdyby takie pliki lub foldery pojawiły się
obok .exe (np. ręcznie przeniesione) – są wyłączone z kopiowania i nigdy nie są nadpisywane ani
usuwane (PROTECTED_FILES / PROTECTED_DIRS).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import APP_NAME, EXE_NAME, GITHUB_REPO, RELEASE_ASSET, __version__

API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"
BACKUP_DIR = "_backup"
FAILED_FLAG = "--aktualizacja-nieudana"

# Dane użytkownika – nigdy nie kopiowane ani nadpisywane (wzorce plików i nazwy folderów)
PROTECTED_FILES = ["ustawienia.json", "cache_osm.json", "decyzje_lokalizacji.json", "historia_sortowania.json",
                   "*.sqlite", "*.sqlite-wal", "*.sqlite-shm", "*.db", "*.log", "*.tmp"]
PROTECTED_DIRS = [BACKUP_DIR, "miniatury", "przegladarka", "kafelki", "logs", "dane", "data"]


class UpdateError(Exception):
    pass


@dataclass
class Release:
    version: str
    tag: str
    notes: str
    asset_url: str | None
    asset_size: int
    page_url: str


def parse_version(text: str) -> tuple[int, ...] | None:
    """„v1.1.15” → (1, 1, 15). Nierozpoznany format → None."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+){0,3})", (text or "").strip())
    if not m:
        return None
    parts = tuple(int(x) for x in m.group(1).split("."))
    return parts + (0,) * (3 - len(parts)) if len(parts) < 3 else parts


def is_newer(remote: str, local: str = __version__) -> bool:
    r, l_ = parse_version(remote), parse_version(local)
    return bool(r and l_ and r > l_)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Folder, w którym leży .exe (wersja przenośna)."""
    return Path(sys.executable).resolve().parent if is_frozen() else Path(__file__).resolve().parent.parent


def _user_agent() -> str:
    return f"{APP_NAME}/{__version__} (aktualizacja wersji przenośnej)"


def parse_release(data: dict) -> Release | None:
    tag = str(data.get("tag_name") or "")
    if parse_version(tag) is None or data.get("draft") or data.get("prerelease"):
        return None
    asset = next((a for a in data.get("assets") or [] if a.get("name") == RELEASE_ASSET), None)
    return Release(version=tag.lstrip("v"), tag=tag, notes=str(data.get("body") or "").strip(),
                   asset_url=asset.get("browser_download_url") if asset else None,
                   asset_size=int(asset.get("size") or 0) if asset else 0,
                   page_url=str(data.get("html_url") or RELEASES_URL))


def check_latest(timeout: float = 8.0, opener=urllib.request.urlopen) -> Release | None:
    """Najnowsze wydanie albo None (brak internetu, limit API, brak wydań…)."""
    req = urllib.request.Request(API_URL, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": _user_agent(),
                                                   "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with opener(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    return parse_release(data) if isinstance(data, dict) else None


def download(release: Release, dest_dir: Path, progress: Callable[[int, int], None] | None = None,
             opener=urllib.request.urlopen) -> Path:
    if not release.asset_url:
        raise UpdateError(f"Wydanie {release.tag} nie zawiera pliku {RELEASE_ASSET}.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / RELEASE_ASSET
    req = urllib.request.Request(release.asset_url, headers={"User-Agent": _user_agent(),
                                                             "Accept": "application/octet-stream"})
    try:
        with opener(req, timeout=60) as resp, open(target, "wb") as f:
            total = int(resp.headers.get("Content-Length") or release.asset_size or 0)
            done = 0
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise UpdateError(f"Nie udało się pobrać aktualizacji: {exc}") from exc
    if release.asset_size and target.stat().st_size != release.asset_size:
        raise UpdateError("Pobrany plik ma nieprawidłowy rozmiar – spróbuj ponownie.")
    return target


def extract(zip_path: Path, dest_dir: Path) -> Path:
    """Rozpakowuje ZIP i zwraca folder zawierający SkyCatalog.exe."""
    dest = dest_dir.resolve()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.infolist():
                target = (dest / member.filename).resolve()
                if dest != target and dest not in target.parents:
                    raise UpdateError(f"Niebezpieczna ścieżka w archiwum: {member.filename}")
            zf.extractall(dest)
    except zipfile.BadZipFile as exc:
        raise UpdateError("Pobrane archiwum jest uszkodzone.") from exc
    for exe in sorted(dest.rglob(EXE_NAME), key=lambda p: len(p.parts)):
        return exe.parent
    raise UpdateError(f"W archiwum nie ma pliku {EXE_NAME}.")


def can_write(folder: Path) -> bool:
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".skycatalog_test_", delete=True):
            return True
    except OSError:
        return False


# Skrypt pomocniczy. Ścieżki przekazywane są jako ARGUMENTY (nie są wklejane w treść skryptu).
# Tylko znaki ASCII – cmd.exe czyta plik w stronie kodowej OEM.
SCRIPT_TEMPLATE = r"""@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem SkyCatalog - aktualizacja wersji przenosnej.
rem Argumenty: PID "folder_programu" "folder_nowej_wersji" "nazwa.exe"
set "PID=%~1"
set "APP=%~2"
set "NEW=%~3"
set "EXE=%~4"
set "LOG=%~dp0aktualizacja.log"
echo [%date% %time%] Start aktualizacji: "%APP%" > "%LOG%"
set /a N=0
:wait
tasklist /FI "PID eq %PID%" /NH 2>nul | find " %PID% " >nul
if errorlevel 1 goto closed
set /a N+=1
if %N% GEQ 120 goto timeout
timeout /t 1 /nobreak >nul
goto wait
:closed
timeout /t 1 /nobreak >nul
echo Kopia zapasowa do _backup >> "%LOG%"
if exist "%APP%\_backup" rmdir /s /q "%APP%\_backup"
robocopy "%APP%" "%APP%\_backup" /E /R:2 /W:1 /NP /XD "%APP%\_backup" {xd} /XF {xf} >> "%LOG%"
if errorlevel 8 goto backup_failed
echo Kopiowanie nowej wersji >> "%LOG%"
robocopy "%NEW%" "%APP%" /E /R:3 /W:2 /NP /XD {xd} /XF {xf} >> "%LOG%"
if errorlevel 8 goto restore
echo Aktualizacja zakonczona >> "%LOG%"
start "" "%APP%\%EXE%"
exit /b 0
:restore
echo BLAD kopiowania - przywracanie poprzedniej wersji z _backup >> "%LOG%"
robocopy "%APP%\_backup" "%APP%" /E /R:3 /W:2 /NP >> "%LOG%"
start "" "%APP%\%EXE%" {failed}
exit /b 1
:backup_failed
echo BLAD kopii zapasowej - aktualizacja przerwana, nic nie zmieniono >> "%LOG%"
start "" "%APP%\%EXE%" {failed}
exit /b 1
:timeout
echo Program nie zamknal sie w ciagu 2 minut - aktualizacja przerwana >> "%LOG%"
exit /b 1
"""


def build_script() -> str:
    quote = lambda items: " ".join(f'"{x}"' for x in items)  # noqa: E731
    text = SCRIPT_TEMPLATE.format(xd=quote(PROTECTED_DIRS), xf=quote(PROTECTED_FILES), failed=FAILED_FLAG)
    text.encode("ascii")                      # gwarancja: skrypt tylko w ASCII
    return text.replace("\n", "\r\n")


def write_script(work_dir: Path) -> Path:
    script = work_dir / "aktualizuj_skycatalog.cmd"
    script.write_bytes(build_script().encode("ascii"))
    return script


def launch_update_script(script: Path, new_dir: Path, target_dir: Path, pid: int | None = None) -> None:
    """Uruchamia skrypt w tle (bez okna konsoli). Program powinien zaraz po tym się zamknąć."""
    if sys.platform != "win32":
        raise UpdateError("Automatyczna aktualizacja działa tylko w Windows.")
    pid = pid or os.getpid()
    cmd = f'cmd.exe /d /s /c ""{script}" {pid} "{target_dir}" "{new_dir}" "{EXE_NAME}""'
    flags = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    subprocess.Popen(cmd, creationflags=flags, close_fds=True, cwd=str(script.parent))


def prepare_update(release: Release, progress: Callable[[int, int], None] | None = None) -> tuple[Path, Path]:
    """Pobiera i rozpakowuje wydanie; zwraca (skrypt, folder_nowej_wersji)."""
    work = Path(tempfile.gettempdir()) / f"{APP_NAME}-aktualizacja-{release.version}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    zip_path = download(release, work, progress)
    new_dir = extract(zip_path, work / "nowa_wersja")
    return write_script(work), new_dir
