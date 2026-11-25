"""Wykrywanie kart pamięci (SD/microSD z drona lub aparatu).

Karta = dostępny dysk wymienny z folderem ``DCIM`` w katalogu głównym. Wykrywanie polega
na okresowym sprawdzaniu listy dysków (bez uprawnień administratora i bez usług systemowych).
Program nigdy nie zapisuje niczego na karcie.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Iterable

from .scanner import Drive, list_drives


@dataclass(frozen=True)
class Card:
    root: str
    label: str
    dcim: str

    @property
    def title(self) -> str:
        return f"{self.root.rstrip(os.sep)} {self.label}".strip()


def find_cards(drives: Iterable[Drive] | None = None,
               isdir: Callable[[str], bool] = os.path.isdir) -> list[Card]:
    cards = []
    for d in (drives if drives is not None else list_drives()):
        if d.kind != "wymienny" or not d.available:
            continue
        dcim = os.path.join(d.root, "DCIM")
        try:
            if isdir(dcim):
                cards.append(Card(d.root, d.label, dcim))
        except OSError:
            continue
    return cards


class CardWatcher:
    """Porównuje kolejne odczyty i zwraca karty, które właśnie się pojawiły."""

    def __init__(self, finder: Callable[[], list[Card]] = find_cards) -> None:
        self.finder = finder
        self.known: set[str] = {c.root for c in self._safe_find()}   # karty włożone przed startem nie wyskakują

    def _safe_find(self) -> list[Card]:
        try:
            return self.finder()
        except OSError:
            return []

    def poll(self) -> list[Card]:
        cards = self._safe_find()
        roots = {c.root for c in cards}
        new = [c for c in cards if c.root not in self.known]
        self.known = roots                                            # wyjęta karta może wrócić jako nowa
        return new

    def current(self) -> list[Card]:
        return self._safe_find()
