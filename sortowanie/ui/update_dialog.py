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
    """Sprawdzanie w tle – ``found`` zawsze dostaje ``updater.CheckResult`` (wydanie albo opis błędu)."""

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


def run_update(parent, release: updater.Release, install_and_restart) -> None:
    """Pobiera i sprawdza wydanie w tle, potem pyta „Zainstaluj i uruchom ponownie”.

    ``install_and_restart(prepared) -> bool`` – bezpiecznie kończy pracę programu i uruchamia
    proces aktualizujący (MainWindow.install_update).
    """
    if not updater.is_frozen():
        QMessageBox.information(parent, APP_NAME, "Program uruchomiono ze źródeł – automatyczna aktualizacja "
                                                  "działa tylko w wersji przenośnej (.exe).")
        return
    dlg = QProgressDialog("Pobieranie aktualizacji…", "Anuluj", 0, 100, parent)
    dlg.setWindowTitle(APP_NAME)
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.setMinimumDuration(0)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    dlg.setValue(0)
    sig = _Progress(dlg)
    state = {"cancelled": False, "finished": False}

    def on_cancel() -> None:
        # QProgressDialog wysyła „canceled” także przy zamykaniu – po zakończeniu pobierania to nie anulowanie
        if not state["finished"]:
            state["cancelled"] = True

    dlg.canceled.connect(on_cancel)

    def on_step(done: int, total: int) -> None:
        dlg.setMaximum(max(total, 1))
        dlg.setValue(min(done, max(total, 1) - 1))       # 100% dopiero po weryfikacji
        dlg.setLabelText(f"Pobieranie aktualizacji… {done / 1048576:.0f} z {total / 1048576:.0f} MB")

    def on_done(prepared, error) -> None:
        state["finished"] = True
        dlg.close()
        if prepared is None:
            if state["cancelled"]:
                updater.stage_log("Pobieranie anulowane przez użytkownika")
                return
            updater.stage_log(f"BŁĄD przygotowania aktualizacji: {error}")
            _show_error(parent, "Aktualizacja nie powiodła się – program działa dalej w bieżącej wersji.", str(error))
            return
        ready = ReadyDialog(parent, prepared)
        if ready.exec():
            install_and_restart(prepared)
        else:
            updater.stage_log("Instalacja odłożona („Później”)")

    sig.step.connect(on_step)
    sig.done.connect(on_done)

    def progress(done: int, total: int) -> None:
        sig.step.emit(done, total)

    def work() -> None:
        try:
            prepared = updater.prepare_update(release, progress, cancelled=lambda: state["cancelled"])
            sig.done.emit(prepared, None)
        except Exception as exc:          # błąd nie może zatrzymać programu
            sig.done.emit(None, exc)
    threading.Thread(target=work, daemon=True).start()


class ReadyDialog(QDialog):
    """„Aktualizacja gotowa” – paczka pobrana i sprawdzona; instalacja dopiero na żądanie."""

    def __init__(self, parent, prepared: updater.PreparedUpdate) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} – aktualizacja gotowa")
        title = QLabel(f"<b>Aktualizacja gotowa: wersja {_esc(prepared.release.version)}</b>")
        title.setTextFormat(Qt.TextFormat.RichText)
        need_admin = not updater.can_write(updater.app_dir())
        info = QLabel(
            "Nowa wersja została pobrana i sprawdzona.\n\n"
            "„Zainstaluj i uruchom ponownie” zapisze ustawienia, zamknie program, podmieni pliki w tym samym "
            "folderze (z kopią zapasową w _backup) i uruchomi nową wersję. Ustawienia, katalog materiałów "
            "i Twoje pliki pozostaną bez zmian."
            + ("\n\nFolder programu wymaga uprawnień administratora – Windows poprosi o zgodę (UAC). "
               "Bez zgody program uruchomi się ponownie w obecnej wersji." if need_admin else ""))
        info.setTextFormat(Qt.TextFormat.PlainText)
        info.setWordWrap(True)
        self.btn_install = QPushButton("Zainstaluj i uruchom ponownie")
        self.btn_install.setDefault(True)
        self.btn_install.clicked.connect(self.accept)
        later = QPushButton("Później")
        later.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(later)
        row.addWidget(self.btn_install)
        lay = QVBoxLayout(self)
        lay.addWidget(title)
        lay.addWidget(info)
        lay.addLayout(row)
        self.resize(520, 220)


def _show_error(parent, text: str, details: str) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(APP_NAME)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    box.setInformativeText(f"{details}\n\nDziennik: {updater.log_path()}")
    log_btn = box.addButton("Otwórz log", QMessageBox.ButtonRole.ActionRole)
    box.addButton("Zamknij", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    if box.clickedButton() is log_btn:
        open_log(parent)


STATUS_TEXT = {
    "uac_anulowano": "Anulowano zgodę administratora (UAC) – aktualizacja nie została zainstalowana.",
    "brak_uprawnien": "Brak uprawnień do zapisu w folderze programu.",
    "pliki_zablokowane": "Pliki programu były używane przez inny proces.",
    "blad_kopii": "Nie udało się utworzyć kopii zapasowej – nic nie zmieniono.",
    "blad_instalacji": "Błąd podczas kopiowania nowej wersji – przywrócono poprzednią.",
    "zla_wersja": "Zainstalowany plik nie raportował nowej wersji – przywrócono poprzednią.",
    "nowa_wersja_nie_dziala": "Nowa wersja nie uruchomiła się – przywrócono poprzednią.",
    "brak_potwierdzenia": "Nowa wersja nie potwierdziła uruchomienia.",
}


def show_update_outcome(parent, outcome: updater.UpdateOutcome, status: str = "") -> None:
    """Komunikat po ponownym uruchomieniu (nowa wersja albo przywrócona poprzednia)."""
    if outcome.ok:
        text = f"Zaktualizowano do wersji {outcome.target}."
        if outcome.stale_shortcuts:
            text += ("\n\nUwaga: te skróty wskazują inną kopię programu (nie zaktualizowaną):\n"
                     + "\n".join(f"• {s}" for s in outcome.stale_shortcuts[:6]))
            _show_error(parent, text, "Popraw skróty, aby uruchamiać zaktualizowany program.")
            return
        QMessageBox.information(parent, APP_NAME, text)
        return
    _show_error(parent, f"Aktualizacja do wersji {outcome.target} nie powiodła się – działa poprzednia wersja.",
                STATUS_TEXT.get(status, "") + ("\n" if status in STATUS_TEXT else "") + outcome.message)


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


def open_log(parent=None) -> None:
    """Otwiera dziennik aktualizacji w domyślnym programie (bez poleceń powłoki)."""
    path = updater.log_path()
    target = path if path.exists() else path.parent
    if not target.exists() or not QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))):
        QMessageBox.information(parent, APP_NAME, f"Dziennik aktualizacji:\n{path}")


def show_check_problem(parent, result: updater.CheckResult) -> None:
    """Okno z przyczyną nieudanego sprawdzenia aktualizacji i przyciskiem „Otwórz log”."""
    box = QMessageBox(parent)
    box.setWindowTitle(f"{APP_NAME} – aktualizacje")
    box.setIcon(QMessageBox.Icon.Warning)
    box.setTextFormat(Qt.TextFormat.PlainText)
    if result.status == updater.STATUS_INVALID:
        box.setText("Serwer odpowiedział, ale najnowsze wydanie nie nadaje się do aktualizacji.")
        box.setInformativeText(f"{result.error}\n\nSzczegóły zapisano w dzienniku:\n{updater.log_path()}")
    else:
        box.setText("Nie udało się sprawdzić aktualizacji.")
        box.setInformativeText(f"{result.error}\n\nSzczegóły (z pełnym opisem błędu) zapisano w dzienniku:\n"
                               f"{updater.log_path()}")
    log_btn = box.addButton("Otwórz log", QMessageBox.ButtonRole.ActionRole)
    page_btn = None
    if result.status == updater.STATUS_INVALID:
        page_btn = box.addButton("Strona wydań", QMessageBox.ButtonRole.ActionRole)
    box.addButton("Zamknij", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    if box.clickedButton() is log_btn:
        open_log(parent)
    elif page_btn is not None and box.clickedButton() is page_btn:
        url = result.release.page_url if result.release else updater.RELEASES_URL
        QDesktopServices.openUrl(QUrl(url))


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def show_failed_update_notice(parent) -> None:
    folders = sorted(Path(updater.tempfile.gettempdir()).glob(f"{APP_NAME}-aktualizacja-*"),
                     key=lambda p: p.stat().st_mtime if p.exists() else 0)
    where = str(folders[-1] / "aktualizacja.log") if folders else "%TEMP%\\SkyCatalog-aktualizacja-*\\aktualizacja.log"
    QMessageBox.warning(parent, APP_NAME, "Ostatnia aktualizacja nie powiodła się – przywrócono poprzednią "
                                          f"wersję programu (kopia z folderu _backup).\n\nSzczegóły:\n{where}")


