"""Numeracja wydań: MAJOR.MINOR z pliku VERSION + kolejny numer poprawki z tagów."""
import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location("wersja", Path(__file__).resolve().parent.parent / "narzedzia" / "wersja.py")
wersja = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wersja)


class NextPatchTests(unittest.TestCase):
    def test_first_release_of_new_minor_is_1(self):
        self.assertEqual(wersja.next_patch(1, 2, ["v1.1.29", "v1.1.3"]), 1)

    def test_increments_highest_existing(self):
        self.assertEqual(wersja.next_patch(1, 2, ["v1.1.29", "v1.2.1", "v1.2.10", "v1.2.2"]), 11)

    def test_ignores_other_tags(self):
        self.assertEqual(wersja.next_patch(1, 2, ["v1.2.1-test", "1.2.5", "v1.20.4", "v11.2.7"]), 1)



class VersionFileTests(unittest.TestCase):
    def run_with(self, text):
        import shutil, subprocess, sys, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(Path(wersja.__file__).parent, root / "narzedzia")
            (root / "sortowanie").mkdir()
            (root / "VERSION").write_text(text, encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            out = subprocess.run([sys.executable, str(root / "narzedzia" / "wersja.py"), "--z-tagow"],
                                 cwd=root, capture_output=True, text=True)
            return out.returncode, out.stdout.strip()

    def test_full_version_used_exactly(self):
        self.assertEqual(self.run_with("1.3.0\n"), (0, "1.3.0"))

    def test_major_minor_gets_first_patch(self):
        self.assertEqual(self.run_with("2.0"), (0, "2.0.1"))

    def test_invalid_rejected(self):
        self.assertNotEqual(self.run_with("wersja 2")[0], 0)


if __name__ == "__main__":
    unittest.main()
