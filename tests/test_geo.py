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
from tests.test_osm import LAT, LON, node, rel, square, way


class FakeServer:
    """Odpowiada na zapytania Overpass na podstawie treści zapytania."""

    def __init__(self, area_data=None, poi_data=None, script=None):
        self.area_data = area_data or {"elements": []}
        self.poi_data = poi_data or {"elements": []}
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
        payload = self.area_data if "is_in(" in ql else self.poi_data
        return 200, json.dumps(payload).encode()

    @property
    def queries(self):
        return [q for kind, q in self.requests if kind == "query"]


AREA = {"elements": [
    rel(1, {"name": "Miasto A", "place": "city", "boundary": "administrative", "admin_level": "6"},
        square(LAT, LON, 0.05)),
    {"type": "relation", "id": 2, "tags": {"name": "gmina Testowo", "boundary": "administrative", "admin_level": "7"}},
]}
POI = {"elements": [
    way(10, {"name": "Pomnik X", "historic": "memorial"}, square(LAT + 0.0012, LON, 0.0001)),
    node(11, LAT + 0.0015, LON + 0.0005, {"name": "Muzeum Y", "tourism": "museum"}),
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

    def objects(self, svc, points):
        out = {}
        svc.objects(points, lambda k, c, s, e, r: out.__setitem__(k, (c, s, e, r)))
        return out

    def test_town_then_points_in_polygon_use_cache(self):
        server = FakeServer(AREA)
        svc = self.service(server)
        r, src, _ = svc.town(LAT, LON)
        self.assertEqual((r.town, src), ("Miasto A", geo.SOURCE_ONLINE))
        r2, src2, _ = svc.town(LAT + 0.02, LON - 0.02)    # ~2,7 km dalej, w tym samym wielokącie
        self.assertEqual((r2.town, src2), ("Miasto A", geo.SOURCE_CACHE))
        self.assertEqual(len(server.queries), 1)

    def test_objects_fetched_once_for_many_points(self):
        server = FakeServer(AREA, POI)
        svc = self.service(server)
        pts = [("a", LAT, LON), ("b", LAT + 0.0003, LON + 0.0002), ("c", LAT + 0.0005, LON)]
        out = self.objects(svc, pts)
        self.assertEqual(len(server.queries), 1)
        self.assertEqual(out["a"][0][0].name, "Pomnik X")
        # Drugi raz – całość z cache, bez zapytań
        out2 = self.objects(svc, pts)
        self.assertEqual(len(server.queries), 1)
        self.assertEqual(out2["b"][0][0].name, "Pomnik X")

    def test_only_coordinates_sent(self):
        server = FakeServer(AREA, POI)
        svc = self.service(server)
        svc.town(LAT, LON)
        self.objects(svc, [("DJI_0001", LAT, LON)])
        for q in server.queries:
            self.assertNotIn("DJI", q)

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

    def test_offline_uses_cache_and_decisions(self):
        svc = self.service(FakeServer(AREA, POI))
        svc.town(LAT, LON)
        self.objects(svc, [("a", LAT, LON)])
        dead = FakeServer()
        dead.fail = True
        svc2 = self.service(dead)
        r, src, _ = svc2.town(LAT, LON)
        self.assertEqual(r.town, "Miasto A")
        out = self.objects(svc2, [("a", LAT, LON)])
        self.assertEqual(out["a"][0][0].name, "Pomnik X")
        self.assertEqual(dead.requests, [])

    def test_no_data_offline(self):
        dead = FakeServer()
        dead.fail = True
        svc = self.service(dead)
        r, src, err = svc.town(LAT, LON)
        self.assertIsNone(r.town)
        self.assertTrue(err)
        out = self.objects(svc, [("a", LAT, LON)])
        self.assertEqual(out["a"][1], geo.SOURCE_NONE)
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

    def test_object_decision_remembered(self):
        svc = self.service(FakeServer(AREA, POI))
        out = self.objects(svc, [("a", LAT, LON)])
        cands = out["a"][0]
        svc.decisions.remember_object(LAT, LON, [c.osm_id for c in cands], "")
        out2 = self.objects(svc, [("a", LAT, LON)])
        self.assertEqual(out2["a"][3], "")

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
