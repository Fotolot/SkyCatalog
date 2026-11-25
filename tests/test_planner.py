import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sortowanie import planner as pl
from sortowanie.geo import Place
from sortowanie.media import KIND_PHOTO, KIND_VIDEO, MediaGroup


def group(key, kind=KIND_VIDEO, gps=(50.0, 22.0), captured=None, files=None, root=Path("/karta")):
    if files is None:
        files = [root / f"{key}.MP4", root / f"{key}.SRT"] if kind == KIND_VIDEO else [root / f"{key}.JPG"]
    return MediaGroup(key=key, name=key, files=files, kind=kind, gps=gps, captured=captured)


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.arch = Path(self._tmp.name) / "Archiwum"

    def tearDown(self):
        self._tmp.cleanup()

    # Hobby
    def test_hobby_with_object(self):
        g = group("DJI_0001")
        a = pl.Assignment(pl.CATEGORY_HOBBY)
        place = Place("Rzeszów", "Pomnik Czynu Rewolucyjnego")
        self.assertEqual(pl.relative_dir(g, a, place),
                         Path("Hobby", "Rzeszów", "Pomnik Czynu Rewolucyjnego", "WIDEO"))
        photo = group("DJI_0002", KIND_PHOTO)
        self.assertEqual(pl.relative_dir(photo, a, place),
                         Path("Hobby", "Rzeszów", "Pomnik Czynu Rewolucyjnego", "Zdjęcia"))

    def test_hobby_without_object_skips_level(self):
        a = pl.Assignment(pl.CATEGORY_HOBBY)
        self.assertEqual(pl.relative_dir(group("A"), a, Place("Rzeszów", None)),
                         Path("Hobby", "Rzeszów", "WIDEO"))

    def test_hobby_groups_split_by_gps(self):
        a = pl.Assignment(pl.CATEGORY_HOBBY)
        g1, g2 = group("A"), group("B")
        places = {"A": Place("Rzeszów", "Zamek"), "B": Place("Łańcut", None)}
        plan = pl.build_plan([g1, g2], {"A": a, "B": a}, places, self.arch)
        self.assertEqual(plan.group_dirs["A"], Path("Hobby/Rzeszów/Zamek/WIDEO"))
        self.assertEqual(plan.group_dirs["B"], Path("Hobby/Łańcut/WIDEO"))
        self.assertTrue(plan.ready, plan.errors)

    def test_hobby_without_gps_requires_manual_town(self):
        g = group("A", gps=None)
        a = pl.Assignment(pl.CATEGORY_HOBBY)
        plan = pl.build_plan([g], {"A": a}, {}, self.arch)
        self.assertFalse(plan.ready)
        self.assertTrue(any("miejscowości" in e for e in plan.errors))
        a.place = "Tyczyn"
        plan = pl.build_plan([g], {"A": a}, {}, self.arch)
        self.assertTrue(plan.ready, plan.errors)
        self.assertEqual(plan.group_dirs["A"], Path("Hobby/Tyczyn/WIDEO"))

    def test_hobby_pending_blocks(self):
        g = group("A")
        plan = pl.build_plan([g], {"A": pl.Assignment(pl.CATEGORY_HOBBY)}, {}, self.arch)
        self.assertTrue(any("trwa rozpoznawanie" in e for e in plan.errors))

    # Eventy
    def test_event_structure_ignores_gps_objects(self):
        a = pl.Assignment(pl.CATEGORY_EVENT, name="Bieg Niepodległości", date="2026-11-11", place="Rzeszów")
        places = {"A": Place("Rzeszów", "Pomnik"), "B": Place("Boguchwała", "Kościół")}
        plan = pl.build_plan([group("A"), group("B", KIND_PHOTO)], {"A": a, "B": a}, places, self.arch)
        self.assertTrue(plan.ready, plan.errors)
        self.assertEqual(plan.group_dirs["A"], Path("Eventy/Rzeszów/2026-11-11 Bieg Niepodległości/WIDEO"))
        self.assertEqual(plan.group_dirs["B"], Path("Eventy/Rzeszów/2026-11-11 Bieg Niepodległości/Zdjęcia"))

    def test_event_requires_fields(self):
        a = pl.Assignment(pl.CATEGORY_EVENT)
        errors = pl.validate_assignment(a, [group("A")], {})
        self.assertEqual(len(errors), 3)
        a.date = "31.02.2026"
        self.assertTrue(any("nieprawidłowa data" in e for e in pl.validate_assignment(a, [], {})))

    def test_polish_date_format_is_normalized(self):
        a = pl.Assignment(pl.CATEGORY_EVENT, name="Dożynki", date="11.11.2026", place="Rzeszów")
        self.assertEqual(pl.relative_dir(group("A"), a, None), Path("Eventy/Rzeszów/2026-11-11 Dożynki/WIDEO"))

    # Zlecenia
    def test_order_structure(self):
        a = pl.Assignment(pl.CATEGORY_ORDER, name="Film promocyjny nieruchomości",
                          date="2026-11-25", place="Łańcut")
        places = {"A": Place("Rzeszów"), "B": Place("Krosno")}
        plan = pl.build_plan([group("A"), group("B")], {"A": a, "B": a}, places, self.arch)
        dirs = set(plan.group_dirs.values())
        self.assertEqual(dirs, {Path("Zlecenia/Łańcut/2026-11-25 Film promocyjny nieruchomości/WIDEO")})

    def test_order_location_can_be_address(self):
        a = pl.Assignment(pl.CATEGORY_ORDER, name="Inspekcja dachu", date="2026-11-25",
                          place="ul. Długa 5/7, Łańcut")
        self.assertEqual(pl.relative_dir(group("A"), a, None).parts[1], "ul. Długa 5-7, Łańcut")

    # Plan ogólnie
    def test_unassigned_blocks(self):
        plan = pl.build_plan([group("A")], {}, {}, self.arch)
        self.assertFalse(plan.ready)
        self.assertTrue(any("Nie wybrano kategorii" in e for e in plan.errors))

    def test_missing_target_root_blocks(self):
        a = pl.Assignment(pl.CATEGORY_HOBBY, place="Rzeszów")
        plan = pl.build_plan([group("A", gps=None)], {"A": a}, {}, "")
        self.assertFalse(plan.ready)

    def test_video_and_srt_share_destination_and_collision_suffix(self):
        a = pl.Assignment(pl.CATEGORY_EVENT, name="Bieg", date="2026-11-11", place="Rzeszów")
        target = self.arch / "Eventy/Rzeszów/2026-11-11 Bieg/WIDEO"
        target.mkdir(parents=True)
        (target / "DJI_0001.SRT").write_text("istniejący", encoding="utf-8")
        # Dwie karty z tym samym numerem pliku
        g1 = group("k1", files=[Path("/k1/DJI_0001.MP4"), Path("/k1/DJI_0001.SRT")])
        g2 = group("k2", files=[Path("/k2/DJI_0001.MP4"), Path("/k2/DJI_0001.SRT")])
        plan = pl.build_plan([g1, g2], {"k1": a, "k2": a}, {}, self.arch)
        names = [m.target.name for m in plan.moves]
        self.assertEqual(names, ["DJI_0001 (1).MP4", "DJI_0001 (1).SRT",
                                 "DJI_0001 (2).MP4", "DJI_0001 (2).SRT"])

    def test_sanitize(self):
        self.assertEqual(pl.sanitize_component('Mecz: "Resovia" vs Stal?'), "Mecz- -Resovia- vs Stal-")
        self.assertEqual(pl.sanitize_component("  nazwa.  "), "nazwa")
        self.assertEqual(pl.sanitize_component("CON"), "CON_")

    def test_suggestions(self):
        g1 = group("A", captured=datetime(2026, 11, 11, 12))
        g2 = group("B", captured=datetime(2026, 11, 10, 23))
        g3 = group("C", captured=None)
        self.assertEqual(pl.suggest_date([g1, g2, g3]), "2026-11-10")
        self.assertEqual(pl.suggest_date([g3]), "")
        places = {"A": Place("Rzeszów"), "B": Place("Rzeszów"), "C": Place("Tyczyn")}
        self.assertEqual(pl.suggest_town([g1, g2, g3], places), "Rzeszów")

    def test_list_existing(self):
        (self.arch / "Eventy/Rzeszów/2026-11-11 Bieg Niepodległości/WIDEO").mkdir(parents=True)
        (self.arch / "Eventy/Rzeszów/inny folder").mkdir(parents=True)
        (self.arch / "Zlecenia/Łańcut/2026-11-25 Film promocyjny").mkdir(parents=True)
        events = pl.list_existing(self.arch, pl.CATEGORY_EVENT)
        self.assertEqual(events, [pl.ExistingTarget(pl.CATEGORY_EVENT, "Rzeszów", "2026-11-11", "Bieg Niepodległości")])
        orders = pl.list_existing(self.arch, pl.CATEGORY_ORDER)
        self.assertEqual(orders[0].name, "Film promocyjny")
        self.assertEqual(pl.list_existing(self.arch, pl.CATEGORY_HOBBY), [])

    def test_joining_existing_event_targets_same_folder(self):
        existing = self.arch / "Eventy/Rzeszów/2026-11-11 Bieg/WIDEO"
        existing.mkdir(parents=True)
        e = pl.list_existing(self.arch, pl.CATEGORY_EVENT)[0]
        a = pl.Assignment(pl.CATEGORY_EVENT, name=e.name, date=e.date, place=e.place)
        plan = pl.build_plan([group("A")], {"A": a}, {}, self.arch)
        self.assertEqual(plan.moves[0].target.parent, existing)

    def test_check_folders(self):
        self.assertIsNotNone(pl.check_folders("/karta", "/karta/arch"))
        self.assertIsNone(pl.check_folders("/karta", "/dysk/arch"))


if __name__ == "__main__":
    unittest.main()
