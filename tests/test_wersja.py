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


if __name__ == "__main__":
    unittest.main()
