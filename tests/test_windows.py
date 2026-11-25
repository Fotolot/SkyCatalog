"""Testy zachowań specyficznych dla Windows – uruchamiane w GitHub Actions (windows-latest)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

from sortowanie import catalog as cat
from sortowanie.scanner import SKIP_LINK, SKIP_SYSTEM, Scanner, list_drives
from tests.helpers import make_jpeg


class FakeSettings(dict):
    def get(self, k, d=None):
        return super().get(k, d)


@unittest.skipUnless(sys.platform == "win32", "tylko Windows")
class WindowsScannerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.disk = self.root / "dysk"
        self.disk.mkdir()
        self.cat = cat.Catalog(self.root / "k.sqlite")

    def tearDown(self):
        self.cat.close()
        self._tmp.cleanup()

    def scan(self):
        return Scanner(self.cat, FakeSettings(scan_exclusions=[])).scan([str(self.disk)])

    def test_drive_list_contains_system_drive(self):
        drives = {d.root.upper(): d for d in list_drives()}
        system = os.environ.get("SystemDrive", "C:").upper() + "\\"
        self.assertIn(system, drives)
        self.assertEqual(drives[system].kind, "lokalny")

    def test_junction_loop_not_followed(self):
        import _winapi
        make_jpeg(self.disk / "a" / "x.JPG", 50.0, 22.0)
        _winapi.CreateJunction(str(self.disk), str(self.disk / "a" / "petla"))
        prog = self.scan()
        self.assertTrue(prog.finished)
        self.assertEqual(len(self.cat.query_list(cat.Filters())), 1)
        self.assertIn(SKIP_LINK, dict(prog.summary))

    def test_hidden_folder_skipped(self):
        import ctypes
        hidden = self.disk / "ukryty"
        make_jpeg(hidden / "x.JPG", 50.0, 22.0)
        ctypes.windll.kernel32.SetFileAttributesW(str(hidden), 0x2)
        prog = self.scan()
        self.assertEqual(self.cat.query_list(cat.Filters()), [])
        self.assertIn(SKIP_SYSTEM, dict(prog.summary))

    def test_case_insensitive_paths(self):
        p = make_jpeg(self.disk / "Lot" / "DJI_0001.JPG", 50.0, 22.0)
        self.scan()
        self.assertIsNotNone(self.cat.material_id_for_path(str(p).upper()))


if __name__ == "__main__":
    unittest.main()
