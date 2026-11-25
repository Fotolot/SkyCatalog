"""Lista materiałów z jednego miejsca na mapie: podgląd po najechaniu, bez blokowania okna."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication, QWidget

    from sortowanie.ui.place_list import PlaceList
except ImportError:  # pragma: no cover
    QApplication = None

HITS = [{"id": 1, "kind": "zdjęcie", "name": "DJI_0001.JPG", "on_track": False},
        {"id": 2, "kind": "wideo", "name": "DJI_0002.MP4", "on_track": True},
        {"id": 3, "kind": "zdjęcie", "name": "<b>dziwna</b> & nazwa.JPG", "on_track": False}]


@unittest.skipIf(QApplication is None, "PySide6 niedostępny")
class PlaceListTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.parent = QWidget()
        self.parent.resize(800, 600)
        self.parent.show()
        self.pl = PlaceList(self.parent)
        self.hovered, self.chosen, self.closed = [], [], []
        self.pl.hovered.connect(self.hovered.append)
        self.pl.chosen.connect(self.chosen.append)
        self.pl.closed.connect(lambda: self.closed.append(True))
        self.pl.show_items(HITS, QPoint(780, 590))

    def tearDown(self):
        self.parent.deleteLater()

    def wait_hover(self):
        self.pl._timer.timeout.emit()
        self.app.processEvents()

    def test_shown_inside_parent_and_not_modal(self):
        self.assertTrue(self.pl.isVisible())
        self.assertFalse(self.pl.isWindow())                  # dziecko okna, nie modalne menu
        geo = self.pl.geometry()
        self.assertTrue(self.parent.rect().contains(geo), geo)
        self.assertEqual(self.pl.ids(), [1, 2, 3])
        self.assertIn("3", self.pl.title.text())

    def test_hover_updates_preview_without_click(self):
        self.pl._on_entered(self.pl.list.item(1))
        self.wait_hover()
        self.assertEqual(self.hovered, [2])
        self.pl._on_entered(self.pl.list.item(1))           # ten sam – bez ponownego wczytania
        self.wait_hover()
        self.assertEqual(self.hovered, [2])
        self.pl._on_entered(self.pl.list.item(0))
        self.wait_hover()
        self.assertEqual(self.hovered, [2, 1])
        self.assertEqual(self.chosen, [])
        self.assertTrue(self.pl.isVisible())                  # lista zostaje otwarta

    def test_click_and_close(self):
        self.pl._on_clicked(self.pl.list.item(2))
        self.assertEqual(self.chosen, [3])
        self.assertTrue(self.pl.isVisible())
        self.pl.dismiss()
        self.assertFalse(self.pl.isVisible())
        self.assertEqual(self.closed, [True])

    def test_names_are_plain_text(self):
        self.assertEqual(self.pl.list.item(2).text(), "📷 <b>dziwna</b> & nazwa.JPG")
        self.assertIn("(trasa)", self.pl.list.item(1).text())


if __name__ == "__main__":
    unittest.main()
