import unittest

from sortowanie import tiles


class FakeSettings(dict):
    def get(self, k, d=None):
        return super().get(k, d)


class ProjectionTests(unittest.TestCase):
    def test_roundtrip(self):
        for z in (3, 10, 17):
            x, y = tiles.latlon_to_px(50.027082, 22.003366, z)
            lat, lon = tiles.px_to_latlon(x, y, z)
            self.assertAlmostEqual(lat, 50.027082, places=6)
            self.assertAlmostEqual(lon, 22.003366, places=6)

    def test_known_tile(self):
        x, y = tiles.latlon_to_px(0, 0, 1)
        self.assertEqual((x, y), (256, 256))

    def test_visible_tiles_only_cover_view(self):
        cx, cy = tiles.latlon_to_px(50.0, 22.0, 12)
        ts = tiles.visible_tiles(cx, cy, 12, 800, 600)
        self.assertLessEqual(len(ts), 5 * 4)       # bez zapasu poza widokiem
        self.assertTrue(all(t[0] == 12 for t in ts))

    def test_fit_zoom(self):
        z_small = tiles.fit_zoom((50.0, 22.0, 50.001, 22.001), 800, 600, 19)
        z_big = tiles.fit_zoom((49.0, 14.0, 55.0, 24.0), 800, 600, 19)
        self.assertGreater(z_small, z_big)


class ProviderTests(unittest.TestCase):
    def test_default_and_validation(self):
        p = tiles.provider_from_settings(FakeSettings())
        self.assertEqual(p.tile_url(1, 2, 3), "https://tile.openstreetmap.org/1/2/3.png")
        self.assertIsNotNone(tiles.validate_url("http://tile.openstreetmap.org/{z}/{x}/{y}.png"))
        self.assertIsNotNone(tiles.validate_url("https://example.com/tiles.png"))
        p2 = tiles.provider_from_settings(FakeSettings(tile_url="https://maps.example/{z}/{x}/{y}.png",
                                                       tile_attribution="© Example"))
        self.assertIn("OpenStreetMap", p2.attribution)     # oznaczenie OSM zawsze obecne

    def test_user_agent_identifies_app(self):
        self.assertTrue(tiles.USER_AGENT.startswith("Sortowanie/"))




if __name__ == "__main__":
    unittest.main()
