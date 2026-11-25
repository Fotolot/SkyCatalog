"""Lista materiałów z jednego miejsca na mapie (kilka nakładających się punktów).

Zastępuje modalne menu: nie blokuje okna, więc podczas przeglądania listy można od razu
używać panelu podglądu (np. odtworzyć lub zatrzymać film). Najechanie na pozycję pokazuje ją
w panelu podglądu, kliknięcie dodatkowo zaznacza ją na liście materiałów. Nazwy plików są
zwykłym tekstem (nigdy HTML).
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QListWidget, QListWidgetItem, QToolButton, QVBoxLayout,
                               QWidget)

from .. import catalog as cat
from .common import materials_word, plain_label

HOVER_DELAY_MS = 120
ROLE_ID = Qt.ItemDataRole.UserRole


class PlaceList(QFrame):
    hovered = Signal(int)          # najechano na materiał – pokaż w podglądzie
    chosen = Signal(int)           # kliknięto materiał
    closed = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("placeList")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAutoFillBackground(True)
        self.setStyleSheet("#placeList { background: palette(window); border: 1px solid palette(mid); "
                           "border-radius: 4px; }")
        self.title = plain_label("", self)
        close = QToolButton(self)
        close.setText("✕")
        close.setToolTip("Zamknij listę (Esc)")
        close.setAutoRaise(True)
        close.clicked.connect(self.dismiss)
        head = QHBoxLayout()
        head.addWidget(self.title, 1)
        head.addWidget(close)
        self.hint = plain_label("Najedź – podgląd obok · kliknij – zaznacz", self, hint=True)
        self.list = QListWidget(self)
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(True)
        self.list.itemEntered.connect(self._on_entered)
        self.list.currentItemChanged.connect(lambda cur, _prev: cur is not None and self._on_entered(cur))
        self.list.itemClicked.connect(self._on_clicked)
        self.list.itemActivated.connect(self._on_clicked)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 6)
        lay.addLayout(head)
        lay.addWidget(self.hint)
        lay.addWidget(self.list, 1)
        self._pending: int | None = None
        self._shown: int | None = None
        self._timer = QTimer(self, singleShot=True, interval=HOVER_DELAY_MS, timeout=self._emit_hover)
        self.hide()

    def show_items(self, hits: list[dict], anchor: QPoint) -> None:
        """``hits``: wyniki ``Catalog.pick``; ``anchor``: pozycja kursora we współrzędnych rodzica."""
        self.list.clear()
        self._shown = self._pending = None
        for h in hits:
            icon = "🎬" if h["kind"] == cat.KIND_VIDEO else "📷"
            extra = " (trasa)" if h.get("on_track") else ""
            item = QListWidgetItem(f"{icon} {h['name']}{extra}")
            item.setData(ROLE_ID, int(h["id"]))
            item.setToolTip(h["name"])
            self.list.addItem(item)
        self.title.setText(f"W tym miejscu: {materials_word(len(hits))}")
        parent = self.parentWidget()
        rows = min(len(hits), 12)
        h = min(parent.height() - 16, 74 + rows * max(20, self.list.sizeHintForRow(0)))
        w = min(max(260, self.list.sizeHintForColumn(0) + 40), max(260, parent.width() // 2))
        self.resize(w, h)
        x = min(max(8, anchor.x() + 12), max(8, parent.width() - w - 8))
        y = min(max(8, anchor.y() - 20), max(8, parent.height() - h - 8))
        self.move(x, y)
        self.show()
        self.raise_()
        self.list.setFocus()

    def ids(self) -> list[int]:
        return [self.list.item(i).data(ROLE_ID) for i in range(self.list.count())]

    def dismiss(self) -> None:
        if self.isVisible():
            self._timer.stop()
            self.hide()
            self.closed.emit()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss()
            return
        super().keyPressEvent(event)

    def _on_entered(self, item: QListWidgetItem) -> None:
        self._pending = item.data(ROLE_ID)
        self._timer.start()

    def _emit_hover(self) -> None:
        if self._pending is not None and self._pending != self._shown:
            self._shown = self._pending
            self.hovered.emit(self._pending)

    def _on_clicked(self, item: QListWidgetItem) -> None:
        self._timer.stop()
        mid = item.data(ROLE_ID)
        self._shown = mid
        self.chosen.emit(mid)
