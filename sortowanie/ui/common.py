"""Wspólne elementy interfejsu Qt."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, QObject, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtWidgets import (QCalendarWidget, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QLayout,
                               QLineEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter, QVBoxLayout, QWidget)

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


SPLITTER_PREFIX = "split_"


def splitter(orientation: Qt.Orientation, name: str, *widgets: QWidget, sizes: list[int] | None = None,
             stretch: list[int] | None = None, parent: QWidget | None = None) -> QSplitter:
    """Rozdzielacz z uchwytem do przeciągania. Nazwa służy do zapamiętania układu między uruchomieniami."""
    s = QSplitter(orientation, parent)
    s.setObjectName(SPLITTER_PREFIX + name)
    s.setChildrenCollapsible(False)          # panelu nie da się „zgubić” przeciągnięciem do krawędzi
    s.setHandleWidth(7)
    s.setOpaqueResize(True)
    for w in widgets:
        s.addWidget(w)
    for i, f in enumerate(stretch or []):
        s.setStretchFactor(i, f)
    if sizes:
        s.setSizes(sizes)
    return s


def scrollable(widget: QWidget, parent: QWidget | None = None) -> QScrollArea:
    """Panel z paskami przewijania – w wąskim lub niskim oknie zawartość się nie ucina."""
    area = QScrollArea(parent)
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setMinimumSize(80, 60)
    return area


class FlowLayout(QLayout):
    """Układ „płynący”: elementy (np. przyciski) przechodzą do kolejnego wiersza, gdy brakuje szerokości."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i):  # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):  # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._layout(QRect(0, 0, width, 0), dry=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, dry=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        w = sum(i.sizeHint().width() for i in self._items) + self._spacing * max(0, len(self._items) - 1)
        h = max((i.sizeHint().height() for i in self._items), default=0)
        return QSize(w, h)

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for i in self._items:
            size = size.expandedTo(i.minimumSize())
        return size

    def _layout(self, rect: QRect, dry: bool) -> int:
        x, y, line_h = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > rect.right() + 1 and line_h > 0:
                x, y = rect.x(), y + line_h + self._spacing
                line_h = 0
            if not dry:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


def flow(*widgets: QWidget, parent: QWidget | None = None) -> QWidget:
    """Kontener z elementami w układzie płynącym."""
    box = QWidget(parent)
    lay = FlowLayout(box)
    for w in widgets:
        lay.addWidget(w)
    box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    return box


def shrinkable(label: QLabel) -> QLabel:
    """Długi tekst zawija się i nie wymusza minimalnej szerokości okna."""
    label.setWordWrap(True)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    label.setMinimumWidth(40)
    return label


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
