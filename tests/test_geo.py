"""Testy usługi lokalizacji i klienta Overpass z atrapą serwera (bez sieci)."""

import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

from sortowanie import geo, overpass
from sortowanie.config import Settings
from tests.test_osm import LAT, LON, rel, square, way


class FakeServer:
    """Odpowiada na zapytania Overpass na podstawie treści zapytania."""

    def __init__(self, area_data=None, script=None):
        self.area_data = area_data or {"elements": []}
        self.script = list(script or [])   # kolejne kody HTTP do zwrócenia przed sukcesem
        self.requests = []
        self.fail = False

    def __call__(self, request, timeout):
        url = request.full_url
        if self.fail:
            raise urllib.error.URLError("brak sieci")
        if url.endswith("/status"):
            self.requests.append(("status", url))
            return 200, b"Connected as: 1\nRate limit: 2\nSlot available after: 2026-11-25T10:00:00Z, in 7 seconds.\n"
        ql = urllib.parse.parse_qs(request.data.decode())["data"][0]
        self.requests.append(("query", ql))
        if self.script:
            return self.script.pop(0), b"<html>busy</html>"
        return 200, json.dumps(self.area_data).encode()

    @property
    def queries(self):
        return [q for kind, q in self.requests if kind == "query"]


AREA = {"elements": [
    rel(1, {"name": "Miasto A", "place": "city", "boundary": "administrative", "admin_level": "6"},
        square(LAT, LON, 0.05)),
    {"type": "relation", "id": 2, "tags": {"name": "gmina Testowo", "boundary": "administrative", "admin_level": "7"}},
]}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.settings = Settings(root / "u.json")
        self.sleeps = []

    def tearDown(self):
        self._tmp.cleanup()

    def service(self, server):
        client = overpass.OverpassClient(lambda: self.settings.get("overpass_url"), opener=server,
                                         sleep=self.sleeps.append)
        client.MIN_INTERVAL = 0
        return geo.LocationService(self.settings, geo.OsmCache(self.root / "cache.json"),
                                   geo.DecisionStore(self.root / "dec.json"), client)

    def test_town_then_points_in_polygon_use_cache(self):
        server = FakeServer(AREA)
        svc = self.service(server)
        r, src, _ = svc.town(LAT, LON)
        self.assertEqual((r.town, src), ("Miasto A", geo.SOURCE_ONLINE))
        r2, src2, _ = svc.town(LAT + 0.02, LON - 0.02)    # ~2,7 km dalej, w tym samym wielokącie
        self.assertEqual((r2.town, src2), ("Miasto A", geo.SOURCE_CACHE))
        self.assertEqual(len(server.queries), 1)

    def test_only_coordinates_sent_and_no_object_search(self):
        server = FakeServer(AREA)
        svc = self.service(server)
        svc.town(LAT, LON)
        self.assertFalse(hasattr(svc, "objects"))
        self.assertEqual(len(server.queries), 1)
        for q in server.queries:
            self.assertIn("is_in(", q)            # wyłącznie rozpoznanie miejscowości
            self.assertNotIn("DJI", q)
            self.assertNotIn("nwr[", q)           # żadnych zapytań o obiekty w okolicy

    def test_429_waits_per_status_and_retries(self):
        server = FakeServer(AREA, script=[429])
        svc = self.service(server)
        r, _, _ = svc.town(LAT, LON)
        self.assertEqual(r.town, "Miasto A")
        self.assertIn(8, self.sleeps)                       # 7 s ze /status + 1
        self.assertIn("status", [k for k, _ in server.requests])

    def test_504_backoff_then_success(self):
        server = FakeServer(AREA, script=[504, 504])
        svc = self.service(server)
        r, _, _ = svc.town(LAT, LON)
        self.assertEqual(r.town, "Miasto A")
        self.assertEqual(self.sleeps[:2], [15, 30])

    def test_gives_up_after_attempts(self):
        server = FakeServer(AREA, script=[504] * 10)
        svc = self.service(server)
        r, src, err = svc.town(LAT, LON)
        self.assertIsNone(r.town)
        self.assertIn("504", err)

    def test_offline_uses_cache(self):
        svc = self.service(FakeServer(AREA))
        svc.town(LAT, LON)
        dead = FakeServer()
        dead.fail = True
        svc2 = self.service(dead)
        r, src, _ = svc2.town(LAT, LON)
        self.assertEqual(r.town, "Miasto A")
        self.assertEqual(dead.requests, [])

    def test_no_data_offline(self):
        dead = FakeServer()
        dead.fail = True
        svc = self.service(dead)
        r, src, err = svc.town(LAT, LON)
        self.assertIsNone(r.town)
        self.assertTrue(err)
        self.assertTrue(svc.network_down)

    def test_offline_setting_blocks_network(self):
        self.settings.set("offline", True)
        server = FakeServer(AREA)
        self.service(server).town(LAT, LON)
        self.assertEqual(server.requests, [])

    def test_ambiguous_town_decision_remembered(self):
        amb = {"elements": [rel(5, {"name": "Wieś G", "place": "village"}, square(LAT, LON, 0.02)),
                            way(6, {"name": "Przysiółek H", "place": "hamlet"}, square(LAT, LON, 0.002))]}
        svc = self.service(FakeServer(amb))
        r, _, _ = svc.town(LAT, LON)
        self.assertTrue(r.ambiguous)
        svc.decisions.remember_town(LAT, LON, r.candidates, "Wieś G")
        r2, src, _ = svc.town(LAT + 0.0003, LON)
        self.assertEqual(r2.town, "Wieś G")
        self.assertEqual(src, geo.SOURCE_DECISION)

    def test_old_object_data_in_user_files_is_kept(self):
        """Pliki użytkownika ze starszej wersji (kafelki i decyzje obiektów) nie są czyszczone."""
        cache_file, dec_file = self.root / "cache.json", self.root / "dec.json"
        cache_file.write_text(json.dumps({"version": 1, "town_areas": {}, "points": [],
                                          "tiles": {"1,2": {"t": 1, "ids": []}}, "elements": {"w1": {}}}),
                              encoding="utf-8")
        dec_file.write_text(json.dumps({"towns": {}, "objects": {"x": "w1"}}), encoding="utf-8")
        svc = self.service(FakeServer(AREA))
        svc.town(LAT, LON)                                   # zapisuje cache
        svc.decisions.remember_town(LAT, LON, ["A", "B"], "A")  # zapisuje decyzje
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
        self.assertIn("1,2", cache["tiles"])
        self.assertEqual(len(cache["points"]), 1)
        dec = json.loads(dec_file.read_text(encoding="utf-8"))
        self.assertEqual(dec["objects"], {"x": "w1"})
        self.assertEqual(len(svc.decisions), 1)
        self.assertNotIn("kafelk", svc.cache.summary())

    def test_endpoint_from_env(self):
        os.environ[overpass.ENV_ENDPOINT] = "https://moj-serwer.example/api/interpreter"
        try:
            client = overpass.OverpassClient(lambda: "https://overpass-api.de/api/interpreter")
            self.assertEqual(client.endpoint, "https://moj-serwer.example/api/interpreter")
        finally:
            del os.environ[overpass.ENV_ENDPOINT]

    def test_parse_status(self):
        self.assertEqual(overpass.parse_status_wait("Rate limit: 2\n2 slots available now.\n"), 0)
        self.assertEqual(overpass.parse_status_wait(
            "Slot available after: X, in 12 seconds.\nSlot available after: Y, in 30 seconds."), 12)
        self.assertIsNone(overpass.parse_status_wait("cokolwiek"))
        self.assertEqual(overpass.status_url("https://a.b/api/interpreter"), "https://a.b/api/status")


if __name__ == "__main__":
    unittest.main()
