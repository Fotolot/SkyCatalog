"""Generatory syntetycznych plików multimedialnych do testów."""

import struct
from datetime import datetime, timezone
from pathlib import Path


def _rational(value: float) -> tuple[int, int]:
    return int(round(value * 10000)), 10000


def _dms(value: float) -> list[tuple[int, int]]:
    value = abs(value)
    d = int(value)
    m = int((value - d) * 60)
    s = (value - d - m / 60) * 3600
    return [(d, 1), (m, 1), _rational(s)]


def make_tiff(lat: float | None, lon: float | None, taken: str | None) -> bytes:
    """Buduje blok TIFF (little-endian) z IFD0, Exif IFD i GPS IFD."""
    out = bytearray(b"II*\x00" + struct.pack("<I", 8))

    def ifd(entries, data_start):
        """entries: list (tag, type, count, payload_bytes). Zwraca (ifd_bytes, data_bytes)."""
        body = bytearray(struct.pack("<H", len(entries)))
        extra = bytearray()
        data_off = data_start + 2 + 12 * len(entries) + 4
        for tag, typ, count, payload in entries:
            if len(payload) <= 4:
                body += struct.pack("<HHI", tag, typ, count) + payload.ljust(4, b"\0")
            else:
                body += struct.pack("<HHII", tag, typ, count, data_off + len(extra))
                extra += payload
                if len(extra) % 2:
                    extra += b"\0"
        body += struct.pack("<I", 0)
        return bytes(body), bytes(extra)

    # Rozmiary IFD0: 2 wpisy (Exif, GPS)
    ifd0_size = 2 + 12 * 2 + 4
    exif_off = 8 + ifd0_size
    exif_entries = []
    if taken:
        exif_entries.append((0x9003, 2, len(taken) + 1, taken.encode() + b"\0"))
    exif_body, exif_extra = ifd(exif_entries, exif_off)
    gps_off = exif_off + len(exif_body) + len(exif_extra)
    gps_entries = []
    if lat is not None and lon is not None:
        def rat(vals):
            return b"".join(struct.pack("<II", n, d) for n, d in vals)
        gps_entries = [
            (1, 2, 2, (b"N" if lat >= 0 else b"S") + b"\0"),
            (2, 5, 3, rat(_dms(lat))),
            (3, 2, 2, (b"E" if lon >= 0 else b"W") + b"\0"),
            (4, 5, 3, rat(_dms(lon))),
        ]
    gps_body, gps_extra = ifd(gps_entries, gps_off)
    ifd0_body, _ = ifd([
        (0x8769, 4, 1, struct.pack("<I", exif_off)),
        (0x8825, 4, 1, struct.pack("<I", gps_off)),
    ], 8)
    out += ifd0_body + exif_body + exif_extra + gps_body + gps_extra
    return bytes(out)


def make_jpeg(path: Path, lat=None, lon=None, taken=None) -> Path:
    tiff = make_tiff(lat, lon, taken)
    app1 = b"Exif\0\0" + tiff
    data = b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + b"\xff\xd9"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _atom(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def make_mp4(path: Path, created: datetime | None = None, lat=None, lon=None,
             moov_at_end: bool = True) -> Path:
    secs = 0
    if created:
        secs = int((created.replace(tzinfo=timezone.utc) - datetime(1904, 1, 1, tzinfo=timezone.utc)).total_seconds())
    mvhd = _atom(b"mvhd", b"\0\0\0\0" + struct.pack(">II", secs, secs) + b"\0" * 88)
    children = mvhd
    if lat is not None:
        xyz = f"{lat:+08.4f}{lon:+09.4f}/".encode()
        children += _atom(b"udta", _atom(b"\xa9xyz", struct.pack(">HH", len(xyz), 0) + xyz))
    moov = _atom(b"moov", children)
    ftyp = _atom(b"ftyp", b"isom\0\0\0\0isom")
    mdat = _atom(b"mdat", b"\0" * 64)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ftyp + (mdat + moov if moov_at_end else moov + mdat))
    return path


DJI_SRT = """1
00:00:00,000 --> 00:00:00,033
<font size="28">FrameCnt: 1, DiffTime: 33ms
2026-11-11 10:15:42.123
[iso: 100] [shutter: 1/1000.0] [fnum: 1.7] [ev: 0] [color_md: default] [focal_len: 24.00] [latitude: {lat}] [longitude: {lon}] [rel_alt: 50.000 abs_alt: 260.000] [ct: 5500] </font>

"""

OLD_DJI_SRT = """1
00:00:00,000 --> 00:00:01,000
HOME(21.9990,50.0410) 2019.07.04 18:12:08
GPS({lon},{lat},19) BAROMETER:63.6
"""
