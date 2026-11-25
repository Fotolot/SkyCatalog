"""Przenoszenie plików, historia operacji i cofanie ostatniego importu."""

from __future__ import annotations

import json
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
                 op_id: int | None = None) -> None:
    data = {
        "czas": datetime.now().isoformat(timespec="seconds"),
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


def clear_history(path: Path | None = None) -> None:
    try:
        (path or history_path()).unlink()
    except FileNotFoundError:
        pass
