"""Rozpoznawanie miejscowości i obiektów z GPS na podstawie OpenStreetMap (Overpass API).

- Do serwera wysyłane są WYŁĄCZNIE współrzędne / prostokąty – nigdy pliki.
- Miejscowość: obszary OSM obejmujące punkt (``is_in``) i analiza ich tagów.
  Wielokąty miejscowości trafiają do cache, więc kolejne punkty w ich granicach
  są rozpoznawane lokalnie, bez zapytania.
- Obiekty: kafelki ~1,1 × 0,7 km pobierane raz (z geometrią) dla wszystkich
  materiałów, których promień je obejmuje; odległość liczona do granic obiektów.
- Decyzje użytkownika (wybór miejscowości, zatwierdzenie/odrzucenie obiektu) są
  zapamiętywane osobno i podpowiadane przy kolejnych importach z tego miejsca.
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import osm
from .config import Settings, app_data_dir, atomic_write_json
from .osm import PoiCandidate, Shape, TownResult, haversine_m
from .overpass import OverpassClient, OverpassError, OverpassUnavailable

SOURCE_ONLINE = "OSM"
SOURCE_CACHE = "cache"
SOURCE_CACHE_NEAR = "cache (pobliski)"
SOURCE_DECISION = "zapamiętany wybór"
SOURCE_NONE = "brak"

TILE_DEG = 0.01            # bok kafelka obiektów (stopnie)
TILES_PER_QUERY = 6        # ile kafelków w jednym zapytaniu (małe zapytania łatwiej przechodzą)
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
    objects: list[PoiCandidate] = field(default_factory=list)
    object_checked: bool = False        # wyszukiwanie obiektów wykonane
    object_failed: bool = False         # nie sprawdzono (brak danych offline)
    object_choice: str | None = None    # None = czeka na decyzję, "" = bez obiektu, inaczej osm_id
    object_choice_source: str = ""      # "użytkownik" / "zapamiętany wybór"

    @property
    def ambiguous(self) -> bool:
        return self.town is None and bool(self.town_candidates)

    @property
    def proposed_object(self) -> PoiCandidate | None:
        return self.objects[0] if self.objects else None

    @property
    def chosen_object(self) -> PoiCandidate | None:
        if not self.object_choice:
            return None
        return next((o for o in self.objects if o.osm_id == self.object_choice), None)

    @property
    def object_name(self) -> str | None:
        chosen = self.chosen_object
        return chosen.name if chosen else None

    @property
    def needs_object_decision(self) -> bool:
        return bool(self.town) and bool(self.objects) and self.object_choice is None


# ── Cache danych OSM ───────────────────────────────────────────

def cache_path() -> Path:
    return app_data_dir() / "cache_osm.json"


def decisions_path() -> Path:
    return app_data_dir() / "decyzje_lokalizacji.json"


def tile_key(ix: int, iy: int) -> str:
    return f"{ix},{iy}"


def tile_bbox(ix: int, iy: int) -> tuple[float, float, float, float]:
    return (round(iy * TILE_DEG, 6), round(ix * TILE_DEG, 6),
            round((iy + 1) * TILE_DEG, 6), round((ix + 1) * TILE_DEG, 6))


def tiles_for_radius(lat: float, lon: float, radius_m: float) -> list[tuple[int, int]]:
    dlat = radius_m / 111_320
    dlon = radius_m / (111_320 * max(0.1, math.cos(math.radians(lat))))
    x0, x1 = math.floor((lon - dlon) / TILE_DEG), math.floor((lon + dlon) / TILE_DEG)
    y0, y1 = math.floor((lat - dlat) / TILE_DEG), math.floor((lat + dlat) / TILE_DEG)
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


_KEEP_TAG_KEYS = {"name", "name:pl", "place", "boundary", "admin_level", "border_type", "type",
                  "wikidata", "wikipedia"} | {k for k, _v, _l in osm.POI_RULES}


def _slim_tags(tags: dict) -> dict:
    return {k: v for k, v in tags.items() if k in _KEEP_TAG_KEYS}


class OsmCache:
    """Lokalny cache: wielokąty miejscowości, wyniki dla punktów i kafelki obiektów."""

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
        return {"version": cls.VERSION, "town_areas": {}, "points": [], "tiles": {}, "elements": {}}

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
            return (f"miejscowości: {len(d['town_areas'])}, punktów: {len(d['points'])}, "
                    f"kafelków obiektów: {len(d['tiles'])}")

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

    # Kafelki obiektów
    def tile_fresh(self, key: str, max_age_days: float | None) -> bool:
        with self._lock:
            t = self.data["tiles"].get(key)
        if t is None:
            return False
        return max_age_days is None or time.time() - t["t"] <= max_age_days * 86400

    def store_tiles(self, keys: list[str], elements: list[dict],
                    bboxes: dict[str, tuple[float, float, float, float]]) -> None:
        with self._lock:
            ids_by_tile: dict[str, list[str]] = {k: [] for k in keys}
            for el in elements:
                eid = osm.element_id(el)
                shape = osm.shape_from_element(el)
                self.data["elements"][eid] = {"type": el.get("type"), "tags": _slim_tags(el.get("tags") or {}),
                                              "shape": shape.to_json()}
                self._shapes[eid] = shape
                b = shape.bbox
                for k in keys:
                    s, w, n, e = bboxes[k]
                    if b is None or not (b[2] < s or b[0] > n or b[3] < w or b[1] > e):
                        ids_by_tile[k].append(eid)
            for k in keys:
                self.data["tiles"][k] = {"t": time.time(), "ids": ids_by_tile[k]}

    def elements_for_tiles(self, keys: Iterable[str]) -> tuple[list[dict], dict[str, Shape]]:
        elements, shapes, seen = [], {}, set()
        with self._lock:
            for k in keys:
                for eid in self.data["tiles"].get(k, {}).get("ids", []):
                    if eid in seen or eid not in self.data["elements"]:
                        continue
                    seen.add(eid)
                    e = self.data["elements"][eid]
                    shape = self._shapes.get(eid)
                    if shape is None:
                        shape = self._shapes[eid] = Shape.from_json(e["shape"])
                    shapes[eid] = shape
                    elements.append({"type": e["type"], "id": eid[1:], "tags": e["tags"]})
        # element_id() składa id z pierwszej litery typu – odtwarzamy zgodny format
        for el in elements:
            el["id"] = int(el["id"]) if str(el["id"]).isdigit() else el["id"]
        return elements, shapes


class DecisionStore:
    """Decyzje użytkownika – nie są usuwane przy czyszczeniu cache danych OSM."""

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

    @staticmethod
    def _object_key(lat: float, lon: float, ids: list[str]) -> str:
        return f"{lat:.3f},{lon:.3f}|" + "|".join(sorted(ids))

    def town_choice(self, lat: float, lon: float, candidates: list[str]) -> str | None:
        with self._lock:
            return self.data["towns"].get(self._town_key(lat, lon, candidates))

    def remember_town(self, lat: float, lon: float, candidates: list[str], choice: str) -> None:
        with self._lock:
            self.data["towns"][self._town_key(lat, lon, candidates)] = choice
            self._save()

    def object_choice(self, lat: float, lon: float, ids: list[str]) -> str | None:
        with self._lock:
            return self.data["objects"].get(self._object_key(lat, lon, ids))

    def remember_object(self, lat: float, lon: float, ids: list[str], choice: str) -> None:
        with self._lock:
            self.data["objects"][self._object_key(lat, lon, ids)] = choice
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
            return len(self.data["towns"]) + len(self.data["objects"])


# ── Usługa lokalizacji ─────────────────────────────────────────

Point = tuple[str, float, float]   # (klucz grupy, lat, lon)


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

    # Obiekty
    def objects(self, points: list[Point],
                on_result: Callable[[str, list[PoiCandidate], str, str, str | None], None]) -> None:
        """Wyszukuje obiekty dla wielu punktów naraz. Pobiera tylko brakujące kafelki.

        on_result(klucz, kandydaci, źródło, błąd, zapamiętana_decyzja)
        """
        radius = float(self.settings.get("poi_radius_m") or 250)
        max_age = self._max_age()
        needed: dict[str, list[str]] = {}
        missing: list[tuple[int, int]] = []
        for key, lat, lon in points:
            tiles = tiles_for_radius(lat, lon, radius)
            needed[key] = [tile_key(*t) for t in tiles]
            for t in tiles:
                if not self.cache.tile_fresh(tile_key(*t), max_age) and t not in missing:
                    missing.append(t)

        error, fetched_ok = "", True
        if missing and self._online():
            for i in range(0, len(missing), TILES_PER_QUERY):
                chunk = missing[i:i + TILES_PER_QUERY]
                keys = [tile_key(*t) for t in chunk]
                bboxes = {tile_key(*t): tile_bbox(*t) for t in chunk}
                try:
                    data = self.client.query(osm.poi_query(list(bboxes.values())),
                                             f"obiekty ({len(chunk)} kafelków)")
                    self.cache.store_tiles(keys, data.get("elements", []), bboxes)
                    self.cache.save()
                except OverpassUnavailable as exc:
                    self._network_down = True
                    error, fetched_ok = f"Brak połączenia z Overpass: {exc}", False
                    break
                except OverpassError as exc:
                    error, fetched_ok = str(exc), False
                    break
        elif missing:
            fetched_ok = False
            error = "Tryb offline" if self.settings.get("offline") else "Brak połączenia z Overpass"

        for key, lat, lon in points:
            keys = needed[key]
            have_all = all(self.cache.tile_fresh(k, None) for k in keys)
            if not have_all:
                on_result(key, [], SOURCE_NONE, error or "Brak danych", None)
                continue
            elements, shapes = self.cache.elements_for_tiles(keys)
            cands = osm.rank_pois(elements, shapes, lat, lon, radius, self.language)
            fresh = all(self.cache.tile_fresh(k, max_age) for k in keys)
            source = SOURCE_ONLINE if fresh and fetched_ok else SOURCE_CACHE
            remembered = self.decisions.object_choice(lat, lon, [c.osm_id for c in cands]) if cands else None
            on_result(key, cands, source, "", remembered)
