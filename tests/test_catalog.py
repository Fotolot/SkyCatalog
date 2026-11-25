import os
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from sortowanie import catalog as cat
from sortowanie.scanner import Scanner, SKIP_DRIVE, SKIP_LINK, SKIP_PERMISSION
from tests.helpers import make_jpeg, make_mp4
from tests.test_srt import build, mini4, walk


class FakeSettings(dict):
    def get(self, k, d=None):
        return super().get(k, d)


def make_srt(path: Path, n=90, start=0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build(lambda i, t: mini4(*walk(i + start), i=i), n), encoding="utf-8")
    return path


class CatalogTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.disk = self.root / "dysk"
        self.disk.mkdir()
        self.cat = cat.Catalog(self.root / "k.sqlite")
        self.settings = FakeSettings(scan_exclusions=[])

    def tearDown(self):
        self.cat.close()
        self._tmp.cleanup()

    def scan(self, roots=None, **kw):
        return Scanner(self.cat, self.settings).scan([str(r) for r in (roots or [self.disk])], **kw)

    def rows(self, **filters):
        return self.cat.query_list(cat.Filters(**filters))

    def by_name(self, name):
        return next(r for r in self.rows(include_missing=True) if r["name"] == name)


class IndexingTests(CatalogTestBase):
    def populate(self):
        d = self.disk / "Lot" / "DCIM"
        make_jpeg(d / "IMG_0001.JPG", 50.0, 22.0, "2026:11:11 10:00:00")
        make_jpeg(d / "IMG_0002.JPG", None, None, "2026:11:12 10:00:00")          # bez GPS
        make_jpeg(d / "DJI_0003.JPG", 50.01, 22.01, "2026:11:11 11:00:00")
        make_jpeg(d / "DJI_0003.DNG", 50.01, 22.01, "2026:11:11 11:00:00")        # para JPG/DNG
        make_mp4(d / "DJI_0004.MP4")
        make_srt(d / "DJI_0004.SRT", 150)
        make_mp4(d / "DJI_0005.MP4")                                                # bez SRT
        make_mp4(d / "DJI_0006.MOV")
        (d / "DJI_0006.SRT").write_bytes(b"\x00\xff uszkodzony")                    # uszkodzony SRT
        (d / "notatki.txt").write_text("x", encoding="utf-8")

    def test_materials_and_pairs(self):
        self.populate()
        prog = self.scan()
        self.assertTrue(prog.finished)
        rows = {r["name"]: r for r in self.rows()}
        self.assertEqual(set(rows), {"IMG_0001.JPG", "IMG_0002.JPG", "DJI_0003.JPG", "DJI_0004.MP4",
                                     "DJI_0005.MP4", "DJI_0006.MOV"})
        pair = self.cat.material(rows["DJI_0003.JPG"]["id"])
        self.assertEqual({f["role"] for f in pair["files"]}, {cat.ROLE_MAIN, cat.ROLE_RAW})
        self.assertEqual(len(pair["files"]), 2)
        self.assertIsNone(rows["IMG_0002.JPG"]["lat"])
        self.assertEqual(rows["IMG_0001.JPG"]["captured_source"], "EXIF")

    def test_video_track_and_statuses(self):
        self.populate()
        self.scan()
        v = self.cat.material(self.by_name("DJI_0004.MP4")["id"])
        self.assertEqual(v["track_status"], "ok")
        self.assertEqual((v["lat"], v["lon"]), walk(0))
        self.assertEqual(v["track_points"], 150)
        self.assertEqual(len(self.cat.track_samples(v["id"])), 150)        # oryginalne próbki
        tracks = self.cat.display_tracks([v["id"]])
        self.assertEqual(tracks[v["id"]][0][-1], walk(149))                 # cała trasa
        self.assertEqual(self.by_name("DJI_0005.MP4")["track_status"], "brak SRT")
        self.assertIsNone(self.by_name("DJI_0005.MP4")["lat"])
        self.assertTrue(self.by_name("DJI_0006.MOV")["track_status"].startswith("uszkodzony"))

    def test_no_location_list(self):
        self.populate()
        self.scan()
        names = {r["name"] for r in self.cat.query_list(cat.Filters(), located=False)}
        self.assertEqual(names, {"IMG_0002.JPG", "DJI_0005.MP4", "DJI_0006.MOV"})

    def test_incremental_scan_parses_only_changes(self):
        self.populate()
        self.scan()
        with mock.patch.object(cat, "parse_group", wraps=cat.parse_group) as spy:
            prog = self.scan()
            self.assertEqual(spy.call_count, 0)
            self.assertEqual(prog.unchanged, 6)
            # zmiana samego SRT
            srt = self.disk / "Lot" / "DCIM" / "DJI_0004.SRT"
            make_srt(srt, 300, start=5)
            os.utime(srt, (time.time() + 5, time.time() + 5))
            prog = self.scan()
            self.assertEqual(spy.call_count, 1)
            self.assertEqual(prog.changed, 1)
        v = self.by_name("DJI_0004.MP4")
        self.assertEqual((v["lat"], v["lon"]), walk(5))

    def test_ids_stable_and_missing_detected(self):
        self.populate()
        self.scan()
        mid = self.by_name("IMG_0001.JPG")["id"]
        self.scan()
        self.assertEqual(self.by_name("IMG_0001.JPG")["id"], mid)
        (self.disk / "Lot" / "DCIM" / "IMG_0001.JPG").unlink()
        self.scan()
        self.assertEqual(self.by_name("IMG_0001.JPG")["status"], cat.STATUS_MISSING)
        self.assertNotIn("IMG_0001.JPG", {r["name"] for r in self.rows()})

    def test_partial_scan_of_folder_does_not_mark_other_folders_missing(self):
        self.populate()
        other = self.disk / "Inne"
        make_jpeg(other / "X.JPG", 50.0, 22.0)
        self.scan()
        self.scan([other])
        self.assertEqual(self.by_name("IMG_0001.JPG")["status"], cat.STATUS_OK)

    def test_cloud_only_not_read(self):
        d = str(self.disk)
        with mock.patch.object(cat, "parse_group", wraps=cat.parse_group):
            with mock.patch("sortowanie.metadata.read_photo_info") as reader:
                self.cat.process_directory(d, [cat.FileEntry(d + "/C.JPG", 10, 1.0, cloud_only=True)], d, 1)
                reader.assert_not_called()
        self.assertEqual(self.by_name("C.JPG")["status"], cat.STATUS_CLOUD)


class ScannerTests(CatalogTestBase):
    def test_cancel_and_resume(self):
        for i in range(5):
            make_jpeg(self.disk / f"f{i}" / "a.JPG", 50.0, 22.0)
        s = Scanner(self.cat, self.settings)
        s.cancel()
        prog = s.scan([str(self.disk)])
        self.assertTrue(prog.cancelled)
        self.assertTrue(s.can_resume())
        s2 = Scanner(self.cat, self.settings)
        prog = s2.scan(resume=True)
        self.assertTrue(prog.finished)
        self.assertEqual(len(self.rows()), 5)
        self.assertFalse(s2.can_resume())

    def test_permission_denied_is_skipped(self):
        make_jpeg(self.disk / "ok" / "a.JPG", 50.0, 22.0)
        make_jpeg(self.disk / "tajne" / "b.JPG", 50.0, 22.0)
        real = os.scandir

        def fake(path):
            if str(path).endswith("tajne"):
                raise PermissionError(13, "Access denied")
            return real(path)
        with mock.patch("sortowanie.scanner.os.scandir", side_effect=fake):
            prog = self.scan()
        self.assertTrue(prog.finished)
        self.assertEqual([r["name"] for r in self.rows()], ["a.JPG"])
        self.assertIn((SKIP_PERMISSION, 1), prog.summary)

    @unittest.skipIf(not hasattr(os, "symlink"), "brak symlinków")
    def test_symlink_loop_not_followed(self):
        make_jpeg(self.disk / "a" / "x.JPG", 50.0, 22.0)
        try:
            os.symlink(self.disk, self.disk / "a" / "petla", target_is_directory=True)
        except OSError:
            self.skipTest("brak uprawnień do symlinków")
        prog = self.scan()
        self.assertTrue(prog.finished)
        self.assertEqual(len(self.rows()), 1)
        self.assertIn(SKIP_LINK, dict(prog.summary))

    def test_exclusions_and_hidden(self):
        make_jpeg(self.disk / "Wyklucz" / "a.JPG", 50.0, 22.0)
        make_jpeg(self.disk / ".ukryty" / "b.JPG", 50.0, 22.0)
        make_jpeg(self.disk / "node_modules" / "c.JPG", 50.0, 22.0)
        make_jpeg(self.disk / "Zdjecia" / "d.JPG", 50.0, 22.0)
        self.settings["scan_exclusions"] = [str(self.disk / "Wyklucz")]
        self.scan()
        self.assertEqual([r["name"] for r in self.rows()], ["d.JPG"])

    def test_disconnected_drive_keeps_entries(self):
        card = self.disk / "Karta"
        make_jpeg(card / "a.JPG", 50.0, 22.0)
        self.scan([card])
        card.rename(self.disk / "Karta_odlaczona")
        prog = self.scan([card])
        r = self.by_name("a.JPG")
        self.assertEqual(r["status"], cat.STATUS_OFFLINE)
        self.assertIn((SKIP_DRIVE, 1), prog.summary)
        (self.disk / "Karta_odlaczona").rename(card)
        self.scan([card])
        self.assertEqual(self.by_name("a.JPG")["status"], cat.STATUS_OK)

    def test_in_flight_files_skipped(self):
        p = make_jpeg(self.disk / "a.JPG", 50.0, 22.0)
        cat.mark_in_flight([p])
        try:
            self.scan()
            self.assertEqual(self.rows(), [])
        finally:
            cat.clear_in_flight([p])


class FilterTests(CatalogTestBase):
    def setUp(self):
        super().setUp()
        d = self.disk
        make_jpeg(d / "a.JPG", 50.0, 22.0, "2026:01:10 10:00:00")
        make_jpeg(d / "b.JPG", 51.0, 23.0, "2026:03:10 10:00:00")
        make_mp4(d / "DJI_20260205120000_0001_D.MP4")
        make_srt(d / "DJI_20260205120000_0001_D.SRT", 60)
        self.scan()

    def test_kind_filters(self):
        self.assertEqual(len(self.rows(photos=True, videos=False)), 2)
        self.assertEqual(len(self.rows(photos=False, videos=True)), 1)
        self.assertEqual(len(self.rows(photos=False, videos=False)), 0)

    def test_date_filter_uses_capture_date(self):
        names = {r["name"] for r in self.rows(date_from=date(2026, 2, 1), date_to=date(2026, 3, 10))}
        self.assertEqual(names, {"b.JPG"})                       # granica date_to włącznie
        video = self.by_name("DJI_20260205120000_0001_D.MP4")
        self.assertEqual(video["captured_source"], "SRT")         # SRT ma pierwszeństwo przed nazwą
        names = {r["name"] for r in self.rows(date_from=date(2026, 11, 11), date_to=date(2026, 11, 11))}
        self.assertEqual(names, {"DJI_20260205120000_0001_D.MP4"})

    def test_category_and_text(self):
        a = self.by_name("a.JPG")
        new = str(self.disk / "Hobby" / "a.JPG")
        Path(new).parent.mkdir()
        os.replace(self.disk / "a.JPG", new)
        self.cat.apply_moves([(str(self.disk / "a.JPG"), new)], {str(self.disk / "a.JPG"): {"category": "hobby", "town": "Miasto A"}})
        self.assertEqual([r["id"] for r in self.rows(category="hobby")], [a["id"]])
        self.assertEqual(len(self.rows(category=cat.UNASSIGNED)), 2)
        self.assertEqual(len(self.rows(text="dji_2026")), 1)
        self.assertEqual(len(self.rows(text="50%")), 0)          # znaki LIKE są escapowane
        self.assertEqual(self.cat.distinct("town"), ["Miasto A"])

    def test_bbox_query_includes_track(self):
        pts = self.cat.query_points(cat.Filters(), bbox=(49.99, 21.99, 50.01, 22.01))
        self.assertEqual(len(pts), 2)        # zdjęcie a + trasa filmu
        self.assertEqual(len(self.cat.query_points(cat.Filters(), bbox=(10, 10, 11, 11))), 0)


class SortIntegrationTests(CatalogTestBase):
    def test_moves_update_paths_and_undo_restores(self):
        card = self.disk / "Karta"
        mp4 = make_mp4(card / "DJI_0001.MP4")
        srt = make_srt(card / "DJI_0001.SRT", 60)
        self.scan([card])
        mid = self.by_name("DJI_0001.MP4")["id"]
        dest = self.disk / "Archiwum" / "Eventy" / "Miasto A" / "2026-11-11 Bieg" / "WIDEO"
        dest.mkdir(parents=True)
        moves = [(str(mp4), str(dest / mp4.name)), (str(srt), str(dest / srt.name))]
        for s, d in moves:
            os.replace(s, d)
        info = {s: {"category": "eventy", "town": "Miasto A", "event_name": "Bieg", "event_date": "2026-11-11"}
                for s, _ in moves}
        op_id, ids = self.cat.apply_moves(moves, info)
        m = self.cat.material(mid)
        self.assertEqual(ids, [mid])
        self.assertEqual(m["main_path"], str(dest / mp4.name))
        self.assertEqual(m["srt_path"], str(dest / srt.name))
        self.assertEqual((m["category"], m["town"], m["event_name"]), ("eventy", "Miasto A", "Bieg"))
        self.assertEqual(m["track_status"], "ok")                              # trasa zachowana
        self.assertEqual(len(self.cat.track_samples(mid)), 60)
        # cofnięcie
        back = [(d, s) for s, d in moves]
        for s, d in back:
            os.replace(s, d)
        self.cat.undo_moves(back, op_id)
        m = self.cat.material(mid)
        self.assertEqual(m["main_path"], str(mp4))
        self.assertIsNone(m["category"])

    def test_partial_failure_updates_only_done_moves(self):
        card = self.disk / "Karta"
        a = make_jpeg(card / "a.JPG", 50.0, 22.0)
        b = make_jpeg(card / "b.JPG", 50.0, 22.0)
        self.scan([card])
        dest = self.disk / "Arch"
        dest.mkdir()
        os.replace(a, dest / "a.JPG")                                         # b się nie udało
        self.cat.apply_moves([(str(a), str(dest / "a.JPG"))], {str(a): {"category": "hobby"}})
        self.assertEqual(self.by_name("a.JPG")["main_path"], str(dest / "a.JPG"))
        self.assertEqual(self.by_name("b.JPG")["main_path"], str(b))
        self.assertIsNone(self.by_name("b.JPG")["category"])

    def test_unknown_files_indexed_at_destination(self):
        dest = self.disk / "Arch" / "Hobby" / "Miasto A" / "Zdjęcia"
        p = make_jpeg(dest / "n.JPG", 50.0, 22.0)
        op_id, ids = self.cat.apply_moves([("/karta/n.JPG", str(p))], {"/karta/n.JPG": {"category": "hobby", "town": "Miasto A"}})
        m = self.cat.material(ids[0])
        self.assertEqual((m["category"], m["town"]), ("hobby", "Miasto A"))
        self.cat.undo_moves([(str(p), "/karta/n.JPG")], op_id)
        self.assertIsNone(self.cat.material(ids[0])["category"])

    def test_external_move_keeps_category(self):
        a = make_jpeg(self.disk / "x" / "a.JPG", 50.0, 22.0)
        self.scan()
        mid = self.by_name("a.JPG")["id"]
        with self.cat.conn() as c:
            c.execute("UPDATE materials SET category='hobby', town='T' WHERE id=?", (mid,))
        (self.disk / "y").mkdir()
        os.replace(a, self.disk / "y" / "a.JPG")                              # przeniesione poza aplikacją
        prog = self.scan()
        self.assertEqual(prog.finalize["rozpoznane_przeniesienia"], 1)
        live = [r for r in self.rows() if r["name"] == "a.JPG"]
        self.assertEqual(live[0]["category"], "hobby")


class PerformanceTests(CatalogTestBase):
    def test_viewport_query_on_large_catalog(self):
        import random
        rnd = random.Random(1)
        c = self.cat.conn()
        rows = []
        for i in range(60000):
            lat, lon = 49 + rnd.random() * 5, 14 + rnd.random() * 10
            rows.append(("zdjęcie" if i % 3 else "wideo", "/d", "/d", f"s{i}", f"s{i}", f"s{i}.JPG",
                         f"/d/s{i}.JPG", "jpg", lat, lon, lat, lon, lat + 0.002, lon + 0.002, "dostępny"))
        with c:
            c.executemany("INSERT INTO materials (kind, dir, dir_key, stem, stem_key, name, main_path, ext, lat, lon,"
                          " min_lat, min_lon, max_lat, max_lon, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        t = time.perf_counter()
        for _ in range(10):
            pts = self.cat.query_points(cat.Filters(), bbox=(50.0, 20.0, 50.5, 21.0))
        elapsed = (time.perf_counter() - t) / 10
        self.assertGreater(len(pts), 100)
        self.assertLess(elapsed, 0.5, f"zapytanie widoku trwało {elapsed:.3f} s")


if __name__ == "__main__":
    unittest.main()


class PickTests(CatalogTestBase):
    def test_pick_overlapping_points_and_track(self):
        d = self.disk
        make_jpeg(d / "a.JPG", 50.0, 22.0)
        make_jpeg(d / "b.JPG", 50.0, 22.00001)              # ~1 m obok
        make_mp4(d / "DJI_0001.MP4")
        make_srt(d / "DJI_0001.SRT", 60, start=-30)         # trasa przechodzi przez punkt (50, 22)
        self.scan()
        hits = self.cat.pick(cat.Filters(), 50.0, 22.0, 10)
        self.assertEqual({h["name"] for h in hits}, {"a.JPG", "b.JPG", "DJI_0001.MP4"})
        video = next(h for h in hits if h["name"] == "DJI_0001.MP4")
        self.assertTrue(video["on_track"])
        only_photos = self.cat.pick(cat.Filters(videos=False), 50.0, 22.0, 10)
        self.assertEqual({h["name"] for h in only_photos}, {"a.JPG", "b.JPG"})
        self.assertEqual(self.cat.pick(cat.Filters(), 51.0, 23.0, 10), [])


class ExclusionDefaultsTests(unittest.TestCase):
    def test_appdata_parent_only_when_named_appdata(self):
        from sortowanie import scanner
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"/x/AppData/Local", "APPDATA": r"/x/AppData/Roaming"}):
            self.assertIn(str(Path("/x/AppData")), scanner.default_excluded_paths())
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": "/tmp/runner/local", "APPDATA": "/tmp/runner/appdata"}):
            paths = scanner.default_excluded_paths()
            self.assertNotIn(str(Path("/tmp/runner")), paths)
            self.assertIn("/tmp/runner/local", paths)


class ReviewFixTests(CatalogTestBase):
    def test_unreadable_folder_keeps_previous_materials(self):
        make_jpeg(self.disk / "ok" / "a.JPG", 50.0, 22.0)
        make_jpeg(self.disk / "tajne" / "b.JPG", 50.0, 22.0)
        self.scan()
        real = os.scandir

        def fake(path):
            if str(path).endswith("tajne"):
                raise PermissionError(13, "Access denied")
            return real(path)
        with mock.patch("sortowanie.scanner.os.scandir", side_effect=fake):
            prog = self.scan()
        self.assertEqual(self.by_name("b.JPG")["status"], cat.STATUS_OK)
        self.assertEqual(prog.finalize["pominięte_niesprawdzone"], 1)

    def test_cloud_state_change_reparsed(self):
        p = make_jpeg(self.disk / "c.JPG", 50.0, 22.0)
        st = p.stat()
        d = str(self.disk)
        self.cat.process_directory(d, [cat.FileEntry(str(p), st.st_size, st.st_mtime, cloud_only=True)], d, 1)
        self.assertEqual(self.by_name("c.JPG")["status"], cat.STATUS_CLOUD)
        stats = self.cat.process_directory(d, [cat.FileEntry(str(p), st.st_size, st.st_mtime)], d, 2)
        self.assertEqual(stats["zmienione"], 1)
        r = self.by_name("c.JPG")
        self.assertEqual(r["status"], cat.STATUS_OK)
        self.assertIsNotNone(r["lat"])
        self.cat.process_directory(d, [cat.FileEntry(str(p), st.st_size, st.st_mtime, cloud_only=True)], d, 3)
        self.assertEqual(self.by_name("c.JPG")["status"], cat.STATUS_CLOUD)

    def test_interrupted_running_scan_resumable(self):
        for i in range(3):
            make_jpeg(self.disk / f"f{i}" / "a.JPG", 50.0, 22.0)
        s = Scanner(self.cat, self.settings)
        s.cancel()
        s.scan([str(self.disk)])
        self.cat.set_state("scan_status", "trwa")        # program zamknięty w trakcie
        self.assertTrue(Scanner(self.cat, self.settings).can_resume())
        prog = Scanner(self.cat, self.settings).scan(resume=True)
        self.assertTrue(prog.finished)
        self.assertEqual(len(self.rows()), 3)


class LinkClassificationTests(unittest.TestCase):
    def test_cloud_placeholders_are_not_links(self):
        from sortowanie import scanner as sc
        cloud_file = sc.FILE_ATTRIBUTE_REPARSE_POINT | sc.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
        self.assertFalse(sc.is_link_entry(False, False, cloud_file, 0x9000101A))     # plik OneDrive
        self.assertFalse(sc.is_link_entry(False, False, sc.FILE_ATTRIBUTE_REPARSE_POINT, 0x9000701A))  # folder OneDrive
        self.assertTrue(sc.is_link_entry(False, False, sc.FILE_ATTRIBUTE_REPARSE_POINT, sc.IO_REPARSE_TAG_MOUNT_POINT))
        self.assertTrue(sc.is_link_entry(False, True, 0, 0))
        self.assertTrue(sc.is_link_entry(True, False, 0, 0))
        self.assertFalse(sc.is_link_entry(False, False, 0, 0))
