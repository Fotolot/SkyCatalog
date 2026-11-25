"""Testy analizy danych OSM na syntetycznych odpowiedziach w formacie Overpass (fikcyjne nazwy)."""

import unittest

from sortowanie import osm


def square(lat, lon, d):
    """Zamknięta geometria kwadratu o boku 2*d stopni."""
    pts = [(lat - d, lon - d), (lat - d, lon + d), (lat + d, lon + d), (lat + d, lon - d), (lat - d, lon - d)]
    return [{"lat": a, "lon": b} for a, b in pts]


def rel(rid, tags, geom):
    return {"type": "relation", "id": rid, "tags": tags,
            "members": [{"type": "way", "ref": rid * 10, "role": "outer", "geometry": geom}]}


def way(wid, tags, geom):
    return {"type": "way", "id": wid, "tags": tags, "geometry": geom}


def node(nid, lat, lon, tags):
    return {"type": "node", "id": nid, "lat": lat, "lon": lon, "tags": tags}


LAT, LON = 50.0, 22.0


class GeometryTests(unittest.TestCase):
    def test_point_inside_building_has_zero_distance(self):
        shape = osm.shape_from_element(way(1, {"building": "yes"}, square(LAT, LON, 0.0005)))
        self.assertEqual(shape.distance_m(LAT, LON), 0.0)

    def test_distance_to_polygon_edge_not_centre(self):
        # Duży obszar: środek ~1,1 km dalej, krawędź ~110 m od punktu
        shape = osm.shape_from_element(way(1, {"leisure": "stadium"}, square(LAT + 0.011, LON, 0.01)))
        d = shape.distance_m(LAT, LON)
        self.assertAlmostEqual(d, 111, delta=3)
        self.assertGreater(osm.haversine_m(LAT, LON, LAT + 0.011, LON), 1000)

    def test_multipolygon_assembled_from_split_ways(self):
        g = square(LAT, LON, 0.001)
        el = {"type": "relation", "id": 5, "tags": {"type": "multipolygon", "historic": "castle"},
              "members": [{"type": "way", "role": "outer", "geometry": g[:3]},
                          {"type": "way", "role": "outer", "geometry": g[2:]}]}
        shape = osm.shape_from_element(el)
        self.assertEqual(len(shape.outer), 1)
        self.assertTrue(shape.contains(LAT, LON))

    def test_inner_ring_excluded(self):
        el = {"type": "relation", "id": 6, "tags": {"type": "multipolygon", "leisure": "park"},
              "members": [{"type": "way", "role": "outer", "geometry": square(LAT, LON, 0.002)},
                          {"type": "way", "role": "inner", "geometry": square(LAT, LON, 0.0005)}]}
        shape = osm.shape_from_element(el)
        self.assertFalse(shape.contains(LAT, LON))
        self.assertGreater(shape.distance_m(LAT, LON), 30)

    def test_open_way_is_line(self):
        shape = osm.shape_from_element(way(1, {"man_made": "bridge"}, [{"lat": LAT, "lon": LON - 0.001},
                                                                       {"lat": LAT, "lon": LON + 0.001}]))
        self.assertAlmostEqual(shape.distance_m(LAT + 0.0009, LON), 100, delta=2)

    def test_shape_json_roundtrip(self):
        shape = osm.shape_from_element(way(1, {"building": "yes"}, square(LAT, LON, 0.001)))
        again = osm.Shape.from_json(shape.to_json())
        self.assertTrue(again.contains(LAT, LON))


class NoObjectSearchTests(unittest.TestCase):
    def test_object_search_removed(self):
        """Program nie wyszukuje pobliskich obiektów – tylko miejscowości."""
        for name in ("poi_query", "rank_pois", "PoiCandidate", "POI_RULES"):
            self.assertFalse(hasattr(osm, name), name)


class TownTests(unittest.TestCase):
    def areas(self, *elements, nodes=()):
        data = {"elements": list(elements) + list(nodes)}
        return osm.parse_area_response(data, LAT, LON)

    def test_place_area_is_confident(self):
        areas, nodes = self.areas(
            rel(1, {"name": "Miasto A", "place": "city", "boundary": "administrative", "admin_level": "6"},
                square(LAT, LON, 0.05)),
            rel(2, {"name": "Dzielnica B", "boundary": "administrative", "admin_level": "9"}, square(LAT, LON, 0.01)),
            rel(3, {"name": "Osiedle C", "place": "neighbourhood"}, square(LAT, LON, 0.005)),
        )
        r = osm.decide_town(areas, nodes)
        self.assertEqual(r.town, "Miasto A")
        kinds = {a.name: a.kind for a in areas}
        self.assertEqual(kinds["Dzielnica B"], osm.KIND_SUBDIVISION)
        self.assertEqual(kinds["Osiedle C"], osm.KIND_SUBDIVISION)

    def test_gmina_is_not_a_town(self):
        areas, nodes = self.areas(
            {"type": "relation", "id": 9, "tags": {"name": "gmina Testowo", "boundary": "administrative",
                                                    "admin_level": "7"}},
        )
        self.assertEqual(areas[0].kind, osm.KIND_LARGER)
        r = osm.decide_town(areas, nodes)
        self.assertIsNone(r.town)
        self.assertEqual(r.candidates, [])

    def test_admin_boundary_confirmed_by_place_node(self):
        areas, nodes = self.areas(
            rel(4, {"name": "Wieś D", "boundary": "administrative", "admin_level": "8"}, square(LAT, LON, 0.01)),
            nodes=[node(40, LAT + 0.002, LON, {"name": "Wieś D", "place": "village"})],
        )
        r = osm.decide_town(areas, nodes)
        self.assertEqual(r.town, "Wieś D")

    def test_unconfirmed_admin_area_is_ambiguous(self):
        areas, nodes = self.areas(
            rel(4, {"name": "Obszar E", "boundary": "administrative", "admin_level": "8"}, square(LAT, LON, 0.01)),
            nodes=[node(41, LAT + 0.004, LON, {"name": "Wieś F", "place": "village"})],
        )
        r = osm.decide_town(areas, nodes)
        self.assertIsNone(r.town)
        self.assertEqual(r.candidates, ["Obszar E", "Wieś F"])

    def test_two_settlement_areas_ask_user(self):
        areas, nodes = self.areas(
            rel(5, {"name": "Wieś G", "place": "village"}, square(LAT, LON, 0.02)),
            way(6, {"name": "Przysiółek H", "place": "hamlet"}, square(LAT, LON, 0.002)),
        )
        r = osm.decide_town(areas, nodes)
        self.assertTrue(r.ambiguous)
        self.assertEqual(r.candidates, ["Wieś G", "Przysiółek H"])

    def test_only_nearby_node(self):
        areas, nodes = self.areas(nodes=[node(42, LAT + 0.003, LON, {"name": "Wieś I", "place": "village"})])
        r = osm.decide_town(areas, nodes)
        self.assertEqual(r.town, "Wieś I")
        areas, nodes = self.areas(nodes=[node(42, LAT + 0.003, LON, {"name": "Wieś I", "place": "village"}),
                                         node(43, LAT - 0.004, LON, {"name": "Wieś J", "place": "village"})])
        self.assertTrue(osm.decide_town(areas, nodes).ambiguous)

    def test_suburb_node_is_never_a_town(self):
        areas, nodes = self.areas(nodes=[node(44, LAT, LON, {"name": "Osiedle K", "place": "suburb"})])
        r = osm.decide_town(areas, nodes)
        self.assertIsNone(r.town)
        self.assertEqual(r.candidates, [])

    def test_area_query_shape(self):
        q = osm.area_query(50.027082, 22.003366)
        self.assertIn("is_in(50.027082,22.003366)", q)
        self.assertIn("out geom", q)
        self.assertIn("[maxsize:", q)


if __name__ == "__main__":
    unittest.main()
