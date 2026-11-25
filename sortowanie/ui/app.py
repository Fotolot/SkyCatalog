"""Główne okno: zakładki „Sortowanie” i „Mapa materiałów” (PySide6)."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QSplitter, QStyleFactory, QTabWidget

from .. import APP_NAME, __version__
from .. import catalog as cat
from ..config import Settings
from .common import SPLITTER_PREFIX
from .dialogs import SettingsDialog

# Uchwyty rozdzielaczy: dobrze widoczne i podświetlane – „złap i przeciągnij”
SPLITTER_STYLE = """
QSplitter::handle { background: palette(midlight); }
QSplitter::handle:horizontal { width: 7px; margin: 0 1px; border-left: 1px solid palette(mid);
                               border-right: 1px solid palette(mid); }
QSplitter::handle:vertical { height: 7px; margin: 1px 0; border-top: 1px solid palette(mid);
                             border-bottom: 1px solid palette(mid); }
QSplitter::handle:hover, QSplitter::handle:pressed { background: palette(highlight); }
"""


def apply_theme(app: QApplication, mode: str) -> bool:
    """Ustawia motyw („systemowy” / „jasny” / „ciemny”). Zwraca True dla ciemnego."""
    app.setStyleSheet(SPLITTER_STYLE)
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
        view = self.menuBar().addMenu("Widok")
        reset = QAction("Przywróć domyślny układ paneli", self)
        reset.triggered.connect(self.reset_layout)
        view.addAction(reset)
        view.addAction(QAction("Panele zmieniasz, przeciągając uchwyty między nimi", self, enabled=False))
        self.setMinimumSize(640, 420)
        self._default_layout = self._splitter_states()
        if not self._restore_layout():
            self._fit_to_screen()

    # ── Układ okna (zapamiętywany między uruchomieniami) ──
    def _splitters(self) -> list[QSplitter]:
        return [s for s in self.findChildren(QSplitter) if s.objectName().startswith(SPLITTER_PREFIX)]

    def _splitter_states(self) -> dict[str, QByteArray]:
        return {s.objectName(): s.saveState() for s in self._splitters()}

    def _restore_layout(self) -> bool:
        state = self.settings.get("ui_state") or {}
        if not isinstance(state, dict) or not state:
            return False
        geometry = state.get("okno")
        restored = False
        if isinstance(geometry, str) and geometry:
            restored = self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
        for s in self._splitters():
            value = state.get(s.objectName())
            if isinstance(value, str) and value:
                if s.restoreState(QByteArray.fromBase64(value.encode("ascii"))) and s.objectName() == "split_map_columns":
                    self.map_tab._sized = True        # nie nadpisuj układu użytkownika domyślnymi proporcjami
        return restored

    def _save_layout(self) -> None:
        state = {name: bytes(value.toBase64()).decode("ascii") for name, value in self._splitter_states().items()}
        state["okno"] = bytes(self.saveGeometry().toBase64()).decode("ascii")
        self.settings.set("ui_state", state)

    def reset_layout(self) -> None:
        for s in self._splitters():
            default = self._default_layout.get(s.objectName())
            if default is not None:
                s.restoreState(default)
        w = self.map_tab._split.width()
        if w > 200:
            self.map_tab._split.setSizes([int(w * 0.24), int(w * 0.50), w - int(w * 0.24) - int(w * 0.50)])
        self.settings.set("ui_state", {})
        if not self.isMaximized():
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
        self._save_layout()
        self.settings.save()
        self.map_tab.shutdown()
        super().closeEvent(event)


ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "ikona.png"


def _windows_app_id() -> None:
    """Własny identyfikator aplikacji – pasek zadań Windows pokazuje ikonę SkyCatalog."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"{APP_NAME}.{__version__}")
        except (AttributeError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    """``SkyCatalog_1.01.exe [folder_importu] [--mapa] [--skanuj]``"""
    argv = list(sys.argv if argv is None else argv)
    from .webmap import register_scheme
    register_scheme()                                  # przed utworzeniem QApplication
    _windows_app_id()
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
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
