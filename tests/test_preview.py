import struct
import unittest

from sortowanie import preview


def make_tiff_with_preview(preview_jpeg: bytes, orientation=1) -> bytes:
    """Minimalny TIFF/DNG: IFD0 z orientacją i podglądem JPEG (tagi 0x201/0x202)."""
    entries = [(0x0112, 3, 1, orientation), (0x0201, 4, 1, 0), (0x0202, 4, 1, len(preview_jpeg))]
    ifd_off = 8
    data_off = ifd_off + 2 + 12 * len(entries) + 4
    entries[1] = (0x0201, 4, 1, data_off)
    body = struct.pack("<H", len(entries))
    for tag, typ, cnt, val in entries:
        body += struct.pack("<HHI", tag, typ, cnt) + (struct.pack("<HH", val, 0) if typ == 3 else struct.pack("<I", val))
    body += struct.pack("<I", 0)
    return b"II*\x00" + struct.pack("<I", ifd_off) + body + preview_jpeg


FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100 + b"\xff\xd9"


class DngTests(unittest.TestCase):
    def test_embedded_preview_and_orientation(self):
        data = make_tiff_with_preview(FAKE_JPEG, orientation=8)
        self.assertEqual(preview.dng_previews(data), [FAKE_JPEG])
        self.assertEqual(preview.dng_orientation(data), 8)

    def test_garbage(self):
        self.assertEqual(preview.dng_previews(b"nie tiff"), [])
        self.assertIsNone(preview.dng_orientation(b""))


if __name__ == "__main__":
    unittest.main()
