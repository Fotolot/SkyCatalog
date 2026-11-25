import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from sortowanie import media, metadata
from tests.helpers import DJI_SRT, OLD_DJI_SRT, make_jpeg, make_mp4


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_jpeg_exif_gps_and_date(self):
        p = make_jpeg(self.root / "a.jpg", 50.0375, 22.0049, "2026:11:11 10:15:00")
        gps, taken = metadata.read_file_metadata(p)
        self.assertAlmostEqual(gps[0], 50.0375, places=4)
        self.assertAlmostEqual(gps[1], 22.0049, places=4)
        self.assertEqual(taken, datetime(2026, 11, 11, 10, 15))

    def test_jpeg_southern_western(self):
        p = make_jpeg(self.root / "a.jpg", -33.85, -70.65, None)
        gps, taken = metadata.read_file_metadata(p)
        self.assertAlmostEqual(gps[0], -33.85, places=4)
        self.assertAlmostEqual(gps[1], -70.65, places=4)
        self.assertIsNone(taken)

    def test_jpeg_without_exif(self):
        p = self.root / "a.jpg"
        p.write_bytes(b"\xff\xd8\xff\xd9")
        self.assertEqual(metadata.read_file_metadata(p), (None, None))

    def test_dji_srt(self):
        gps, taken = metadata.parse_srt_text(DJI_SRT.format(lat=50.0412, lon=21.9991))
        self.assertEqual(gps, (50.0412, 21.9991))
        self.assertEqual(taken, datetime(2026, 11, 11, 10, 15, 42))

    def test_srt_skips_zero_gps(self):
        text = DJI_SRT.format(lat=0.0, lon=0.0) + DJI_SRT.format(lat=50.1, lon=22.1)
        gps, _ = metadata.parse_srt_text(text)
        self.assertEqual(gps, (50.1, 22.1))

    def test_srt_longtitude_typo(self):
        gps, _ = metadata.parse_srt_text("[latitude : 50.5] [longtitude : 21.5]")
        self.assertEqual(gps, (50.5, 21.5))

    def test_old_dji_srt(self):
        gps, taken = metadata.parse_srt_text(OLD_DJI_SRT.format(lat=50.0412, lon=21.9991))
        self.assertEqual(gps, (50.0412, 21.9991))
        self.assertEqual(taken, datetime(2019, 7, 4, 18, 12, 8))

    def test_mp4_moov_at_end(self):
        p = make_mp4(self.root / "a.mp4", datetime(2026, 11, 25, 9, 0), 50.0, 22.25)
        gps, created = metadata.read_file_metadata(p)
        self.assertEqual(gps, (50.0, 22.25))
        self.assertIsNotNone(created)

    def test_mp4_without_data(self):
        p = make_mp4(self.root / "a.mp4", None, moov_at_end=False)
        self.assertEqual(metadata.read_file_metadata(p), (None, None))

    def test_date_from_filename(self):
        self.assertEqual(metadata.date_from_filename("DJI_20261111123456_0001_D"),
                         datetime(2026, 11, 11, 12, 34, 56))
        self.assertEqual(metadata.date_from_filename("VID_20250102_030405"),
                         datetime(2025, 1, 2, 3, 4, 5))
        self.assertIsNone(metadata.date_from_filename("DJI_0001"))

    def test_corrupted_file_does_not_crash(self):
        p = self.root / "x.mp4"
        p.write_bytes(b"\x00\x00\x00\x01moov" + b"\xff" * 5)
        self.assertEqual(metadata.read_file_metadata(p), (None, None))


class GroupingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_video_srt_lrf_grouped(self):
        d = self.root / "DCIM" / "100MEDIA"
        make_mp4(d / "DJI_0001.MP4")
        (d / "DJI_0001.SRT").write_text(DJI_SRT.format(lat=50.04, lon=22.0), encoding="utf-8")
        (d / "DJI_0001.LRF").write_bytes(b"")
        make_jpeg(d / "DJI_0002.JPG", 50.0, 22.0, "2026:11:11 09:00:00")
        (d / "DJI_0002.DNG").write_bytes(b"")
        (d / "notatka.docx").write_bytes(b"")  # nie-multimedia – pomijane

        groups = media.scan_source(self.root)
        self.assertEqual(len(groups), 2)
        video = next(g for g in groups if g.kind == media.KIND_VIDEO)
        photo = next(g for g in groups if g.kind == media.KIND_PHOTO)
        self.assertEqual({p.suffix for p in video.files}, {".MP4", ".SRT", ".LRF"})
        self.assertEqual(video.files[0].suffix, ".MP4")
        self.assertEqual(video.gps, (50.04, 22.0))
        self.assertEqual(video.captured_source, "srt")
        self.assertEqual(video.media_folder, "WIDEO")
        self.assertEqual({p.suffix for p in photo.files}, {".JPG", ".DNG"})
        self.assertEqual(photo.media_folder, "Zdjęcia")
        self.assertEqual(photo.captured, datetime(2026, 11, 11, 9, 0))

    def test_same_name_in_different_folders_are_separate(self):
        make_jpeg(self.root / "100MEDIA" / "DJI_0001.JPG")
        make_jpeg(self.root / "101MEDIA" / "DJI_0001.JPG")
        self.assertEqual(len(media.scan_source(self.root)), 2)

    def test_modification_time_is_not_used(self):
        p = make_jpeg(self.root / "IMG.jpg")
        ts = time.mktime((2020, 1, 1, 0, 0, 0, 0, 0, -1))
        os.utime(p, (ts, ts))
        group = media.scan_source(self.root)[0]
        self.assertIsNone(group.captured)

    def test_sidecar_only_is_ignored(self):
        (self.root / "opis.txt").write_text("x", encoding="utf-8")
        self.assertEqual(media.scan_source(self.root), [])


if __name__ == "__main__":
    unittest.main()


class VideoInfoTests(unittest.TestCase):
    def test_duration_resolution_fps(self):
        import struct
        from tests.helpers import _atom

        def full(kind, payload):
            return _atom(kind, b"\0\0\0\0" + payload)
        mvhd = full(b"mvhd", struct.pack(">IIII", 3000000000, 3000000000, 1000, 12500) + b"\0" * 80)
        tkhd = full(b"tkhd", b"\0" * 72 + struct.pack(">II", 3840 << 16, 2160 << 16))
        mdhd = full(b"mdhd", struct.pack(">IIII", 0, 0, 30000, 375000) + b"\0" * 4)
        hdlr = full(b"hdlr", b"\0\0\0\0vide" + b"\0" * 12)
        stts = full(b"stts", struct.pack(">III", 1, 375, 1000))
        trak = _atom(b"trak", tkhd + _atom(b"mdia", mdhd + hdlr + _atom(b"minf", _atom(b"stbl", stts))))
        mak = _atom(b"\xa9mak", struct.pack(">HH", 3, 0) + b"DJI")
        moov = _atom(b"moov", mvhd + trak + _atom(b"udta", mak))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "DJI_0001.MP4"
            p.write_bytes(_atom(b"ftyp", b"isom\0\0\0\0") + moov)
            info = metadata.read_video_info(p)
        self.assertEqual(info["duration_s"], 12.5)
        self.assertEqual((info["width"], info["height"]), (3840, 2160))
        self.assertEqual(info["fps"], 30.0)
        self.assertEqual(info["make"], "DJI")

    def test_photo_info_dimensions(self):
        with tempfile.TemporaryDirectory() as d:
            p = make_jpeg(Path(d) / "a.jpg", -10.5, -20.25, "2026:01:02 03:04:05")
            info = metadata.read_photo_info(p)
        self.assertAlmostEqual(info["gps"][0], -10.5, places=4)
        self.assertAlmostEqual(info["gps"][1], -20.25, places=4)
