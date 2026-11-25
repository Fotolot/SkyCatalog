"""Parser napisów SRT z dronów DJI – próbki GPS na osi czasu.

Dwa ROZDZIELNE zastosowania:

* ``sorting_location()`` – zakładka sortowania: pierwsza prawidłowa pozycja drona
  w pierwszych 3 sekundach nagrania (dotychczasowa zasada; czyta tylko początek pliku),
* ``parse_track()`` – zakładka mapy: CAŁY plik, punkt startowy = pierwsza prawidłowa
  pozycja na osi czasu, linia lotu przez kolejne prawidłowe pozycje, podział na
  segmenty przy lukach i oczywistych skokach GPS.

Obsługiwane warianty (rozpoznawane po treści, bez wpisanych na sztywno modeli):

* Mini 3 / Mini 4 Pro / Air 3 / Mavic 3: ``[latitude: 50.04] [longitude: 21.99] [rel_alt: 1.2 abs_alt: 250.1]``
* Mavic Air 2 / Mini 2: ``[latitude : 50.04] [longtitude : 21.99]`` (literówka DJI)
* starsze (Phantom, Mavic Pro): ``HOME(21.99,50.04) …  GPS(21.99,50.04,19)`` – kolejność lon, lat
* gogle DJI / Avata / Avata 2 (O3/O4): ``GPS (21.99, 50.04, 19), D 12.3m, H 5.6m``

Pozycja drona jest odróżniana od punktu startowego / pilota (``HOME(...)``,
``home_latitude`` itp.) – ten jest zapisywany osobno i nie trafia do trasy.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SORTING_WINDOW_S = 3.0          # zakładka sortowania: pierwsze 3 sekundy
SORTING_READ_BYTES = 512 * 1024  # wystarcza na >3 s nawet przy 60 kl./s
GAP_S = 5.0                     # przerwa w próbkach dzieląca trasę
MAX_SPEED_MS = 60.0             # szybciej niż ~216 km/h = skok GPS, nie lot
MIN_JUMP_M = 30.0               # drobne przesunięcia nie są skokami

_TIMING = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
_NUM = r"(-?\d+(?:\.\d+)?)"
_LAT = re.compile(r"(?<![\w])latitude\s*[:=]\s*" + _NUM, re.I)
_LON = re.compile(r"(?<![\w])long?t?itude\s*[:=]\s*" + _NUM, re.I)
_HOME_LAT = re.compile(r"home_?lat(?:itude)?\s*[:=]\s*" + _NUM, re.I)
_HOME_LON = re.compile(r"home_?lon(?:g|gitude|titude)?\s*[:=]\s*" + _NUM, re.I)
_GPS_FN = re.compile(r"(?<![\w])GPS\s*\(\s*" + _NUM + r"\s*,\s*" + _NUM + r"(?:\s*,\s*" + _NUM + r")?", re.I)
_HOME_FN = re.compile(r"HOME\s*\(\s*" + _NUM + r"\s*,\s*" + _NUM, re.I)
_REL_ALT = re.compile(r"rel_alt\s*[:=]\s*" + _NUM, re.I)
_ABS_ALT = re.compile(r"abs_alt\s*[:=]\s*" + _NUM, re.I)
_H_ALT = re.compile(r"(?<![\w.])H\s+" + _NUM + r"\s*m\b")
_DATE = re.compile(r"(\d{4})[-./](\d{2})[-./](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
_TAG = re.compile(r"<[^>]*>")


def valid_coord(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None or math.isnan(lat) or math.isnan(lon):
        return False
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return False
    return not (abs(lat) < 1e-6 and abs(lon) < 1e-6)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(min(1.0, a)))


@dataclass
class SrtSample:
    t: float                       # sekunda nagrania (początek napisu)
    lat: float | None = None       # pozycja DRONA
    lon: float | None = None
    alt: float | None = None       # wysokość względna, gdy dostępna
    home: tuple[float, float] | None = None   # punkt startowy / pilot, jeśli zapisany
    when: datetime | None = None

    @property
    def valid(self) -> bool:
        return valid_coord(self.lat, self.lon)


def _f(m) -> float | None:
    try:
        return float(m.group(1)) if m else None
    except (ValueError, IndexError):
        return None


def parse_block(t: float, text: str) -> SrtSample:
    text = _TAG.sub(" ", text)
    s = SrtSample(t=t)
    lat_m, lon_m = _LAT.search(text), _LON.search(text)
    if lat_m and lon_m:
        s.lat, s.lon = _f(lat_m), _f(lon_m)
    else:
        g = _GPS_FN.search(text)
        if g:
            # DJI zapisuje GPS(długość, szerokość[, wysokość])
            s.lon, s.lat = float(g.group(1)), float(g.group(2))
            if g.group(3) is not None:
                s.alt = float(g.group(3))
    hl_m, hn_m = _HOME_LAT.search(text), _HOME_LON.search(text)
    if hl_m and hn_m:
        hl, hn = _f(hl_m), _f(hn_m)
        if valid_coord(hl, hn):
            s.home = (hl, hn)
    else:
        h = _HOME_FN.search(text)
        if h:
            hn, hl = float(h.group(1)), float(h.group(2))
            if valid_coord(hl, hn):
                s.home = (hl, hn)
    alt = _f(_REL_ALT.search(text)) or _f(_H_ALT.search(text))
    if alt is None:
        alt = _f(_ABS_ALT.search(text))
    if alt is not None:
        s.alt = alt
    d = _DATE.search(text)
    if d:
        try:
            s.when = datetime(*map(int, d.groups()))
        except ValueError:
            pass
    return s


def parse_samples(text: str) -> list[SrtSample]:
    """Wszystkie bloki napisów jako próbki (także te bez prawidłowego GPS)."""
    matches = list(_TIMING.finditer(text))
    samples = []
    for i, m in enumerate(matches):
        h, mi, se = (int(x) for x in m.groups()[:3])
        t = h * 3600 + mi * 60 + se + int(m.group(4).ljust(3, "0")) / 1000
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        samples.append(parse_block(t, text[m.end():end]))
    return samples


def decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    for enc in ("utf-8-sig", "cp1250"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def read_text(path: Path, limit: int | None = None) -> str:
    with open(path, "rb") as f:
        raw = f.read(limit) if limit else f.read()
    return decode(raw)


# ── Zakładka SORTOWANIA: pierwsze 3 sekundy ────────────────────

def sorting_location(samples: list[SrtSample], window_s: float = SORTING_WINDOW_S
                     ) -> tuple[tuple[float, float] | None, datetime | None]:
    """Pierwsza prawidłowa pozycja drona w pierwszych ``window_s`` sekundach + data."""
    if not samples:
        return None, None
    t0 = samples[0].t
    when = next((s.when for s in samples if s.when), None)
    for s in samples:
        if s.t - t0 > window_s:
            break
        if s.valid:
            return (s.lat, s.lon), when
    return None, when


def read_sorting_location(path: Path) -> tuple[tuple[float, float] | None, datetime | None]:
    """Czyta tylko początek pliku SRT – wystarczy dla okna 3 s."""
    return sorting_location(parse_samples(read_text(path, SORTING_READ_BYTES)))


# ── Zakładka MAPY: pełna trasa ─────────────────────────────────

TRACK_OK = "ok"
TRACK_NO_GPS = "brak prawidłowego GPS"
TRACK_DAMAGED = "uszkodzony lub nierozpoznany SRT"


@dataclass
class Track:
    status: str
    samples: list[SrtSample] = field(default_factory=list)   # oryginalne próbki
    segments: list[list[tuple[float, float]]] = field(default_factory=list)  # pełna geometria
    start: tuple[float, float] | None = None
    home: tuple[float, float] | None = None
    started_at: datetime | None = None
    duration_s: float = 0.0
    valid_count: int = 0
    invalid_count: int = 0
    breaks: int = 0             # liczba podziałów (luki/skoki)

    @property
    def bbox(self) -> tuple[float, float, float, float] | None:
        pts = [p for seg in self.segments for p in seg] or ([self.start] if self.start else [])
        if not pts:
            return None
        lats, lons = [p[0] for p in pts], [p[1] for p in pts]
        return min(lats), min(lons), max(lats), max(lons)

    def compact_samples(self) -> list[list]:
        """Oryginalne próbki do zapisania w katalogu: [t, lat, lon, alt]."""
        return [[round(s.t, 3), s.lat if s.valid else None, s.lon if s.valid else None,
                 None if s.alt is None else round(s.alt, 2)] for s in self.samples]


def split_segments(samples: list[SrtSample], gap_s: float = GAP_S,
                   max_speed: float = MAX_SPEED_MS) -> tuple[list[list[tuple[float, float]]], int]:
    """Dzieli prawidłowe pozycje na segmenty – bez fałszywych połączeń przez luki i skoki."""
    segments: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    prev: SrtSample | None = None
    breaks = 0
    for s in samples:
        if not s.valid:
            continue
        if prev is not None:
            dt = s.t - prev.t
            dist = haversine_m(prev.lat, prev.lon, s.lat, s.lon)
            jump = dist > MIN_JUMP_M and dist / max(dt, 0.04) > max_speed
            if dt > gap_s or jump:
                if len(current) >= 1:
                    segments.append(current)
                current = []
                breaks += 1
        point = (s.lat, s.lon)
        if not current or current[-1] != point:
            current.append(point)
        prev = s
    if current:
        segments.append(current)
    # Pojedyncze punkty (np. izolowany skok GPS) nie tworzą linii
    return [seg for seg in segments if len(seg) >= 2], breaks


def parse_track(text: str) -> Track:
    samples = parse_samples(text)
    if not samples:
        return Track(TRACK_DAMAGED)
    valid = [s for s in samples if s.valid]
    track = Track(TRACK_OK if valid else TRACK_NO_GPS, samples=samples)
    track.valid_count = len(valid)
    track.invalid_count = len(samples) - len(valid)
    track.duration_s = max(0.0, samples[-1].t - samples[0].t)
    track.started_at = next((s.when for s in samples if s.when), None)
    track.home = next((s.home for s in samples if s.home), None)
    if valid:
        track.start = (valid[0].lat, valid[0].lon)
        track.segments, track.breaks = split_segments(samples)
    return track


def read_track(path: Path) -> Track:
    try:
        return parse_track(read_text(path))
    except OSError as exc:
        t = Track(TRACK_DAMAGED)
        t.status = f"{TRACK_DAMAGED}: {exc}"
        return t


# ── Uproszczenie geometrii (tylko do wyświetlania) ─────────────

def simplify(points: list[tuple[float, float]], tolerance_m: float) -> list[tuple[float, float]]:
    """Douglas–Peucker w metrach (iteracyjnie – bez rekurencji przy długich trasach)."""
    n = len(points)
    if n <= 2 or tolerance_m <= 0:
        return list(points)
    lat0 = points[0][0]
    kx = math.cos(math.radians(lat0)) * 111_320
    ky = 110_540
    xy = [((p[1] - points[0][1]) * kx, (p[0] - lat0) * ky) for p in points]
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = xy[a]
        bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            if norm == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if d > best:
                best, idx = d, i
        if best > tolerance_m and idx > 0:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [p for p, k in zip(points, keep) if k]


def simplify_segments(segments: list[list[tuple[float, float]]], tolerance_m: float = 3.0
                      ) -> list[list[tuple[float, float]]]:
    return [simplify(seg, tolerance_m) for seg in segments]
