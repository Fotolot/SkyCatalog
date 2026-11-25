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


# Tagi EXIF wykorzystywane w panelu podglądu
_TAG_MAKE, _TAG_MODEL, _TAG_ORIENT, _TAG_SOFT = 0x010F, 0x0110, 0x0112, 0x0131
_TAG_W, _TAG_H, _TAG_PX_W, _TAG_PX_H = 0x0100, 0x0101, 0xA002, 0xA003
_TAG_EXPOSURE, _TAG_FNUMBER, _TAG_ISO, _TAG_FOCAL, _TAG_FOCAL35 = 0x829A, 0x829D, 0x8827, 0x920A, 0xA405


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def parse_tiff_info(data: bytes) -> dict:
    """Wszystkie potrzebne dane z bloku TIFF/EXIF: GPS (z prawidłowym N/S, E/W), data,
    aparat, orientacja, rozdzielczość i parametry ekspozycji."""
    info: dict = {"gps": None, "taken": None}
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        return info
    endian = "<" if data[:2] == b"II" else ">"
    (ifd0_off,) = struct.unpack_from(endian + "I", data, 4)
    ifd0 = _parse_ifd(data, ifd0_off, endian)

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
                info["gps"] = (round(lat, 7), round(lon, 7))
        alt = _first(g.get(6))
        if isinstance(alt, float):
            below = g.get(5)
            below = below[0] if isinstance(below, (bytes, bytearray)) and below else below
            info["gps_alt"] = round(-alt if below == 1 else alt, 1)

    exif: dict = {}
    exif_off = ifd0.get(0x8769)
    if isinstance(exif_off, int):
        exif = _parse_ifd(data, exif_off, endian)
    info["taken"] = (_parse_exif_datetime(exif.get(0x9003)) or _parse_exif_datetime(exif.get(0x9004))
                     or _parse_exif_datetime(ifd0.get(0x0132)))
    for key, tag in (("make", _TAG_MAKE), ("model", _TAG_MODEL), ("software", _TAG_SOFT)):
        if isinstance(ifd0.get(tag), str) and ifd0[tag]:
            info[key] = ifd0[tag]
    orient = _first(ifd0.get(_TAG_ORIENT))
    if isinstance(orient, int):
        info["orientation"] = orient
    w = _first(exif.get(_TAG_PX_W)) or _first(ifd0.get(_TAG_W))
    h = _first(exif.get(_TAG_PX_H)) or _first(ifd0.get(_TAG_H))
    if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
        info["width"], info["height"] = w, h
    exp = _first(exif.get(_TAG_EXPOSURE))
    if isinstance(exp, float) and exp > 0:
        info["exposure"] = f"1/{round(1 / exp)} s" if exp < 1 else f"{exp:g} s"
    fn = _first(exif.get(_TAG_FNUMBER))
    if isinstance(fn, float) and fn > 0:
        info["fnumber"] = f"f/{fn:.1f}"
    iso = _first(exif.get(_TAG_ISO))
    if isinstance(iso, int):
        info["iso"] = iso
    fl = _first(exif.get(_TAG_FOCAL))
    if isinstance(fl, float) and fl > 0:
        info["focal"] = f"{fl:.1f} mm"
    f35 = _first(exif.get(_TAG_FOCAL35))
    if isinstance(f35, int) and f35 > 0:
        info["focal35"] = f"{f35} mm (ekw. 35 mm)"
    return info


def parse_tiff(data: bytes) -> tuple[GPS | None, datetime | None]:
    """Parsuje blok TIFF (z EXIF w JPEG albo cały plik DNG/TIFF)."""
    info = parse_tiff_info(data)
    return info["gps"], info["taken"]


def _jpeg_segments(path: Path) -> tuple[bytes | None, tuple[int, int] | None]:
    """(blok TIFF z APP1 Exif, wymiary z nagłówka SOF)."""
    with open(path, "rb") as f:
        data = f.read(_MAX_JPEG_SCAN)
    if data[:2] != b"\xff\xd8":
        return None, None
    tiff, size = None, None
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker in (0xD9, 0xDA):  # koniec obrazu / początek danych
            break
        (length,) = struct.unpack_from(">H", data, pos + 2)
        segment = data[pos + 4:pos + 2 + length]
        if marker == 0xE1 and tiff is None and segment.startswith(b"Exif\0\0"):
            tiff = segment[6:]
        elif marker in (0xC0, 0xC1, 0xC2) and len(segment) >= 5:
            h, w = struct.unpack_from(">HH", segment, 1)
            size = (w, h)
        pos += 2 + length
    return tiff, size


def read_jpeg_exif(path: Path) -> tuple[GPS | None, datetime | None]:
    tiff, _size = _jpeg_segments(path)
    return parse_tiff(tiff) if tiff else (None, None)


def read_tiff_file(path: Path) -> tuple[GPS | None, datetime | None]:
    with open(path, "rb") as f:
        return parse_tiff(f.read(_MAX_TIFF_READ))


def read_photo_info(path: Path) -> dict:
    """Szczegóły zdjęcia JPG/DNG do katalogu i panelu podglądu (bez dekodowania obrazu)."""
    ext = path.suffix.lower()
    try:
        if ext in JPEG_EXT:
            tiff, size = _jpeg_segments(path)
            info = parse_tiff_info(tiff) if tiff else {"gps": None, "taken": None}
            if size and "width" not in info:
                info["width"], info["height"] = size
            return info
        if ext in TIFF_EXT:
            with open(path, "rb") as f:
                return parse_tiff_info(f.read(_MAX_TIFF_READ))
    except (OSError, struct.error, ValueError) as exc:
        return {"gps": None, "taken": None, "error": str(exc)}
    return {"gps": None, "taken": None}


# ── SRT z drona ────────────────────────────────────────────────
# Zakładka sortowania używa pierwszych 3 sekund nagrania (srt.sorting_location).
# Pełną trasę dla zakładki mapy czyta srt.read_track – to dwa rozdzielne zastosowania.

def parse_srt_text(text: str) -> tuple[GPS | None, datetime | None]:
    from . import srt
    samples = srt.parse_samples(text)
    if not samples:   # fragment bez znaczników czasu – jeden blok w chwili 0
        samples = [srt.parse_block(0.0, text)]
    return srt.sorting_location(samples)


def read_srt(path: Path) -> tuple[GPS | None, datetime | None]:
    from . import srt
    return parse_srt_text(srt.read_text(path, srt.SORTING_READ_BYTES))


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


def parse_moov_info(data: bytes) -> dict:
    """GPS, data utworzenia, czas trwania, rozdzielczość, FPS i dane urządzenia z atomu moov."""
    info: dict = {"gps": None, "taken": None}
    tracks: list[dict] = []

    def walk(start: int, end: int, track: dict | None) -> None:
        for kind, s, e in _iter_atoms(data, start, end):
            if kind == b"mvhd" and e - s >= 20:
                version = data[s]
                if version == 1 and e - s >= 32:
                    secs, _m, scale, dur = struct.unpack_from(">QQIQ", data, s + 4)
                else:
                    secs, _m, scale, dur = struct.unpack_from(">IIII", data, s + 4)
                if secs > 0:
                    try:
                        info["taken"] = (_MP4_EPOCH + timedelta(seconds=secs)).astimezone().replace(tzinfo=None)
                    except (OverflowError, ValueError, OSError):
                        pass
                if scale:
                    info["duration_s"] = round(dur / scale, 2)
            elif kind == b"trak":
                t: dict = {}
                tracks.append(t)
                walk(s, e, t)
            elif kind == b"tkhd" and track is not None and e - s >= 84:
                w, h = struct.unpack_from(">II", data, e - 8)
                track["w"], track["h"] = w >> 16, h >> 16
            elif kind == b"hdlr" and track is not None and e - s >= 12:
                track["handler"] = data[s + 8:s + 12]
            elif kind == b"mdhd" and track is not None and e - s >= 24:
                version = data[s]
                if version == 1:
                    scale, dur = struct.unpack_from(">IQ", data, s + 20)
                else:
                    scale, dur = struct.unpack_from(">II", data, s + 12)
                track["scale"], track["dur"] = scale, dur
            elif kind == b"stts" and track is not None and e - s >= 8:
                (count,) = struct.unpack_from(">I", data, s + 4)
                total = 0
                for i in range(min(count, 100000)):
                    off = s + 8 + i * 8
                    if off + 8 > e:
                        break
                    total += struct.unpack_from(">I", data, off)[0]
                track["samples"] = total
            elif kind == b"\xa9xyz" and info["gps"] is None:
                m = _ISO6709.search(data[s:e].decode("latin-1"))
                if m:
                    lat, lon = float(m.group(1)), float(m.group(2))
                    if valid_gps(lat, lon):
                        info["gps"] = (lat, lon)
            elif kind in (b"\xa9mak", b"\xa9mod", b"\xa9too", b"\xa9swr") and e - s > 4:
                text = data[s + 4:e].decode("utf-8", "ignore").strip("\x00 ").strip()
                key = {b"\xa9mak": "make", b"\xa9mod": "model", b"\xa9too": "encoder", b"\xa9swr": "software"}[kind]
                if text and key not in info:
                    info[key] = text[:80]
            elif kind in _CONTAINERS or kind in (b"minf", b"stbl"):
                # atom 'meta' w MP4 ma 4 bajty wersji/flag przed potomkami
                inner = s + 4 if kind == b"meta" and data[s + 4:s + 8] != b"hdlr" and e - s > 8 else s
                walk(inner, e, track)

    walk(0, len(data), None)
    video = next((t for t in tracks if t.get("handler") == b"vide"), None)
    if video:
        if video.get("w") and video.get("h"):
            info["width"], info["height"] = video["w"], video["h"]
        if video.get("scale") and video.get("dur") and video.get("samples"):
            info["fps"] = round(video["samples"] / (video["dur"] / video["scale"]), 2)
    return info


def parse_moov(data: bytes) -> tuple[GPS | None, datetime | None]:
    info = parse_moov_info(data)
    return info["gps"], info["taken"]


def _read_moov(path: Path) -> bytes | None:
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
                    return None
                f.seek(pos + hdr)
                return f.read(size - hdr)
            pos += size
    return None


def read_mp4(path: Path) -> tuple[GPS | None, datetime | None]:
    moov = _read_moov(path)
    return parse_moov(moov) if moov else (None, None)


def read_video_info(path: Path) -> dict:
    try:
        moov = _read_moov(path)
        return parse_moov_info(moov) if moov else {"gps": None, "taken": None, "error": "brak atomu moov"}
    except (OSError, struct.error, ValueError) as exc:
        return {"gps": None, "taken": None, "error": str(exc)}


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
