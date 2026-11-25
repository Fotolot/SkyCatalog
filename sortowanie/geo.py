"""Rozpoznawanie miejscowości z GPS na podstawie OpenStreetMap (Overpass API).

- Do serwera wysyłane są WYŁĄCZNIE współrzędne – nigdy pliki.
- Miejscowość: obszary OSM obejmujące punkt (``is_in``) i analiza ich tagów.
  Wielokąty miejscowości trafiają do cache, więc kolejne punkty w ich granicach
  są rozpoznawane lokalnie, bez zapytania.
- Program NIE wyszukuje pobliskich obiektów i nie zgaduje, co sfotografowano –
  nazwę miejsca (opcjonalną) wpisuje użytkownik.
- Wybór miejscowości przy niejednoznacznym wyniku jest zapamiętywany osobno
  i podpowiadany przy kolejnych importach z tego miejsca.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import osm
from .config import Settings, app_data_dir, atomic_write_json
from .osm import Shape, TownResult, haversine_m
from .overpass import OverpassClient, OverpassError, OverpassUnavailable

SOURCE_ONLINE = "OSM"
SOURCE_CACHE = "cache"
SOURCE_CACHE_NEAR = "cache (pobliski)"
SOURCE_DECISION = "zapamiętany wybór"
SOURCE_NONE = "brak"

POINT_REUSE_M = 150        # ponowne użycie wyniku „bez obszaru” dla bliskich punktów
NEAR_FALLBACK_M = 1000     # offline: najbliższy zapisany wynik


@dataclass
class Place:
    """Stan lokalizacji jednej grupy materiałów."""

    town: str | None = None
    town_candidates: list[str] = field(default_factory=list)
    town_reason: str = ""
    town_source: str = SOURCE_NONE
    town_decided: bool = False          # miejscowość wybrana przez użytkownika
    error: str = ""

    @property
    def ambiguous(self) -> bool:
        return self.town is None and bool(self.town_candidates)


# ── Cache danych OSM ───────────────────────────────────────────

def cache_path() -> Path:
    return app_data_dir() / "cache_osm.json"


def decisions_path() -> Path:
    return app_data_dir() / "decyzje_lokalizacji.json"


class OsmCache:
    """Lokalny cache: wielokąty miejscowości i wyniki dla punktów.

    Starsze wersje zapisywały tu także kafelki obiektów („tiles”, „elements”) – nie są już
    używane, ale nie są też usuwane z pliku użytkownika (znikną po „Wyczyść cache”)."""

    VERSION = 1

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or cache_path()
        self._lock = threading.RLock()
        self.data = self._empty()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("version") == self.VERSION:
                self.data.update(loaded)
        except (OSError, ValueError):
            pass
        self._shapes: dict[str, Shape] = {}

    @classmethod
    def _empty(cls) -> dict:
        return {"version": cls.VERSION, "town_areas": {}, "points": []}

    def save(self) -> None:
        with self._lock:
            snapshot = json.loads(json.dumps(self.data))
        try:
            atomic_write_json(self.path, snapshot)
        except OSError:
            pass

    def clear(self) -> None:
        with self._lock:
            self.data = self._empty()
            self._shapes = {}
        try:
            self.path.unlink()
        except OSError:
            pass

    def summary(self) -> str:
        with self._lock:
            d = self.data
            return f"miejscowości: {len(d['town_areas'])}, punktów: {len(d['points'])}"

    # Miejscowości
    def add_town_area(self, area: osm.AreaInfo, town: str, why: str) -> None:
        if area.shape is None or not area.shape.outer:
            return
        with self._lock:
            self.data["town_areas"][area.osm_id] = {"town": town, "why": why, "t": time.time(),
                                                    "shape": area.shape.to_json()}
            self._shapes[area.osm_id] = area.shape

    def towns_containing(self, lat: float, lon: float, max_age_days: float | None) -> list[tuple[str, str]]:
        found = []
        now = time.time()
        with self._lock:
            for aid, entry in self.data["town_areas"].items():
                if max_age_days is not None and now - entry["t"] > max_age_days * 86400:
                    continue
                shape = self._shapes.get(aid)
                if shape is None:
                    shape = self._shapes[aid] = Shape.from_json(entry["shape"])
                if shape.contains(lat, lon):
                    found.append((entry["town"], entry["why"]))
        return found

    def add_point(self, lat: float, lon: float, result: TownResult) -> None:
        with self._lock:
            self.data["points"].append({"lat": lat, "lon": lon, "t": time.time(), "r": result.to_json()})

    def nearest_point(self, lat: float, lon: float, max_m: float, max_age_days: float | None
                      ) -> tuple[TownResult, float] | None:
        best, best_d = None, max_m
        now = time.time()
        with self._lock:
            for p in self.data["points"]:
                if max_age_days is not None and now - p["t"] > max_age_days * 86400:
                    continue
                d = haversine_m(lat, lon, p["lat"], p["lon"])
                if d <= best_d:
                    best, best_d = p, d
        return (TownResult.from_json(best["r"]), best_d) if best else None


class DecisionStore:
    """Wybory miejscowości – nie są usuwane przy czyszczeniu cache danych OSM.

    Sekcja „objects” ze starszych wersji jest wczytywana i zapisywana bez zmian (nieużywana)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or decisions_path()
        self._lock = threading.Lock()
        self.data = {"towns": {}, "objects": {}}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data.update({k: loaded.get(k, {}) for k in ("towns", "objects")})
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        try:
            atomic_write_json(self.path, self.data)
        except OSError:
            pass

    @staticmethod
    def _town_key(lat: float, lon: float, candidates: list[str]) -> str:
        return f"{lat:.2f},{lon:.2f}|" + "|".join(sorted(c.casefold() for c in candidates))

    def town_choice(self, lat: float, lon: float, candidates: list[str]) -> str | None:
        with self._lock:
            return self.data["towns"].get(self._town_key(lat, lon, candidates))

    def remember_town(self, lat: float, lon: float, candidates: list[str], choice: str) -> None:
        with self._lock:
            self.data["towns"][self._town_key(lat, lon, candidates)] = choice
            self._save()

    def clear(self) -> None:
        with self._lock:
            self.data = {"towns": {}, "objects": {}}
            try:
                self.path.unlink()
            except OSError:
                pass

    def __len__(self) -> int:
        with self._lock:
            return len(self.data["towns"])


# ── Usługa lokalizacji ─────────────────────────────────────────

class LocationService:
    def __init__(self, settings: Settings, cache: OsmCache | None = None,
                 decisions: DecisionStore | None = None, client: OverpassClient | None = None) -> None:
        self.settings = settings
        self.cache = cache if cache is not None else OsmCache()
        self.decisions = decisions if decisions is not None else DecisionStore()
        self.client = client if client is not None else OverpassClient(
            lambda: settings.get("overpass_url"), lambda: settings.get("contact_email"))
        self._network_down = False

    @property
    def network_down(self) -> bool:
        return self._network_down

    def reset_network_state(self) -> None:
        self._network_down = False

    def _online(self) -> bool:
        return not self.settings.get("offline") and not self._network_down

    def _max_age(self) -> float | None:
        days = self.settings.get("cache_days")
        return float(days) if days else None

    @property
    def language(self) -> str:
        return self.settings.get("language") or "pl"

    # Miejscowości
    def _apply_town_decision(self, lat: float, lon: float, result: TownResult) -> tuple[TownResult, bool]:
        if result.ambiguous:
            choice = self.decisions.town_choice(lat, lon, result.candidates)
            if choice:
                return TownResult(choice, result.candidates, "zapamiętany wybór użytkownika",
                                  result.areas, result.nodes), True
        return result, False

    def town(self, lat: float, lon: float) -> tuple[TownResult, str, str]:
        """(wynik, źródło, błąd). Najpierw cache, potem jedno zapytanie Overpass."""
        max_age = self._max_age()
        # 1. Punkt w zapisanym wielokącie miejscowości – bez zapytania
        inside = self.cache.towns_containing(lat, lon, max_age)
        names = list(dict.fromkeys(t for t, _ in inside))
        if len(names) == 1:
            return TownResult(names[0], [], f"cache: {inside[0][1]}"), SOURCE_CACHE, ""
        # 2. Wcześniejszy wynik dla bardzo bliskiego punktu
        near = self.cache.nearest_point(lat, lon, POINT_REUSE_M, max_age)
        if near is not None and len(names) == 0:
            result, decided = self._apply_town_decision(lat, lon, near[0])
            return result, SOURCE_DECISION if decided else SOURCE_CACHE, ""
        error = ""
        if self._online():
            try:
                data = self.client.query(osm.area_query(lat, lon), "obszary obejmujące punkt")
                areas, nodes = osm.parse_area_response(data, lat, lon, self.language)
                result = osm.decide_town(areas, nodes)
                if result.town:
                    for a in areas:
                        if a.name == result.town and a.kind in (osm.KIND_SETTLEMENT, osm.KIND_ADMIN):
                            self.cache.add_town_area(a, result.town, result.reason)
                self.cache.add_point(lat, lon, result)
                self.cache.save()
                result, decided = self._apply_town_decision(lat, lon, result)
                return result, SOURCE_DECISION if decided else SOURCE_ONLINE, ""
            except OverpassUnavailable as exc:
                self._network_down = True
                error = f"Brak połączenia z Overpass: {exc}"
            except OverpassError as exc:
                error = str(exc)
        # 3. Bez internetu – dowolnie stare dane lub najbliższy zapisany punkt
        inside = self.cache.towns_containing(lat, lon, None)
        names = list(dict.fromkeys(t for t, _ in inside))
        if len(names) == 1:
            return TownResult(names[0], [], "cache (bez internetu)"), SOURCE_CACHE, error
        near = self.cache.nearest_point(lat, lon, NEAR_FALLBACK_M, None)
        if near is not None:
            result, decided = self._apply_town_decision(lat, lon, near[0])
            return result, SOURCE_DECISION if decided else SOURCE_CACHE_NEAR, error
        return TownResult(None, [], "brak danych"), SOURCE_NONE, error or "Brak zapisanych danych offline"
