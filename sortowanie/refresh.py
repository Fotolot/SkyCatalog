"""„Odśwież bibliotekę” – ponowna weryfikacja folderów, w których leżą zaindeksowane materiały.

Zasady:
- Sprawdzane są wyłącznie foldery objęte dotychczasowym indeksowaniem (bez skanowania komputera).
- Folder jest analizowany w całości albo wcale – wpisy usuwane są dopiero po pełnym odczycie jego
  zawartości. Anulowanie przerywa przed kolejnym folderem, więc nieprzejrzane foldery zostają
  bez zmian.
- Brak pliku w odczytanym folderze = wpis usuwany z katalogu (razem z trasą i miniaturami).
- Odłączony dysk, brak uprawnień lub inny błąd odczytu ≠ usunięcie: wpisy zostają z oznaczeniem
  „niedostępny”. Folder uznajemy za usunięty tylko wtedy, gdy dysk jest dostępny, a najbliższy
  istniejący folder nadrzędny daje się odczytać.
- Niezmienione pliki (ta sama ścieżka, rozmiar i data) nie są ponownie odczytywane.
- Oryginalne pliki na dysku NIGDY nie są usuwane ani zmieniane.
"""

from __future__ import annotations

import os
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import catalog as cat
from . import thumbs
from .scanner import CLOUD_ATTRS, FILE_ATTRIBUTE_SYSTEM, is_link_entry


@dataclass
class RefreshProgress:
    done: int = 0
    total: int = 0
    current: str = ""
    added: int = 0
    updated: int = 0
    removed: int = 0
    unavailable: int = 0
    removed_ids: list[int] = field(default_factory=list)
    changed_ids: list[int] = field(default_factory=list)
    unavailable_dirs: list[str] = field(default_factory=list)
    thumbs_removed: int = 0
    finished: bool = False
    cancelled: bool = False

    @property
    def summary(self) -> str:
        return (f"Dodano: {self.added}, zaktualizowano: {self.updated}, usunięto nieaktualne wpisy: "
                f"{self.removed}, niedostępne: {self.unavailable}")


class FolderUnavailable(Exception):
    """Folderu nie da się teraz sprawdzić – nie wolno uznać jego plików za usunięte."""


def volume_available(path: str) -> bool:
    """Czy dysk / udział sieciowy, na którym leży ścieżka, jest podłączony."""
    anchor = Path(path).anchor
    return bool(anchor) and os.path.isdir(anchor)


class LibraryRefresher:
    def __init__(self, catalog: cat.Catalog, on_progress: Callable[[RefreshProgress], None] | None = None,
                 volume_check: Callable[[str], bool] = volume_available) -> None:
        self.catalog = catalog
        self.on_progress = on_progress
        self.volume_check = volume_check
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._volumes: dict[str, bool] = {}

    # Sterowanie
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._cancel.clear()
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def wait(self, timeout: float = 5.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _emit(self, prog: RefreshProgress) -> None:
        if self.on_progress:
            try:
                self.on_progress(prog)
            except Exception:  # noqa: BLE001 – interfejs nie może zatrzymać odświeżania
                pass

    # Praca (w wątku)
    def run(self) -> RefreshProgress:
        prog = RefreshProgress()
        dirs = self.catalog.indexed_dirs()
        prog.total = len(dirs)
        self._emit(prog)
        for directory in dirs:
            if self._cancel.is_set():
                prog.cancelled = True
                break
            prog.current = directory
            self.refresh_dir(directory, prog)
            prog.done += 1
            if prog.done % 10 == 0 or prog.done == prog.total:
                self._emit(prog)
        prog.finished = not prog.cancelled
        prog.current = ""
        self._emit(prog)
        return prog

    def refresh_dir(self, directory: str, prog: RefreshProgress) -> None:
        c = self.catalog
        before = c.dir_files(directory)
        try:
            entries = self._list(directory)
        except FolderUnavailable:
            prog.unavailable += c.mark_dir_unavailable(directory)
            prog.unavailable_dirs.append(directory)
            return
        groups = cat.group_entries(entries)
        present = {stem for stem, files in groups.items() if cat.classify_group(files) is not None}
        stats = c.process_directory(directory, entries, cat.Catalog.root_for(directory), c.current_run())
        prog.added += stats["nowe"]
        prog.updated += stats["zmienione"]
        removed = c.remove_absent(directory, present)
        prog.removed += len(removed)
        prog.removed_ids += removed
        if stats["nowe"] or stats["zmienione"]:
            prog.changed_ids += [r[0] for r in c.conn().execute(
                "SELECT id FROM materials WHERE dir_key=?", (cat.path_key(directory),))]
        # miniatury plików usuniętych lub zmienionych (nowa wersja ma inny klucz miniatury)
        stale = before - c.dir_files(directory)
        if stale:
            prog.thumbs_removed += thumbs.remove_thumbs(stale)

    def _list(self, directory: str) -> list[cat.FileEntry]:
        """Pełna lista plików katalogu w folderze. Folder usunięty → []; niedostępny → FolderUnavailable."""
        try:
            it = os.scandir(directory)
        except FileNotFoundError:
            if self._folder_deleted(directory):
                return []
            raise FolderUnavailable(directory)
        except OSError as exc:                    # brak uprawnień, błąd dysku/sieci
            raise FolderUnavailable(directory) from exc
        entries: list[cat.FileEntry] = []
        try:
            with it:
                for entry in it:
                    fe = self._file_entry(entry)
                    if fe is not None:
                        entries.append(fe)
        except OSError as exc:                    # przerwany odczyt – folder niepełny, nic nie usuwamy
            raise FolderUnavailable(directory) from exc
        return entries

    @staticmethod
    def _file_entry(entry: os.DirEntry) -> cat.FileEntry | None:
        ext = os.path.splitext(entry.name)[1].lower()
        if ext not in cat.CATALOG_EXT:
            return None
        st = entry.stat(follow_symlinks=False)
        attrs = getattr(st, "st_file_attributes", 0)
        if is_link_entry(entry.is_symlink(), hasattr(entry, "is_junction") and entry.is_junction(),
                         attrs, getattr(st, "st_reparse_tag", 0)):
            return None
        if not stat.S_ISREG(st.st_mode) or attrs & FILE_ATTRIBUTE_SYSTEM or cat.is_in_flight(entry.path):
            return None
        return cat.FileEntry(entry.path, st.st_size, st.st_mtime, cloud_only=bool(attrs & CLOUD_ATTRS))

    def _folder_deleted(self, directory: str) -> bool:
        """Folder zniknął: usunięty (dysk dostępny, folder nadrzędny czytelny) czy odłączony dysk?"""
        anchor = Path(directory).anchor
        if anchor not in self._volumes:
            self._volumes[anchor] = bool(self.volume_check(directory))
        if not self._volumes[anchor]:
            return False
        for parent in Path(directory).parents:
            try:
                with os.scandir(parent):
                    return True                    # istniejący, czytelny folder nadrzędny – usunięto
            except FileNotFoundError:
                continue                           # też zniknął – sprawdź wyżej
            except OSError:
                return False                       # brak dostępu – nie wiemy, czy usunięto
        return False
