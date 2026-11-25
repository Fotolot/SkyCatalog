import tempfile
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


if __name__ == "__main__":
    unittest.main()
