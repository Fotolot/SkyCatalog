"""Kategorie lotów, walidacja formularza i wyliczanie folderów docelowych.

Struktury:
    Hobby/[Miejscowość]/[Obiekt]/WIDEO|Zdjęcia/      (obiekt pomijany, gdy brak w promieniu)
    Eventy/[Miejscowość]/[RRRR-MM-DD] [Nazwa]/WIDEO|Zdjęcia/
    Zlecenia/[Lokalizacja]/[RRRR-MM-DD] [Nazwa]/WIDEO|Zdjęcia/

Kategoria NIE jest rozpoznawana automatycznie – zawsze wybiera ją użytkownik.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import count
from pathlib import Path

from .core import Move
from .geo import Place
from .media import MediaGroup

CATEGORY_HOBBY = "hobby"
CATEGORY_EVENT = "eventy"
CATEGORY_ORDER = "zlecenia"

CATEGORY_LABELS = {
    CATEGORY_HOBBY: "Hobby / Luźne loty",
    CATEGORY_EVENT: "Eventy",
    CATEGORY_ORDER: "Zlecenia",
}
ROOT_FOLDERS = {
    CATEGORY_HOBBY: "Hobby",
    CATEGORY_EVENT: "Eventy",
    CATEGORY_ORDER: "Zlecenia",
}

_ids = count(1)


@dataclass
class Assignment:
    """Przypisanie grupy materiałów do kategorii wraz z polami formularza."""

    category: str
    name: str = ""       # nazwa wydarzenia / zlecenia
    date: str = ""       # RRRR-MM-DD
    place: str = ""      # Eventy: miejscowość; Zlecenia: lokalizacja; Hobby: miejscowość zastępcza
    # Czy użytkownik sam edytował pole (wtedy nie nadpisujemy go propozycjami)
    edited: set = field(default_factory=set)
    id: int = field(default_factory=lambda: next(_ids))

    def copy_fields_from(self, other: "Assignment") -> None:
        self.name, self.date, self.place = other.name, other.date, other.place
        self.edited = set(other.edited)


# ── Pomocnicze ─────────────────────────────────────────────────

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def sanitize_component(text: str, max_len: int = 100) -> str:
    """Zamienia tekst na bezpieczną nazwę folderu Windows (polskie znaki zostają)."""
    text = _INVALID_CHARS.sub("-", text or "")
    text = re.sub(r"\s+", " ", text).strip().rstrip(". ")
    if len(text) > max_len:
        text = text[:max_len].rstrip(". ")
    if text.upper().split(".")[0] in _RESERVED:
        text += "_"
    return text


def parse_date(text: str) -> date | None:
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def suggest_date(groups: list[MediaGroup]) -> str:
    """Najwcześniejsza data wykonania materiałów (EXIF/SRT/nazwa/MP4). Nigdy data modyfikacji."""
    dates = [g.captured for g in groups if g.captured]
    return min(dates).strftime("%Y-%m-%d") if dates else ""


def suggest_town(groups: list[MediaGroup], places: dict[str, Place]) -> str:
    """Najczęściej rozpoznana miejscowość wśród grup."""
    counter: dict[str, int] = {}
    for g in groups:
        p = places.get(g.key)
        if p and p.town:
            counter[p.town] = counter.get(p.town, 0) + 1
    return max(counter, key=counter.get) if counter else ""


def is_pending(group: MediaGroup, places: dict[str, Place]) -> bool:
    """Grupa ma GPS, ale rozpoznawanie lokalizacji jeszcze trwa."""
    return group.gps is not None and group.key not in places


# ── Foldery docelowe ───────────────────────────────────────────

def event_folder_name(assignment: Assignment) -> str | None:
    d = parse_date(assignment.date)
    name = sanitize_component(assignment.name)
    if d is None or not name:
        return None
    return sanitize_component(f"{d:%Y-%m-%d} {name}", 140)


def hobby_location(group: MediaGroup, place: Place | None, assignment: Assignment) -> tuple[str | None, str | None]:
    """(miejscowość, obiekt) dla Hobby. Ręczna miejscowość służy, gdy GPS nic nie dał."""
    if place and place.town:
        return place.town, place.object_name
    manual = assignment.place.strip()
    return (manual or None), None


def relative_dir(group: MediaGroup, assignment: Assignment | None, place: Place | None) -> Path | None:
    """Folder docelowy względem folderu archiwum lub None, gdy brakuje danych."""
    if assignment is None:
        return None
    root = ROOT_FOLDERS[assignment.category]
    if assignment.category == CATEGORY_HOBBY:
        town, obj = hobby_location(group, place, assignment)
        town = sanitize_component(town or "")
        if not town:
            return None
        parts = [root, town]
        obj = sanitize_component(obj or "")
        if obj:
            parts.append(obj)
        return Path(*parts, group.media_folder)

    place_name = sanitize_component(assignment.place)
    folder = event_folder_name(assignment)
    if not place_name or not folder:
        return None
    return Path(root, place_name, folder, group.media_folder)


# ── Walidacja ──────────────────────────────────────────────────

def validate_assignment(assignment: Assignment, groups: list[MediaGroup],
                        places: dict[str, Place]) -> list[str]:
    errors: list[str] = []
    label = CATEGORY_LABELS[assignment.category]
    if assignment.category == CATEGORY_HOBBY:
        pending = [g for g in groups if is_pending(g, places)]
        if pending:
            errors.append(f"{label}: trwa rozpoznawanie lokalizacji ({len(pending)} grup).")
        missing = [g for g in groups if not is_pending(g, places)
                   and not hobby_location(g, places.get(g.key), assignment)[0]]
        if missing:
            errors.append(f"{label}: brak miejscowości dla {len(missing)} grup – "
                          "wpisz miejscowość ręcznie.")
        return errors

    what = "wydarzenia" if assignment.category == CATEGORY_EVENT else "zlecenia"
    if not sanitize_component(assignment.name):
        errors.append(f"{label}: wpisz nazwę {what}.")
    if not assignment.date.strip():
        errors.append(f"{label}: wybierz datę {what}.")
    elif parse_date(assignment.date) is None:
        errors.append(f"{label}: nieprawidłowa data „{assignment.date}” (RRRR-MM-DD).")
    if not sanitize_component(assignment.place):
        errors.append(f"{label}: uzupełnij {'miejscowość' if assignment.category == CATEGORY_EVENT else 'lokalizację zlecenia'}.")
    return errors


# ── Plan importu ───────────────────────────────────────────────

@dataclass
class ImportPlan:
    moves: list[Move]
    group_dirs: dict[str, Path]           # klucz grupy -> folder docelowy (względny)
    errors: list[str]
    unassigned: list[MediaGroup]

    @property
    def ready(self) -> bool:
        return not self.errors and not self.unassigned and bool(self.moves)


def _group_targets(group: MediaGroup, target_dir: Path, taken: set[Path]) -> list[Path]:
    """Docelowe ścieżki plików grupy. Przy kolizji cała grupa dostaje ten sam sufiks,
    dzięki czemu film i jego SRT nadal mają pasujące nazwy."""
    for n in count(0):
        suffix = "" if n == 0 else f" ({n})"
        targets = [target_dir / f"{p.stem}{suffix}{p.suffix}" for p in group.files]
        if not any(t.exists() or t in taken for t in targets):
            return targets
    raise RuntimeError("unreachable")


def build_plan(groups: list[MediaGroup], assignment_of: dict[str, Assignment | None],
               places: dict[str, Place], target_root: str | Path,
               check_disk: bool = True) -> ImportPlan:
    errors: list[str] = []
    root = Path(target_root) if str(target_root or "").strip() else None
    if root is None:
        errors.append("Wybierz folder docelowy (archiwum).")

    unassigned = [g for g in groups if assignment_of.get(g.key) is None]
    if unassigned:
        errors.append(f"Nie wybrano kategorii dla {len(unassigned)} z {len(groups)} grup materiałów.")

    # Walidacja każdego przypisania osobno
    by_assignment: dict[int, tuple[Assignment, list[MediaGroup]]] = {}
    for g in groups:
        a = assignment_of.get(g.key)
        if a is not None:
            by_assignment.setdefault(a.id, (a, []))[1].append(g)
    for a, members in by_assignment.values():
        errors.extend(validate_assignment(a, members, places))

    group_dirs: dict[str, Path] = {}
    moves: list[Move] = []
    taken: set[Path] = set()
    for g in groups:
        rel = relative_dir(g, assignment_of.get(g.key), places.get(g.key))
        if rel is None:
            continue
        group_dirs[g.key] = rel
        if root is not None and check_disk:
            for src, dst in zip(g.files, _group_targets(g, root / rel, taken)):
                taken.add(dst)
                moves.append(Move(src, dst))
    if not groups:
        errors.append("Brak materiałów do przeniesienia.")
    return ImportPlan(moves, group_dirs, errors, unassigned)


def check_folders(source: str, target_root: str) -> str | None:
    """Zwraca komunikat błędu, jeśli folder docelowy i źródłowy się zawierają."""
    if not source or not target_root:
        return None
    try:
        s, t = Path(source).resolve(), Path(target_root).resolve()
    except OSError:
        return None
    if s == t or s in t.parents:
        return "Folder docelowy nie może znajdować się wewnątrz folderu importu."
    # Import z podfolderu archiwum (np. „Do posortowania”) jest dozwolony
    return None


# ── Istniejące wydarzenia i zlecenia ───────────────────────────

_EXISTING_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (.+)$")


@dataclass(frozen=True)
class ExistingTarget:
    category: str
    place: str
    date: str
    name: str

    @property
    def label(self) -> str:
        return f"{self.place} / {self.date} {self.name}"


def list_existing(target_root: str | Path, category: str) -> list[ExistingTarget]:
    """Wydarzenia/zlecenia już istniejące w archiwum – aby dołączać kolejne importy."""
    if category not in (CATEGORY_EVENT, CATEGORY_ORDER) or not target_root:
        return []
    base = Path(target_root) / ROOT_FOLDERS[category]
    result: list[ExistingTarget] = []
    try:
        for place_dir in base.iterdir():
            if not place_dir.is_dir():
                continue
            for item in place_dir.iterdir():
                m = _EXISTING_RE.match(item.name)
                if item.is_dir() and m:
                    result.append(ExistingTarget(category, place_dir.name, m.group(1), m.group(2)))
    except OSError:
        return result
    result.sort(key=lambda e: (e.date, e.place, e.name), reverse=True)
    return result
