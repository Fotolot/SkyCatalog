"""Import z karty SD: wykrywanie karty, kopiowanie z weryfikacją, rejestr importów, cofanie."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sortowanie import catalog as cat
from sortowanie import core
from sortowanie.cards import CardWatcher, find_cards
from sortowanie.scanner import Drive
from tests.helpers import make_jpeg, make_mp4


class CopyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_copy_keeps_source_and_mtime(self):
        src = make_mp4(self.root / "karta" / "DJI_0001.MP4")
        os.utime(src, (1_700_000_000, 1_700_000_000))
        dst = self.root / "arch" / "Hobby" / "WIDEO" / "DJI_0001.MP4"
        done, failed = core.execute_copies([core.Move(src, dst)])
        self.assertEqual((len(done), failed), (1, []))
        self.assertTrue(src.exists())
        self.assertEqual(dst.read_bytes(), src.read_bytes())
        self.assertEqual(int(dst.stat().st_mtime), 1_700_000_000)
        self.assertEqual(list(dst.parent.glob("*.part")), [])

    def test_never_overwrites(self):
        src = make_mp4(self.root / "a.MP4")
        dst = self.root / "b.MP4"
        dst.write_bytes(b"stary")
        done, failed = core.execute_copies([core.Move(src, dst)])
        self.assertEqual(done, [])
        self.assertEqual(dst.read_bytes(), b"stary")

    def test_interrupted_copy_leaves_no_partial_file(self):
        src = make_mp4(self.root / "a.MP4")
        dst = self.root / "arch" / "a.MP4"
        with mock.patch("sortowanie.core.shutil.copystat", side_effect=OSError("dysk odłączony")):
            done, failed = core.execute_copies([core.Move(src, dst)])
        self.assertEqual(len(failed), 1)
        self.assertFalse(dst.exists())
        self.assertEqual(list(dst.parent.glob("*")), [])

    def test_undo_copies_deletes_only_copies(self):
        src = make_mp4(self.root / "karta" / "a.MP4")
        arch = self.root / "arch"
        arch.mkdir()
        dst = arch / "Eventy" / "X" / "a.MP4"
        done, _ = core.execute_copies([core.Move(src, dst)])
        undone, failed = core.undo_copies(done, stop_at=arch)
        self.assertEqual((len(undone), failed), (1, []))
        self.assertTrue(src.exists())
        self.assertFalse(dst.exists())
        self.assertFalse((arch / "Eventy").exists())

    def test_undo_skips_modified_copy(self):
        src = make_mp4(self.root / "a.MP4")
        dst = self.root / "arch" / "a.MP4"
        done, _ = core.execute_copies([core.Move(src, dst)])
        dst.write_bytes(b"zmieniony po imporcie")
        undone, failed = core.undo_copies(done)
        self.assertEqual(undone, [])
        self.assertTrue(dst.exists())

    def test_history_mode(self):
        h = self.root / "h.json"
        core.save_history([core.Move(Path("a"), Path("b"))], None, h, op_id=5, mode=core.MODE_COPY)
        self.assertEqual(core.load_history_mode(h), core.MODE_COPY)
        core.save_history([core.Move(Path("a"), Path("b"))], None, h)
        self.assertEqual(core.load_history_mode(h), core.MODE_MOVE)


class ImportRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cat = cat.Catalog(self.root / "k.sqlite")

    def tearDown(self):
        self.cat.close()
        self._tmp.cleanup()

    def test_copy_registered_and_indexed_with_category(self):
        card = self.root / "karta" / "DCIM"
        src = make_jpeg(card / "DJI_0001.JPG", 50.0, 22.0, "2026:11:11 10:00:00")
        dst = self.root / "arch" / "Eventy" / "Rzeszów" / "2026-11-11 Bieg" / "Zdjęcia" / "DJI_0001.JPG"
        core.execute_copies([core.Move(src, dst)])
        st = src.stat()
        self.assertFalse(self.cat.is_imported(src.name, st.st_size, st.st_mtime, str(card)))
        op_id, ids = self.cat.apply_copies([(str(src), str(dst))],
                                          {str(src): {"category": "eventy", "town": "Rzeszów", "event_name": "Bieg"}})
        self.assertTrue(self.cat.is_imported(src.name, st.st_size, st.st_mtime, str(card)))
        m = self.cat.material(ids[0])
        self.assertEqual((m["category"], m["main_path"]), ("eventy", str(dst)))
        self.assertIsNone(self.cat.material_id_for_path(src))        # karta nie trafia do katalogu
        self.cat.remove_copies([str(dst)])
        self.assertFalse(self.cat.is_imported(src.name, st.st_size, st.st_mtime, str(card)))
        self.assertIsNone(self.cat.material(ids[0]))

    def test_file_already_in_archive_counts_as_imported(self):
        arch = self.root / "arch"
        a = make_jpeg(arch / "x" / "DJI_0002.JPG", 50.0, 22.0)
        self.cat.index_paths([a])
        card = self.root / "karta"
        b = card / "DJI_0002.JPG"
        card.mkdir()
        b.write_bytes(a.read_bytes())
        os.utime(b, (a.stat().st_mtime, a.stat().st_mtime))
        self.assertTrue(self.cat.is_imported(b.name, b.stat().st_size, b.stat().st_mtime, str(card)))
        # ten sam plik zaindeksowany NA KARCIE nie oznacza importu
        self.cat.index_paths([b])
        a.unlink()
        with self.cat.conn() as c:
            c.execute("DELETE FROM files WHERE path_key=?", (cat.path_key(a),))
        self.assertFalse(self.cat.is_imported(b.name, b.stat().st_size, b.stat().st_mtime, str(card)))


class CardDetectionTests(unittest.TestCase):
    def test_find_cards_requires_removable_with_dcim(self):
        drives = [Drive("E:\\", "wymienny", "DJI"), Drive("F:\\", "wymienny", "PENDRIVE"),
                  Drive("C:\\", "lokalny", ""), Drive("G:\\", "wymienny", "", available=False)]
        dcims = {os.path.join("E:\\", "DCIM"), os.path.join("C:\\", "DCIM")}
        cards = find_cards(drives, isdir=lambda p: p in dcims)
        self.assertEqual([c.root for c in cards], ["E:\\"])

    def test_watcher_reports_new_and_reinserted_cards(self):
        from sortowanie.cards import Card
        state = {"cards": [Card("E:\\", "A", "E:\\DCIM")]}
        w = CardWatcher(lambda: state["cards"])
        self.assertEqual(w.poll(), [])                          # była już przy starcie
        state["cards"] = []
        self.assertEqual(w.poll(), [])                          # wyjęta
        state["cards"] = [Card("E:\\", "A", "E:\\DCIM")]
        self.assertEqual([c.root for c in w.poll()], ["E:\\"])  # włożona ponownie
        self.assertEqual(w.poll(), [])


if __name__ == "__main__":
    unittest.main()
