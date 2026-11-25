"""Przenoszenie i kopiowanie plików, historia operacji i cofanie ostatniego importu.

Istniejący plik w archiwum jest zastępowany WYŁĄCZNIE wtedy, gdy użytkownik wybrał „Nadpisz”
(``Move.overwrite``). Poprzednia wersja nie jest kasowana – trafia do folderu
``.SkyCatalog-zastapione`` w archiwum (``Move.backup``), skąd przywraca ją „Cofnij ostatni import”.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import app_data_dir, atomic_write_json


@dataclass(frozen=True)
class Move:
    """Pojedyncza zaplanowana operacja przeniesienia lub skopiowania pliku."""

    source: Path
    target: Path
    overwrite: bool = False          # użytkownik wybrał „Nadpisz” dla istniejącego pliku
    backup: Path | None = None       # gdzie trafiła poprzednia wersja nadpisanego pliku


BACKUP_FOLDER = ".SkyCatalog-zastapione"


def with_backups(moves: list[Move], archive: Path, stamp: str | None = None) -> list[Move]:
    """Przydziela nadpisywanym plikom miejsce na poprzednią wersję:
    ``<archiwum>/.SkyCatalog-zastapione/<data_czas>/<ścieżka względna>``."""
    stamp = stamp or datetime.now().strftime("%Y-%m-%d_%H%M%S")
    base = Path(archive) / BACKUP_FOLDER / stamp
    out = []
    for m in moves:
        if m.overwrite:
            try:
                rel = m.target.relative_to(archive)
            except ValueError:
                rel = Path(m.target.name)
            m = replace(m, backup=base / rel)
        out.append(m)
    return out


def _set_aside(target: Path, backup: Path | None) -> bool:
    """Odkłada istniejący plik docelowy do kopii zapasowej (tylko przy „Nadpisz”).
    Zwraca True, gdy coś odłożono. Bez zgody na nadpisanie – FileExistsError."""
    if not target.exists():
        return False
    if backup is None:
        raise FileExistsError(f"Plik docelowy już istnieje: {target}")
    if backup.exists():
        raise FileExistsError(f"Kopia zapasowa już istnieje: {backup}")
    backup.parent.mkdir(parents=True, exist_ok=True)
    os.replace(target, backup)          # ten sam dysk (folder w archiwum) – szybka zmiana nazwy
    return True


def _put_back(target: Path, backup: Path | None, set_aside: bool) -> None:
    if set_aside and backup is not None and backup.exists() and not target.exists():
        os.replace(backup, target)


ProgressCallback = Callable[[int, int, Move], None]


def execute_moves(moves: list[Move], progress: ProgressCallback | None = None
                  ) -> tuple[list[Move], list[tuple[Move, str]]]:
    """Wykonuje przeniesienia (także między dyskami). Zwraca (udane, nieudane_z_błędem).

    Istniejące pliki nie są nadpisywane – chyba że ``move.overwrite`` i ``move.backup``:
    wtedy poprzednia wersja trafia do kopii zapasowej (patrz ``with_backups``).
    """
    done: list[Move] = []
    failed: list[tuple[Move, str]] = []
    total = len(moves)
    for i, move in enumerate(moves, start=1):
        try:
            if move.target.exists() and not (move.overwrite and move.backup):
                raise FileExistsError(f"Plik docelowy już istnieje: {move.target}")
            move.target.parent.mkdir(parents=True, exist_ok=True)
            if move.target.exists():
                _move_replacing(move)
            else:
                shutil.move(str(move.source), str(move.target))
            done.append(move)
        except OSError as exc:
            failed.append((move, str(exc)))
        if progress:
            progress(i, total, move)
    return done, failed


def _move_replacing(move: Move) -> None:
    """Przeniesienie z nadpisaniem: najpierw plik trafia obok celu (``.part``), dopiero potem
    poprzednia wersja jest odkładana do kopii zapasowej, a nowa zajmuje jej miejsce."""
    tmp = move.target.with_name(move.target.name + ".part")
    if tmp.exists():
        raise FileExistsError(f"Plik tymczasowy już istnieje: {tmp}")
    shutil.move(str(move.source), str(tmp))
    aside = False
    try:
        aside = _set_aside(move.target, move.backup)
        os.replace(tmp, move.target)
    except BaseException:
        _put_back(move.target, move.backup, aside)
        if tmp.exists() and not move.source.exists():
            shutil.move(str(tmp), str(move.source))      # plik wraca na miejsce
        raise


MODE_MOVE = "przenieś"
MODE_COPY = "kopiuj"
COPY_CHUNK = 8 * 1024 * 1024


def copy_verified(source: Path, target: Path, on_bytes: Callable[[int], None] | None = None,
                  backup: Path | None = None) -> None:
    """Kopiuje plik przez plik tymczasowy ``.part`` i sprawdza rozmiar; zachowuje datę modyfikacji.

    Przerwana kopia nie zostawia niekompletnego pliku pod docelową nazwą. Źródło nie jest zmieniane.
    Istniejący plik docelowy jest zastępowany tylko z ``backup`` (zgoda na „Nadpisz”) – wtedy
    poprzednia wersja trafia tam dopiero po udanej i sprawdzonej kopii.
    """
    if target.exists() and backup is None:
        raise FileExistsError(f"Plik docelowy już istnieje: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    try:
        with open(source, "rb") as fin, open(tmp, "wb") as fout:
            while True:
                chunk = fin.read(COPY_CHUNK)
                if not chunk:
                    break
                fout.write(chunk)
                if on_bytes:
                    on_bytes(len(chunk))
            fout.flush()
            os.fsync(fout.fileno())
        shutil.copystat(source, tmp)
        src_size, tmp_size = source.stat().st_size, tmp.stat().st_size
        if src_size != tmp_size:
            raise OSError(f"Weryfikacja kopii nie powiodła się ({tmp_size} z {src_size} B)")
        aside = _set_aside(target, backup)       # bez zgody na nadpisanie: FileExistsError
        try:
            os.replace(tmp, target)
        except BaseException:
            _put_back(target, backup, aside)
            raise
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def execute_copies(moves: list[Move], progress: ProgressCallback | None = None
                   ) -> tuple[list[Move], list[tuple[Move, str]]]:
    """Kopiuje pliki (np. z karty SD) do archiwum z weryfikacją. Źródło pozostaje nietknięte."""
    done: list[Move] = []
    failed: list[tuple[Move, str]] = []
    total = len(moves)
    for i, move in enumerate(moves, start=1):
        try:
            copy_verified(move.source, move.target, backup=move.backup if move.overwrite else None)
            done.append(move)
        except OSError as exc:
            failed.append((move, str(exc)))
        if progress:
            progress(i, total, move)
    return done, failed


def undo_copies(copies: list[Move], stop_at: Path | None = None
                ) -> tuple[list[Move], list[tuple[Move, str]]]:
    """Cofa import kopiujący: usuwa WYŁĄCZNIE utworzone kopie (oryginały na karcie zostają).

    Kopia jest usuwana tylko wtedy, gdy nadal ma rozmiar oryginału (lub oryginał jest niedostępny,
    a rozmiar zgadza się z zapisanym) – plik zmieniony po imporcie nie zostanie skasowany.
    Nadpisany plik wraca z kopii zapasowej na swoje miejsce.
    """
    done: list[Move] = []
    failed: list[tuple[Move, str]] = []
    for m in reversed(copies):
        try:
            if not m.target.exists():
                raise FileNotFoundError(f"Kopia nie istnieje: {m.target}")
            if m.source.exists() and m.source.stat().st_size != m.target.stat().st_size:
                raise OSError(f"Kopia różni się od oryginału – nie usunięto: {m.target}")
            m.target.unlink()
            restore_backup(m, stop_at)
            done.append(m)
            remove_empty_parents(m.target.parent, stop_at)
        except OSError as exc:
            failed.append((m, str(exc)))
    return done, failed


def undo_moves(moves: list[Move], stop_at: Path | None = None
               ) -> tuple[list[Move], list[tuple[Move, str]]]:
    """Cofa przeniesienia (w odwrotnej kolejności) i usuwa puste foldery docelowe.
    Nadpisane pliki wracają z kopii zapasowej na swoje miejsce."""
    originals = {(m.target, m.source): m for m in moves}
    reversed_moves = [Move(m.target, m.source) for m in reversed(moves)]
    done, failed = execute_moves(reversed_moves)
    for move in done:
        original = originals.get((move.source, move.target))
        if original is not None and original.backup is not None:
            try:
                restore_backup(original, stop_at)
            except OSError as exc:
                failed.append((move, f"Nie przywrócono poprzedniej wersji: {exc}"))
        remove_empty_parents(move.source.parent, stop_at)
    return done, failed


def restore_backup(move: Move, stop_at: Path | None = None) -> bool:  # noqa: ARG001
    """Przywraca poprzednią wersję nadpisanego pliku (gdy miejsce jest już wolne)."""
    if move.backup is None or not move.backup.exists():
        return False
    if move.target.exists():
        raise FileExistsError(f"Nie przywrócono poprzedniej wersji – miejsce zajęte: {move.target}")
    move.target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(move.backup, move.target)
    # sprzątanie pustych folderów kopii zapasowej – nigdy wyżej niż folder archiwum
    archive = next((p.parent for p in move.backup.parents if p.name == BACKUP_FOLDER), None)
    if archive is not None:
        remove_empty_parents(move.backup.parent, archive, levels=40)
    return True


def remove_empty_parents(folder: Path, stop_at: Path | None = None, levels: int = 6) -> None:
    stop = stop_at.resolve() if stop_at else None
    for _ in range(levels):
        try:
            if stop is not None and folder.resolve() == stop:
                return
            folder.rmdir()  # usuwa wyłącznie pusty folder
        except OSError:
            return
        folder = folder.parent


# ── Historia operacji ──────────────────────────────────────────

def history_path() -> Path:
    return app_data_dir() / "historia_sortowania.json"


def save_history(moves: list[Move], root: Path | None = None, path: Path | None = None,
                 op_id: int | None = None, mode: str = MODE_MOVE) -> None:
    data = {
        "czas": datetime.now().isoformat(timespec="seconds"),
        "tryb": mode,
        "archiwum": str(root) if root else "",
        "operacja_katalogu": op_id,
        "przeniesienia": [{"z": str(m.source), "do": str(m.target),
                           **({"zastapiony": str(m.backup)} if m.backup else {})} for m in moves],
    }
    try:
        atomic_write_json(path or history_path(), data)
    except OSError:
        pass


def load_history(path: Path | None = None) -> tuple[list[Move], Path | None]:
    path = path or history_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        moves = [Move(Path(m["z"]), Path(m["do"]), overwrite=bool(m.get("zastapiony")),
                      backup=Path(m["zastapiony"]) if m.get("zastapiony") else None)
                 for m in data.get("przeniesienia", [])]
        root = Path(data["archiwum"]) if data.get("archiwum") else None
        return moves, root
    except (OSError, ValueError, KeyError, TypeError):
        return [], None


def load_history_op(path: Path | None = None) -> int | None:
    """Identyfikator operacji w katalogu materiałów (do przywrócenia kategorii przy cofaniu)."""
    try:
        data = json.loads((path or history_path()).read_text(encoding="utf-8"))
        value = data.get("operacja_katalogu")
        return int(value) if value is not None else None
    except (OSError, ValueError, TypeError):
        return None


def load_history_mode(path: Path | None = None) -> str:
    try:
        data = json.loads((path or history_path()).read_text(encoding="utf-8"))
        return MODE_COPY if data.get("tryb") == MODE_COPY else MODE_MOVE
    except (OSError, ValueError, TypeError):
        return MODE_MOVE


def clear_history(path: Path | None = None) -> None:
    try:
        (path or history_path()).unlink()
    except FileNotFoundError:
        pass
