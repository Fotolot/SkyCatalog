"""Podgląd materiałów i akcje systemowe – tylko odczyt plików.

* DNG: wyodrębnienie wbudowanego podglądu JPEG (bez dekodowania RAW) – dekodowanie
  obrazu i miniatury w ui/preview_panel.py (QImageReader z orientacją EXIF),
* Eksplorator / aplikacja domyślna: wywołania API (SHOpenFolderAndSelectItems,
  os.startfile) z listą argumentów – nigdy przez powłokę ani z budowanego tekstu poleceń.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys


# ── DNG: wbudowany podgląd JPEG ────────────────────────────────

def _ifd_entries(data: bytes, off: int, e: str) -> dict[int, tuple[int, int, int]]:
    out = {}
    if off <= 0 or off + 2 > len(data):
        return out
    (n,) = struct.unpack_from(e + "H", data, off)
    for i in range(min(n, 400)):
        p = off + 2 + 12 * i
        if p + 12 > len(data):
            break
        tag, typ, cnt = struct.unpack_from(e + "HHI", data, p)
        if typ == 3 and cnt == 1:
            val = struct.unpack_from(e + "H", data, p + 8)[0]
        else:
            val = struct.unpack_from(e + "I", data, p + 8)[0]
        out[tag] = (typ, cnt, val)
    return out


def dng_previews(data: bytes) -> list[bytes]:
    """Osadzone strumienie JPEG z pliku DNG/TIFF – od największego."""
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        return []
    e = "<" if data[:2] == b"II" else ">"
    seen, todo, found = set(), [struct.unpack_from(e + "I", data, 4)[0]], []
    while todo and len(seen) < 64:
        off = todo.pop()
        if off in seen or off <= 0 or off >= len(data):
            continue
        seen.add(off)
        ent = _ifd_entries(data, off, e)
        # następny IFD w łańcuchu
        n = struct.unpack_from(e + "H", data, off)[0]
        nxt = off + 2 + 12 * n
        if nxt + 4 <= len(data):
            todo.append(struct.unpack_from(e + "I", data, nxt)[0])
        if 0x014A in ent:                                     # SubIFDs
            typ, cnt, val = ent[0x014A]
            if cnt == 1:
                todo.append(val)
            else:
                for i in range(min(cnt, 16)):
                    if val + 4 * i + 4 <= len(data):
                        todo.append(struct.unpack_from(e + "I", data, val + 4 * i)[0])
        start = length = None
        if 0x0201 in ent and 0x0202 in ent:
            start, length = ent[0x0201][2], ent[0x0202][2]
        elif ent.get(0x0103, (0, 0, 0))[2] in (6, 7) and 0x0111 in ent and 0x0117 in ent \
                and ent[0x0111][1] == 1:
            start, length = ent[0x0111][2], ent[0x0117][2]
        if start and length and start + length <= len(data) and data[start:start + 2] == b"\xff\xd8":
            found.append(data[start:start + length])
    return sorted(found, key=len, reverse=True)


def dng_orientation(data: bytes) -> int | None:
    if len(data) < 8 or data[:2] not in (b"II", b"MM"):
        return None
    e = "<" if data[:2] == b"II" else ">"
    ent = _ifd_entries(data, struct.unpack_from(e + "I", data, 4)[0], e)
    return ent.get(0x0112, (0, 0, None))[2]


# ── Akcje systemowe (bez powłoki) ──────────────────────────────

def reveal_in_explorer(path: str) -> None:
    """Otwiera Eksplorator z zaznaczonym plikiem."""
    p = os.path.abspath(path)
    if not os.path.exists(p):
        raise FileNotFoundError(p)
    if sys.platform == "win32":
        import ctypes
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)
        shell32.ILCreateFromPathW.restype = ctypes.c_void_p
        shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
        shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong]
        shell32.ILFree.argtypes = [ctypes.c_void_p]
        pidl = shell32.ILCreateFromPathW(p)
        if pidl:
            try:
                if shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0:
                    return
            finally:
                shell32.ILFree(pidl)
        # zapasowo: explorer z argumentami jako lista (bez powłoki)
        subprocess.Popen(["explorer", "/select,", p])
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, os.path.dirname(p)])


def open_default(path: str) -> None:
    p = os.path.abspath(path)
    if not os.path.exists(p):
        raise FileNotFoundError(p)
    if sys.platform == "win32":
        os.startfile(p)  # noqa: S606 – skojarzenie plików Windows, bez powłoki
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", p])
