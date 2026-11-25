"""Główne okno: zakładki „Sortowanie” i „Mapa materiałów” (PySide6)."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QPalette
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QStyleFactory, QTabWidget

from .. import APP_NAME, __version__
from .. import catalog as cat
from ..config import Settings
from .dialogs import SettingsDialog


def apply_theme(app: QApplication, mode: str) -> bool:
    """Ustawia motyw („systemowy” / „jasny” / „ciemny”). Zwraca True dla ciemnego."""
    if mode == "systemowy":
        app.setStyle(QStyleFactory.create("windowsvista") or QStyleFactory.create("Fusion"))
        app.setPalette(app.style().standardPalette())
        hints = app.styleHints()
        try:
            return hints.colorScheme() == Qt.ColorScheme.Dark
        except AttributeError:  # pragma: no cover – starsze Qt
            return app.palette().color(QPalette.ColorRole.Window).lightness() < 128
    app.setStyle(QStyleFactory.create("Fusion"))
    if mode == "ciemny":
        p = QPalette()
        base, alt, text = QColor(30, 31, 34), QColor(43, 45, 49), QColor(230, 230, 230)
        for role, color in ((QPalette.ColorRole.Window, alt), (QPalette.ColorRole.WindowText, text),
                            (QPalette.ColorRole.Base, base), (QPalette.ColorRole.AlternateBase, alt),
                            (QPalette.ColorRole.ToolTipBase, alt), (QPalette.ColorRole.ToolTipText, text),
                            (QPalette.ColorRole.Text, text), (QPalette.ColorRole.Button, alt),
                            (QPalette.ColorRole.ButtonText, text), (QPalette.ColorRole.Link, QColor(138, 180, 248)),
                            (QPalette.ColorRole.Highlight, QColor(53, 132, 228)),
                            (QPalette.ColorRole.HighlightedText, QColor(255, 255, 255)),
                            (QPalette.ColorRole.PlaceholderText, QColor(150, 150, 150))):
            p.setColor(role, color)
        p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(120, 120, 120))
        p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(120, 120, 120))
        app.setPalette(p)
        return True
    app.setPalette(app.style().standardPalette())
    return False


class MainWindow(QMainWindow):
    def __init__(self, app: QApplication, settings: Settings) -> None:
        super().__init__()
        from .map_tab import MapTab          # import po utworzeniu QApplication (QtWebEngine)
        from .sort_tab import SortTab
        self.app = app
        self.settings = settings
        self.setWindowTitle(f"{APP_NAME} {__version__} – archiwizacja i mapa materiałów z lotów")
        self.dark = apply_theme(app, settings.get("theme") or "systemowy")
        self.catalog = cat.Catalog()
        self.sort_tab = SortTab(settings, self.catalog, self)
        self.map_tab = MapTab(self.catalog, settings, self.dark, self)
        self.sort_tab.catalogChanged.connect(self.map_tab.refresh_after_change)
        self.tabs = QTabWidget(self)
        self.tabs.addTab(self.sort_tab, "Sortowanie")
        self.tabs.addTab(self.map_tab, "Mapa materiałów")
        self.setCentralWidget(self.tabs)

        menu = self.menuBar().addMenu("Aplikacja")
        act = QAction("Ustawienia…", self)
        act.triggered.connect(self.open_settings)
        menu.addAction(act)
        quit_act = QAction("Zamknij", self)
        quit_act.triggered.connect(self.close)
        menu.addAction(quit_act)
        self._fit_to_screen()

    def _fit_to_screen(self) -> None:
        screen = self.screen().availableGeometry()
        w, h = min(1500, screen.width() - 40), min(900, screen.height() - 60)
        self.resize(w, h)
        self.move(screen.x() + (screen.width() - w) // 2, screen.y() + max(0, (screen.height() - h) // 3))
        if screen.width() < 1280 or screen.height() < 800:
            self.showMaximized()

    def open_settings(self) -> None:
        st = self.sort_tab
        dlg = SettingsDialog(self, self.settings, st.cache.summary(), len(st.decisions),
                             st.cache.clear, st.decisions.clear)
        if dlg.exec():
            st.geo.reset_network_state()
            st._update_geo_status()
            self.dark = apply_theme(self.app, self.settings.get("theme") or "systemowy")
            self.map_tab.set_theme(self.dark)
            QMessageBox.information(self, "Ustawienia", "Zapisano. Zmiana dostawcy podkładu mapy zadziała po "
                                                        "ponownym uruchomieniu aplikacji.")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.sort_tab.busy and QMessageBox.question(
                self, APP_NAME, "Trwa operacja na plikach. Zamknąć mimo to?") != QMessageBox.StandardButton.Yes:
            event.ignore()
            return
        self.sort_tab.save_state()
        self.settings.save()
        self.map_tab.shutdown()
        super().closeEvent(event)


def main(argv: list[str] | None = None) -> int:
    """``SkyCatalog_1.01.exe [folder_importu] [--mapa] [--skanuj]``"""
    argv = list(sys.argv if argv is None else argv)
    from .webmap import register_scheme
    register_scheme()                                  # przed utworzeniem QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    settings = Settings()
    win = MainWindow(app, settings)
    win.show()
    args = [a for a in argv[1:] if not a.startswith("-")]
    if "--mapa" in argv:
        win.tabs.setCurrentWidget(win.map_tab)
    if args and Path(args[0]).is_dir():
        win.sort_tab.source.setText(str(Path(args[0])))
        QTimer.singleShot(300, win.sort_tab.load_source)
    if "--skanuj" in argv:
        QTimer.singleShot(800, win.map_tab.start_scan)
    return app.exec()
