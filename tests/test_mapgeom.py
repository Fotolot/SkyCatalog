import random
import time
import unittest

from sortowanie import mapgeom


class MapGeomTests(unittest.TestCase):
    def test_thin_points_large_set_fast(self):
        r = random.Random(0)
        pts = [(i, r.random() * 1600, r.random() * 900) for i in range(200000)]
        t = time.perf_counter()
        thin = mapgeom.thin_points(pts, 3)
        self.assertLess(time.perf_counter() - t, 1.0)
        self.assertLessEqual(len(thin), (1600 // 3 + 1) * (900 // 3 + 1))

    def test_thin_latlon_only_above_limit(self):
        rows = [(i, 50.0 + i * 1e-7, 22.0) for i in range(100)]
        self.assertEqual(mapgeom.thin_latlon(rows, 10, 1000), rows)
        thinned = mapgeom.thin_latlon(rows, 10, 50)
        self.assertEqual(len(thinned), 1)                # wszystkie w jednym pikselu


if __name__ == "__main__":
    unittest.main()
