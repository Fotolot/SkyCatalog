import os
import tempfile
import time
import unittest
from pathlib import Path

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

    def test_category_for(self):
        self.assertEqual(core.category_for(Path("a.JPG")), "Obrazy")
        self.assertEqual(core.category_for(Path("a.pdf")), "Dokumenty")
        self.assertEqual(core.category_for(Path("a.unknown")), "Inne")
        self.assertEqual(core.category_for(Path("Makefile")), "Inne")

    def test_sort_by_type(self):
        self.touch("foto.jpg")
        self.touch("raport.pdf")
        self.touch("dziwny.xyz")
        moves = core.plan_moves(self.root, core.MODE_TYPE)
        done, failed = core.execute_moves(moves)
        self.assertEqual(len(done), 3)
        self.assertFalse(failed)
        self.assertTrue((self.root / "Obrazy" / "foto.jpg").exists())
        self.assertTrue((self.root / "Dokumenty" / "raport.pdf").exists())
        self.assertTrue((self.root / "Inne" / "dziwny.xyz").exists())

    def test_plan_does_not_touch_disk(self):
        f = self.touch("foto.jpg")
        core.plan_moves(self.root, core.MODE_TYPE)
        self.assertTrue(f.exists())
        self.assertFalse((self.root / "Obrazy").exists())

    def test_sort_by_extension(self):
        self.touch("a.pdf")
        self.touch("bez_rozszerzenia")
        core.execute_moves(core.plan_moves(self.root, core.MODE_EXTENSION))
        self.assertTrue((self.root / "PDF" / "a.pdf").exists())
        self.assertTrue((self.root / "Bez_rozszerzenia" / "bez_rozszerzenia").exists())

    def test_sort_by_date(self):
        f = self.touch("stary.txt")
        ts = time.mktime((2021, 3, 15, 12, 0, 0, 0, 0, -1))
        os.utime(f, (ts, ts))
        core.execute_moves(core.plan_moves(self.root, core.MODE_DATE))
        self.assertTrue((self.root / "2021" / "03" / "stary.txt").exists())

    def test_name_collision_gets_suffix(self):
        self.touch("Obrazy/foto.jpg", "stary")
        self.touch("foto.jpg", "nowy")
        moves = core.plan_moves(self.root, core.MODE_TYPE)
        self.assertEqual(moves[0].target.name, "foto (1).jpg")
        core.execute_moves(moves)
        self.assertEqual((self.root / "Obrazy" / "foto.jpg").read_text(encoding="utf-8"), "stary")
        self.assertEqual((self.root / "Obrazy" / "foto (1).jpg").read_text(encoding="utf-8"), "nowy")

    def test_recursive_collisions_within_plan(self):
        self.touch("a/foto.jpg")
        self.touch("b/foto.jpg")
        moves = core.plan_moves(self.root, core.MODE_TYPE, recursive=True)
        names = sorted(m.target.name for m in moves)
        self.assertEqual(names, ["foto (1).jpg", "foto.jpg"])

    def test_recursive_skips_already_sorted(self):
        self.touch("Obrazy/foto.jpg")
        self.assertEqual(core.plan_moves(self.root, core.MODE_TYPE, recursive=True), [])

    def test_ignored_files(self):
        self.touch("desktop.ini")
        self.touch("Thumbs.db")
        self.assertEqual(core.plan_moves(self.root, core.MODE_TYPE), [])

    def test_undo_restores_and_cleans_up(self):
        self.touch("foto.jpg")
        done, _ = core.execute_moves(core.plan_moves(self.root, core.MODE_TYPE))
        core.undo_moves(done)
        self.assertTrue((self.root / "foto.jpg").exists())
        self.assertFalse((self.root / "Obrazy").exists())

    def test_history_roundtrip(self):
        hist = self.root / "hist" / "h.json"
        moves = [core.Move(Path("C:/a.txt"), Path("C:/Dokumenty/a.txt"))]
        core.save_history(moves, hist)
        self.assertEqual(core.load_history(hist), moves)
        core.clear_history(hist)
        self.assertEqual(core.load_history(hist), [])

    def test_invalid_folder(self):
        with self.assertRaises(NotADirectoryError):
            core.plan_moves(self.root / "nie_ma", core.MODE_TYPE)


if __name__ == "__main__":
    unittest.main()
