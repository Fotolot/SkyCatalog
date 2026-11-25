"""Aktualizacja wersji przenośnej z GitHub Releases.

1. ``check_latest()`` – pyta https://api.github.com/repos/OWNER/REPO/releases/latest i zwraca
   ``CheckResult``: wydanie albo opis błędu. Błędy nigdy nie blokują startu; każdy jest zapisywany
   (typ, komunikat, traceback, kod HTTP…) do %LOCALAPPDATA%\\SkyCatalog\\logs\\aktualizacja.log.
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
import traceback
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import APP_NAME, EXE_NAME, GITHUB_REPO, RELEASE_ASSET, __version__
from .config import local_data_dir

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
    # Tylko ASCII: nagłówki HTTP muszą dać się zakodować w latin-1 (polskie litery powodowały
    # UnicodeEncodeError jeszcze przed wysłaniem zapytania – sprawdzanie aktualizacji zawsze zawodziło).
    return f"{APP_NAME}/{__version__} (aktualizacja wersji przenosnej; +https://github.com/{GITHUB_REPO})"


# ── Wynik sprawdzania i dziennik diagnostyczny ─────────────────

STATUS_OK = "ok"               # poprawne wydanie (``release``)
STATUS_ERROR = "błąd"          # błąd sieci, HTTP, TLS, JSON… (``error``)
STATUS_INVALID = "niepoprawne" # odpowiedź nie jest poprawnym wydaniem (``error``)
LOG_MAX_BYTES = 512 * 1024


@dataclass
class CheckResult:
    status: str
    release: Release | None = None
    error: str = ""            # krótki opis dla okna, np. „HTTP 403: rate limit exceeded”
    details: str = ""          # pełny opis (do dziennika)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK and self.release is not None


def log_path() -> Path:
    return local_data_dir() / "logs" / "aktualizacja.log"


def write_log(lines: list[str]) -> None:
    """Dopisuje wpis do dziennika aktualizacji (po 512 KB poprzedni plik → aktualizacja.log.1)."""
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n\n")
    except OSError:
        pass                    # dziennik nie może zablokować programu


def describe_error(exc: BaseException) -> tuple[str, list[str]]:
    """(krótki opis dla użytkownika, dodatkowe wiersze dziennika)."""
    extra: list[str] = []
    if isinstance(exc, urllib.error.HTTPError):
        body = ""
        try:
            body = exc.read(4096).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        extra += [f"Kod HTTP: {exc.code}", f"Odpowiedź (500 znaków): {body[:500]}"]
        message = exc.reason or ""
        try:
            message = json.loads(body).get("message") or message
        except (ValueError, AttributeError):
            pass
        return f"HTTP {exc.code}: {message}", extra
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        extra.append(f"URLError.reason: {reason!r}")
        if isinstance(reason, BaseException):
            return f"{type(reason).__name__}: {reason}", extra
        return f"URLError: {reason}", extra
    return f"{type(exc).__name__}: {exc}", extra


def _log_entry(kind: str, summary: str, extra: list[str], tb: str = "") -> None:
    lines = [f"[{datetime.now().isoformat(timespec='seconds')}] {kind}",
             f"Wersja programu: {__version__} ({'exe' if is_frozen() else 'źródła'})",
             f"Adres: {API_URL}",
             f"Wynik: {summary}", *extra]
    if tb:
        lines += ["Traceback:", tb.rstrip()]
    write_log(lines)


def release_problem(data) -> str | None:
    """Opis, dlaczego odpowiedź nie jest poprawnym wydaniem, albo None."""
    if not isinstance(data, dict):
        return f"odpowiedź nie jest obiektem JSON ({type(data).__name__})"
    tag = data.get("tag_name")
    if not tag:
        return "brak pola tag_name" + (f" (komunikat serwera: {data['message']})" if data.get("message") else "")
    if parse_version(str(tag)) is None:
        return f"nieprawidłowy numer wersji w tag_name: {tag!r}"
    if data.get("draft") or data.get("prerelease"):
        return f"wydanie {tag} jest szkicem lub wersją testową"
    if not any(isinstance(a, dict) and a.get("name") == RELEASE_ASSET and a.get("browser_download_url")
               for a in data.get("assets") or []):
        return f"wydanie {tag} nie zawiera pliku {RELEASE_ASSET}"
    return None


def parse_release(data: dict) -> Release | None:
    tag = str(data.get("tag_name") or "")
    if parse_version(tag) is None or data.get("draft") or data.get("prerelease"):
        return None
    asset = next((a for a in data.get("assets") or [] if a.get("name") == RELEASE_ASSET), None)
    return Release(version=tag.lstrip("v"), tag=tag, notes=str(data.get("body") or "").strip(),
                   asset_url=asset.get("browser_download_url") if asset else None,
                   asset_size=int(asset.get("size") or 0) if asset else 0,
                   page_url=str(data.get("html_url") or RELEASES_URL))


def check_latest(timeout: float = 8.0, opener=urllib.request.urlopen) -> CheckResult:
    """Najnowsze wydanie albo opis błędu. Nie zgłasza wyjątków – każdy błąd trafia do dziennika."""
    try:
        req = urllib.request.Request(API_URL, headers={"Accept": "application/vnd.github+json",
                                                       "User-Agent": _user_agent(),
                                                       "X-GitHub-Api-Version": "2022-11-28"})
        with opener(req, timeout=timeout) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 – każdy błąd ma być widoczny, a nie ukryty
        summary, extra = describe_error(exc)
        _log_entry("BŁĄD sprawdzania aktualizacji", summary, [f"Typ wyjątku: {type(exc).__module__}."
                                                              f"{type(exc).__qualname__}",
                                                              f"Komunikat: {exc}", *extra],
                   traceback.format_exc())
        return CheckResult(STATUS_ERROR, error=summary, details=summary)
    problem = release_problem(data)
    if problem:
        excerpt = raw[:500].decode("utf-8", "replace")
        _log_entry("NIEPOPRAWNE wydanie", problem, [f"Odpowiedź (500 znaków): {excerpt}"])
        return CheckResult(STATUS_INVALID, release=parse_release(data) if isinstance(data, dict) else None,
                           error=problem, details=problem)
    release = parse_release(data)
    _log_entry("OK", f"najnowsze wydanie {release.tag}", [])
    return CheckResult(STATUS_OK, release=release)


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
if errorlevel 4 goto backup_failed
echo Kopiowanie nowej wersji >> "%LOG%"
robocopy "%NEW%" "%APP%" /E /R:3 /W:2 /NP /XD {xd} /XF {xf} >> "%LOG%"
rem kody robocopy: 0-3 = OK, 4 = konflikt plik/folder (niepelna kopia), 8+ = bledy
if errorlevel 4 goto restore
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
