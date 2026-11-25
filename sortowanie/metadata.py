"""Odczyt GPS i daty wykonania materiałów – tylko biblioteka standardowa.

Obsługiwane źródła:
- EXIF w JPEG oraz plikach TIFF/DNG (zdjęcia),
- napisy SRT z drona (DJI: ``[latitude: …] [longitude: …]`` lub ``GPS(lon,lat,…)``),
- atomy MP4/MOV (``mvhd`` – czas utworzenia, ``©xyz`` – współrzędne),
- nazwa pliku (np. ``DJI_20261111123456_0001_D.MP4``).

Data modyfikacji pliku NIE jest używana – nie jest wiarygodną datą nagrania.
"""

from __future__ import annotations

import re
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

GPS = tuple[float, float]

_MAX_TIFF_READ = 4 * 1024 * 1024
_MAX_JPEG_SCAN = 512 * 1024
_MAX_SRT_READ = 256 * 1024
_MAX_MOOV_READ = 32 * 1024 * 1024


def valid_gps(lat: float, lon: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lon <= 180 and not (abs(lat) < 1e-6 and abs(lon) < 1e-6)


# ── EXIF / TIFF ────────────────────────────────────────────────

_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


def _parse_ifd(data: bytes, offset: int, endian: str) -> dict[int, object]:
    tags: dict[int, object] = {}
    if offset <= 0 or offset + 2 > len(data):
        return tags
    (count,) = struct.unpack_from(endian + "H", data, offset)
    for i in range(min(count, 512)):
        entry = offset + 2 + i * 12
        if entry + 12 > len(data):
            break
        tag, typ, n = struct.unpack_from(endian + "HHI", data, entry)
        size = _TYPE_SIZES.get(typ)
        if size is None:
            continue
        total = size * n
        if total <= 4:
            value_off = entry + 8
        else:
            (value_off,) = struct.unpack_from(endian + "I", data, entry + 8)
        if value_off + total > len(data) or n > 4096:
            continue
        if typ == 2:
            tags[tag] = data[value_off:value_off + n].split(b"\0", 1)[0].decode("ascii", "ignore").strip()
        elif typ in (5, 10):
            fmt = endian + ("I" if typ == 5 else "i") * 2 * n
            raw = struct.unpack_from(fmt, data, value_off)
            tags[tag] = [raw[j] / raw[j + 1] if raw[j + 1] else 0.0 for j in range(0, len(raw), 2)]
        elif typ in (3, 4, 9):
            code = {3: "H", 4: "I", 9: "i"}[typ]
            vals = struct.unpack_from(endian + code * n, data, value_off)
            tags[tag] = vals[0] if n == 1 else list(vals)
        else:
            tags[tag] = data[value_off:value_off + total]
    return tags


def _dms(values) -> float | None:
    if not isinstance(values, list) or not values:
        return None
    parts = list(values) + [0.0, 0.0]
    return parts[0] + parts[1] / 60 + parts[2] / 3600


def _parse_exif_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def parse_tiff(data: bytes) -> tuple[GPS | None, datetime | None]:
    """Parsuje blok TIFF (z EXIF w JPEG albo cały plik DNG/TIFF)."""
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        return None, None
    endian = "<" if data[:2] == b"II" else ">"
    (ifd0_off,) = struct.unpack_from(endian + "I", data, 4)
    ifd0 = _parse_ifd(data, ifd0_off, endian)

    gps: GPS | None = None
    gps_off = ifd0.get(0x8825)
    if isinstance(gps_off, int):
        g = _parse_ifd(data, gps_off, endian)
        lat, lon = _dms(g.get(2)), _dms(g.get(4))
        if lat is not None and lon is not None:
            if str(g.get(1, "N")).upper().startswith("S"):
                lat = -lat
            if str(g.get(3, "E")).upper().startswith("W"):
                lon = -lon
            if valid_gps(lat, lon):
                gps = (round(lat, 7), round(lon, 7))

    taken: datetime | None = None
    exif_off = ifd0.get(0x8769)
    if isinstance(exif_off, int):
        exif = _parse_ifd(data, exif_off, endian)
        taken = _parse_exif_datetime(exif.get(0x9003)) or _parse_exif_datetime(exif.get(0x9004))
    if taken is None:
        taken = _parse_exif_datetime(ifd0.get(0x0132))
    return gps, taken


def read_jpeg_exif(path: Path) -> tuple[GPS | None, datetime | None]:
    with open(path, "rb") as f:
        data = f.read(_MAX_JPEG_SCAN)
    if data[:2] != b"\xff\xd8":
        return None, None
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker in (0xD9, 0xDA):  # koniec obrazu / początek danych
            break
        (length,) = struct.unpack_from(">H", data, pos + 2)
        segment = data[pos + 4:pos + 2 + length]
        if marker == 0xE1 and segment.startswith(b"Exif\0\0"):
            return parse_tiff(segment[6:])
        pos += 2 + length
    return None, None


def read_tiff_file(path: Path) -> tuple[GPS | None, datetime | None]:
    with open(path, "rb") as f:
        return parse_tiff(f.read(_MAX_TIFF_READ))


# ── SRT z drona ────────────────────────────────────────────────

_SRT_LAT = re.compile(r"\blatitude\s*[:=]\s*(-?\d+(?:\.\d+)?)", re.I)
_SRT_LON = re.compile(r"\blon\w*\s*[:=]\s*(-?\d+(?:\.\d+)?)", re.I)
_SRT_GPS = re.compile(r"GPS\s*\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)")  # GPS(lon, lat, alt)
_SRT_DATE = re.compile(r"(\d{4})[-./](\d{2})[-./](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


def parse_srt_text(text: str) -> tuple[GPS | None, datetime | None]:
    gps: GPS | None = None
    for lat_s, lon_s in zip(_SRT_LAT.findall(text), _SRT_LON.findall(text)):
        lat, lon = float(lat_s), float(lon_s)
        if valid_gps(lat, lon):
            gps = (lat, lon)
            break
    if gps is None:
        for lon_s, lat_s in _SRT_GPS.findall(text):
            lat, lon = float(lat_s), float(lon_s)
            if valid_gps(lat, lon):
                gps = (lat, lon)
                break

    taken: datetime | None = None
    m = _SRT_DATE.search(text)
    if m:
        try:
            taken = datetime(*map(int, m.groups()))
        except ValueError:
            taken = None
    return gps, taken


def read_srt(path: Path) -> tuple[GPS | None, datetime | None]:
    with open(path, "rb") as f:
        raw = f.read(_MAX_SRT_READ)
    for enc in ("utf-8-sig", "utf-16", "cp1250"):
        try:
            return parse_srt_text(raw.decode(enc))
        except UnicodeDecodeError:
            continue
    return parse_srt_text(raw.decode("latin-1"))


# ── MP4 / MOV ──────────────────────────────────────────────────

_MP4_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)
_ISO6709 = re.compile(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)")
_CONTAINERS = {b"moov", b"udta", b"trak", b"mdia", b"meta"}


def _iter_atoms(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1 and pos + 16 <= end:
            (size,) = struct.unpack_from(">Q", data, pos + 8)
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            return
        yield kind, pos + header, min(pos + size, end)
        pos += size


def parse_moov(data: bytes) -> tuple[GPS | None, datetime | None]:
    gps: GPS | None = None
    created: datetime | None = None

    def walk(start: int, end: int) -> None:
        nonlocal gps, created
        for kind, s, e in _iter_atoms(data, start, end):
            if kind == b"mvhd" and e - s >= 12:
                version = data[s]
                secs = struct.unpack_from(">Q" if version == 1 else ">I", data, s + 4)[0]
                if secs > 0:
                    try:
                        created = (_MP4_EPOCH + timedelta(seconds=secs)).astimezone().replace(tzinfo=None)
                    except (OverflowError, ValueError, OSError):
                        created = None
            elif kind == b"\xa9xyz" and gps is None:
                m = _ISO6709.search(data[s:e].decode("latin-1"))
                if m:
                    lat, lon = float(m.group(1)), float(m.group(2))
                    if valid_gps(lat, lon):
                        gps = (lat, lon)
            elif kind in _CONTAINERS:
                # atom 'meta' w MP4 ma 4 bajty wersji/flag przed potomkami
                inner = s + 4 if kind == b"meta" and data[s + 4:s + 8] != b"hdlr" and e - s > 8 else s
                walk(inner, e)

    walk(0, len(data))
    return gps, created


def read_mp4(path: Path) -> tuple[GPS | None, datetime | None]:
    with open(path, "rb") as f:
        f.seek(0, 2)
        file_size = f.tell()
        pos = 0
        while pos + 8 <= file_size:
            f.seek(pos)
            header = f.read(16)
            if len(header) < 8:
                break
            size, kind = struct.unpack_from(">I4s", header)
            hdr = 8
            if size == 1 and len(header) >= 16:
                (size,) = struct.unpack_from(">Q", header, 8)
                hdr = 16
            elif size == 0:
                size = file_size - pos
            if size < hdr:
                break
            if kind == b"moov":
                if size > _MAX_MOOV_READ:
                    break
                f.seek(pos + hdr)
                return parse_moov(f.read(size - hdr))
            pos += size
    return None, None


# ── Nazwa pliku ────────────────────────────────────────────────

_NAME_PATTERNS = (
    re.compile(r"(20\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})"),                 # DJI_20261111123456
    re.compile(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})[-_ T](\d{2})[-_.]?(\d{2})[-_.]?(\d{2})"),  # 20261111_123456
)


def date_from_filename(name: str) -> datetime | None:
    for pattern in _NAME_PATTERNS:
        m = pattern.search(name)
        if m:
            try:
                return datetime(*map(int, m.groups()))
            except ValueError:
                continue
    return None


# ── Interfejs zbiorczy ─────────────────────────────────────────

JPEG_EXT = {".jpg", ".jpeg"}
TIFF_EXT = {".dng", ".tif", ".tiff", ".arw", ".nef", ".cr2"}
MP4_EXT = {".mp4", ".mov", ".m4v", ".lrf"}


def read_file_metadata(path: Path) -> tuple[GPS | None, datetime | None]:
    """Zwraca (GPS, data wykonania) z jednego pliku; błędy odczytu => (None, None)."""
    ext = path.suffix.lower()
    try:
        if ext in JPEG_EXT:
            return read_jpeg_exif(path)
        if ext in TIFF_EXT:
            return read_tiff_file(path)
        if ext == ".srt":
            return read_srt(path)
        if ext in MP4_EXT:
            return read_mp4(path)
    except (OSError, struct.error, ValueError):
        pass
    return None, None
