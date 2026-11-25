"""Formularz Hobby w zakładce Sortowanie: opcjonalna nazwa miejsca dla zaznaczonej grupy
(wymaga PySide6 – inaczej test jest pomijany). Bez sieci: tryb offline."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QApplication = None

from sortowanie import planner as pl
from sortowanie.geo import Place
from sortowanie.media import KIND_PHOTO, KIND_VIDEO, MediaGroup


def group(key, kind=KIND_VIDEO, gps=(50.03, 22.0)):
    ext = ".MP4" if kind == KIND_VIDEO else ".JPG"
    return MediaGroup(key=key, name=key, files=[Path("/karta") / f"{key}{ext}"], kind=kind, gps=gps)


@unittest.skipIf(QApplication is None, "PySide6 niedostępny")
class HobbyFormTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        # baza SQLite może być jeszcze otwarta przy sprzątaniu w Windows
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmp.name)
        env = {"APPDATA": str(root / "roaming"), "LOCALAPPDATA": str(root / "local")}
        self._env = mock.patch.dict(os.environ, env)
        self._env.start()
        from sortowanie import catalog as cat
        from sortowanie.config import Settings
        from sortowanie.ui.sort_tab import SortTab
        settings = Settings(root / "ustawienia.json")
        settings.set("offline", True)
        settings.set("target_root", str(root / "Archiwum"))
        self.tab = SortTab(settings, cat.Catalog(root / "katalog.sqlite"))
        self.tab._card_timer.stop()
        groups = [group("A"), group("B", KIND_PHOTO), group("C")]
        self.tab._on_scanned(groups, None, set())
        for g in groups:                                   # miejscowość „rozpoznana z GPS”
            self.tab.places[g.key] = Place(town="Rzeszów")
        self.tab.scope_all.setChecked(True)
        self.tab.category_boxes[pl.CATEGORY_HOBBY].click()

    def tearDown(self):
        self.tab.deleteLater()
        self._env.stop()
        self._tmp.cleanup()

    def plan(self):
        self.tab._refresh_preview()
        return self.tab.plan

    def select(self, *keys):
        self.tab.tree.clearSelection()
        for k in keys:
            self.tab.items[k].setSelected(True)

    def test_no_object_search_ui(self):
        self.assertFalse(hasattr(self.tab, "poi_requested"))
        self.assertFalse(hasattr(self.tab, "_request_objects"))
        self.assertIn("Pomnik Czynu Rewolucyjnego", self.tab.hobby_place_name.placeholderText())

    def test_empty_name_goes_to_town_folder(self):
        plan = self.plan()
        self.assertTrue(plan.ready, plan.errors)
        self.assertEqual(plan.group_dirs, {"A": Path("Hobby/Rzeszów/WIDEO"), "B": Path("Hobby/Rzeszów/Zdjęcia"),
                                           "C": Path("Hobby/Rzeszów/WIDEO")})

    def test_name_applies_to_whole_selection(self):
        self.tab.hobby_place_name.setText("Pomnik")
        plan = self.plan()
        self.assertEqual(plan.group_dirs, {"A": Path("Hobby/Rzeszów/Pomnik/WIDEO"),
                                           "B": Path("Hobby/Rzeszów/Pomnik/Zdjęcia"),
                                           "C": Path("Hobby/Rzeszów/Pomnik/WIDEO")})
        self.assertIn("Pomnik", self.tab.items["A"].text(5))       # kolumna „Kategoria”

    def test_split_groups_get_different_names(self):
        self.tab.hobby_place_name.setText("Pomnik")
        self.select("C")                                           # zakres: zaznaczone grupy
        self.assertTrue(self.tab.scope_sel.isChecked())
        self.tab._split_selection()
        self.assertEqual(self.tab.hobby_place_name.text(), "")     # nowa część – własna nazwa
        self.tab.hobby_place_name.setText("Bulwary")
        plan = self.plan()
        self.assertTrue(plan.ready, plan.errors)
        self.assertEqual(plan.group_dirs["A"], Path("Hobby/Rzeszów/Pomnik/WIDEO"))
        self.assertEqual(plan.group_dirs["C"], Path("Hobby/Rzeszów/Bulwary/WIDEO"))
        self.select("A")
        self.assertEqual(self.tab.hobby_place_name.text(), "Pomnik")

    def test_groups_without_gps_get_suggestion_and_explanation(self):
        """Zdjęcia bez GPS obok zdjęć z Rzeszowa: przycisk aktywny, wyjaśnia brak miejscowości,
        a podpowiedź „Z GPS pozostałych: Rzeszów” wpisuje miejscowość dopiero po kliknięciu."""
        from PySide6.QtWidgets import QMessageBox
        g = group("D", KIND_PHOTO, gps=None)
        groups = self.tab.groups + [g]
        self.tab._on_scanned(groups, None, set())
        for k in "ABC":
            self.tab.places[k] = Place(town="Rzeszów")
        self.tab.scope_all.setChecked(True)
        self.tab.category_boxes[pl.CATEGORY_HOBBY].click()
        self.assertFalse(self.plan().ready)
        self.assertTrue(self.tab.move_btn.isEnabled())            # klikalny – wyjaśni, czego brakuje
        self.assertIn("brak miejscowości", self.tab.move_btn.toolTip())
        self.assertFalse(self.tab.hobby_town_suggest.isHidden())
        self.assertIn("Rzeszów", self.tab.hobby_town_suggest.text())
        self.assertEqual(self.tab.hobby_town.text(), "")         # nic nie wpisano automatycznie
        shown = []
        with mock.patch.object(QMessageBox, "exec", lambda box: shown.append(box.informativeText())), \
                mock.patch("sortowanie.core.execute_moves") as moves:
            self.tab._move()
        moves.assert_not_called()
        self.assertIn("brak miejscowości", shown[0])
        self.assertEqual(self.tab._groups_without_town(), [g])
        self.tab.hobby_town_suggest.click()
        self.assertEqual(self.tab.hobby_town.text(), "Rzeszów")
        plan = self.plan()
        self.assertTrue(plan.ready, plan.errors)
        self.assertEqual(plan.group_dirs["D"], Path("Hobby/Rzeszów/Zdjęcia"))
        self.assertTrue(self.tab.hobby_town_suggest.isHidden())

    def test_ambiguous_town_choice(self):
        self.tab.places["A"] = Place(town=None, town_candidates=["Wieś G", "Przysiółek H"])
        self.tab._sync_form()
        self.assertFalse(self.plan().ready)
        self.assertFalse(self.tab.decision_box.isHidden())
        self.tab.choice_combo.setCurrentText("Przysiółek H")
        with mock.patch.object(self.tab.decisions, "remember_town") as remember:
            self.tab._apply_decision()
        remember.assert_called_once()
        self.assertEqual(self.plan().group_dirs["A"], Path("Hobby/Przysiółek H/WIDEO"))


@unittest.skipIf(QApplication is None, "PySide6 niedostępny")
class NameConflictFlowTests(unittest.TestCase):
    """Plik o tej samej nazwie w archiwum: pytanie, nadpisanie z kopią zapasową, cofnięcie."""

    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"APPDATA": str(root / "roaming"),
                                                 "LOCALAPPDATA": str(root / "local")})
        self._env.start()
        from sortowanie import catalog as cat
        from sortowanie.config import Settings
        from sortowanie.ui.sort_tab import SortTab
        self.arch = root / "Archiwum"
        self.existing = self.arch / "Hobby" / "Rzeszów" / "Zdjęcia" / "DJI_0001.JPG"
        self.existing.parent.mkdir(parents=True)
        self.existing.write_bytes(b"stare zdjecie")
        self.src = root / "karta" / "DJI_0001.JPG"
        self.src.parent.mkdir()
        self.src.write_bytes(b"nowe zdjecie z karty")
        settings = Settings(root / "ustawienia.json")
        settings.set("offline", True)
        settings.set("target_root", str(self.arch))
        self.tab = SortTab(settings, cat.Catalog(root / "katalog.sqlite"))
        self.tab._card_timer.stop()
        self.tab.mode_copy.setChecked(True)
        g = MediaGroup(key="A", name="DJI_0001", files=[self.src], kind=KIND_PHOTO, gps=(50.03, 22.0))
        self.tab._on_scanned([g], None, set())
        self.tab.places["A"] = Place(town="Rzeszów")
        self.tab.category_boxes[pl.CATEGORY_HOBBY].click()

    def tearDown(self):
        self.tab.deleteLater()
        self._env.stop()
        self._tmp.cleanup()

    def run_move(self, action, answer=True):
        from PySide6.QtWidgets import QMessageBox
        from sortowanie.ui import sort_tab
        asked = []

        def fake_exec(dlg):
            asked.append([c.key for c in dlg.conflicts])
            dlg.set_all(action)
            return 1 if answer else 0
        with mock.patch.object(sort_tab.ConflictDialog, "exec", fake_exec), \
                mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
                mock.patch.object(QMessageBox, "information"), mock.patch.object(QMessageBox, "warning"):
            self.tab._move()
            self.wait()
        return asked

    def wait(self):
        import time
        end = time.monotonic() + 10
        while (self.tab.busy) and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()

    def test_preview_warns_before_import(self):
        self.tab._refresh_preview()
        self.assertEqual([c.key for c in self.tab.plan.unresolved], ["A"])
        self.assertIn("zapytam", self.tab.errors_label.text())

    def test_cancel_changes_nothing(self):
        asked = self.run_move(pl.ACTION_OVERWRITE, answer=False)
        self.assertEqual(asked, [["A"]])
        self.assertEqual(self.existing.read_bytes(), b"stare zdjecie")
        self.assertEqual(len(list(self.existing.parent.iterdir())), 1)

    def test_rename_keeps_both(self):
        self.run_move(pl.ACTION_RENAME)
        self.assertEqual(self.existing.read_bytes(), b"stare zdjecie")
        self.assertEqual((self.existing.parent / "DJI_0001 (1).JPG").read_bytes(), b"nowe zdjecie z karty")

    def test_skip_imports_nothing(self):
        self.run_move(pl.ACTION_SKIP)
        self.assertEqual(sorted(p.name for p in self.existing.parent.iterdir()), ["DJI_0001.JPG"])
        self.assertTrue(self.src.exists())

    def test_overwrite_then_undo(self):
        from PySide6.QtWidgets import QMessageBox
        from sortowanie import core
        self.run_move(pl.ACTION_OVERWRITE)
        self.assertEqual(self.existing.read_bytes(), b"nowe zdjecie z karty")
        backups = list((self.arch / core.BACKUP_FOLDER).rglob("DJI_0001.JPG"))
        self.assertEqual([b.read_bytes() for b in backups], [b"stare zdjecie"])
        self.assertTrue(self.src.exists())                       # karta bez zmian
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
                mock.patch.object(QMessageBox, "warning"):
            self.tab._undo()
            self.wait()
        self.assertEqual(self.existing.read_bytes(), b"stare zdjecie")
        self.assertFalse((self.arch / core.BACKUP_FOLDER).exists())
        self.assertIsNotNone(self.tab.catalog.material_id_for_path(str(self.existing)))


if __name__ == "__main__":
    unittest.main()
