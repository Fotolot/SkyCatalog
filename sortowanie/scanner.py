"""Skanowanie dysków w poszukiwaniu zdjęć i filmów (w tle, z anulowaniem i wznowieniem).

* bez uprawnień administratora – niedostępne foldery są pomijane i zapisywane w podsumowaniu,
* nie wchodzi w dowiązania symboliczne ani punkty połączenia (junction / reparse point),
* pomija foldery systemowe i ukryte oraz wykluczenia z ustawień,
* pliki „tylko w chmurze” (OneDrive itp.) są zapisywane bez czytania treści – nie wymusza
  to ich pobrania,
* kolejka folderów jest w SQLite – przerwane skanowanie można wznowić także po restarcie,
* nic nie jest wysyłane do internetu.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import catalog as cat
from .config import local_data_dir

# Atrybuty plików Windows (st_file_attributes)
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
CLOUD_ATTRS = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE, DRIVE_CDROM, DRIVE_RAMDISK = 2, 3, 4, 5, 6
DRIVE_TYPES = {DRIVE_REMOVABLE: "wymienny", DRIVE_FIXED: "lokalny", DRIVE_REMOTE: "sieciowy",
               DRIVE_CDROM: "optyczny", DRIVE_RAMDISK: "RAM"}

# Nazwy folderów pomijanych zawsze (dowolne miejsce na dysku)
EXCLUDED_NAMES = {"$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws",
                  "$sysreset", "windowsapps", "node_modules", ".git", "__pycache__", "$getcurrent",
                  "config.msi", "recovery", "perflogs"}

SKIP_PERMISSION = "brak uprawnień"
SKIP_GONE = "folder zniknął w trakcie skanowania"
SKIP_ERROR = "błąd odczytu"
SKIP_LINK = "dowiązanie / punkt połączenia (pominięte)"
SKIP_SYSTEM = "folder systemowy lub ukryty"
SKIP_EXCLUDED = "wykluczenie z ustawień"
SKIP_DRIVE = "dysk niedostępny / odłączony"
SKIP_IN_FLIGHT = "plik w trakcie przenoszenia"


@dataclass
class Drive:
    root: str
    kind: str
    label: str = ""
    available: bool = True


def list_drives() -> list[Drive]:
    """Dyski Windows (litery) z typem; na innych systemach – katalog domowy."""
    if sys.platform != "win32":
        return [Drive(str(Path.home()), "lokalny", "katalog domowy")]
    import ctypes
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    drives = []
    for i in range(26):
        if not mask & (1 << i):
            continue
        root = f"{chr(65 + i)}:\\"
        dtype = k32.GetDriveTypeW(root)
        label_buf = ctypes.create_unicode_buffer(261)
        ok = k32.GetVolumeInformationW(root, label_buf, 261, None, None, None, None, 0)
        drives.append(Drive(root, DRIVE_TYPES.get(dtype, "inny"), label_buf.value if ok else "",
                            available=bool(ok)))
    return drives


def default_roots(settings) -> list[str]:
    chosen = list(settings.get("scan_drives") or [])
    if chosen:
        return chosen
    allow_network = bool(settings.get("scan_network_drives"))
    roots = []
    for d in list_drives():
        if d.kind in ("lokalny", "wymienny") or (d.kind == "sieciowy" and allow_network):
            roots.append(d.root)
    return roots


def default_excluded_paths() -> list[str]:
    paths = []
    for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"):
        v = os.environ.get(var)
        if v:
            paths.append(v)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        paths.append(str(Path(local).parent))          # cały AppData (cache przeglądarek, programów)
    paths.append(str(local_data_dir()))
    return paths


@dataclass
class ScanProgress:
    run: int = 0
    dirs_done: int = 0
    dirs_pending: int = 0
    materials_seen: int = 0
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    skipped: int = 0
    current: str = ""
    finished: bool = False
    cancelled: bool = False
    summary: list = field(default_factory=list)
    finalize: dict = field(default_factory=dict)


class Scanner:
    def __init__(self, catalog: cat.Catalog, settings=None,
                 on_progress: Callable[[ScanProgress], None] | None = None) -> None:
        self.catalog = catalog
        self.settings = settings
        self.on_progress = on_progress
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        excluded = list(default_excluded_paths())
        if settings is not None:
            excluded += [e for e in (settings.get("scan_exclusions") or []) if e]
        self.excluded_keys = [cat.path_key(p) for p in excluded]
        self.excluded_names = set(EXCLUDED_NAMES) | {
            e.casefold() for e in (settings.get("scan_exclusions") or []) if e and not any(c in e for c in "\\/:")
        } if settings is not None else set(EXCLUDED_NAMES)

    # Sterowanie
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, roots: list[str] | None = None, resume: bool = False) -> None:
        if self.running:
            return
        self._cancel.clear()
        self._thread = threading.Thread(target=self.scan, args=(roots, resume), daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def can_resume(self) -> bool:
        return self.catalog.get_state("scan_status") == "przerwane" and self.catalog.queue_pending() > 0

    # Wykluczenia
    def _excluded(self, path: str) -> bool:
        key = cat.path_key(path)
        for ex in self.excluded_keys:
            if key == ex or key.startswith(ex.rstrip("\\/") + os.sep):
                return True
        return False

    # Skanowanie (wywoływane w wątku)
    def scan(self, roots: list[str] | None = None, resume: bool = False) -> ScanProgress:
        c = self.catalog
        prog = ScanProgress()
        if resume and c.queue_pending() > 0:
            prog.run = c.current_run()
            roots = json.loads(c.get_state("scan_roots", "[]") or "[]")
        else:
            roots = roots if roots is not None else default_roots(self.settings)
            prog.run = c.current_run() + 1
            c.set_state("run", str(prog.run))
            c.set_state("scan_roots", json.dumps(roots))
            available = []
            for r in roots:
                if os.path.isdir(r):
                    available.append(r)
                else:
                    c.mark_root_unavailable(r)
                    c.add_skip(prog.run, r, SKIP_DRIVE)
                    prog.skipped += 1
            c.set_state("scan_available_roots", json.dumps(available))
            c.queue_reset(available)
        c.set_state("scan_status", "trwa")
        visited: set[tuple[int, int]] = set()

        while True:
            batch = c.queue_next(64)
            if not batch:
                break
            for row in batch:
                if self._cancel.is_set():
                    c.set_state("scan_status", "przerwane")
                    prog.cancelled = True
                    prog.dirs_pending = c.queue_pending()
                    self._emit(prog)
                    return prog
                self._scan_dir(row["path"], row["root"], prog, visited)
                c.queue_done(row["path"])
                prog.dirs_done += 1
                if prog.dirs_done % 20 == 0:
                    prog.dirs_pending = c.queue_pending()
                    self._emit(prog)

        available = json.loads(c.get_state("scan_available_roots", "[]") or "[]")
        prog.finalize = c.finalize_scan(prog.run, available)
        c.set_state("scan_status", "zakończone")
        prog.finished = True
        prog.dirs_pending = 0
        prog.summary = c.skip_summary(prog.run)
        self._emit(prog)
        return prog

    def _emit(self, prog: ScanProgress) -> None:
        if self.on_progress:
            try:
                self.on_progress(prog)
            except Exception:
                pass

    def _scan_dir(self, directory: str, root: str, prog: ScanProgress, visited: set) -> None:
        c = self.catalog
        prog.current = directory
        try:
            st = os.stat(directory, follow_symlinks=False)
            ident = (st.st_dev, st.st_ino)
            if st.st_ino and ident in visited:       # ochrona przed pętlą
                return
            visited.add(ident)
            it = os.scandir(directory)
        except PermissionError:
            c.add_skip(prog.run, directory, SKIP_PERMISSION)
            prog.skipped += 1
            return
        except FileNotFoundError:
            c.add_skip(prog.run, directory, SKIP_GONE)
            prog.skipped += 1
            return
        except OSError as exc:
            c.add_skip(prog.run, directory, f"{SKIP_ERROR}: {exc.strerror or exc}")
            prog.skipped += 1
            return

        subdirs: list[tuple[str, str]] = []
        entries: list[cat.FileEntry] = []
        with it:
            while True:
                try:
                    entry = next(it)
                except StopIteration:
                    break
                except OSError as exc:
                    c.add_skip(prog.run, directory, f"{SKIP_ERROR}: {exc.strerror or exc}")
                    prog.skipped += 1
                    break
                try:
                    self._handle_entry(entry, root, prog, subdirs, entries)
                except OSError as exc:
                    c.add_skip(prog.run, entry.path, f"{SKIP_ERROR}: {exc.strerror or exc}")
                    prog.skipped += 1
        c.queue_add(subdirs)
        if entries:
            stats = c.process_directory(directory, entries, cat.Catalog.root_for(directory), prog.run)
            prog.new += stats["nowe"]
            prog.changed += stats["zmienione"]
            prog.unchanged += stats["bez_zmian"]
            prog.materials_seen += stats["nowe"] + stats["zmienione"] + stats["bez_zmian"]

    def _handle_entry(self, entry: os.DirEntry, root: str, prog: ScanProgress,
                      subdirs: list, entries: list) -> None:
        name_cf = entry.name.casefold()
        st = entry.stat(follow_symlinks=False)
        attrs = getattr(st, "st_file_attributes", 0)
        is_link = entry.is_symlink() or bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT) or \
            (hasattr(entry, "is_junction") and entry.is_junction())
        if is_link:
            # Nie wchodzimy w dowiązania ani punkty połączenia (ryzyko pętli / innego dysku)
            try:
                points_to_dir = entry.is_dir()
            except OSError:
                points_to_dir = False
            if points_to_dir or stat.S_ISDIR(st.st_mode):
                self.catalog.add_skip(prog.run, entry.path, SKIP_LINK)
                prog.skipped += 1
            return
        if stat.S_ISDIR(st.st_mode):
            if name_cf in self.excluded_names or self._excluded(entry.path):
                self.catalog.add_skip(prog.run, entry.path, SKIP_EXCLUDED)
                prog.skipped += 1
                return
            # ukryte/systemowe (atrybut Windows) oraz foldery narzędzi zaczynające się od kropki
            if attrs & (FILE_ATTRIBUTE_SYSTEM | FILE_ATTRIBUTE_HIDDEN) or entry.name.startswith("."):
                self.catalog.add_skip(prog.run, entry.path, SKIP_SYSTEM)
                prog.skipped += 1
                return
            subdirs.append((entry.path, root))
            return
        if not stat.S_ISREG(st.st_mode):
            return
        ext = os.path.splitext(entry.name)[1].lower()
        if ext not in cat.CATALOG_EXT or attrs & FILE_ATTRIBUTE_SYSTEM:
            return
        if cat.is_in_flight(entry.path):
            self.catalog.add_skip(prog.run, entry.path, SKIP_IN_FLIGHT)
            prog.skipped += 1
            return
        entries.append(cat.FileEntry(entry.path, st.st_size, st.st_mtime, cloud_only=bool(attrs & CLOUD_ATTRS)))
