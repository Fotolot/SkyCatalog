"""Podgląd zdjęcia po najechaniu myszą na pozycję listy (bez HTML – nazwy plików to zwykły tekst)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QCursor, QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import QFrame, QLabel, QTreeWidget, QVBoxLayout

from .common import Poster
from .preview_panel import load_image

HOVER_DELAY_MS = 350
PREVIEW_PX = 420
# Kolejność wyboru pliku do podglądu w grupie: najpierw lekki JPG, potem DNG/inne obrazy
PREFERRED = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".tif", ".tiff", ".dng")


def preview_file(files: list[Path]) -> Path | None:
    images = [f for f in files if f.suffix.lower() in PREFERRED]
    return min(images, key=lambda f: PREFERRED.index(f.suffix.lower())) if images else None


class HoverPopup(QFrame):
    """Okienko z miniaturą i podpisem, wyświetlane przy kursorze."""

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFrameShape(QFrame.Shape.Box)
        self.image = QLabel(self)
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption = QLabel(self)
        self.caption.setTextFormat(Qt.TextFormat.PlainText)
        self.caption.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(self.image)
        lay.addWidget(self.caption)

    def show_at(self, pos: QPoint, pixmap: QPixmap | None, text: str) -> None:
        if pixmap is not None:
            self.image.setPixmap(pixmap)
            self.image.show()
        else:
            self.image.clear()
            self.image.hide()
        self.caption.setText(text)
        self.caption.setMaximumWidth(max(PREVIEW_PX, pixmap.width() if pixmap else 0))
        self.adjustSize()
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x, y = pos.x() + 18, pos.y() + 18
        if x + self.width() > area.right():
            x = pos.x() - self.width() - 12
        if y + self.height() > area.bottom():
            y = max(area.top(), area.bottom() - self.height())
        self.move(max(area.left(), x), y)
        self.show()
        self.raise_()


class TreeHoverPreview(QObject):
    """Po zatrzymaniu kursora nad wierszem listy wczytuje w tle miniaturę i pokazuje ją obok.

    ``resolve(item)`` zwraca (ścieżka obrazu lub None, podpis) – None = brak podglądu dla wiersza.
    """

    def __init__(self, tree: QTreeWidget, resolve: Callable) -> None:
        super().__init__(tree)
        self.tree = tree
        self.resolve = resolve
        self.popup = HoverPopup()
        self.poster = Poster(self)
        self.poster.event.connect(self._on_loaded)
        self._gen = 0
        self._item = None
        self._timer = QTimer(self, singleShot=True, interval=HOVER_DELAY_MS, timeout=self._start)
        tree.setMouseTracking(True)
        tree.viewport().setMouseTracking(True)
        tree.itemEntered.connect(self._on_entered)
        tree.viewport().installEventFilter(self)
        tree.verticalScrollBar().valueChanged.connect(lambda _v: self.hide())

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() in (QEvent.Type.Leave, QEvent.Type.MouseButtonPress, QEvent.Type.Wheel,
                            QEvent.Type.KeyPress, QEvent.Type.Hide):
            self.hide()
        elif event.type() == QEvent.Type.MouseMove and self.tree.itemAt(event.position().toPoint()) is None:
            self.hide()
        return False

    def hide(self) -> None:
        self._gen += 1
        self._item = None
        self._timer.stop()
        self.popup.hide()

    def _on_entered(self, item, _column: int) -> None:
        if item is self._item:
            return
        self.hide()
        self._item = item
        self._timer.start()

    def _start(self) -> None:
        item = self._item
        if item is None:
            return
        path, caption = self.resolve(item)
        gen = self._gen
        if path is None:
            if caption:
                self.popup.show_at(QCursor.pos(), None, caption)
            return
        self.popup.show_at(QCursor.pos(), None, f"{caption}\nWczytywanie podglądu…")

        def work() -> None:
            try:
                img = load_image(str(path), PREVIEW_PX)
                self.poster.post((gen, img, caption, ""))
            except ValueError as exc:
                self.poster.post((gen, None, caption, str(exc)))
            except Exception as exc:  # noqa: BLE001 – podgląd nie może przerwać programu
                self.poster.post((gen, None, caption, f"{type(exc).__name__}: {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _on_loaded(self, ev) -> None:
        gen, img, caption, error = ev
        if gen != self._gen:
            return                                   # kursor poszedł dalej
        if isinstance(img, QImage) and not img.isNull():
            pix = QPixmap.fromImage(img)
            if max(pix.width(), pix.height()) > PREVIEW_PX:
                pix = pix.scaled(PREVIEW_PX, PREVIEW_PX, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            self.popup.show_at(QCursor.pos(), pix, caption)
        else:
            self.popup.show_at(QCursor.pos(), None, f"{caption}\n{error}")
