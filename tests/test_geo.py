import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

from sortowanie import geo
from sortowanie.config import Settings

NOMINATIM = {"address": {"road": "Aleja Cieplińskiego", "city": "Rzeszów", "country": "Polska"}}
OVERPASS = {"elements": [
    {"type": "node", "lat": 50.0390, "lon": 22.0040,
     "tags": {"name": "Kawiarnia", "amenity": "cafe"}},
    {"type": "way", "center": {"lat": 50.0377, "lon": 22.0051},
     "tags": {"name": "Pomnik Czynu Rewolucyjnego", "historic": "memorial"}},
    {"type": "node", "lat": 50.0376, "lon": 22.0050,
     "tags": {"name": "Park Jedności", "leisure": "park"}},
]}


class FakeOpener:
    def __init__(self, responses=None, fail=False):
        self.responses = responses or {}
        self.fail = fail
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.fail:
            raise urllib.error.URLError("brak sieci")
        for fragment, payload in self.responses.items():
            if fragment in request.full_url:
                return json.dumps(payload).encode()
        raise AssertionError(f"Nieoczekiwane zapytanie: {request.full_url}")


class GeoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.settings = Settings(root / "ustawienia.json")
        self.cache_file = root / "cache.json"
        geo.HttpClient.MIN_INTERVAL = {}
        geo.HttpClient.DEFAULT_INTERVAL = 0

    def tearDown(self):
        self._tmp.cleanup()

    def service(self, opener):
        cache = geo.GeoCache(self.cache_file)
        return geo.LocationService(self.settings, cache, geo.HttpClient(self.settings, opener))

    def test_resolve_town_and_object(self):
        opener = FakeOpener({"nominatim": NOMINATIM, "overpass": OVERPASS})
        place = self.service(opener).resolve(50.0375, 22.0049)
        self.assertEqual(place.town, "Rzeszów")
        self.assertEqual(place.object_name, "Pomnik Czynu Rewolucyjnego")
        self.assertEqual(place.source, geo.SOURCE_ONLINE)

    def test_only_coordinates_are_sent(self):
        opener = FakeOpener({"nominatim": NOMINATIM, "overpass": OVERPASS})
        self.service(opener).resolve(50.0375, 22.0049)
        rev = opener.requests[0]
        params = urllib.parse.parse_qs(urllib.parse.urlparse(rev.full_url).query)
        self.assertEqual(set(params), {"lat", "lon", "format", "zoom", "addressdetails", "accept-language"})
        self.assertIn("Sortowanie", rev.get_header("User-agent"))
        body = urllib.parse.parse_qs(opener.requests[1].data.decode())["data"][0]
        self.assertIn("around:250,50.037500,22.004900", body)

    def test_cache_is_used_offline(self):
        self.service(FakeOpener({"nominatim": NOMINATIM, "overpass": OVERPASS})).resolve(50.0375, 22.0049)
        offline = FakeOpener(fail=True)
        place = self.service(offline).resolve(50.0375, 22.0049)
        self.assertEqual(place.town, "Rzeszów")
        self.assertEqual(place.object_name, "Pomnik Czynu Rewolucyjnego")
        self.assertEqual(place.source, geo.SOURCE_CACHE)
        self.assertEqual(offline.requests, [])

    def test_network_failure_falls_back_to_nearby_cache(self):
        self.service(FakeOpener({"nominatim": NOMINATIM, "overpass": OVERPASS})).resolve(50.0375, 22.0049)
        svc = self.service(FakeOpener(fail=True))
        place = svc.resolve(50.0400, 22.0060)  # ~300 m dalej, brak dokładnego wpisu
        self.assertEqual(place.town, "Rzeszów")
        self.assertEqual(place.source, geo.SOURCE_CACHE_NEAR)
        self.assertIsNone(place.object_name)
        self.assertTrue(svc.network_down)

    def test_no_internet_no_cache(self):
        place = self.service(FakeOpener(fail=True)).resolve(49.0, 20.0)
        self.assertIsNone(place.town)
        self.assertTrue(place.error)

    def test_offline_setting_blocks_requests(self):
        self.settings.set("offline", True)
        opener = FakeOpener({"nominatim": NOMINATIM})
        self.service(opener).resolve(50.0, 22.0)
        self.assertEqual(opener.requests, [])

    def test_photon_provider(self):
        self.settings.set("reverse_provider", "photon")
        self.settings.set("poi_provider", "brak")
        photon = {"features": [{"properties": {"name": "Łańcut", "type": "city"}}]}
        place = self.service(FakeOpener({"photon": photon})).resolve(50.07, 22.23)
        self.assertEqual(place.town, "Łańcut")
        self.assertIsNone(place.object_name)

    def test_no_object_in_radius(self):
        far = {"elements": [{"type": "node", "lat": 50.05, "lon": 22.05,
                             "tags": {"name": "Daleko", "historic": "castle"}}]}
        place = self.service(FakeOpener({"nominatim": NOMINATIM, "overpass": far})).resolve(50.0375, 22.0049)
        self.assertEqual(place.town, "Rzeszów")
        self.assertIsNone(place.object_name)

    def test_parse_nominatim_village(self):
        self.assertEqual(geo.parse_nominatim({"address": {"village": "Tyczyn"}}), "Tyczyn")
        self.assertIsNone(geo.parse_nominatim({}))

    def test_haversine(self):
        self.assertAlmostEqual(geo.haversine_m(50.0, 22.0, 50.0, 22.0), 0)
        self.assertAlmostEqual(geo.haversine_m(50.0, 22.0, 50.001, 22.0), 111.2, delta=0.5)


if __name__ == "__main__":
    unittest.main()
