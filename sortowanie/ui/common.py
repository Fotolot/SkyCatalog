"""Wspólne elementy interfejsu Qt."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, QObject, Qt, Signal
from PySide6.QtWidgets import (QCalendarWidget, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)

from ..planner import parse_date


def plural(n: int, one: str, few: str, many: str) -> str:
    if n == 1:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def groups_word(n: int) -> str:
    return f"{n} {plural(n, 'grupa', 'grupy', 'grup')}"


def files_word(n: int) -> str:
    return plural(n, "plik", "pliki", "plików")


def materials_word(n: int) -> str:
    return f"{n} {plural(n, 'materiał', 'materiały', 'materiałów')}"


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def plain_label(text: str = "", parent=None, wrap: bool = False, hint: bool = False) -> QLabel:
    """Etykieta wyłącznie tekstowa – niezaufane nazwy plików nie są interpretowane jako HTML."""
    lbl = QLabel(parent)
    lbl.setTextFormat(Qt.TextFormat.PlainText)
    lbl.setText(text)
    lbl.setWordWrap(wrap)
    if hint:
        lbl.setProperty("hint", True)
        lbl.setStyleSheet("color: palette(placeholder-text);")
    return lbl


def menu_text(text: str) -> str:
    """Tekst do QAction/QMenu – „&” nie może tworzyć skrótów klawiszowych."""
    return text.replace("&", "&&")


class Poster(QObject):
    """Przekazywanie zdarzeń z wątków roboczych do wątku interfejsu (połączenie kolejkowane)."""

    event = Signal(object)

    def post(self, ev) -> None:
        self.event.emit(ev)


class CalendarDialog(QDialog):
    def __init__(self, parent: QWidget | None, initial: date | None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Wybierz datę")
        self.calendar = QCalendarWidget(self)
        self.calendar.setGridVisible(True)
        self.calendar.setFirstDayOfWeek(Qt.DayOfWeek.Monday)
        if initial:
            self.calendar.setSelectedDate(QDate(initial.year, initial.month, initial.day))
        self.calendar.activated.connect(lambda _d: self.accept())
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(self.calendar)
        lay.addWidget(buttons)

    def selected(self) -> date:
        d = self.calendar.selectedDate()
        return date(d.year(), d.month(), d.day())


class DateField(QWidget):
    """Pole daty RRRR-MM-DD (może być puste) z przyciskiem kalendarza."""

    changed = Signal(str)

    def __init__(self, parent=None, placeholder: str = "RRRR-MM-DD") -> None:
        super().__init__(parent)
        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText(placeholder)
        self.edit.textChanged.connect(self.changed.emit)
        btn = QPushButton("Kalendarz…", self)
        btn.clicked.connect(self._open)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.edit, 1)
        lay.addWidget(btn)

    def text(self) -> str:
        return self.edit.text()

    def setText(self, text: str) -> None:  # noqa: N802 (konwencja Qt)
        self.edit.setText(text)

    def value(self) -> date | None:
        return parse_date(self.edit.text())

    def _open(self) -> None:
        dlg = CalendarDialog(self, parse_date(self.edit.text()))
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.edit.setText(dlg.selected().strftime("%Y-%m-%d"))
