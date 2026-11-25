"""Przenoszenie i kopiowanie plików, historia operacji i cofanie ostatniego importu."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import app_data_dir, atomic_write_json


@dataclass(frozen=True)
class Move:
    """Pojedyncza zaplanowana operacja przeniesienia pliku."""

    source: Path
    target: Path


ProgressCallback = Callable[[int, int, Move], None]


def execute_moves(moves: list[Move], progress: ProgressCallback | None = None
                  ) -> tuple[list[Move], list[tuple[Move, str]]]:
    """Wykonuje przeniesienia (także między dyskami). Zwraca (udane, nieudane_z_błędem).

    Istniejące pliki nigdy nie są nadpisywane.
    """
    done: list[Move] = []
    failed: list[tuple[Move, str]] = []
    total = len(moves)
    for i, move in enumerate(moves, start=1):
        try:
            if move.target.exists():
                raise FileExistsError(f"Plik docelowy już istnieje: {move.target}")
            move.target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(move.source), str(move.target))
            done.append(move)
        except OSError as exc:
            failed.append((move, str(exc)))
        if progress:
            progress(i, total, move)
    return done, failed


MODE_MOVE = "przenieś"
MODE_COPY = "kopiuj"
COPY_CHUNK = 8 * 1024 * 1024


def copy_verified(source: Path, target: Path, on_bytes: Callable[[int], None] | None = None) -> None:
    """Kopiuje plik przez plik tymczasowy ``.part`` i sprawdza rozmiar; zachowuje datę modyfikacji.

    Przerwana kopia nie zostawia niekompletnego pliku pod docelową nazwą. Źródło nie jest zmieniane.
    """
    if target.exists():
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
        if target.exists():                      # nic nie nadpisujemy, nawet w razie wyścigu
            raise FileExistsError(f"Plik docelowy już istnieje: {target}")
        os.replace(tmp, target)
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
            copy_verified(move.source, move.target)
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
            done.append(m)
            remove_empty_parents(m.target.parent, stop_at)
        except OSError as exc:
            failed.append((m, str(exc)))
    return done, failed


def undo_moves(moves: list[Move], stop_at: Path | None = None
               ) -> tuple[list[Move], list[tuple[Move, str]]]:
    """Cofa przeniesienia (w odwrotnej kolejności) i usuwa puste foldery docelowe."""
    reversed_moves = [Move(m.target, m.source) for m in reversed(moves)]
    done, failed = execute_moves(reversed_moves)
    for move in done:
        remove_empty_parents(move.source.parent, stop_at)
    return done, failed


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
        "przeniesienia": [{"z": str(m.source), "do": str(m.target)} for m in moves],
    }
    try:
        atomic_write_json(path or history_path(), data)
    except OSError:
        pass


def load_history(path: Path | None = None) -> tuple[list[Move], Path | None]:
    path = path or history_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        moves = [Move(Path(m["z"]), Path(m["do"])) for m in data.get("przeniesienia", [])]
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
