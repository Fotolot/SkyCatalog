"""Główne okno: zakładki „Sortowanie” i „Mapa materiałów” (PySide6)."""

from __future__ import annotations

import os
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
        about = QAction("O programie…", self)
        about.triggered.connect(self.show_about)
        menu.addAction(about)
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

    # ── Aktualizacje i „O programie” ──
    def check_updates(self, manual: bool = False) -> None:
        from .update_dialog import UpdateChecker
        self._checker = UpdateChecker(self)
        self._checker.found.connect(lambda release: self._on_update_found(release, manual))
        self._checker.start()

    def _on_update_found(self, release, manual: bool) -> None:
        from .. import updater
        if release is None or not updater.is_newer(release.tag):
            if manual:
                QMessageBox.information(self, APP_NAME, f"Masz aktualną wersję {__version__}." if release else
                                        "Nie udało się sprawdzić aktualizacji (brak internetu lub błąd serwera).")
            return
        from .update_dialog import UpdateDialog, run_update
        if UpdateDialog(self, release).exec():
            if self.sort_tab.busy:
                QMessageBox.information(self, APP_NAME, "Poczekaj na zakończenie trwającej operacji na plikach "
                                                        "i spróbuj ponownie (O programie → Sprawdź aktualizacje).")
                return
            run_update(self, release, self._quit_for_update)

    def _quit_for_update(self) -> None:
        self.close()                      # zapis ustawień i układu, zatrzymanie skanowania
        self.app.quit()

    def show_about(self) -> None:
        from .update_dialog import AboutDialog
        AboutDialog(self, lambda: self.check_updates(manual=True)).exec()

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


def _load_qt_translations(app: QApplication) -> None:
    """Polskie napisy standardowych przycisków i okien Qt (Tak/Nie/Anuluj/Zamknij…)."""
    from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
    path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    app._translators = []
    for name in ("qtbase", "qtmultimedia", "qtwebengine"):
        tr = QTranslator(app)
        if tr.load(QLocale(QLocale.Language.Polish), name, "_", path):
            app.installTranslator(tr)
            app._translators.append(tr)


def main(argv: list[str] | None = None) -> int:
    """``SkyCatalog_1.02.exe [folder_importu] [--mapa] [--skanuj]``"""
    argv = list(sys.argv if argv is None else argv)
    from .webmap import register_scheme
    register_scheme()                                  # przed utworzeniem QApplication
    _windows_app_id()
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    _load_qt_translations(app)
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
    selftest = os.environ.get("SKYCATALOG_SELFTEST")
    if selftest:
        _run_selftest(app, win, Path(selftest))
    else:
        from .. import updater
        if updater.FAILED_FLAG in argv:
            from .update_dialog import show_failed_update_notice
            QTimer.singleShot(500, lambda: show_failed_update_notice(win))
        QTimer.singleShot(4000, win.check_updates)        # w tle; błędy nie blokują programu
    return app.exec()


def _run_selftest(app: QApplication, win: "MainWindow", shot: Path) -> None:
    """Samotest zbudowanego programu (CI): mapa Leaflet musi się załadować; zrzut + wynik w JSON."""
    import json
    import time
    win.tabs.setCurrentWidget(win.map_tab)
    started = time.monotonic()

    def check() -> None:
        ready = win.map_tab.map.ready
        if not ready and time.monotonic() - started < 60:
            QTimer.singleShot(500, check)
            return

        def finish() -> None:
            win.grab().save(str(shot))
            shot.with_suffix(".json").write_text(json.dumps(
                {"wersja": __version__, "mapa_gotowa": ready, "sekundy": round(time.monotonic() - started, 1)}),
                encoding="utf-8")
            app.exit(0 if ready else 3)
        QTimer.singleShot(3000 if ready else 0, finish)
    QTimer.singleShot(500, check)
