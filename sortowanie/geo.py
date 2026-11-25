"""Rozpoznawanie miejscowości i pobliskich obiektów na podstawie GPS.

Do usług wysyłane są WYŁĄCZNIE współrzędne (szerokość, długość) – nigdy zdjęcia,
filmy ani nazwy plików. Wyniki są zapisywane w lokalnym cache, dzięki czemu
ponowne importy z tego samego miejsca nie wymagają internetu.

Domyślni dostawcy (bez płatnej subskrypcji, dane OpenStreetMap):
- Nominatim (reverse geocoding) – limit 1 zapytanie/s, wymagany User-Agent,
- Photon (komoot) – alternatywa dla Nominatim,
- Overpass API – wyszukiwanie obiektów w promieniu (domyślnie 250 m).
Adresy usług można zmienić w ustawieniach (np. na własny serwer).
"""

from __future__ import annotations

import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__
from .config import Settings, app_data_dir, atomic_write_json

# Status wyniku
SOURCE_ONLINE = "online"
SOURCE_CACHE = "cache"
SOURCE_CACHE_NEAR = "cache (pobliski)"
SOURCE_NONE = "brak"


class NetworkUnavailable(Exception):
    """Brak połączenia z usługą (offline, timeout, błąd DNS)."""


@dataclass
class Place:
    town: str | None = None
    object_name: str | None = None
    object_distance_m: float | None = None
    source: str = SOURCE_NONE
    error: str = ""

    @property
    def label(self) -> str:
        if not self.town:
            return "—"
        if self.object_name:
            return f"{self.town} / {self.object_name}"
        return self.town


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ── Cache ──────────────────────────────────────────────────────

def cache_path() -> Path:
    return app_data_dir() / "cache_lokalizacji.json"


class GeoCache:
    """Cache wyników w pliku JSON. Klucz zawiera współrzędne zaokrąglone do ~11 m."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or cache_path()
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._data = loaded
        except (OSError, ValueError):
            pass

    @staticmethod
    def key(kind: str, provider: str, lat: float, lon: float, extra: str = "") -> str:
        return f"{kind}|{provider}|{extra}|{lat:.4f},{lon:.4f}"

    def get(self, key: str, max_age_days: float | None = None) -> dict | None:
        with self._lock:
            entry = self._data.get(key)
        if not entry:
            return None
        if max_age_days is not None and time.time() - entry.get("t", 0) > max_age_days * 86400:
            return None
        return entry.get("v")

    def put(self, key: str, value: dict, lat: float, lon: float) -> None:
        with self._lock:
            self._data[key] = {"t": time.time(), "lat": lat, "lon": lon, "v": value}
            snapshot = dict(self._data)
        try:
            atomic_write_json(self.path, snapshot)
        except OSError:
            pass

    def nearest(self, kind: str, lat: float, lon: float, max_m: float) -> dict | None:
        """Najbliższy zapisany wynik danego rodzaju (używany bez internetu)."""
        best, best_d = None, max_m
        with self._lock:
            items = list(self._data.items())
        for key, entry in items:
            if not key.startswith(kind + "|") or "lat" not in entry:
                continue
            d = haversine_m(lat, lon, entry["lat"], entry["lon"])
            if d <= best_d:
                best, best_d = entry.get("v"), d
        return best

    def clear(self) -> None:
        with self._lock:
            self._data = {}
        try:
            self.path.unlink()
        except OSError:
            pass

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


# ── HTTP z limitem zapytań ─────────────────────────────────────

Opener = Callable[[urllib.request.Request, float], bytes]


def _default_opener(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.read()


class HttpClient:
    # Minimalny odstęp między zapytaniami do jednego hosta (sekundy)
    MIN_INTERVAL = {"nominatim.openstreetmap.org": 1.1, "photon.komoot.io": 0.5}
    DEFAULT_INTERVAL = 1.0

    def __init__(self, settings: Settings, opener: Opener | None = None) -> None:
        self.settings = settings
        self.opener = opener or _default_opener
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def user_agent(self) -> str:
        ua = f"Sortowanie/{__version__} (aplikacja desktopowa do archiwizacji nagran)"
        email = (self.settings.get("contact_email") or "").strip()
        return f"{ua} {email}" if email else ua

    def _throttle(self, host: str) -> None:
        interval = self.MIN_INTERVAL.get(host, self.DEFAULT_INTERVAL)
        with self._lock:
            wait = self._last.get(host, 0) + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()

    def request_json(self, url: str, params: dict | None = None, data: dict | None = None,
                     timeout: float = 20.0):
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(url, data=body, headers={
            "User-Agent": self.user_agent(),
            "Accept": "application/json",
            "Accept-Language": self.settings.get("language") or "pl",
        })
        self._throttle(urllib.parse.urlparse(url).hostname or "")
        try:
            raw = self.opener(req, timeout)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Usługa zwróciła błąd HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NetworkUnavailable(str(exc)) from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise RuntimeError("Nieprawidłowa odpowiedź usługi") from exc


# ── Dostawcy ───────────────────────────────────────────────────

_TOWN_KEYS = ("city", "town", "village", "hamlet", "municipality", "suburb", "county")


def parse_nominatim(data: dict) -> str | None:
    address = (data or {}).get("address") or {}
    for key in _TOWN_KEYS:
        if address.get(key):
            return str(address[key])
    return None


def parse_photon(data: dict) -> str | None:
    features = (data or {}).get("features") or []
    if not features:
        return None
    props = features[0].get("properties") or {}
    if props.get("city"):
        return str(props["city"])
    if props.get("type") in ("city", "town", "village", "hamlet", "locality") and props.get("name"):
        return str(props["name"])
    for key in ("district", "county"):
        if props.get(key):
            return str(props[key])
    return None


# Charakterystyczne obiekty: (klucz, wartości lub None = dowolna) – kolejność = priorytet
POI_RULES: list[tuple[str, tuple[str, ...] | None]] = [
    ("historic", None),
    ("tourism", ("attraction", "museum", "viewpoint", "artwork", "gallery", "zoo",
                 "theme_park", "aquarium")),
    ("building", ("church", "cathedral", "chapel", "castle", "basilica")),
    ("amenity", ("place_of_worship", "townhall", "university", "theatre", "arts_centre")),
    ("man_made", ("tower", "bridge", "lighthouse", "water_tower", "observation_tower", "dam")),
    ("leisure", ("park", "stadium", "garden", "marina", "water_park", "nature_reserve")),
    ("bridge", ("yes", "viaduct", "aqueduct")),
    ("natural", ("peak", "water", "beach", "cliff")),
]


def overpass_query(lat: float, lon: float, radius: int) -> str:
    parts = []
    for key, values in POI_RULES:
        flt = f'["{key}"]' if values is None else f'["{key}"~"^({"|".join(values)})$"]'
        parts.append(f'nwr(around:{radius},{lat:.6f},{lon:.6f})["name"]{flt};')
    return f"[out:json][timeout:25];({''.join(parts)});out center tags 200;"


def _poi_priority(tags: dict) -> int:
    for i, (key, values) in enumerate(POI_RULES):
        if key in tags and (values is None or tags[key] in values):
            return i
    return len(POI_RULES)


def pick_poi(data: dict, lat: float, lon: float, radius: float, language: str = "pl"):
    """Wybiera najbardziej charakterystyczny, a przy równym priorytecie najbliższy obiekt."""
    best = None
    for el in (data or {}).get("elements", []):
        tags = el.get("tags") or {}
        name = tags.get(f"name:{language}") or tags.get("name")
        if not name:
            continue
        plat = el.get("lat", (el.get("center") or {}).get("lat"))
        plon = el.get("lon", (el.get("center") or {}).get("lon"))
        if plat is None or plon is None:
            continue
        dist = haversine_m(lat, lon, plat, plon)
        # Dla dużych obiektów (park, zamek) środek może leżeć dalej niż promień,
        # ale Overpass zwrócił je, bo ich obrys jest w promieniu – akceptujemy.
        if el.get("type") == "node" and dist > radius:
            continue
        score = (_poi_priority(tags), dist)
        if best is None or score < best[0]:
            best = (score, str(name).strip(), dist)
    if best is None:
        return None
    return {"name": best[1], "distance": round(best[2], 1)}


# ── Usługa lokalizacji ─────────────────────────────────────────

class LocationService:
    def __init__(self, settings: Settings, cache: GeoCache | None = None,
                 http: HttpClient | None = None) -> None:
        self.settings = settings
        self.cache = cache if cache is not None else GeoCache()
        self.http = http if http is not None else HttpClient(settings)
        self._network_down = False
        self._lock = threading.Lock()

    @property
    def network_down(self) -> bool:
        return self._network_down

    def reset_network_state(self) -> None:
        self._network_down = False

    def _online_allowed(self) -> bool:
        return not self.settings.get("offline") and not self._network_down

    def _mark_down(self) -> None:
        self._network_down = True

    # Rozpoznanie miejscowości
    def town(self, lat: float, lon: float) -> tuple[str | None, str, str]:
        provider = self.settings.get("reverse_provider")
        if provider not in ("nominatim", "photon"):
            return None, SOURCE_NONE, ""
        key = GeoCache.key("rev", provider, lat, lon)
        cached = self.cache.get(key, self.settings.get("cache_days"))
        if cached is not None:
            return cached.get("town"), SOURCE_CACHE, ""
        error = ""
        if self._online_allowed():
            try:
                if provider == "nominatim":
                    data = self.http.request_json(
                        self.settings.get("nominatim_url").rstrip("/") + "/reverse",
                        {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "format": "jsonv2",
                         "zoom": 14, "addressdetails": 1,
                         "accept-language": self.settings.get("language") or "pl"})
                    town = parse_nominatim(data)
                else:
                    data = self.http.request_json(
                        self.settings.get("photon_url").rstrip("/") + "/reverse",
                        {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "limit": 1})
                    town = parse_photon(data)
                self.cache.put(key, {"town": town}, lat, lon)
                return town, SOURCE_ONLINE, ""
            except NetworkUnavailable as exc:
                self._mark_down()
                error = f"Brak połączenia: {exc}"
            except RuntimeError as exc:
                error = str(exc)
        # Bez internetu – dowolnie stary wpis lub najbliższy w promieniu 1 km
        stale = self.cache.get(key)
        if stale is not None:
            return stale.get("town"), SOURCE_CACHE, error
        near = self.cache.nearest("rev", lat, lon, 1000)
        if near is not None and near.get("town"):
            return near.get("town"), SOURCE_CACHE_NEAR, error
        return None, SOURCE_NONE, error or "Brak zapisanych wyników offline"

    # Obiekt w promieniu
    def nearby_object(self, lat: float, lon: float) -> tuple[dict | None, str, str]:
        if self.settings.get("poi_provider") != "overpass":
            return None, SOURCE_NONE, ""
        radius = int(self.settings.get("poi_radius_m") or 250)
        key = GeoCache.key("poi", "overpass", lat, lon, str(radius))
        cached = self.cache.get(key, self.settings.get("cache_days"))
        if cached is not None:
            return cached.get("poi"), SOURCE_CACHE, ""
        error = ""
        if self._online_allowed():
            try:
                data = self.http.request_json(self.settings.get("overpass_url"),
                                              data={"data": overpass_query(lat, lon, radius)},
                                              timeout=40)
                poi = pick_poi(data, lat, lon, radius, self.settings.get("language") or "pl")
                self.cache.put(key, {"poi": poi}, lat, lon)
                return poi, SOURCE_ONLINE, ""
            except NetworkUnavailable as exc:
                self._mark_down()
                error = f"Brak połączenia: {exc}"
            except RuntimeError as exc:
                error = str(exc)
        stale = self.cache.get(key)
        if stale is not None:
            return stale.get("poi"), SOURCE_CACHE, error
        return None, SOURCE_NONE, error

    def resolve(self, lat: float, lon: float, with_object: bool = True) -> Place:
        town, src, err = self.town(lat, lon)
        place = Place(town=town, source=src, error=err)
        if with_object and town:
            poi, _psrc, perr = self.nearby_object(lat, lon)
            if poi:
                place.object_name = poi["name"]
                place.object_distance_m = poi.get("distance")
            if perr and not place.error:
                place.error = perr
        return place
