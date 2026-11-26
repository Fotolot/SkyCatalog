"""„Odśwież bibliotekę”: usunięte zdjęcie, usunięty film z SRT, zmieniony plik, odłączony dysk,
brak uprawnień, anulowanie, nowe pliki – bez ponownego czytania niezmienionych i bez kasowania plików."""
import os
import time
from pathlib import Path
from unittest import mock

from sortowanie import catalog as cat
from sortowanie import refresh, thumbs
from tests.helpers import make_jpeg, make_mp4
from tests.test_catalog import CatalogTestBase, make_srt


class RefreshTests(CatalogTestBase):
    def setUp(self):
        super().setUp()
        self._env = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "local")})
        self._env.start()
        self.a = self.disk / "Hobby" / "Rzeszów" / "Zdjęcia"
        self.v = self.disk / "Hobby" / "Rzeszów" / "WIDEO"
        self.photo1 = make_jpeg(self.a / "DJI_0001.JPG", 50.03, 22.0, "2026:05:01 10:00:00")
        self.photo2 = make_jpeg(self.a / "DJI_0002.JPG", 50.04, 22.1, "2026:05:01 10:01:00")
        self.video = make_mp4(self.v / "DJI_0010.MP4")
        self.srt = make_srt(self.v / "DJI_0010.SRT")
        self.scan()
        self.assertEqual(self.cat.totals().get("razem"), 3)

    def tearDown(self):
        self._env.stop()
        super().tearDown()

    def refresh(self, **kw) -> refresh.RefreshProgress:
        return refresh.LibraryRefresher(self.cat, **kw).run()

    def mid(self, path) -> int | None:
        return self.cat.material_id_for_path(str(path))

    def fake_thumb(self, path: Path) -> Path:
        st = path.stat()
        t = thumbs.thumb_path(str(path), st.st_size, st.st_mtime)
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_bytes(b"jpg")
        return t

    def test_nothing_changed(self):
        with mock.patch("sortowanie.catalog.parse_group", wraps=cat.parse_group) as parse:
            p = self.refresh()
        parse.assert_not_called()                        # niezmienione pliki nie są czytane ponownie
        self.assertEqual((p.added, p.updated, p.removed, p.unavailable), (0, 0, 0, 0))
        self.assertTrue(p.finished)
        self.assertEqual(p.summary, "Dodano: 0, zaktualizowano: 0, usunięto nieaktualne wpisy: 0, niedostępne: 0")

    def test_deleted_photo_removed_with_thumbnail(self):
        thumb = self.fake_thumb(self.photo1)
        other = self.fake_thumb(self.photo2)
        mid = self.mid(self.photo1)
        self.photo1.unlink()
        p = self.refresh()
        self.assertEqual((p.removed, p.removed_ids), (1, [mid]))
        self.assertFalse(self.cat.exists(mid))
        self.assertFalse(thumb.exists())
        self.assertTrue(other.exists())                  # miniatura istniejącego zdjęcia zostaje
        self.assertTrue(self.photo2.exists())            # oryginały nietknięte

    def test_deleted_video_with_srt_removes_entry_and_track(self):
        mid = self.mid(self.video)
        self.assertTrue(self.cat.display_tracks([mid]).get(mid))
        self.video.unlink()
        self.srt.unlink()
        p = self.refresh()
        self.assertEqual(p.removed, 1)
        self.assertFalse(self.cat.exists(mid))
        self.assertEqual(self.cat.display_tracks([mid]), {})
        self.assertEqual(self.cat.conn().execute("SELECT COUNT(*) FROM files WHERE material_id=?",
                                                 (mid,)).fetchone()[0], 0)

    def test_video_left_without_mp4_is_removed_srt_untouched(self):
        mid = self.mid(self.video)
        self.video.unlink()                              # został sam SRT – to nie jest materiał
        p = self.refresh()
        self.assertEqual(p.removed, 1)
        self.assertFalse(self.cat.exists(mid))
        self.assertTrue(self.srt.exists())

    def test_srt_deleted_updates_video(self):
        mid = self.mid(self.video)
        self.srt.unlink()
        p = self.refresh()
        self.assertEqual((p.updated, p.removed), (1, 0))
        m = self.cat.material(mid)
        self.assertIsNone(m["srt_path"])
        self.assertIn(mid, p.changed_ids)

    def test_changed_file_is_reread_only_once(self):
        thumb = self.fake_thumb(self.photo1)
        mid = self.mid(self.photo1)
        time.sleep(0.01)
        make_jpeg(self.photo1, 49.5, 21.5, "2026:06:01 12:00:00")   # nowa treść (np. edycja)
        os.utime(self.photo1, (time.time() + 5, time.time() + 5))
        with mock.patch("sortowanie.catalog.parse_group", wraps=cat.parse_group) as parse:
            p = self.refresh()
        self.assertEqual(parse.call_count, 1)            # tylko zmieniony plik
        self.assertEqual((p.updated, p.removed), (1, 0))
        m = self.cat.material(mid)
        self.assertAlmostEqual(m["lat"], 49.5, places=3)
        self.assertFalse(thumb.exists())                 # stara miniatura nieaktualna

    def test_new_file_added(self):
        make_jpeg(self.a / "DJI_0003.JPG", 50.05, 22.2, "2026:05:01 10:02:00")
        p = self.refresh()
        self.assertEqual(p.added, 1)
        self.assertIsNotNone(self.mid(self.a / "DJI_0003.JPG"))

    def test_disconnected_drive_keeps_entries_as_unavailable(self):
        mids = [self.mid(self.photo1), self.mid(self.photo2), self.mid(self.video)]
        gone = self.root / "odlaczony"
        os.rename(self.disk, gone)                       # „dysk” zniknął razem z folderami
        p = self.refresh(volume_check=lambda path: False)
        self.assertEqual((p.removed, p.unavailable), (0, 3))
        for mid in mids:
            self.assertEqual(self.cat.material(mid)["status"], cat.STATUS_OFFLINE)
        os.rename(gone, self.disk)                       # podłączony ponownie
        p = self.refresh()
        self.assertEqual((p.removed, p.unavailable), (0, 0))
        for mid in mids:
            self.assertEqual(self.cat.material(mid)["status"], cat.STATUS_OK)

    def test_deleted_folder_on_available_drive_is_removed(self):
        for f in self.a.iterdir():
            f.unlink()
        self.a.rmdir()
        p = self.refresh(volume_check=lambda path: True)
        self.assertEqual((p.removed, p.unavailable), (2, 0))

    def test_permission_error_is_not_deletion(self):
        real = os.scandir

        def scandir(path="."):
            if Path(path) == self.a:
                raise PermissionError(13, "Odmowa dostępu")
            return real(path)
        with mock.patch("sortowanie.refresh.os.scandir", side_effect=scandir):
            p = self.refresh()
        self.assertEqual((p.removed, p.unavailable), (0, 2))
        self.assertEqual(self.cat.material(self.mid(self.photo1))["status"], cat.STATUS_OFFLINE)

    def test_cancel_leaves_unchecked_folders_untouched(self):
        self.photo1.unlink()
        self.video.unlink()
        self.srt.unlink()
        r = refresh.LibraryRefresher(self.cat)
        r.cancel()
        p = r.run()
        self.assertTrue(p.cancelled)
        self.assertEqual((p.done, p.removed), (0, 0))
        self.assertEqual(self.cat.totals().get("razem"), 3)
        # anulowanie po pierwszym folderze – drugi nie jest uznany za usunięty
        r = refresh.LibraryRefresher(self.cat)
        real = r.refresh_dir

        def first_then_cancel(directory, prog):
            real(directory, prog)
            r.cancel()
        r.refresh_dir = first_then_cancel
        p = r.run()
        self.assertTrue(p.cancelled)
        self.assertEqual((p.done, p.removed), (1, 1))
        self.assertEqual(self.cat.totals().get("razem"), 2)

    def test_only_indexed_folders_are_checked(self):
        outside = self.root / "inny_folder"
        make_jpeg(outside / "X.JPG", 50.0, 22.0, "2026:01:01 00:00:00")
        p = self.refresh()
        self.assertEqual(p.added, 0)
        self.assertEqual(sorted(Path(d) for d in self.cat.indexed_dirs()), sorted([self.a, self.v]))
