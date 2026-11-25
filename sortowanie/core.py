"""Logika sortowania plików – niezależna od interfejsu graficznego."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

# Tryby sortowania
MODE_TYPE = "typ"
MODE_DATE = "data"
MODE_EXTENSION = "rozszerzenie"
MODES = {
    MODE_TYPE: "Według typu (Obrazy, Dokumenty, …)",
    MODE_DATE: "Według daty modyfikacji (rok/miesiąc)",
    MODE_EXTENSION: "Według rozszerzenia (PDF, JPG, …)",
}

OTHER_CATEGORY = "Inne"

CATEGORIES: dict[str, tuple[str, ...]] = {
    "Obrazy": (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp",
               ".heic", ".svg", ".ico", ".raw", ".cr2", ".nef"),
    "Dokumenty": (".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md",
                  ".xls", ".xlsx", ".ods", ".csv", ".ppt", ".pptx", ".odp", ".epub"),
    "Wideo": (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"),
    "Muzyka": (".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"),
    "Archiwa": (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"),
    "Programy": (".exe", ".msi", ".bat", ".cmd", ".appx", ".msix"),
    "Kod": (".py", ".js", ".ts", ".html", ".css", ".json", ".xml", ".yml", ".yaml",
            ".c", ".cpp", ".h", ".cs", ".java", ".go", ".rs", ".sql", ".ps1", ".sh"),
}

_EXT_TO_CATEGORY = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts}

# Pliki, których nigdy nie ruszamy
IGNORED_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}


@dataclass(frozen=True)
class Move:
    """Pojedyncza zaplanowana operacja przeniesienia pliku."""

    source: Path
    target: Path


def category_for(path: Path) -> str:
    return _EXT_TO_CATEGORY.get(path.suffix.lower(), OTHER_CATEGORY)


def subfolder_for(path: Path, mode: str) -> Path:
    """Zwraca względną ścieżkę podfolderu docelowego dla pliku."""
    if mode == MODE_TYPE:
        return Path(category_for(path))
    if mode == MODE_EXTENSION:
        ext = path.suffix.lower().lstrip(".")
        return Path(ext.upper() if ext else "Bez_rozszerzenia")
    if mode == MODE_DATE:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        return Path(f"{mtime:%Y}") / f"{mtime:%m}"
    raise ValueError(f"Nieznany tryb sortowania: {mode}")


def _iter_files(folder: Path, recursive: bool, skip_dirs: set[Path]) -> Iterable[Path]:
    if recursive:
        for root, dirs, files in os.walk(folder):
            root_path = Path(root)
            # Nie wchodzimy do folderów, które sami tworzymy jako docelowe
            dirs[:] = [d for d in dirs if (root_path / d) not in skip_dirs]
            for name in files:
                yield root_path / name
    else:
        for entry in folder.iterdir():
            if entry.is_file():
                yield entry


def _unique_target(target: Path, taken: set[Path]) -> Path:
    """Dodaje sufiks ' (1)', ' (2)', … jeśli plik docelowy już istnieje."""
    candidate = target
    counter = 1
    while candidate.exists() or candidate in taken:
        candidate = target.with_name(f"{target.stem} ({counter}){target.suffix}")
        counter += 1
    return candidate


def plan_moves(folder: str | Path, mode: str, recursive: bool = False) -> list[Move]:
    """Przygotowuje listę przeniesień bez zmieniania czegokolwiek na dysku."""
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise NotADirectoryError(f"To nie jest folder: {folder}")
    if mode not in MODES:
        raise ValueError(f"Nieznany tryb sortowania: {mode}")

    # Przy sortowaniu rekurencyjnym pomijamy istniejące foldery docelowe
    skip_dirs: set[Path] = set()
    if recursive and mode == MODE_TYPE:
        skip_dirs = {folder / c for c in (*CATEGORIES, OTHER_CATEGORY)}

    moves: list[Move] = []
    taken: set[Path] = set()
    for file in sorted(_iter_files(folder, recursive, skip_dirs)):
        if file.name.lower() in IGNORED_NAMES or file.is_symlink():
            continue
        target_dir = folder / subfolder_for(file, mode)
        if file.parent == target_dir:
            continue  # plik już jest na swoim miejscu
        target = _unique_target(target_dir / file.name, taken)
        taken.add(target)
        moves.append(Move(file, target))
    return moves


ProgressCallback = Callable[[int, int, Move], None]


def execute_moves(moves: list[Move], progress: ProgressCallback | None = None) -> tuple[list[Move], list[tuple[Move, str]]]:
    """Wykonuje przeniesienia. Zwraca (udane, nieudane_z_błędem)."""
    done: list[Move] = []
    failed: list[tuple[Move, str]] = []
    total = len(moves)
    for i, move in enumerate(moves, start=1):
        try:
            move.target.parent.mkdir(parents=True, exist_ok=True)
            if move.target.exists():
                raise FileExistsError(f"Plik docelowy już istnieje: {move.target}")
            shutil.move(str(move.source), str(move.target))
            done.append(move)
        except OSError as exc:
            failed.append((move, str(exc)))
        if progress:
            progress(i, total, move)
    return done, failed


def undo_moves(moves: list[Move]) -> tuple[list[Move], list[tuple[Move, str]]]:
    """Cofa przeniesienia (w odwrotnej kolejności) i usuwa puste foldery."""
    reversed_moves = [Move(m.target, m.source) for m in reversed(moves)]
    done, failed = execute_moves(reversed_moves)
    for move in done:
        _remove_empty_parents(move.source.parent)
    return done, failed


def _remove_empty_parents(folder: Path, levels: int = 2) -> None:
    for _ in range(levels):
        try:
            folder.rmdir()  # usuwa tylko pusty folder
        except OSError:
            return
        folder = folder.parent


# ── Historia operacji (umożliwia cofnięcie) ────────────────────

def history_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "Sortowanie" / "historia_sortowania.json"


def save_history(moves: list[Move], path: Path | None = None) -> None:
    path = path or history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "czas": datetime.now().isoformat(timespec="seconds"),
        "przeniesienia": [{"z": str(m.source), "do": str(m.target)} for m in moves],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_history(path: Path | None = None) -> list[Move]:
    path = path or history_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [Move(Path(m["z"]), Path(m["do"])) for m in data.get("przeniesienia", [])]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def clear_history(path: Path | None = None) -> None:
    path = path or history_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
