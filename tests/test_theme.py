"""Zmiana motywu w trakcie pracy programu (wymaga PySide6 – inaczej test jest pomijany)."""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QGroupBox, QMainWindow, QPushButton, QTabWidget, QVBoxLayout

    from sortowanie.ui.app import apply_theme
except ImportError:  # pragma: no cover
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 niedostępny")
class ThemeSwitchTests(unittest.TestCase):
    def test_switch_while_running_repaints_existing_widgets(self):
        app = QApplication.instance() or QApplication([])
        apply_theme(app, "jasny")
        win = QMainWindow()
        tabs = QTabWidget()
        box = QGroupBox("Panel")
        QVBoxLayout(box).addWidget(QPushButton("Przycisk"))
        tabs.addTab(box, "Zakładka")
        win.setCentralWidget(tabs)
        win.resize(300, 200)
        win.show()
        app.processEvents()
        try:
            self.assertTrue(apply_theme(app, "ciemny"))
            app.processEvents()
            self.assertLess(box.palette().window().color().lightness(), 100)
            self.assertFalse(apply_theme(app, "jasny"))
            app.processEvents()
            self.assertGreater(box.palette().window().color().lightness(), 180)
        finally:
            win.close()
            apply_theme(app, "jasny")


if __name__ == "__main__":
    unittest.main()
