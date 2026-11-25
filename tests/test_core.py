import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sortowanie import core


class CoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def touch(self, rel: str, content: str = "x") -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def test_execute_creates_folders(self):
        src = self.touch("karta/a.mp4")
        dst = self.root / "arch" / "Hobby" / "X" / "WIDEO" / "a.mp4"
        done, failed = core.execute_moves([core.Move(src, dst)])
        self.assertEqual(len(done), 1)
        self.assertFalse(failed)
        self.assertTrue(dst.exists())
        self.assertFalse(src.exists())

    def test_never_overwrites(self):
        src = self.touch("a.mp4", "nowy")
        dst = self.touch("arch/a.mp4", "stary")
        done, failed = core.execute_moves([core.Move(src, dst)])
        self.assertFalse(done)
        self.assertEqual(len(failed), 1)
        self.assertEqual(dst.read_text(encoding="utf-8"), "stary")
        self.assertTrue(src.exists())

    def test_undo_restores_and_stops_at_root(self):
        src = self.touch("karta/a.mp4")
        arch = self.root / "arch"
        arch.mkdir()
        dst = arch / "Eventy" / "Rzeszów" / "2026-11-11 Bieg" / "WIDEO" / "a.mp4"
        done, _ = core.execute_moves([core.Move(src, dst)])
        core.undo_moves(done, stop_at=arch)
        self.assertTrue(src.exists())
        self.assertFalse((arch / "Eventy").exists())
        self.assertTrue(arch.exists())

    def test_history_roundtrip(self):
        hist = self.root / "hist" / "h.json"
        moves = [core.Move(Path("E:/DCIM/a.MP4"), Path("D:/Archiwum/Hobby/a.MP4"))]
        core.save_history(moves, Path("D:/Archiwum"), hist)
        loaded, root = core.load_history(hist)
        self.assertEqual(loaded, moves)
        self.assertEqual(root, Path("D:/Archiwum"))
        core.clear_history(hist)
        self.assertEqual(core.load_history(hist), ([], None))


class OverwriteTests(unittest.TestCase):
    """„Nadpisz” – tylko na wyraźne życzenie, z kopią poprzedniej wersji i cofaniem."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.arch = self.root / "arch"
        self.target = self.arch / "Hobby" / "Rzeszów" / "WIDEO" / "DJI_0001.MP4"
        self.target.parent.mkdir(parents=True)
        self.target.write_text("stara wersja", encoding="utf-8")
        self.src = self.root / "karta" / "DJI_0001.MP4"
        self.src.parent.mkdir()
        self.src.write_text("nowa wersja z karty", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def plan(self, overwrite=True):
        return core.with_backups([core.Move(self.src, self.target, overwrite=overwrite)], self.arch, "2026-11-25_120000")

    def test_backup_location_inside_archive(self):
        m = self.plan()[0]
        self.assertEqual(m.backup, self.arch / core.BACKUP_FOLDER / "2026-11-25_120000" / "Hobby" / "Rzeszów"
                         / "WIDEO" / "DJI_0001.MP4")
        self.assertIsNone(self.plan(overwrite=False)[0].backup)

    def test_without_consent_nothing_is_overwritten(self):
        for run in (core.execute_copies, core.execute_moves):
            done, failed = run([core.Move(self.src, self.target)])
            self.assertEqual((done, len(failed)), ([], 1))
            self.assertEqual(self.target.read_text(encoding="utf-8"), "stara wersja")
            self.assertTrue(self.src.exists())

    def test_copy_overwrite_keeps_previous_version_and_undo_restores_it(self):
        moves = self.plan()
        done, failed = core.execute_copies(moves)
        self.assertEqual((len(done), failed), (1, []))
        self.assertEqual(self.target.read_text(encoding="utf-8"), "nowa wersja z karty")
        self.assertEqual(moves[0].backup.read_text(encoding="utf-8"), "stara wersja")
        self.assertTrue(self.src.exists())                       # karta bez zmian
        undone, failed = core.undo_copies(done, stop_at=self.arch)
        self.assertEqual((len(undone), failed), (1, []))
        self.assertEqual(self.target.read_text(encoding="utf-8"), "stara wersja")
        self.assertFalse((self.arch / core.BACKUP_FOLDER).exists())   # puste foldery kopii usunięte

    def test_move_overwrite_and_undo(self):
        moves = self.plan()
        done, failed = core.execute_moves(moves)
        self.assertEqual((len(done), failed), (1, []))
        self.assertFalse(self.src.exists())
        self.assertEqual(self.target.read_text(encoding="utf-8"), "nowa wersja z karty")
        self.assertEqual(moves[0].backup.read_text(encoding="utf-8"), "stara wersja")
        undone, failed = core.undo_moves(done, stop_at=self.arch)
        self.assertEqual(failed, [])
        self.assertEqual(self.src.read_text(encoding="utf-8"), "nowa wersja z karty")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "stara wersja")

    def test_failed_copy_leaves_old_file_in_place(self):
        with mock.patch("sortowanie.core.shutil.copystat", side_effect=OSError("dysk odłączony")):
            done, failed = core.execute_copies(self.plan())
        self.assertEqual((done, len(failed)), ([], 1))
        self.assertEqual(self.target.read_text(encoding="utf-8"), "stara wersja")
        self.assertEqual([p.name for p in self.target.parent.iterdir()], ["DJI_0001.MP4"])

    def test_failed_replace_puts_old_file_back(self):
        real_replace = core.os.replace

        def flaky(a, b):
            if str(a).endswith(".part"):
                raise OSError("plik zablokowany")
            return real_replace(a, b)
        with mock.patch("sortowanie.core.os.replace", side_effect=flaky):
            done, failed = core.execute_moves(self.plan())
        self.assertEqual((done, len(failed)), ([], 1))
        self.assertEqual(self.target.read_text(encoding="utf-8"), "stara wersja")
        self.assertEqual(self.src.read_text(encoding="utf-8"), "nowa wersja z karty")   # plik wrócił na kartę

    def test_history_keeps_backup_for_undo(self):
        moves = self.plan()
        done, _ = core.execute_copies(moves)
        hist = self.root / "historia.json"
        core.save_history(done, self.arch, path=hist, mode=core.MODE_COPY)
        loaded, root = core.load_history(hist)
        self.assertEqual(loaded, done)
        self.assertTrue(loaded[0].overwrite)


if __name__ == "__main__":
    unittest.main()
