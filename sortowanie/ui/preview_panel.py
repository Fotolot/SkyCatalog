"""Prawy panel podglądu wybranego materiału (tylko odczyt plików)."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QGuiApplication, QImage, QImageReader, QPixmap, QTransform
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSizePolicy, QSlider,
                               QStackedWidget, QStyle, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import catalog as cat
from .. import preview
from ..thumbs import thumb_dir, thumb_path  # noqa: F401 – thumb_dir używane też poza modułem
from .common import Poster, flow, human_size, plain_label, scrollable, shrinkable, splitter

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    MULTIMEDIA = True
except ImportError:  # pragma: no cover
    MULTIMEDIA = False

DISPLAY_MAX = 1600
THUMB_MAX = 1024


def _thumb_path(path: Path, size: int, mtime: float) -> Path:
    return thumb_path(path, size, mtime)


def _oriented(img: QImage, orientation: int | None) -> QImage:
    if not orientation or orientation == 1:
        return img
    t = QTransform()
    if orientation in (3, 4):
        t.rotate(180)
    elif orientation in (5, 6):
        t.rotate(90)
    elif orientation in (7, 8):
        t.rotate(270)
    img = img.transformed(t)
    if orientation in (2, 4, 5, 7):
        img = img.mirrored(True, False)
    return img


def load_image(path: str, max_size: int = DISPLAY_MAX, use_cache: bool = True) -> QImage:
    """Obraz do podglądu z poprawną orientacją. Miniatura trafia do cache (na żądanie).

    Zgłasza ValueError z czytelnym komunikatem – nigdy nie przerywa działania programu.
    """
    p = Path(path)
    try:
        st = p.stat()
    except OSError as exc:
        raise ValueError(f"Plik niedostępny: {exc.strerror or exc}") from exc
    cache = _thumb_path(p, st.st_size, st.st_mtime)
    if use_cache and cache.exists():
        img = QImage(str(cache))
        if not img.isNull():
            return img
    if p.suffix.lower() == ".dng":
        try:
            with open(p, "rb") as f:
                data = f.read(96 * 1024 * 1024)
        except OSError as exc:
            raise ValueError(f"Nie można odczytać pliku: {exc}") from exc
        img = QImage()
        for blob in preview.dng_previews(data):
            img = QImage.fromData(QByteArray(blob))
            if not img.isNull():
                break
        if img.isNull():
            raise ValueError("Brak podglądu w pliku DNG, który można wyświetlić. "
                             "Użyj „Otwórz w aplikacji domyślnej”.")
        img = _oriented(img, preview.dng_orientation(data))
    else:
        reader = QImageReader(str(p))
        reader.setAutoTransform(True)                  # orientacja z EXIF
        size = reader.size()
        if size.isValid() and max(size.width(), size.height()) > max_size:
            reader.setScaledSize(size.scaled(QSize(max_size, max_size), Qt.AspectRatioMode.KeepAspectRatio))
        img = reader.read()
        if img.isNull():
            raise ValueError(f"Nie można wyświetlić podglądu: {reader.errorString()}")
    if max(img.width(), img.height()) > max_size:
        img = img.scaled(max_size, max_size, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    if use_cache:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            small = img if max(img.width(), img.height()) <= THUMB_MAX else img.scaled(
                THUMB_MAX, THUMB_MAX, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            small.save(str(cache), "JPG", 85)
        except OSError:
            pass
    return img


class ImageView(QLabel):
    """Obraz skalowany do rozmiaru panelu."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(200, 150)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self._pix: QPixmap | None = None

    def set_image(self, img: QImage | None) -> None:
        self._pix = QPixmap.fromImage(img) if img is not None else None
        self._rescale()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._pix is None:
            self.clear()
            return
        self.setPixmap(self._pix.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation))


def _fmt_ms(ms: int) -> str:
    s = max(0, ms) // 1000
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class PreviewPanel(QWidget):
    fitRequested = Signal(int)

    def __init__(self, catalog: cat.Catalog, parent=None) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self.material: dict | None = None
        self._generation = 0
        self._poster = Poster(self)
        self._poster.event.connect(self._on_event)

        self.title = plain_label("", self)
        f = self.title.font()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 1)
        self.title.setFont(f)
        self.title.setWordWrap(True)
        self.status = plain_label("Kliknij punkt lub trasę na mapie albo pozycję na liście.", self, wrap=True, hint=True)

        self.stack = QStackedWidget(self)
        self.page_empty = plain_label("", self, wrap=True)
        self.image = ImageView(self)
        self.message = plain_label("", self, wrap=True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self.page_empty)
        self.stack.addWidget(self.image)
        self.stack.addWidget(self.message)
        self.video_page = self._build_video() if MULTIMEDIA else None
        if self.video_page is not None:
            self.stack.addWidget(self.video_page)
        self.stack.setMinimumHeight(220)

        self.file_combo = QComboBox(self)
        self.file_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.file_combo.setMinimumContentsLength(8)
        self.file_combo.currentIndexChanged.connect(self._on_file_changed)
        self.details = QTreeWidget(self)
        self.details.setColumnCount(2)
        self.details.setHeaderLabels(["Właściwość", "Wartość"])
        self.details.setRootIsDecorated(False)
        self.details.setColumnWidth(0, 150)
        self.details.setWordWrap(True)

        self.btn_reveal = QPushButton("Przejdź do pliku", self)
        self.btn_open = QPushButton("Otwórz w aplikacji domyślnej", self)
        self.btn_copy = QPushButton("Kopiuj ścieżkę", self)
        self.btn_fit = QPushButton("Pokaż na mapie", self)
        self.btn_reveal.clicked.connect(self._reveal)
        self.btn_open.clicked.connect(self._open_default)
        self.btn_copy.clicked.connect(self._copy)
        self.btn_fit.clicked.connect(lambda: self.material and self.fitRequested.emit(self.material["id"]))

        top = QWidget(self)
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(shrinkable(self.title))
        tl.addWidget(shrinkable(self.status))
        tl.addWidget(self.stack, 1)
        actions = QWidget(self)
        al = QVBoxLayout(actions)
        al.setContentsMargins(0, 0, 0, 0)
        al.addWidget(self.file_combo)
        al.addWidget(flow(self.btn_reveal, self.btn_copy, self.btn_open, self.btn_fit))
        bottom = QWidget(self)
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(actions, 0)
        bl.addWidget(self.details, 1)
        self.stack.setMinimumHeight(120)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(splitter(Qt.Orientation.Vertical, "preview", top, bottom, sizes=[420, 380], stretch=[3, 2]))
        self.show_material(None)

    # ── Wideo ──
    def _build_video(self) -> QWidget:
        page = QWidget(self)
        self.video = QVideoWidget(page)
        self.video.setMinimumHeight(160)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.audio.setVolume(0.6)
        self.btn_play = QPushButton(page)
        self.btn_play.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.btn_play.setToolTip("Odtwórz / pauza")
        self.btn_play.clicked.connect(self._toggle_play)
        self.btn_stop = QPushButton(page)
        self.btn_stop.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.btn_stop.setToolTip("Zatrzymaj (powrót na początek)")
        self.btn_stop.clicked.connect(self.player.stop)
        self.pos_slider = QSlider(Qt.Orientation.Horizontal, page)
        self.pos_slider.sliderMoved.connect(self.player.setPosition)
        self.time_label = plain_label("0:00 / 0:00", page)
        self.vol_slider = QSlider(Qt.Orientation.Horizontal, page)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(60)
        self.vol_slider.setFixedWidth(90)
        self.vol_slider.setToolTip("Głośność")
        self.vol_slider.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))
        self.player.durationChanged.connect(lambda d: (self.pos_slider.setRange(0, d), self._update_time()))
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorOccurred.connect(self._on_player_error)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        controls = QHBoxLayout()
        controls.addWidget(self.btn_play)
        controls.addWidget(self.btn_stop)
        controls.addWidget(self.pos_slider, 1)
        controls.addWidget(self.time_label)
        controls.addWidget(QLabel("🔊", page))
        controls.addWidget(self.vol_slider)
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.video, 1)
        lay.addLayout(controls)
        return page

    def _toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _on_state(self, state) -> None:
        icon = QStyle.StandardPixmap.SP_MediaPause if state == QMediaPlayer.PlaybackState.PlayingState \
            else QStyle.StandardPixmap.SP_MediaPlay
        self.btn_play.setIcon(self.style().standardIcon(icon))

    def _on_position(self, pos: int) -> None:
        if not self.pos_slider.isSliderDown():
            self.pos_slider.setValue(pos)
        self._update_time()

    def _update_time(self) -> None:
        self.time_label.setText(f"{_fmt_ms(self.player.position())} / {_fmt_ms(self.player.duration())}")

    def _on_player_error(self, _error, text: str) -> None:
        self._show_message("Nie można odtworzyć tego filmu w aplikacji (brak obsługi kodeka lub "
                           f"uszkodzony plik).\n{text}\n\nUżyj „Otwórz w aplikacji domyślnej”. "
                           "Aplikacja nie instaluje kodeków ani nie konwertuje plików.")

    def _on_media_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.InvalidMedia and self.stack.currentWidget() is self.video_page:
            self._on_player_error(None, self.player.errorString() or "nieobsługiwany format")

    def _stop_video(self) -> None:
        if MULTIMEDIA:
            self.player.stop()
            self.player.setSource(QUrl())

    # ── Wyświetlanie ──
    def _show_message(self, text: str) -> None:
        self.message.setText(text)
        self.stack.setCurrentWidget(self.message)

    def show_material(self, material: dict | None) -> None:
        self._generation += 1
        self._stop_video()
        self.material = material
        self.details.clear()
        self.file_combo.blockSignals(True)
        self.file_combo.clear()
        self.file_combo.blockSignals(False)
        has = material is not None
        for b in (self.btn_reveal, self.btn_open, self.btn_copy):
            b.setEnabled(has)
        self.btn_fit.setEnabled(has and material.get("lat") is not None)
        if not has:
            self.title.setText("Podgląd materiału")
            self.status.setText("Kliknij punkt lub trasę na mapie albo pozycję na liście.")
            self.page_empty.setText("")
            self.stack.setCurrentWidget(self.page_empty)
            self.file_combo.hide()
            return
        self.title.setText(material["name"])
        self.status.setText(self._status_text(material))
        for f in material.get("files", []):
            self.file_combo.addItem(f"{f['role']}: {Path(f['path']).name}", f["path"])
        self.file_combo.setVisible(self.file_combo.count() > 1)
        self._fill_details(material)
        self._show_media(material, self._current_path())

    def _status_text(self, m: dict) -> str:
        parts = [m["kind"].capitalize(), m["status"]]
        if m["kind"] == cat.KIND_VIDEO:
            if m.get("track_status") == "ok":
                parts.append(f"trasa z SRT: {m.get('track_points', 0)} pozycji")
            else:
                parts.append(f"wymaga danych: {m.get('track_status') or 'brak SRT'}")
        elif m.get("lat") is None:
            parts.append("bez lokalizacji")
        return " · ".join(parts)

    def _current_path(self) -> str | None:
        path = self.file_combo.currentData()
        if path:
            return path
        return self.material["main_path"] if self.material else None

    def _on_file_changed(self, _idx: int) -> None:
        if self.material:
            self._generation += 1
            self._stop_video()
            self._show_media(self.material, self._current_path())

    def _show_media(self, m: dict, path: str | None) -> None:
        if not path:
            return
        if m["status"] in (cat.STATUS_MISSING, cat.STATUS_OFFLINE, cat.STATUS_CLOUD):
            self._show_message(f"Plik jest {m['status']} – podgląd niedostępny.")
            return
        ext = Path(path).suffix.lower()
        if ext in (".jpg", ".jpeg", ".dng"):
            self._show_message("Wczytywanie podglądu…")
            gen = self._generation
            threading.Thread(target=self._load_image_worker, args=(gen, path), daemon=True).start()
        elif ext in (".mp4", ".mov"):
            if not MULTIMEDIA:
                self._show_message("Odtwarzacz wideo niedostępny w tej instalacji – użyj „Otwórz w aplikacji domyślnej”.")
                return
            self.player.setSource(QUrl.fromLocalFile(path))
            self.pos_slider.setValue(0)
            self._update_time()
            self.stack.setCurrentWidget(self.video_page)
        else:
            self._show_message("Brak podglądu dla tego pliku (np. SRT, LRF). Użyj „Otwórz w aplikacji domyślnej”.")

    def _load_image_worker(self, gen: int, path: str) -> None:
        try:
            img = load_image(path)
            self._poster.post(("image", gen, img, None))
        except Exception as exc:   # brak obsługi formatu nie może przerwać programu
            self._poster.post(("image", gen, None, str(exc)))

    def _on_event(self, ev) -> None:
        kind, gen, img, err = ev
        if kind != "image" or gen != self._generation:
            return
        if img is None:
            self._show_message(f"{err}\n\nMożesz otworzyć plik w aplikacji domyślnej.")
            return
        self.image.set_image(img)
        self.stack.setCurrentWidget(self.image)

    def _row(self, key: str, value) -> None:
        if value is None or value == "":
            return
        item = QTreeWidgetItem([key, str(value)])
        item.setToolTip(1, str(value))
        self.details.addTopLevelItem(item)

    def _fill_details(self, m: dict) -> None:
        meta = m.get("meta") or {}
        self._row("Nazwa", m["name"])
        self._row("Ścieżka", m["main_path"])
        for f in m.get("files", []):
            if f["path"] != m["main_path"]:
                self._row(f"Plik {f['role']}", f["path"])
        if m.get("captured_at"):
            self._row("Data wykonania", f"{m['captured_at'].replace('T', ' ')} (źródło: {m.get('captured_source')})")
        else:
            self._row("Data wykonania", "nieznana (data modyfikacji pliku nie jest używana)")
        if m.get("lat") is not None:
            label = "Punkt startowy" if m["kind"] == cat.KIND_VIDEO else "GPS"
            alt = f", wys. {meta['gps_alt']} m" if meta.get("gps_alt") is not None else ""
            self._row(label, f"{m['lat']:.6f}, {m['lon']:.6f}{alt}")
        self._row("Rozmiar", human_size(m.get("size") or 0))
        if meta.get("width"):
            self._row("Rozdzielczość", f"{meta['width']} × {meta['height']}")
        if m["kind"] == cat.KIND_VIDEO:
            if meta.get("duration_s"):
                self._row("Czas trwania", _fmt_ms(int(meta["duration_s"] * 1000)))
            self._row("FPS", meta.get("fps"))
            srt = m.get("srt_path")
            self._row("SRT", f"{Path(srt).name} – {m.get('track_status')}" if srt else "brak powiązanego pliku SRT")
            if m.get("track_points"):
                self._row("Pozycje GPS w SRT", m["track_points"])
            self._row("Podziały trasy (luki/skoki)", meta.get("podziały_trasy"))
            self._row("Próbki bez GPS", meta.get("próbki_bez_gps"))
            home = meta.get("punkt_startowy_pilota")
            if home:
                self._row("Punkt startu / pilota (z SRT)", f"{home[0]:.6f}, {home[1]:.6f}")
        device = " ".join(x for x in (meta.get("make"), meta.get("model")) if x)
        self._row("Aparat / sprzęt", device)
        self._row("Oprogramowanie", meta.get("software") or meta.get("encoder"))
        for key, label in (("exposure", "Czas naświetlania"), ("fnumber", "Przysłona"), ("iso", "ISO"),
                           ("focal", "Ogniskowa"), ("focal35", "Ogniskowa (35 mm)")):
            self._row(label, meta.get(key))
        if m.get("category"):
            self._row("Kategoria", cat.CATEGORY_LABELS.get(m["category"], m["category"]))
        self._row("Miejscowość / lokalizacja", m.get("town"))
        self._row("Wydarzenie / zlecenie", " ".join(x for x in (m.get("event_date"), m.get("event_name")) if x))
        self._row("Nazwa miejsca", meta.get("obiekt"))
        self._row("Status", m["status"])
        self._row("Błąd odczytu", m.get("error"))
        self._row("Uwaga", meta.get("uwaga"))

    # ── Akcje (wyłącznie dla plików z katalogu) ──
    def _checked_path(self) -> str | None:
        path = self._current_path()
        if not path or not self.catalog.is_indexed_file(path):
            QMessageBox.warning(self, "Podgląd", "Ten plik nie jest zapisany w katalogu materiałów.")
            return None
        return path

    def _reveal(self) -> None:
        path = self._checked_path()
        if path:
            try:
                preview.reveal_in_explorer(path)
            except OSError as exc:
                QMessageBox.warning(self, "Podgląd", f"Nie można otworzyć Eksploratora:\n{exc}")

    def _open_default(self) -> None:
        path = self._checked_path()
        if path:
            self._stop_video()
            try:
                preview.open_default(path)
            except OSError as exc:
                QMessageBox.warning(self, "Podgląd", f"Nie można otworzyć pliku:\n{exc}")

    def _copy(self) -> None:
        path = self._current_path()
        if path:
            QGuiApplication.clipboard().setText(path)
            self.status.setText(f"Skopiowano ścieżkę: {path}")
