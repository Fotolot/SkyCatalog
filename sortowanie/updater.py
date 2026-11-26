"""Aktualizacja wersji przenośnej z GitHub Releases.

1. ``check_latest()`` – pyta https://api.github.com/repos/OWNER/REPO/releases/latest i zwraca
   ``CheckResult``: wydanie albo opis błędu. Błędy nigdy nie blokują startu; każdy jest zapisywany
   (typ, komunikat, traceback, kod HTTP…) do %LOCALAPPDATA%\\SkyCatalog\\logs\\aktualizacja.log.
2. ``prepare_update()`` – pobiera ZIP do folderu tymczasowego (plik ``.part``), sprawdza rozmiar,
   sumę SHA-256 podaną przez GitHub, spójność archiwum i jego zawartość, rozpakowuje go
   (z ochroną przed ścieżkami spoza folderu). Niepełna paczka nigdy nie jest instalowana.
3. ``launch_helper()`` – po bezpiecznym zamknięciu pracy programu uruchamia NIEZALEŻNY proces
   aktualizujący (``aktualizator.ps1``, Windows PowerShell) i program się zamyka. Proces czeka na
   zakończenie programu i jego procesów pomocniczych, robi kopię zapasową w ``_backup``, podmienia
   pliki w tym samym folderze (gdy brak prawa zapisu – za zgodą UAC), sprawdza wersję w metadanych
   nowego .exe, uruchamia nową wersję w zwykłym kontekście użytkownika i czeka na jej potwierdzenie
   (``confirm_started()``). Przy błędzie przywraca kopię i uruchamia poprzednią wersję.
4. Wszystkie etapy (wersje, ścieżki, PID-y, kody wyjścia, błędy) trafiają do dziennika
   %LOCALAPPDATA%\\SkyCatalog\\logs\\aktualizacja.log; wynik ostatniej aktualizacji – do pliku
   ``wynik.json`` w folderze roboczym, odczytywanego po ponownym uruchomieniu.

Dane użytkownika SkyCatalog nie leżą w folderze programu – są w:
  %APPDATA%\\SkyCatalog       ustawienia.json, cache_osm.json, decyzje_lokalizacji.json,
                              historia_sortowania.json
  %LOCALAPPDATA%\\SkyCatalog  katalog.sqlite (+ -wal, -shm), miniatury\\, przegladarka\\ (cache mapy)
Aktualizacja nie zagląda do tych folderów. Dodatkowo – gdyby takie pliki lub foldery pojawiły się
obok .exe (np. ręcznie przeniesione) – są wyłączone z kopiowania i nigdy nie są nadpisywane ani
usuwane (PROTECTED_FILES / PROTECTED_DIRS).
"""

from __future__ import annotations

import hashlib
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

from . import APP_NAME, EXE_NAME, GITHUB_REPO, RELEASE_ASSET, __version__, tls
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
    sha256: str = ""          # z pola „digest” zasobu wydania GitHub („sha256:…”), jeśli podane


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
             f"Weryfikacja TLS: {tls.mode()}" + (f" ({tls.problems()})" if tls.problems() else ""),
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
                   page_url=str(data.get("html_url") or RELEASES_URL),
                   sha256=_digest(asset))


def _digest(asset: dict | None) -> str:
    value = str((asset or {}).get("digest") or "")
    return value.split(":", 1)[1].lower() if value.lower().startswith("sha256:") else ""


def check_latest(timeout: float = 8.0, opener=tls.urlopen) -> CheckResult:
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


def stage_log(message: str) -> None:
    """Wpis etapu aktualizacji w trwałym dzienniku (strona programu)."""
    write_log([f"[{datetime.now().isoformat(timespec='seconds')}] [program {__version__} pid {os.getpid()}] {message}"])


def download(release: Release, dest_dir: Path, progress: Callable[[int, int], None] | None = None,
             opener=tls.urlopen, cancelled: Callable[[], bool] | None = None) -> Path:
    """Pobiera paczkę do ``.part`` i dopiero po sprawdzeniu rozmiaru i sumy SHA-256 nadaje jej
    docelową nazwę – przerwane lub uszkodzone pobieranie nigdy nie zostanie zainstalowane."""
    if not release.asset_url:
        raise UpdateError(f"Wydanie {release.tag} nie zawiera pliku {RELEASE_ASSET}.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / RELEASE_ASSET
    part = dest_dir / (RELEASE_ASSET + ".part")
    req = urllib.request.Request(release.asset_url, headers={"User-Agent": _user_agent(),
                                                             "Accept": "application/octet-stream"})
    digest = hashlib.sha256()
    try:
        with opener(req, timeout=60) as resp, open(part, "wb") as f:
            total = int(resp.headers.get("Content-Length") or release.asset_size or 0)
            done = 0
            while True:
                if cancelled and cancelled():
                    raise UpdateError("Anulowano pobieranie.")
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
            f.flush()
            os.fsync(f.fileno())
        size = part.stat().st_size
        if release.asset_size and size != release.asset_size:
            raise UpdateError(f"Pobrany plik ma nieprawidłowy rozmiar ({size} z {release.asset_size} B) – "
                              "spróbuj ponownie.")
        if release.sha256 and digest.hexdigest() != release.sha256:
            raise UpdateError("Suma kontrolna SHA-256 pobranego pliku nie zgadza się z wydaniem – plik "
                              "uszkodzony lub niepełny.")
        os.replace(part, target)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        part.unlink(missing_ok=True)
        raise UpdateError(f"Nie udało się pobrać aktualizacji: {exc}") from exc
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    stage_log(f"Pobrano {target} ({size} B, SHA-256 {digest.hexdigest()}"
              f"{', zgodna z wydaniem' if release.sha256 else ', wydanie nie podaje sumy'})")
    return target


def extract(zip_path: Path, dest_dir: Path) -> Path:
    """Sprawdza spójność ZIP, rozpakowuje go i zwraca folder zawierający SkyCatalog.exe."""
    dest = dest_dir.resolve()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()                      # CRC każdego pliku
            if bad is not None:
                raise UpdateError(f"Uszkodzony plik w archiwum: {bad}")
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


def verify_package(new_dir: Path, version: str) -> None:
    """Paczka musi zawierać program i jego biblioteki oraz deklarować wersję wydania."""
    exe = new_dir / EXE_NAME
    if not exe.is_file() or exe.stat().st_size < 100_000:
        raise UpdateError(f"Paczka nie zawiera poprawnego pliku {EXE_NAME}.")
    internal = new_dir / "_internal"
    if not internal.is_dir() or not any(internal.iterdir()):
        raise UpdateError("Paczka nie zawiera bibliotek programu (_internal).")
    declared = internal / "VERSION"
    if declared.is_file():
        text = declared.read_text(encoding="utf-8", errors="replace").strip()
        if re.fullmatch(r"\d+\.\d+\.\d+", text) and text != version:
            raise UpdateError(f"Paczka zawiera wersję {text}, a wydanie to {version}.")


def can_write(folder: Path) -> bool:
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".skycatalog_test_", delete=True):
            return True
    except OSError:
        return False


# ── Przygotowanie i uruchomienie procesu aktualizującego ───────

HELPER_NAME = "aktualizator.ps1"
WAIT_SECONDS = 120          # na zamknięcie programu i jego procesów pomocniczych
CONFIRM_SECONDS = 90        # na potwierdzenie startu nowej wersji


def helper_source() -> Path:
    return Path(__file__).resolve().parent / HELPER_NAME


def work_dir_for(version: str) -> Path:
    return Path(tempfile.gettempdir()) / f"{APP_NAME}-aktualizacja-{version}"


def pending_path() -> Path:
    """Znacznik oczekującej aktualizacji (odczytywany przez program po ponownym uruchomieniu)."""
    return local_data_dir() / "aktualizacja_oczekuje.json"


@dataclass
class PreparedUpdate:
    release: Release
    work: Path
    zip_path: Path
    new_dir: Path


def prepare_update(release: Release, progress: Callable[[int, int], None] | None = None,
                   cancelled: Callable[[], bool] | None = None, opener=tls.urlopen) -> PreparedUpdate:
    """Pobiera, sprawdza i rozpakowuje wydanie do folderu tymczasowego (bez zmian w programie)."""
    work = work_dir_for(release.version)
    stage_log(f"=== Przygotowanie aktualizacji {__version__} -> {release.version} (folder {work})")
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    zip_path = download(release, work, progress, opener=opener, cancelled=cancelled)
    new_dir = extract(zip_path, work / "nowa_wersja")
    verify_package(new_dir, release.version)
    stage_log(f"Paczka sprawdzona i rozpakowana: {new_dir}")
    return PreparedUpdate(release, work, zip_path, new_dir)


def descendant_pids(root_pid: int) -> list[int]:
    """Procesy potomne programu (np. QtWebEngineProcess) – proces aktualizujący czeka i na nie."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)           # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    parents: dict[int, list[int]] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            parents.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    out, stack = [], [root_pid]
    while stack:
        for child in parents.get(stack.pop(), []):
            if child not in out and child != root_pid:
                out.append(child)
                stack.append(child)
    return out


def path_variants(path: Path) -> list[str]:
    """Ta sama ścieżka w postaci podanej, pełnej i krótkiej (8.3) – Windows może zgłaszać ścieżkę
    procesu w innej postaci niż ta, którą zna program."""
    out = [str(path)]
    if sys.platform == "win32":
        import ctypes
        for fn in ("GetLongPathNameW", "GetShortPathNameW"):
            buf = ctypes.create_unicode_buffer(32768)
            if getattr(ctypes.windll.kernel32, fn)(str(path), buf, len(buf)):
                out.append(buf.value)
    return list(dict.fromkeys(v.rstrip("\\/") for v in out if v))


def write_helper_files(prep: PreparedUpdate, target: Path, pids: list[int]) -> tuple[Path, Path]:
    """Kopiuje skrypt pomocniczy do folderu roboczego i zapisuje jego parametry (JSON)."""
    script = prep.work / HELPER_NAME
    shutil.copyfile(helper_source(), script)
    params = {
        "app_dir": str(target), "app_dir_variants": path_variants(target),
        "new_dir": str(prep.new_dir), "exe_name": EXE_NAME,
        "current_version": __version__, "target_version": prep.release.version,
        "pids": pids, "wait_seconds": WAIT_SECONDS, "confirm_seconds": CONFIRM_SECONDS,
        "log_path": str(log_path()), "result_path": str(prep.work / "wynik.json"),
        "stage_path": str(prep.work / "etap.json"), "confirm_path": str(prep.work / "potwierdzenie.json"),
        "script_path": str(script), "failed_flag": FAILED_FLAG,
        "protected_dirs": PROTECTED_DIRS, "protected_files": PROTECTED_FILES,
        # tylko test end-to-end w CI: zmienne środowiska testu muszą trafić do nowej wersji, więc
        # bez uruchamiania przez Eksplorator (ten nie przekazuje środowiska procesu)
        "launch_direct": bool(os.environ.get("SKYCATALOG_UPDATE_SELFTEST")),
    }
    path = prep.work / "parametry.json"
    path.write_text(json.dumps(params, ensure_ascii=False, indent=1), encoding="utf-8")
    log_path().parent.mkdir(parents=True, exist_ok=True)
    return script, path


def helper_command(script: Path, params: Path, mode: str = "main") -> list[str]:
    """Polecenie procesu aktualizującego. Treść skryptu jest wczytywana jako tekst, więc nie
    blokują jej zasady wykonywania skryptów; ścieżki są literałami PowerShell (apostrof podwojony)."""
    q = lambda p: "'" + str(p).replace("'", "''") + "'"  # noqa: E731
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / \
        "powershell.exe"
    return [str(powershell if powershell.exists() else "powershell.exe"), "-NoProfile", "-NonInteractive",
            "-WindowStyle", "Hidden", "-Command",
            f"& ([scriptblock]::Create([IO.File]::ReadAllText({q(script)}))) {q(params)} '{mode}'"]


def launch_helper(prep: PreparedUpdate, target: Path | None = None, pid: int | None = None) -> int:
    """Uruchamia niezależny proces aktualizujący i zapisuje znacznik oczekującej aktualizacji.
    Zwraca PID procesu aktualizującego. Program powinien zaraz potem zakończyć pracę."""
    if sys.platform != "win32":
        raise UpdateError("Automatyczna aktualizacja działa tylko w Windows.")
    target = target or app_dir()
    pid = pid or os.getpid()
    pids = [pid] + descendant_pids(pid)
    script, params = write_helper_files(prep, target, pids)
    for stale in ("wynik.json", "etap.json", "potwierdzenie.json"):
        (prep.work / stale).unlink(missing_ok=True)
    cmd = helper_command(script, params)
    stage_log(f"Folder programu: {target}; proces aktualizujący: {script}; PID-y programu: {pids}")
    flags = 0x08000000 | 0x00000200          # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(cmd, creationflags=flags | 0x01000000, close_fds=True, cwd=str(prep.work))
    except OSError:                           # zadanie (job) bez prawa wyjścia – bez CREATE_BREAKAWAY_FROM_JOB
        proc = subprocess.Popen(cmd, creationflags=flags, close_fds=True, cwd=str(prep.work))
    stage_log(f"Uruchomiono proces aktualizujący (pid {proc.pid}): {subprocess.list2cmdline(cmd)}")
    pending = {"current": __version__, "target": prep.release.version, "work": str(prep.work),
               "result": str(prep.work / "wynik.json"), "confirm": str(prep.work / "potwierdzenie.json"),
               "app_dir": str(target), "helper_pid": proc.pid, "time": datetime.now().isoformat(timespec="seconds")}
    pending_path().parent.mkdir(parents=True, exist_ok=True)
    pending_path().write_text(json.dumps(pending, ensure_ascii=False, indent=1), encoding="utf-8")
    return proc.pid


# ── Po ponownym uruchomieniu ───────────────────────────────────

def read_pending() -> dict | None:
    try:
        data = json.loads(pending_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def confirm_started() -> dict | None:
    """Wywoływane na samym początku programu: jeśli trwa aktualizacja, potwierdza procesowi
    aktualizującemu, jaką wersję faktycznie uruchomiono (wersja, PID, ścieżka .exe)."""
    pending = read_pending()
    if not pending:
        return None
    info = {"version": __version__, "pid": os.getpid(),
            "exe": sys.executable if is_frozen() else str(Path(sys.argv[0]).resolve())}
    try:
        Path(pending["confirm"]).write_text(json.dumps(info), encoding="utf-8")
    except (OSError, KeyError, TypeError):
        pass
    stage_log(f"Program uruchomiony po aktualizacji: wersja {__version__}, pid {os.getpid()}, {info['exe']} "
              f"(oczekiwana {pending.get('target')})")
    return pending


@dataclass
class UpdateOutcome:
    ok: bool
    target: str
    message: str
    stale_shortcuts: list[str]
    status: str = ""


def finish_pending(pending: dict, wait_s: float = 20.0) -> UpdateOutcome:
    """Wynik zakończonej aktualizacji (czeka chwilę na zapis wyniku przez proces aktualizujący)."""
    import time
    result: dict = {}
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        try:
            result = json.loads(Path(pending["result"]).read_text(encoding="utf-8-sig"))
            break
        except (OSError, ValueError, KeyError, TypeError):
            time.sleep(0.5)
    target = str(pending.get("target", ""))
    ok = __version__ == target
    stale = list(result.get("skroty_nieaktualne") or []) if isinstance(result, dict) else []
    message = str(result.get("komunikat") or "") if isinstance(result, dict) else ""
    if not ok and not message:
        message = f"Uruchomiona wersja to {__version__}, a oczekiwana {target}."
    stage_log(f"Wynik aktualizacji do {target}: {'OK' if ok else 'BŁĄD'} – {result.get('status', 'brak wyniku')}")
    try:
        pending_path().unlink()
    except OSError:
        pass
    status = str(result.get("status") or "") if isinstance(result, dict) else ""
    return UpdateOutcome(ok, target, message, stale, status)
