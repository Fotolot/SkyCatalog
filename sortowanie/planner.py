"""Kategorie lotów, walidacja formularza i wyliczanie folderów docelowych.

Struktury:
    Hobby/[Miejscowość]/Zdjęcia|WIDEO/                  (bez nazwy miejsca)
    Hobby/[Miejscowość]/[Nazwa miejsca]/Zdjęcia|WIDEO/  (nazwa wpisana przez użytkownika)
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
    place: str = ""      # Eventy: miejscowość; Zlecenia: lokalizacja; Hobby: miejscowość ręcznie
    # Hobby: ``name`` = opcjonalna nazwa miejsca / obiektu – wspólna dla wszystkich grup przypisania
    # (różne nazwy = osobne przypisania, np. po „Wydziel jako osobne”)
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


def town_options(groups: list[MediaGroup], places: dict[str, Place]) -> list[str]:
    """Propozycje miejscowości dla Eventów/Zleceń: rozpoznane najpierw, potem niejednoznaczne."""
    sure = [places[g.key].town for g in groups if g.key in places and places[g.key].town]
    ordered = sorted(set(sure), key=lambda t: -sure.count(t))
    for g in groups:
        p = places.get(g.key)
        if p is not None:
            ordered += [c for c in p.town_candidates if c not in ordered]
    return ordered


def suggest_town(groups: list[MediaGroup], places: dict[str, Place]) -> str:
    """Najczęściej rozpoznana miejscowość wśród grup."""
    counter: dict[str, int] = {}
    for g in groups:
        p = places.get(g.key)
        if p and p.town:
            counter[p.town] = counter.get(p.town, 0) + 1
    return max(counter, key=counter.get) if counter else ""


def is_pending(group: MediaGroup, places: dict[str, Place]) -> bool:
    """Grupa ma GPS, ale rozpoznawanie miejscowości jeszcze trwa."""
    return group.gps is not None and group.key not in places


# ── Foldery docelowe ───────────────────────────────────────────

def event_folder_name(assignment: Assignment) -> str | None:
    d = parse_date(assignment.date)
    name = sanitize_component(assignment.name)
    if d is None or not name:
        return None
    return sanitize_component(f"{d:%Y-%m-%d} {name}", 140)


def hobby_location(group: MediaGroup, place: Place | None, assignment: Assignment) -> tuple[str | None, str | None]:
    """(miejscowość, nazwa miejsca) dla Hobby.

    - miejscowość: rozpoznana z GPS lub wybrana przez użytkownika z listy propozycji;
      miejscowość wpisana ręcznie (``assignment.place``) – dla materiałów bez GPS lub bez wyniku,
    - nazwa miejsca: wyłącznie wpisana przez użytkownika (``assignment.name``), wspólna dla
      całego przypisania; pusta = materiały trafiają bezpośrednio do folderu miejscowości.
    """
    name = assignment.name.strip() or None
    if place is not None and place.town:
        return place.town, name
    if place is not None and place.ambiguous:
        return None, name
    return (assignment.place.strip() or None), name


def relative_dir(group: MediaGroup, assignment: Assignment | None, place: Place | None) -> Path | None:
    """Folder docelowy względem folderu archiwum lub None, gdy brakuje danych."""
    if assignment is None:
        return None
    root = ROOT_FOLDERS[assignment.category]
    if assignment.category == CATEGORY_HOBBY:
        town, name = hobby_location(group, place, assignment)
        town = sanitize_component(town or "")
        if not town:
            return None
        parts = [root, town]
        name = sanitize_component(name or "")
        if name:
            parts.append(name)
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
            errors.append(f"{label}: trwa rozpoznawanie miejscowości (grupy: {len(pending)}).")
        ready = [g for g in groups if not is_pending(g, places)]
        ambiguous = [g for g in ready if (p := places.get(g.key)) is not None and p.ambiguous]
        if ambiguous:
            errors.append(f"{label}: wybierz miejscowość z listy propozycji (grupy: {len(ambiguous)}).")
        if assignment.name.strip() and not sanitize_component(assignment.name):
            errors.append(f"{label}: nazwa miejsca „{assignment.name.strip()}” nie zawiera dozwolonych znaków.")
        missing = [g for g in ready if g not in ambiguous
                   and not hobby_location(g, places.get(g.key), assignment)[0]]
        if missing:
            errors.append(f"{label}: brak miejscowości (grupy: {len(missing)}) – wpisz miejscowość ręcznie.")
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

# Co zrobić, gdy w folderze docelowym jest już plik o tej samej nazwie (wybiera użytkownik)
ACTION_RENAME = "zmień nazwę"      # zachowaj oba – nowy plik dostaje dopisek (1), (2)…
ACTION_OVERWRITE = "nadpisz"       # poprzednia wersja trafia do kopii zapasowej w archiwum
ACTION_SKIP = "pomiń"              # nie kopiuj / nie przenoś tej grupy

ACTION_LABELS = {
    ACTION_RENAME: "Zmień nazwę nowego pliku (zachowaj oba)",
    ACTION_OVERWRITE: "Nadpisz plik w archiwum",
    ACTION_SKIP: "Pomiń (nie importuj)",
}


@dataclass
class Conflict:
    """Grupa materiałów, której plik(i) mają nazwy zajęte w folderze docelowym."""

    group: MediaGroup
    pairs: list[tuple[Path, Path]]        # (plik z importu, istniejący plik w archiwum)

    @property
    def key(self) -> str:
        return self.group.key


@dataclass
class ImportPlan:
    moves: list[Move]
    group_dirs: dict[str, Path]           # klucz grupy -> folder docelowy (względny)
    errors: list[str]
    unassigned: list[MediaGroup]
    conflicts: list[Conflict] = field(default_factory=list)
    resolutions: dict[str, str] = field(default_factory=dict)

    @property
    def unresolved(self) -> list[Conflict]:
        """Konflikty nazw bez decyzji użytkownika – przed wykonaniem trzeba zapytać."""
        return [c for c in self.conflicts if c.key not in self.resolutions]

    @property
    def skipped(self) -> list[str]:
        return [c.key for c in self.conflicts if self.resolutions.get(c.key) == ACTION_SKIP]

    @property
    def ready(self) -> bool:
        return not self.errors and not self.unassigned and bool(self.moves or self.unresolved)


def _renamed_targets(group: MediaGroup, target_dir: Path, taken: set[Path]) -> list[Path]:
    """Docelowe ścieżki plików grupy. Przy kolizji cała grupa dostaje ten sam sufiks,
    dzięki czemu film i jego SRT nadal mają pasujące nazwy."""
    for n in count(0):
        suffix = "" if n == 0 else f" ({n})"
        targets = [target_dir / f"{p.stem}{suffix}{p.suffix}" for p in group.files]
        if not any(t.exists() or t in taken for t in targets):
            return targets
    raise RuntimeError("unreachable")


def _group_moves(group: MediaGroup, target_dir: Path, taken: set[Path], action: str | None
                 ) -> tuple[list[Move], list[tuple[Path, Path]]]:
    """Operacje dla grupy i lista konfliktów z plikami już leżącymi w archiwum.

    Bez decyzji (``action`` None) grupa z konfliktem nie dostaje żadnych operacji – program
    najpierw pyta użytkownika. Kolizje nazw wewnątrz jednego importu (np. dwie karty z tym
    samym numerem pliku) zawsze dostają dopisek (1), (2)… – nic nie jest wtedy nadpisywane.
    """
    plain = [target_dir / p.name for p in group.files]
    existing = [(src, dst) for src, dst in zip(group.files, plain) if dst.exists()]
    if not existing:
        return [Move(s, t) for s, t in zip(group.files, _renamed_targets(group, target_dir, taken))], []
    if action is None or action == ACTION_SKIP:
        return [], existing
    if action == ACTION_OVERWRITE and not any(t in taken for t in plain):
        return [Move(s, t, overwrite=t.exists()) for s, t in zip(group.files, plain)], existing
    return [Move(s, t) for s, t in zip(group.files, _renamed_targets(group, target_dir, taken))], existing


def build_plan(groups: list[MediaGroup], assignment_of: dict[str, Assignment | None],
               places: dict[str, Place], target_root: str | Path,
               check_disk: bool = True, resolutions: dict[str, str] | None = None) -> ImportPlan:
    """``resolutions``: klucz grupy → ACTION_* – decyzje użytkownika dla zajętych nazw plików."""
    errors: list[str] = []
    resolutions = dict(resolutions or {})
    root = Path(target_root) if str(target_root or "").strip() else None
    if root is None:
        errors.append("Wybierz folder docelowy (archiwum).")

    unassigned = [g for g in groups if assignment_of.get(g.key) is None]
    if unassigned:
        errors.append(f"Nie wybrano kategorii (grupy: {len(unassigned)} z {len(groups)}).")

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
    conflicts: list[Conflict] = []
    taken: set[Path] = set()
    for g in groups:
        rel = relative_dir(g, assignment_of.get(g.key), places.get(g.key))
        if rel is None:
            continue
        group_dirs[g.key] = rel
        if root is not None and check_disk:
            group_moves, existing = _group_moves(g, root / rel, taken, resolutions.get(g.key))
            if existing:
                conflicts.append(Conflict(g, existing))
            for m in group_moves:
                taken.add(m.target)
            moves.extend(group_moves)
    if not groups:
        errors.append("Brak materiałów do przeniesienia.")
    known = {c.key for c in conflicts}
    resolutions = {k: v for k, v in resolutions.items() if k in known}
    return ImportPlan(moves, group_dirs, errors, unassigned, conflicts, resolutions)


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
