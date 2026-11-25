"""Okna aktualizacji i „O programie” (Qt)."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QMessageBox,
                               QProgressDialog, QPushButton, QTextBrowser, QVBoxLayout)

from .. import APP_NAME, __version__, updater
from ..config import app_data_dir, local_data_dir


class UpdateChecker(QObject):
    """Sprawdzanie w tle – brak internetu lub błąd API kończy się po cichu (``found`` z None)."""

    found = Signal(object)

    def start(self) -> None:
        threading.Thread(target=lambda: self.found.emit(updater.check_latest()), daemon=True).start()


class _Progress(QObject):
    step = Signal(int, int)
    done = Signal(object, object)          # (script, new_dir) albo (None, błąd)


class UpdateDialog(QDialog):
    def __init__(self, parent, release: updater.Release) -> None:
        super().__init__(parent)
        self.release = release
        self.setWindowTitle(f"{APP_NAME} – aktualizacja")
        title = QLabel(f"<b>Dostępna wersja {release.version}</b>")
        title.setTextFormat(Qt.TextFormat.RichText)
        info = QLabel(f"Zainstalowana wersja: {__version__}")
        info.setTextFormat(Qt.TextFormat.PlainText)
        notes = QTextBrowser(self)
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(release.notes or "_Brak opisu zmian._")
        notes.setMinimumSize(460, 220)
        self.btn_update = QPushButton("Aktualizuj")
        self.btn_later = QPushButton("Później")
        self.btn_update.setDefault(True)
        self.btn_update.clicked.connect(self.accept)
        self.btn_later.clicked.connect(self.reject)
        page = QPushButton("Strona wydania")
        page.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(release.page_url)))
        hint = QLabel()
        hint.setTextFormat(Qt.TextFormat.PlainText)
        hint.setWordWrap(True)
        if not updater.is_frozen():
            self.btn_update.setEnabled(False)
            hint.setText("Program uruchomiono ze źródeł – automatyczna aktualizacja działa tylko w wersji "
                         "przenośnej (.exe). Pobierz nową wersję ze strony wydania.")
        elif not release.asset_url:
            self.btn_update.setEnabled(False)
            hint.setText(f"Wydanie nie zawiera pliku {updater.RELEASE_ASSET}.")
        else:
            hint.setText("Aktualizacja pobierze nową wersję, zamknie program, zrobi kopię zapasową bieżącej "
                         "wersji (folder _backup) i uruchomi program ponownie. Ustawienia, katalog materiałów "
                         "i inne dane pozostaną bez zmian.")
        buttons = QHBoxLayout()
        buttons.addWidget(page)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_later)
        buttons.addWidget(self.btn_update)
        lay = QVBoxLayout(self)
        lay.addWidget(title)
        lay.addWidget(info)
        lay.addWidget(QLabel("Zmiany:"))
        lay.addWidget(notes, 1)
        lay.addWidget(hint)
        lay.addLayout(buttons)


def run_update(parent, release: updater.Release, quit_app) -> None:
    """Pobiera i rozpakowuje wydanie (w tle), uruchamia skrypt i zamyka program."""
    target = updater.app_dir()
    if not updater.can_write(target):
        QMessageBox.warning(parent, APP_NAME, f"Brak uprawnień do zapisu w folderze programu:\n{target}\n\n"
                                              "Przenieś folder programu w miejsce z prawem zapisu (np. Dokumenty) "
                                              "albo pobierz nową wersję ręcznie.")
        return
    dlg = QProgressDialog("Pobieranie aktualizacji…", "Anuluj", 0, 100, parent)
    dlg.setWindowTitle(APP_NAME)
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.setMinimumDuration(0)
    dlg.setValue(0)
    sig = _Progress(dlg)
    cancelled = {"v": False}
    dlg.canceled.connect(lambda: cancelled.update(v=True))

    def on_step(done: int, total: int) -> None:
        dlg.setMaximum(max(total, 1))
        dlg.setValue(min(done, max(total, 1)))
        dlg.setLabelText(f"Pobieranie aktualizacji… {done / 1048576:.0f} z {total / 1048576:.0f} MB")

    def on_done(script, result) -> None:
        dlg.close()
        if cancelled["v"]:
            return
        if script is None:
            QMessageBox.warning(parent, APP_NAME, f"Aktualizacja nie powiodła się:\n{result}\n\n"
                                                  "Program działa dalej w bieżącej wersji.")
            return
        try:
            updater.launch_update_script(script, result, target)
        except (OSError, updater.UpdateError) as exc:
            QMessageBox.warning(parent, APP_NAME, f"Nie można uruchomić aktualizacji:\n{exc}")
            return
        quit_app()

    sig.step.connect(on_step)
    sig.done.connect(on_done)

    def progress(done: int, total: int) -> None:
        if cancelled["v"]:
            raise updater.UpdateError("Anulowano pobieranie.")
        sig.step.emit(done, total)

    def work() -> None:
        try:
            script, new_dir = updater.prepare_update(release, progress)
            sig.done.emit(script, new_dir)
        except Exception as exc:          # błąd nie może zatrzymać programu
            sig.done.emit(None, str(exc))
    threading.Thread(target=work, daemon=True).start()


class AboutDialog(QDialog):
    def __init__(self, parent, on_check) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"O programie {APP_NAME}")
        mode = "wersja przenośna" if updater.is_frozen() else "uruchomiono ze źródeł"
        text = QLabel(
            f"<h3>{APP_NAME}</h3>"
            f"<p><b>Wersja {__version__}</b> ({mode})</p>"
            f"<p>Archiwizacja i mapa materiałów z lotów.</p>"
            f"<p>Folder programu: {_esc(str(updater.app_dir()))}<br>"
            f"Ustawienia: {_esc(str(app_data_dir()))}<br>"
            f"Katalog materiałów i cache: {_esc(str(local_data_dir()))}</p>"
            f'<p><a href="{updater.RELEASES_URL}">Wydania na GitHubie</a> · '
            f'Dane map: <a href="https://www.openstreetmap.org/copyright">© OpenStreetMap contributors</a></p>')
        text.setTextFormat(Qt.TextFormat.RichText)
        text.setOpenExternalLinks(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        text.setWordWrap(True)
        check = QPushButton("Sprawdź aktualizacje")
        check.clicked.connect(on_check)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        box.addButton(check, QDialogButtonBox.ButtonRole.ActionRole)
        lay = QVBoxLayout(self)
        lay.addWidget(text)
        lay.addWidget(box)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def show_failed_update_notice(parent) -> None:
    folders = sorted(Path(updater.tempfile.gettempdir()).glob(f"{APP_NAME}-aktualizacja-*"),
                     key=lambda p: p.stat().st_mtime if p.exists() else 0)
    where = str(folders[-1] / "aktualizacja.log") if folders else "%TEMP%\\SkyCatalog-aktualizacja-*\\aktualizacja.log"
    QMessageBox.warning(parent, APP_NAME, "Ostatnia aktualizacja nie powiodła się – przywrócono poprzednią "
                                          f"wersję programu (kopia z folderu _backup).\n\nSzczegóły:\n{where}")


