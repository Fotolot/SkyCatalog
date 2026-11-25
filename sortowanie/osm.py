"""Analiza danych OpenStreetMap: geometria i rozpoznawanie miejscowości.

Moduł nie wykonuje zapytań sieciowych – przetwarza odpowiedzi Overpass API
(format JSON z ``out geom``). Nazwy miejsc nie są nigdzie wpisane na sztywno:
wynik zależy wyłącznie od tagów OSM.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

ATTRIBUTION = "© OpenStreetMap contributors (ODbL)"
ATTRIBUTION_URL = "https://www.openstreetmap.org/copyright"

# ── Geometria (rzutowanie lokalne – wystarczające dla odległości do kilku km) ──

EARTH_R = 6371000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(min(1.0, a)))


def _project(lat0: float, lon0: float):
    kx = math.cos(math.radians(lat0)) * math.pi / 180 * EARTH_R
    ky = math.pi / 180 * EARTH_R
    return lambda lat, lon: ((lon - lon0) * kx, (lat - lat0) * ky)


def _seg_dist(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def point_in_ring(lat: float, lon: float, ring: list[tuple[float, float]]) -> bool:
    """Test promienia (ray casting) – ring to lista (lat, lon), zamknięta lub nie."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def assemble_rings(ways: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Łączy odcinki (np. członków relacji multipolygon) w zamknięte pierścienie."""
    pending = [list(w) for w in ways if len(w) >= 2]
    rings: list[list[tuple[float, float]]] = []
    while pending:
        ring = pending.pop(0)
        changed = True
        while ring[0] != ring[-1] and changed:
            changed = False
            for i, w in enumerate(pending):
                if w[0] == ring[-1]:
                    ring += w[1:]
                elif w[-1] == ring[-1]:
                    ring += w[-2::-1]
                elif w[-1] == ring[0]:
                    ring = w[:-1] + ring
                elif w[0] == ring[0]:
                    ring = w[:0:-1] + ring
                else:
                    continue
                pending.pop(i)
                changed = True
                break
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


@dataclass
class Shape:
    """Geometria elementu OSM gotowa do liczenia odległości."""

    points: list[tuple[float, float]] = field(default_factory=list)   # węzeł
    lines: list[list[tuple[float, float]]] = field(default_factory=list)
    outer: list[list[tuple[float, float]]] = field(default_factory=list)
    inner: list[list[tuple[float, float]]] = field(default_factory=list)
    bbox: tuple[float, float, float, float] | None = None              # s, w, n, e

    def contains(self, lat: float, lon: float) -> bool:
        if self.bbox and not (self.bbox[0] <= lat <= self.bbox[2] and self.bbox[1] <= lon <= self.bbox[3]):
            return False
        if not any(point_in_ring(lat, lon, r) for r in self.outer):
            return False
        return not any(point_in_ring(lat, lon, r) for r in self.inner)

    def distance_m(self, lat: float, lon: float) -> float:
        """Odległość do granicy obiektu; 0, gdy punkt leży wewnątrz obszaru/budynku."""
        if self.outer and self.contains(lat, lon):
            return 0.0
        best = math.inf
        for p in self.points:
            best = min(best, haversine_m(lat, lon, p[0], p[1]))
        proj = _project(lat, lon)
        for line in self.lines + self.outer + self.inner:
            pts = [proj(a, b) for a, b in line]
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                best = min(best, _seg_dist(0.0, 0.0, ax, ay, bx, by))
            if len(pts) == 1:
                best = min(best, math.hypot(*pts[0]))
        return best

    def to_json(self) -> dict:
        return {"p": self.points, "l": self.lines, "o": self.outer, "i": self.inner, "b": self.bbox}

    @classmethod
    def from_json(cls, d: dict) -> "Shape":
        tup = lambda seq: [tuple(x) for x in seq]  # noqa: E731
        return cls(tup(d.get("p", [])), [tup(x) for x in d.get("l", [])],
                   [tup(x) for x in d.get("o", [])], [tup(x) for x in d.get("i", [])],
                   tuple(d["b"]) if d.get("b") else None)


_AREA_TAGS = ("building", "leisure", "amenity", "landuse", "historic", "tourism", "place",
              "boundary", "natural", "man_made", "area")


def _coords(geom) -> list[tuple[float, float]]:
    return [(g["lat"], g["lon"]) for g in geom or [] if g and "lat" in g]


def shape_from_element(el: dict) -> Shape:
    """Tworzy geometrię z elementu Overpass (``out geom``)."""
    kind = el.get("type")
    shape = Shape()
    if kind == "node":
        shape.points = [(el["lat"], el["lon"])]
    elif kind == "way":
        pts = _coords(el.get("geometry"))
        tags = el.get("tags") or {}
        closed = len(pts) >= 4 and pts[0] == pts[-1]
        if closed and (tags.get("area") != "no") and any(k in tags for k in _AREA_TAGS):
            shape.outer = [pts]
        elif pts:
            shape.lines = [pts]
    elif kind == "relation":
        outer_ways, inner_ways = [], []
        for m in el.get("members") or []:
            pts = _coords(m.get("geometry"))
            if m.get("type") == "node" and "lat" in m:
                continue  # węzły etykiet (label/admin_centre) nie są granicą
            if not pts:
                continue
            (inner_ways if m.get("role") == "inner" else outer_ways).append(pts)
        shape.outer = assemble_rings(outer_ways)
        shape.inner = assemble_rings(inner_ways)
        # Niezamknięte fragmenty (np. relacja przycięta) – liczymy jako linie
        if not shape.outer:
            shape.lines = outer_ways
    if "bounds" in el:
        b = el["bounds"]
        shape.bbox = (b["minlat"], b["minlon"], b["maxlat"], b["maxlon"])
    else:
        allpts = shape.points + [p for r in shape.lines + shape.outer for p in r]
        if allpts:
            lats, lons = [p[0] for p in allpts], [p[1] for p in allpts]
            shape.bbox = (min(lats), min(lons), max(lats), max(lons))
    return shape


def element_id(el: dict) -> str:
    return f"{el.get('type', '?')[0]}{el.get('id')}"


def element_name(tags: dict, language: str = "pl") -> str | None:
    name = tags.get(f"name:{language}") or tags.get("name")
    return str(name).strip() if name else None


# ── Miejscowości ───────────────────────────────────────────────

SETTLEMENT_PLACES = ("city", "town", "village", "hamlet")
SUBDIVISION_PLACES = ("suburb", "quarter", "neighbourhood", "borough", "city_block", "plot",
                      "allotments", "isolated_dwelling", "farm", "locality")
_SETTLEMENT_RANK = {"city": 0, "town": 1, "village": 2, "hamlet": 3}
# Obszary administracyjne większe niż miejscowość (rozpoznawane po początku nazwy
# lub typie granicy) – nigdy nie są traktowane jako miejscowość
_LARGER_UNIT_RE = re.compile(r"^(gmina|powiat|województwo|obwód|okręg|związek)\b", re.I)
_SETTLEMENT_BORDER_TYPES = {"city", "town", "village", "hamlet", "miasto", "wieś"}

KIND_SETTLEMENT = "miejscowość"
KIND_ADMIN = "obszar administracyjny"
KIND_SUBDIVISION = "dzielnica / osiedle"
KIND_LARGER = "gmina / powiat"


@dataclass
class AreaInfo:
    osm_id: str
    name: str
    kind: str
    tags: dict
    shape: Shape | None = None
    reason: str = ""

    @property
    def admin_level(self) -> int | None:
        try:
            return int(str(self.tags.get("admin_level", "")).split(";")[0])
        except ValueError:
            return None


def classify_area(tags: dict, language: str = "pl") -> tuple[str, str] | None:
    """(rodzaj, uzasadnienie) obszaru OSM obejmującego punkt."""
    name = element_name(tags, language)
    if not name:
        return None
    place = tags.get("place")
    if place in SETTLEMENT_PLACES:
        return KIND_SETTLEMENT, f"place={place}"
    if place in SUBDIVISION_PLACES:
        return KIND_SUBDIVISION, f"place={place}"
    if tags.get("boundary") == "administrative":
        level = tags.get("admin_level", "?")
        if _LARGER_UNIT_RE.match(name) or _LARGER_UNIT_RE.match(tags.get("official_name", "")):
            return KIND_LARGER, f"admin_level={level}, nazwa jednostki"
        border = (tags.get("border_type") or "").lower()
        if border in _SETTLEMENT_BORDER_TYPES:
            return KIND_SETTLEMENT, f"admin_level={level}, border_type={border}"
        try:
            lvl = int(str(level).split(";")[0])
        except ValueError:
            lvl = None
        if lvl is not None and lvl >= 9:
            return KIND_SUBDIVISION, f"admin_level={level}"
        if lvl is not None and lvl <= 5:
            return KIND_LARGER, f"admin_level={level}"
        return KIND_ADMIN, f"admin_level={level}"
    return None


@dataclass
class PlaceNode:
    osm_id: str
    name: str
    place: str
    distance_m: float


@dataclass
class TownResult:
    """Wynik rozpoznania miejscowości dla punktu."""

    town: str | None                       # pewna miejscowość (lub None)
    candidates: list[str]                  # gdy niejednoznaczne – opcje dla użytkownika
    reason: str                            # krótkie uzasadnienie
    areas: list[AreaInfo] = field(default_factory=list)
    nodes: list[PlaceNode] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Ta sama miejscowość bywa w OSM kilkoma obszarami (np. miasto, gmina miejska i powiat
        # grodzki o jednej nazwie) – na liście wyboru występuje tylko raz. Jeśli wszystkie
        # propozycje to ta sama nazwa, nie ma czego wybierać: to jest miejscowość.
        unique: list[str] = []
        for c in self.candidates:
            c = (c or "").strip()
            if c and c.casefold() not in {u.casefold() for u in unique}:
                unique.append(c)
        if self.town is None and len(unique) == 1 and len(self.candidates) > 1:
            self.town = unique[0]
            self.reason = f"{self.reason}; wszystkie propozycje to ta sama miejscowość".lstrip("; ")
            unique = []
        self.candidates = unique

    @property
    def ambiguous(self) -> bool:
        return self.town is None and len(self.candidates) > 0

    def to_json(self) -> dict:
        return {"town": self.town, "candidates": self.candidates, "reason": self.reason,
                "areas": [{"id": a.osm_id, "name": a.name, "kind": a.kind, "why": a.reason} for a in self.areas],
                "nodes": [{"id": n.osm_id, "name": n.name, "place": n.place, "d": round(n.distance_m)}
                          for n in self.nodes]}

    @classmethod
    def from_json(cls, d: dict) -> "TownResult":
        return cls(d.get("town"), d.get("candidates", []), d.get("reason", ""),
                   [AreaInfo(a["id"], a["name"], a["kind"], {}, None, a.get("why", "")) for a in d.get("areas", [])],
                   [PlaceNode(n["id"], n["name"], n["place"], n.get("d", 0)) for n in d.get("nodes", [])])


def decide_town(areas: list[AreaInfo], nodes: list[PlaceNode]) -> TownResult:
    """Wybór miejscowości na podstawie obszarów obejmujących punkt i pobliskich węzłów place.

    Zasady:
    1. Obszar z ``place=city|town|village|hamlet`` obejmujący punkt – miejscowość pewna.
    2. Granica administracyjna (bez ``place``) jest miejscowością tylko wtedy, gdy potwierdza ją
       węzeł ``place`` o tej samej nazwie albo ``border_type``. Gminy, powiaty i województwa
       są pomijane, podobnie dzielnice i osiedla (nie tworzymy ich folderów).
    3. Kilka różnych miejscowości lub brak obszaru – wynik niejednoznaczny, decyduje użytkownik.
    """
    settlement_nodes = [n for n in nodes if n.place in SETTLEMENT_PLACES]
    node_names = {n.name.casefold(): n for n in settlement_nodes}

    confident: dict[str, tuple[int, str]] = {}   # nazwa -> (ranga, uzasadnienie)
    admin_unconfirmed: list[AreaInfo] = []
    for a in areas:
        if a.kind == KIND_SETTLEMENT:
            rank = _SETTLEMENT_RANK.get(a.tags.get("place", ""), 2)
            confident.setdefault(a.name, (rank, f"obszar OSM {a.reason}"))
        elif a.kind == KIND_ADMIN:
            node = node_names.get(a.name.casefold())
            if node is not None:
                rank = _SETTLEMENT_RANK.get(node.place, 2)
                confident.setdefault(a.name, (rank, f"granica {a.reason} + węzeł place={node.place}"))
            else:
                admin_unconfirmed.append(a)

    if len(confident) == 1:
        name, (_rank, why) = next(iter(confident.items()))
        return TownResult(name, [], why, areas, nodes)
    if len(confident) > 1:
        # Np. przysiółek (hamlet) w granicach wsi – nie zgadujemy, pytamy użytkownika
        ordered = sorted(confident, key=lambda n: confident[n][0])
        return TownResult(None, ordered, "punkt leży w kilku miejscowościach OSM", areas, nodes)

    # Brak obszaru miejscowości – kandydaci z granic administracyjnych i najbliższych węzłów
    options: list[str] = [a.name for a in admin_unconfirmed]
    nearest = sorted(settlement_nodes, key=lambda n: n.distance_m)
    for n in nearest[:4]:
        if n.name not in options:
            options.append(n.name)
    if not options:
        return TownResult(None, [], "nie znaleziono miejscowości w pobliżu", areas, nodes)
    if len(options) == 1 and nearest and nearest[0].distance_m <= 1500:
        return TownResult(options[0], [], f"najbliższy węzeł place={nearest[0].place} "
                                          f"({nearest[0].distance_m:.0f} m)", areas, nodes)
    return TownResult(None, options, "brak jednoznacznego obszaru miejscowości", areas, nodes)


def parse_area_response(data: dict, lat: float, lon: float, language: str = "pl"
                        ) -> tuple[list[AreaInfo], list[PlaceNode]]:
    """Dzieli odpowiedź zapytania o obszary na obszary obejmujące punkt i węzły place."""
    areas: list[AreaInfo] = []
    nodes: list[PlaceNode] = []
    seen: set[str] = set()
    for el in (data or {}).get("elements", []):
        tags = el.get("tags") or {}
        eid = element_id(el)
        if eid in seen:
            continue
        seen.add(eid)
        if el.get("type") == "node":
            name = element_name(tags, language)
            if name and tags.get("place"):
                nodes.append(PlaceNode(eid, name, tags["place"], haversine_m(lat, lon, el["lat"], el["lon"])))
            continue
        cls = classify_area(tags, language)
        if cls is None:
            continue
        shape = shape_from_element(el) if (el.get("geometry") or el.get("members")) else None
        areas.append(AreaInfo(eid, element_name(tags, language) or "", cls[0], tags, shape, cls[1]))
    nodes.sort(key=lambda n: n.distance_m)
    return areas, nodes


# ── Zapytania Overpass QL ──────────────────────────────────────

def area_query(lat: float, lon: float, node_radius: int = 3000, timeout: int = 25,
               maxsize: int = 64 * 1024 * 1024) -> str:
    """Obszary OSM obejmujące punkt (is_in) z geometrią + pobliskie węzły place.

    Geometria jest pobierana tylko dla obszarów mogących być miejscowością lub
    dzielnicą – duże jednostki (gmina, powiat, województwo) tylko z tagami.
    """
    p = f"{lat:.6f},{lon:.6f}"
    big = "^(gmina|powiat|województwo)"
    node_places = "|".join(SETTLEMENT_PLACES + ("suburb", "quarter", "neighbourhood"))
    return (
        f"[out:json][timeout:{timeout}][maxsize:{maxsize}];"
        f"is_in({p})->.a;"
        f'(rel(pivot.a)["boundary"="administrative"]["admin_level"~"^([6-9]|1[01])$"];'
        f'rel(pivot.a)["place"];way(pivot.a)["place"];)->.all;'
        f'rel.all["name"~"{big}",i]->.big;'
        f"(.all; - .big;)->.small;"
        f".small out geom;"
        f".big out tags;"
        f'node(around:{node_radius},{p})["place"~"^({node_places})$"]["name"];'
        f"out qt;"
    )
