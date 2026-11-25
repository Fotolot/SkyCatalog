"""Testy parsera SRT: różne warianty DJI, pełna trasa, luki, skoki i okno 3 s sortowania.

Próbki tekstu odwzorowują układ pól znanych formatów DJI; współrzędne są syntetyczne.
"""

import tempfile
import unittest
from pathlib import Path

from sortowanie import metadata, srt


def ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build(lines_for_t, n: int, step: float = 1 / 30) -> str:
    out = []
    for i in range(n):
        t = i * step
        out.append(f"{i + 1}\n{ts(t)} --> {ts(t + step)}\n{lines_for_t(i, t)}\n")
    return "\n".join(out)


def mini4(lat, lon, i=0):
    return ('<font size="28">SrtCnt : %d, DiffTime : 33ms\n2026-11-11 10:15:%02d.123\n'
            '[iso : 100] [shutter : 1/1000.0] [fnum : 1.7] [ev : 0] [color_md : default] '
            '[focal_len : 24.00] [latitude: %s] [longitude: %s] [rel_alt: 12.500 abs_alt: 260.300] '
            '[ct : 5500] </font>' % (i + 1, min(59, i // 30), lat, lon))


def air2(lat, lon):
    return ('<font size="36">FrameCnt : 1, DiffTime : 33ms\n2021-07-04 18:12:08,123,456\n'
            '[iso : 110] [shutter : 1/200.0] [fnum : 280] [ev : 0] [ct : 5064] [color_md : default] '
            '[focal_len : 240] [latitude : %s] [longtitude : %s] [altitude: 120.5] </font>' % (lat, lon))


def avata2(lat, lon):
    # Gogle DJI (O3/O4): GPS (długość, szerokość, wysokość)
    return "F/2.8, SS 1000.0, ISO 100, EV 0, DZOOM 1.000, GPS (%s, %s, 19), D 12.34m, H 5.60m, H.S 2.30m/s, V.S 0.00m/s" % (lon, lat)


def old_dji(lat, lon):
    return "HOME(22.0100,50.0500) 2019.07.04 18:12:08\nGPS(%s,%s,19) BAROMETER:63.6" % (lon, lat)


LAT0, LON0 = 50.0, 22.0


def walk(i):  # ~1 m na klatkę na wschód
    return LAT0, LON0 + i * 0.000014


class SrtFormatTests(unittest.TestCase):
    def test_mini4_full_track(self):
        text = build(lambda i, t: mini4(*walk(i), i=i), 300)       # 10 s
        track = srt.parse_track(text)
        self.assertEqual(track.status, srt.TRACK_OK)
        self.assertEqual(track.valid_count, 300)
        self.assertEqual(track.start, walk(0))
        self.assertEqual(len(track.segments), 1)
        self.assertEqual(track.segments[0][-1], walk(299))          # cała trasa, nie 3 s
        self.assertAlmostEqual(track.duration_s, 299 / 30, places=2)
        self.assertEqual(track.samples[0].alt, 12.5)

    def test_air2_longtitude(self):
        track = srt.parse_track(build(lambda i, t: air2(*walk(i)), 60))
        self.assertEqual(track.valid_count, 60)
        self.assertEqual(track.start, walk(0))

    def test_avata2_gps_lon_lat_order(self):
        track = srt.parse_track(build(lambda i, t: avata2(*walk(i)), 60))
        self.assertEqual(track.start, walk(0))
        self.assertEqual(track.samples[0].alt, 5.6)                 # H 5.60m
        self.assertIsNone(track.home)

    def test_old_dji_home_is_not_drone_position(self):
        track = srt.parse_track(build(lambda i, t: old_dji(*walk(i)), 30))
        self.assertEqual(track.home, (50.05, 22.01))
        self.assertEqual(track.start, walk(0))
        self.assertNotIn((50.05, 22.01), track.segments[0])

    def test_home_latitude_fields_separate(self):
        text = build(lambda i, t: "[home_latitude: 50.1] [home_longitude: 22.1] [latitude: %s] [longitude: %s]"
                     % walk(i), 10)
        track = srt.parse_track(text)
        self.assertEqual(track.home, (50.1, 22.1))
        self.assertEqual(track.start, walk(0))

    def test_utf16_and_cp1250_decoding(self):
        text = build(lambda i, t: mini4(*walk(i), i=i), 5)
        with tempfile.TemporaryDirectory() as d:
            for enc in ("utf-16", "cp1250"):
                p = Path(d) / f"a_{enc}.SRT"
                p.write_bytes(text.encode(enc))
                self.assertEqual(srt.read_track(p).start, walk(0))


class TrackQualityTests(unittest.TestCase):
    def test_invalid_coordinates_rejected(self):
        def line(i, t):
            if i < 40:
                return mini4(0.0, 0.0, i)               # brak fixu na starcie
            if i == 50:
                return mini4(123.0, 22.0, i)             # poza zakresem
            return mini4(*walk(i), i=i)
        track = srt.parse_track(build(line, 120))
        self.assertEqual(track.start, walk(40))         # pierwsza PRAWIDŁOWA pozycja
        self.assertEqual(track.invalid_count, 41)

    def test_gap_splits_segments(self):
        def line(i, t):
            if 100 <= i < 400:                           # ~10 s bez GPS
                return mini4(0, 0, i)
            return mini4(*walk(i), i=i)
        track = srt.parse_track(build(line, 500))
        self.assertEqual(len(track.segments), 2)
        self.assertEqual(track.breaks, 1)

    def test_jump_splits_and_isolated_spike_dropped(self):
        def line(i, t):
            if i == 60:
                return mini4(50.05, 22.0, i)             # skok o ~5,5 km na 1 klatkę
            return mini4(*walk(i), i=i)
        track = srt.parse_track(build(line, 120))
        self.assertEqual(len(track.segments), 2)
        flat = [p for seg in track.segments for p in seg]
        self.assertNotIn((50.05, 22.0), flat)            # izolowany punkt nie tworzy linii
        self.assertEqual(len(track.samples), 120)        # oryginalne próbki zachowane

    def test_no_gps(self):
        track = srt.parse_track(build(lambda i, t: mini4(0, 0, i), 30))
        self.assertEqual(track.status, srt.TRACK_NO_GPS)
        self.assertIsNone(track.start)
        self.assertEqual(track.segments, [])

    def test_damaged(self):
        self.assertEqual(srt.parse_track("\x00\x01 śmieci bez znaczników czasu").status, srt.TRACK_DAMAGED)
        self.assertEqual(srt.parse_track("").status, srt.TRACK_DAMAGED)

    def test_truncated_file_still_parses(self):
        text = build(lambda i, t: mini4(*walk(i), i=i), 60)
        track = srt.parse_track(text[: len(text) // 2 + 37])
        self.assertEqual(track.status, srt.TRACK_OK)
        self.assertGreater(track.valid_count, 20)

    def test_simplify_keeps_shape(self):
        pts = [(LAT0, LON0 + i * 0.00001) for i in range(1000)] + [(LAT0 + 0.001, LON0 + 0.01)]
        simple = srt.simplify(pts, 2.0)
        self.assertLess(len(simple), 10)
        self.assertEqual(simple[0], pts[0])
        self.assertEqual(simple[-1], pts[-1])


class SortingWindowTests(unittest.TestCase):
    def test_sorting_uses_only_first_3_seconds(self):
        def line(i, t):
            if t < 4.0:
                return mini4(0, 0, i)                     # GPS pojawia się po 4 s
            return mini4(*walk(i), i=i)
        text = build(line, 300)
        gps, when = metadata.parse_srt_text(text)
        self.assertIsNone(gps)                            # sortowanie: brak pozycji w 3 s
        self.assertIsNotNone(when)
        self.assertIsNotNone(srt.parse_track(text).start)  # mapa: pełna trasa ma start

    def test_sorting_takes_first_valid_within_window(self):
        def line(i, t):
            return mini4(0, 0, i) if t < 1.0 else mini4(*walk(i), i=i)
        gps, _ = metadata.parse_srt_text(build(line, 150))
        self.assertEqual(gps, walk(30))


if __name__ == "__main__":
    unittest.main()
