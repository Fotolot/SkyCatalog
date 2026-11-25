"""Wczytywanie importu (karta pamięci / folder) i grupowanie powiązanych plików.

Grupa = wszystkie pliki o tej samej nazwie (bez rozszerzenia) w tym samym folderze,
np. ``DJI_0001.MP4`` + ``DJI_0001.SRT`` + ``DJI_0001.LRF``. Cała grupa zawsze
otrzymuje tę samą kategorię i to samo miejsce docelowe.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .metadata import GPS, date_from_filename, read_file_metadata

VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".mts", ".m2ts", ".insv", ".360"}
PHOTO_EXT = {".jpg", ".jpeg", ".dng", ".heic", ".png", ".tif", ".tiff", ".raw",
             ".arw", ".cr2", ".cr3", ".nef", ".insp"}
# Pliki towarzyszące – trafiają razem z „głównym” plikiem grupy
SIDECAR_EXT = {".srt", ".lrf", ".lrv", ".thm", ".xmp", ".aae", ".json", ".txt", ".ass"}

VIDEO_FOLDER = "WIDEO"
PHOTO_FOLDER = "Zdjęcia"

KIND_VIDEO = "wideo"
KIND_PHOTO = "zdjęcie"

_SKIP_DIRS = {"system volume information", "$recycle.bin", ".trashes", ".spotlight-v100", ".fseventsd"}


@dataclass
class MediaGroup:
    key: str
    name: str
    files: list[Path]
    kind: str
    gps: GPS | None = None
    captured: datetime | None = None
    captured_source: str = ""  # skąd pochodzi data: exif / srt / nazwa / mp4
    size: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def media_folder(self) -> str:
        return VIDEO_FOLDER if self.kind == KIND_VIDEO else PHOTO_FOLDER

    @property
    def extensions(self) -> str:
        return ", ".join(sorted({p.suffix.upper().lstrip(".") for p in self.files}))


def _group_kind(files: list[Path]) -> str | None:
    exts = {p.suffix.lower() for p in files}
    if exts & VIDEO_EXT:
        return KIND_VIDEO
    if exts & PHOTO_EXT:
        return KIND_PHOTO
    if ".lrf" in exts or ".lrv" in exts:  # sam podgląd wideo z drona
        return KIND_VIDEO
    return None


def _metadata_order(files: list[Path], kind: str) -> list[Path]:
    """Kolejność plików, z których czytamy GPS/datę (najbardziej wiarygodne najpierw)."""
    def rank(p: Path) -> int:
        ext = p.suffix.lower()
        if kind == KIND_VIDEO:
            return {".srt": 0}.get(ext, 1 if ext in VIDEO_EXT else 2)
        return {".jpg": 0, ".jpeg": 0}.get(ext, 1 if ext in PHOTO_EXT else 2)
    return sorted(files, key=rank)


def build_group(key: str, files: list[Path]) -> MediaGroup | None:
    kind = _group_kind(files)
    if kind is None:
        return None
    files = sorted(files, key=lambda p: (p.suffix.lower() in SIDECAR_EXT, p.name.lower()))
    group = MediaGroup(key=key, name=files[0].stem, files=files, kind=kind)
    group.size = sum(_safe_size(p) for p in files)

    srt_time: datetime | None = None
    other_time: datetime | None = None
    exif_time: datetime | None = None
    for path in _metadata_order(files, kind):
        gps, taken = read_file_metadata(path)
        if group.gps is None and gps is not None:
            group.gps = gps
        if taken is not None:
            ext = path.suffix.lower()
            if ext == ".srt":
                srt_time = srt_time or taken
            elif ext in {".jpg", ".jpeg", ".dng", ".tif", ".tiff", ".arw", ".nef", ".cr2"}:
                exif_time = exif_time or taken
            else:
                other_time = other_time or taken

    name_time = date_from_filename(group.name)
    for value, source in ((exif_time, "exif"), (srt_time, "srt"), (name_time, "nazwa pliku"),
                          (other_time, "mp4")):
        if value is not None:
            group.captured, group.captured_source = value, source
            break
    return group


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def collect_files(folder: Path) -> dict[str, list[Path]]:
    """Zbiera pliki rekurencyjnie i grupuje je po (folder, nazwa bez rozszerzenia)."""
    groups: dict[str, list[Path]] = {}
    known = VIDEO_EXT | PHOTO_EXT | SIDECAR_EXT
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d.lower() not in _SKIP_DIRS and not d.startswith(".")]
        for name in files:
            path = Path(root) / name
            if name.startswith(".") or path.suffix.lower() not in known or path.is_symlink():
                continue
            key = str(path.parent / path.stem).lower()
            groups.setdefault(key, []).append(path)
    return groups


def scan_source(folder: str | Path,
                progress: Callable[[int, int], None] | None = None) -> list[MediaGroup]:
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError(f"To nie jest folder: {folder}")
    raw = collect_files(folder)
    result: list[MediaGroup] = []
    total = len(raw)
    for i, (key, files) in enumerate(sorted(raw.items()), start=1):
        group = build_group(key, files)
        if group is not None:
            result.append(group)
        if progress:
            progress(i, total)
    result.sort(key=lambda g: (g.captured or datetime.max, g.name.lower()))
    return result
