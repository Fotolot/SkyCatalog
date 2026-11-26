"""Ścieżki miniatur podglądu (cache w %LOCALAPPDATA%\\SkyCatalog\\miniatury) – bez zależności od Qt."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .config import local_data_dir


def thumb_dir() -> Path:
    return local_data_dir() / "miniatury"


def thumb_path(path: str | Path, size: int, mtime: float) -> Path:
    """Miniatura zależy od ścieżki, rozmiaru i daty – zmieniony plik dostaje nową miniaturę."""
    h = hashlib.sha1(f"{Path(path)}|{size}|{mtime}".encode("utf-8", "surrogatepass")).hexdigest()[:24]
    return thumb_dir() / f"{h}.jpg"


def remove_thumbs(files) -> int:
    """Usuwa miniatury dla krotek (ścieżka, rozmiar, data). Zwraca liczbę usuniętych plików cache.
    Usuwane są WYŁĄCZNIE pliki w folderze miniatur – nigdy oryginały."""
    removed = 0
    base = thumb_dir().resolve()
    for path, size, mtime in files:
        t = thumb_path(path, size, mtime)
        try:
            if t.resolve().parent == base and t.is_file():
                t.unlink()
                removed += 1
        except OSError:
            pass
    return removed
