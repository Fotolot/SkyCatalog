"""Aktualizacja wersji przenośnej: wersje, odpowiedź API, rozpakowanie, skrypt (.cmd w Windows)."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from sortowanie import updater


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, headers=None):
        super().__init__(data)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


RELEASE = {"tag_name": "v1.1.15", "body": "## Zmiany\n* Import z karty", "html_url": "https://x/r",
           "draft": False, "prerelease": False,
           "assets": [{"name": "SkyCatalog-portable.zip", "browser_download_url": "https://x/z.zip", "size": 3}]}


class VersionTests(unittest.TestCase):
    def test_compare(self):
        self.assertTrue(updater.is_newer("v1.1.15", "1.1.9"))
        self.assertTrue(updater.is_newer("v1.2.0", "1.1.99"))
        self.assertFalse(updater.is_newer("v1.1.9", "1.1.15"))
        self.assertFalse(updater.is_newer("v1.1.15", "1.1.15"))
        self.assertFalse(updater.is_newer("nie-wersja", "1.0.0"))
        self.assertEqual(updater.parse_version("v2"), (2, 0, 0))


class ApiTests(unittest.TestCase):
    def test_parse_latest(self):
        opener = lambda req, timeout: FakeResponse(json.dumps(RELEASE).encode())  # noqa: E731
        r = updater.check_latest(opener=opener)
        self.assertEqual((r.version, r.asset_url, r.asset_size), ("1.1.15", "https://x/z.zip", 3))
        self.assertIn("Import z karty", r.notes)

    def test_network_error_returns_none(self):
        def opener(req, timeout):
            raise OSError("brak sieci")
        self.assertIsNone(updater.check_latest(opener=opener))
        self.assertIsNone(updater.check_latest(opener=lambda r, timeout: FakeResponse(b"<html>")))

    def test_prerelease_and_missing_asset(self):
        self.assertIsNone(updater.parse_release({**RELEASE, "prerelease": True}))
        r = updater.parse_release({**RELEASE, "assets": []})
        self.assertIsNone(r.asset_url)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make_zip(self, entries: dict) -> Path:
        p = self.dir / "a.zip"
        with zipfile.ZipFile(p, "w") as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        return p

    def test_extract_finds_exe_folder(self):
        z = self.make_zip({"SkyCatalog/SkyCatalog.exe": b"exe", "SkyCatalog/_internal/x.dll": b"dll"})
        new = updater.extract(z, self.dir / "out")
        self.assertEqual(new.name, "SkyCatalog")
        self.assertTrue((new / "_internal" / "x.dll").exists())

    def test_zip_slip_rejected(self):
        z = self.make_zip({"../../zly.txt": b"x", "SkyCatalog/SkyCatalog.exe": b"e"})
        with self.assertRaises(updater.UpdateError):
            updater.extract(z, self.dir / "out")
        self.assertFalse((self.dir.parent / "zly.txt").exists())

    def test_download_size_check(self):
        rel = updater.parse_release(RELEASE)
        ok = updater.download(rel, self.dir / "d", opener=lambda r, timeout: FakeResponse(b"abc"))
        self.assertEqual(ok.read_bytes(), b"abc")
        with self.assertRaises(updater.UpdateError):
            updater.download(rel, self.dir / "d2", opener=lambda r, timeout: FakeResponse(b"abcdef"))

    def test_script_is_ascii_and_protects_user_data(self):
        text = updater.build_script()
        text.encode("ascii")
        for name in ("ustawienia.json", "*.sqlite", "_backup", "miniatury", "przegladarka"):
            self.assertIn(f'"{name}"', text)
        self.assertIn("\r\n", text)


@unittest.skipUnless(sys.platform == "win32", "skrypt .cmd – tylko Windows")
class WindowsScriptTests(unittest.TestCase):
    """Uruchamia prawdziwy skrypt aktualizacji na folderach testowych."""

    def setUp(self):
        # skrypt ponownie uruchamia program – może jeszcze działać podczas sprzątania
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.app = self.root / "Moja aplikacja & testy"         # spacja i & w ścieżce
        self.new = self.root / "nowa" / "SkyCatalog"
        exe_src = Path(os.environ["SystemRoot"]) / "System32" / "whoami.exe"
        for d, ver in ((self.app, "v1"), (self.new, "v2")):
            (d / "_internal").mkdir(parents=True)
            (d / "SkyCatalog.exe").write_bytes(exe_src.read_bytes())
            (d / "_internal" / "lib.dll").write_text(ver, encoding="utf-8")
        (self.app / "ustawienia.json").write_text("dane użytkownika", encoding="utf-8")
        (self.new / "ustawienia.json").write_text("NIE KOPIOWAĆ", encoding="utf-8")
        (self.app / "katalog.sqlite").write_text("baza", encoding="utf-8")
        self.script = updater.write_script(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def run_script(self):
        return subprocess.run(["cmd.exe", "/d", "/c", str(self.script), "999999", str(self.app), str(self.new),
                               "SkyCatalog.exe"], capture_output=True, timeout=120)

    def test_update_copies_new_files_keeps_user_data_and_backup(self):
        res = self.run_script()
        log = (self.root / "aktualizacja.log").read_text(errors="replace")
        self.assertEqual(res.returncode, 0, log)
        self.assertEqual((self.app / "_internal" / "lib.dll").read_text(), "v2")
        self.assertEqual((self.app / "ustawienia.json").read_text(encoding="utf-8"), "dane użytkownika")
        self.assertEqual((self.app / "katalog.sqlite").read_text(), "baza")
        self.assertEqual((self.app / "_backup" / "_internal" / "lib.dll").read_text(), "v1")
        self.assertFalse((self.app / "_backup" / "ustawienia.json").exists())

    def test_failed_copy_restores_backup(self):
        # Konflikt: w nowej wersji „konflikt” jest folderem, w starej plikiem – kopiowanie się nie uda
        (self.app / "_internal" / "konflikt").write_text("plik", encoding="utf-8")
        (self.new / "_internal" / "konflikt").mkdir()
        (self.new / "_internal" / "konflikt" / "x.dll").write_text("x", encoding="utf-8")
        res = self.run_script()
        log = (self.root / "aktualizacja.log").read_text(errors="replace")
        self.assertEqual(res.returncode, 1, log)
        self.assertIn("przywracanie", log)
        self.assertEqual((self.app / "_internal" / "lib.dll").read_text(), "v1")      # stara wersja wróciła
        self.assertEqual((self.app / "ustawienia.json").read_text(encoding="utf-8"), "dane użytkownika")

    def test_backup_failure_changes_nothing(self):
        locked = self.app / "_internal" / "lib.dll"
        import msvcrt
        with open(locked, "r+b") as f:                    # nie da się odczytać => brak kopii zapasowej
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            res = self.run_script()
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        log = (self.root / "aktualizacja.log").read_text(errors="replace")
        self.assertEqual(res.returncode, 1, log)
        self.assertIn("nic nie zmieniono", log)
        self.assertEqual(locked.read_text(), "v1")                                     # nic nie podmieniono

if __name__ == "__main__":
    unittest.main()
