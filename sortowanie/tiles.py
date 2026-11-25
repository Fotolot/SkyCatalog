"""Konfiguracja podkładu mapy (raster XYZ) zgodnie z zasadami OSMF.

Kafelki pobiera i buforuje silnik przeglądarki QtWebEngine (Leaflet) – z cache HTTP
zgodnym z nagłówkami i zapytaniami warunkowymi; aplikacja ustawia własny User-Agent.

https://operations.osmfoundation.org/policies/tiles/ – najważniejsze punkty:

* adres https://tile.openstreetmap.org/{z}/{x}/{y}.png (konfigurowalny – bez aktualizacji programu),
* widoczne oznaczenie „© OpenStreetMap contributors”,
* własny, stały User-Agent aplikacji (nie domyślny biblioteki),
* cache zgodny z nagłówkami HTTP (Cache-Control / Expires) lub co najmniej 7 dni,
  zapytania warunkowe (If-None-Match / If-Modified-Since) dla wygasłych kafelków,
* ZAKAZ pobierania masowego, wstępnego ładowania i trybu offline – pobierane są
  wyłącznie kafelki aktualnie oglądanego widoku; nie ma funkcji „pobierz obszar”.

Overpass API NIE służy do rysowania mapy – tylko do wyszukiwania obiektów (geo.py).
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

from . import __version__

TILE_SIZE = 256
MIN_ZOOM = 2
FIX_THE_MAP_URL = "https://www.openstreetmap.org/fixthemap"
OSM_COPYRIGHT_URL = "https://www.openstreetmap.org/copyright"
USER_AGENT = f"Sortowanie/{__version__} (aplikacja desktopowa Windows; +https://github.com/skipperpvp-sudo/Sortowanie)"


# ── Rzutowanie Web Mercator ────────────────────────────────────

def world_size(z: int) -> int:
    return TILE_SIZE * (1 << z)


def latlon_to_px(lat: float, lon: float, z: int) -> tuple[float, float]:
    lat = max(-85.05112878, min(85.05112878, lat))
    n = world_size(z)
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def px_to_latlon(x: float, y: float, z: int) -> tuple[float, float]:
    n = world_size(z)
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def fit_zoom(bbox: tuple[float, float, float, float], width: int, height: int,
             max_zoom: int, padding: int = 40) -> int:
    s, w, n, e = bbox
    for z in range(max_zoom, MIN_ZOOM - 1, -1):
        x0, y1 = latlon_to_px(s, w, z)
        x1, y0 = latlon_to_px(n, e, z)
        if abs(x1 - x0) <= max(1, width - 2 * padding) and abs(y1 - y0) <= max(1, height - 2 * padding):
            return z
    return MIN_ZOOM


def visible_tiles(cx: float, cy: float, z: int, width: int, height: int) -> list[tuple[int, int, int]]:
    """Kafelki dokładnie pokrywające widok (bez zapasu – zgodnie z zakazem prefetch)."""
    n = 1 << z
    x0 = math.floor((cx - width / 2) / TILE_SIZE)
    x1 = math.floor((cx + width / 2) / TILE_SIZE)
    y0 = max(0, math.floor((cy - height / 2) / TILE_SIZE))
    y1 = min(n - 1, math.floor((cy + height / 2) / TILE_SIZE))
    tiles = []
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            tiles.append((z, tx % n, ty, tx))     # ostatni element: kolumna na ekranie
    # od środka widoku – najważniejsze najpierw
    tiles.sort(key=lambda t: abs((t[3] + 0.5) * TILE_SIZE - cx) + abs((t[2] + 0.5) * TILE_SIZE - cy))
    return tiles


# ── Dostawca ───────────────────────────────────────────────────

@dataclass(frozen=True)
class Provider:
    url: str
    attribution: str
    max_zoom: int = 19

    @property
    def key(self) -> str:
        return hashlib.sha1(self.url.encode("utf-8")).hexdigest()[:12]

    def tile_url(self, z: int, x: int, y: int) -> str:
        return self.url.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))


_TEMPLATE = re.compile(r"^https://[^\s/]+/.*\{z\}.*\{x\}.*\{y\}.*$")


def validate_url(url: str) -> str | None:
    """Komunikat błędu albo None. Wymagane HTTPS i znaczniki {z} {x} {y}."""
    if not _TEMPLATE.match(url or ""):
        return "Adres musi zaczynać się od https:// i zawierać {z}, {x} oraz {y}."
    return None


def provider_from_settings(settings) -> Provider:
    url = (settings.get("tile_url") or "").strip()
    if validate_url(url):
        url = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    attribution = (settings.get("tile_attribution") or "").strip() or "© OpenStreetMap contributors"
    if "openstreetmap" not in attribution.lower():
        attribution = f"{attribution} · © OpenStreetMap contributors"
    try:
        max_zoom = max(MIN_ZOOM, min(22, int(settings.get("tile_max_zoom") or 19)))
    except (TypeError, ValueError):
        max_zoom = 19
    return Provider(url, attribution, max_zoom)
