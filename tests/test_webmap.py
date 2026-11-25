"""Biała lista zasobów mapy (wymaga PySide6 – na maszynach bez niego test jest pomijany)."""
import unittest

try:
    from sortowanie.ui import webmap
except ImportError:  # pragma: no cover
    webmap = None


@unittest.skipIf(webmap is None, "PySide6 niedostępny")
class AssetWhitelistTests(unittest.TestCase):
    def test_allowed_assets(self):
        self.assertIn(b"QWebChannel", webmap.resolve_asset("/map.js"))
        self.assertIn(b"Content-Security-Policy", webmap.resolve_asset("/map.html"))
        self.assertTrue(webmap.resolve_asset("/leaflet/leaflet.js").startswith(b"/* @preserve"))
        self.assertIsNotNone(webmap.resolve_asset("/leaflet/images/layers.png"))

    def test_everything_else_rejected(self):
        for path in ("/", "/../config.py", "/leaflet/../../catalog.py", "/leaflet/images/../../map.js",
                     "/C:/Windows/win.ini", "/etc/passwd", "/leaflet/LICENSE", "/map.html?x=1"):
            self.assertIsNone(webmap.resolve_asset(path), path)

    def test_no_inner_html_in_map_script(self):
        js = webmap.resolve_asset("/map.js").decode("utf-8")
        self.assertNotIn(".innerHTML =", js)
        self.assertNotIn("eval(", js)


if __name__ == "__main__":
    unittest.main()
