"""Zakładka „Mapa materiałów”: skanowanie komputera, filtry, mapa Leaflet i podgląd.

Zakładka tylko ODCZYTUJE materiały – nie zmienia metadanych, nazw ani położenia plików.
Lokalizacje pochodzą z lokalnego katalogu (EXIF / SRT). Overpass nie jest tu używany.
"""

from __future__ import annotations

import math
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCursor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit, QMenu,
                               QMessageBox, QProgressBar, QPushButton, QSplitter, QTabWidget, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import catalog as cat
from ..config import Settings
from ..mapgeom import thin_latlon
from ..scanner import Scanner, ScanProgress
from ..tiles import provider_from_settings
from .common import DateField, Poster, materials_word, menu_text, plain_label
from .dialogs import ScanSettingsDialog, SkipsDialog
from .preview_panel import PreviewPanel
from .webmap import MapWidget

MAX_POINTS_IN_VIEW = 20000      # powyżej – przerzedzanie rysowania (wybór kliknięciem widzi wszystkie)
MAX_TRACKS_PER_MESSAGE = 400
LIST_LIMIT = 2000
CATEGORIES = [("Wszystkie kategorie", None), ("Hobby", "hobby"), ("Eventy", "eventy"),
              ("Zlecenia", "zlecenia"), ("Nieprzypisane", cat.UNASSIGNED)]


def meters_per_px(lat: float, zoom: int) -> float:
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** zoom)


class MapTab(QWidget):
    scanFinished = Signal()

    def __init__(self, catalog: cat.Catalog, settings: Settings, dark: bool = False, parent=None) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self.settings = settings
        self.dark = dark
        self.scanner: Scanner | None = None
        self.view: dict | None = None
        self.selected_id: int | None = None
        self._poster = Poster(self)
        self._poster.event.connect(self._on_scan_event)
        self._filter_timer = QTimer(self, singleShot=True, interval=250, timeout=self._apply_filters)
        self._last_run = 0
        self._build()
        self._had_located = self.catalog.bounds(cat.Filters()) is not None
        self._load_filter_values()
        self._refresh_lists()
        self._update_scan_buttons()

    # ══ Budowa ═════════════════════════════════════════════════
    def _build(self) -> None:
        # Skanowanie
        scan_box = QGroupBox("Skanowanie komputera", self)
        self.btn_scan = QPushButton("Skanuj komputer")
        self.btn_cancel = QPushButton("Anuluj")
        self.btn_resume = QPushButton("Wznów")
        self.btn_scan_settings = QPushButton("Ustawienia skanowania…")
        self.btn_scan.clicked.connect(self.start_scan)
        self.btn_cancel.clicked.connect(self.cancel_scan)
        self.btn_resume.clicked.connect(lambda: self.start_scan(resume=True))
        self.btn_scan_settings.clicked.connect(self._scan_settings)
        self.scan_progress = QProgressBar()
        self.scan_progress.setTextVisible(False)
        self.scan_status = plain_label("", wrap=True)
        self.scan_folder = plain_label("", hint=True)
        self.scan_folder.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.btn_skips = QPushButton("Pominięte…")
        self.btn_skips.clicked.connect(self._show_skips)
        g = QVBoxLayout(scan_box)
        row = QHBoxLayout()
        row.addWidget(self.btn_scan)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_resume)
        g.addLayout(row)
        row2 = QHBoxLayout()
        row2.addWidget(self.btn_scan_settings)
        row2.addWidget(self.btn_skips)
        g.addLayout(row2)
        g.addWidget(self.scan_progress)
        g.addWidget(self.scan_status)
        g.addWidget(self.scan_folder)

        # Filtry
        flt = QGroupBox("Filtry", self)
        self.cb_photos = QCheckBox("Pokaż zdjęcia")
        self.cb_videos = QCheckBox("Pokaż wideo")
        self.cb_photos.setChecked(True)
        self.cb_videos.setChecked(True)
        self.date_from = DateField(placeholder="od RRRR-MM-DD")
        self.date_to = DateField(placeholder="do RRRR-MM-DD")
        self.category = QComboBox()
        for label, key in CATEGORIES:
            self.category.addItem(label, key)
        self.town = QComboBox()
        self.event = QComboBox()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Nazwa lub fragment ścieżki…")
        self.search.setClearButtonEnabled(True)
        self.btn_clear = QPushButton("Wyczyść filtry")
        self.btn_clear.clicked.connect(self.clear_filters)
        for w in (self.cb_photos, self.cb_videos):
            w.toggled.connect(self._on_layers)
        for w in (self.date_from, self.date_to):
            w.changed.connect(lambda _t: self._filter_timer.start())
        for w in (self.category, self.town, self.event):
            w.currentIndexChanged.connect(lambda _i: self._filter_timer.start())
        self.search.textChanged.connect(lambda _t: self._filter_timer.start())
        f = QFormLayout(flt)
        kinds = QHBoxLayout()
        kinds.addWidget(self.cb_photos)
        kinds.addWidget(self.cb_videos)
        f.addRow(kinds)
        f.addRow("Data wykonania od:", self.date_from)
        f.addRow("do:", self.date_to)
        f.addRow("Kategoria:", self.category)
        f.addRow("Miejscowość:", self.town)
        f.addRow("Wydarzenie / zlecenie:", self.event)
        f.addRow("Szukaj:", self.search)
        f.addRow(self.btn_clear)

        # Listy
        self.lists = QTabWidget(self)
        self.list_located = self._make_list()
        self.list_missing = self._make_list()
        self.lists.addTab(self.list_located, "Na mapie")
        self.lists.addTab(self.list_missing, "Bez lokalizacji")
        self.list_info = plain_label("", hint=True, wrap=True)

        left = QWidget(self)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(scan_box)
        ll.addWidget(flt)
        ll.addWidget(self.lists, 1)
        ll.addWidget(self.list_info)

        # Mapa
        center = QWidget(self)
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.btn_all = QPushButton("Pokaż wszystkie materiały")
        self.btn_all.clicked.connect(self.fit_all)
        self.btn_fit_sel = QPushButton("Przybliż do wybranego")
        self.btn_fit_sel.clicked.connect(lambda: self.selected_id and self.select(self.selected_id, fit=True))
        bar.addWidget(self.btn_all)
        bar.addWidget(self.btn_fit_sel)
        bar.addStretch(1)
        self.map_status = plain_label("", hint=True)
        bar.addWidget(self.map_status)
        cl.addLayout(bar)
        self.map = MapWidget(center)
        self.map.message.connect(self._on_map_message)
        cl.addWidget(self.map, 1)

        # Podgląd
        self.preview = PreviewPanel(self.catalog, self)
        self.preview.fitRequested.connect(lambda mid: self.select(mid, fit=True))

        split = QSplitter(Qt.Orientation.Horizontal, self)
        split.addWidget(left)
        split.addWidget(center)
        split.addWidget(self.preview)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 3)
        split.setStretchFactor(2, 1)
        split.setSizes([330, 820, 360])
        self._split = split
        self._sized = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(split)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._sized and self.width() > 200:
            # proporcje paneli zależne od szerokości okna (także ekrany 1024 px)
            w = self._split.width()
            self._split.setSizes([int(w * 0.24), int(w * 0.50), w - int(w * 0.24) - int(w * 0.50)])
            self._sized = True

    def _make_list(self) -> QTreeWidget:
        t = QTreeWidget(self)
        t.setHeaderLabels(["Nazwa", "Typ", "Data wykonania", "Kategoria", "Stan"])
        t.setRootIsDecorated(False)
        t.setUniformRowHeights(True)
        t.setColumnWidth(0, 160)
        t.setColumnWidth(1, 60)
        t.setColumnWidth(2, 125)
        t.itemSelectionChanged.connect(lambda tree=t: self._on_list_select(tree))
        t.itemDoubleClicked.connect(lambda item, _c: self.select(item.data(0, Qt.ItemDataRole.UserRole), fit=True))
        return t

    # ══ Filtry ═════════════════════════════════════════════════
    def filters(self, for_map: bool = False) -> cat.Filters:
        return cat.Filters(
            photos=self.cb_photos.isChecked(), videos=self.cb_videos.isChecked(),
            date_from=self.date_from.value(), date_to=self.date_to.value(),
            category=self.category.currentData(), town=self.town.currentData(), event=self.event.currentData(),
            text=self.search.text())

    def _load_filter_values(self) -> None:
        for combo, column, label in ((self.town, "town", "Wszystkie miejscowości"),
                                     (self.event, "event_name", "Wszystkie wydarzenia i zlecenia")):
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(label, None)
            for v in self.catalog.distinct(column):
                combo.addItem(v, v)
            idx = combo.findData(current)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

    def clear_filters(self) -> None:
        for w in (self.cb_photos, self.cb_videos):
            w.blockSignals(True)
            w.setChecked(True)
            w.blockSignals(False)
        for w in (self.date_from, self.date_to):
            w.setText("")
        for combo in (self.category, self.town, self.event):
            combo.setCurrentIndex(0)
        self.search.clear()
        self._on_layers()

    def _on_layers(self) -> None:
        self.map.send("layers", photos=self.cb_photos.isChecked(), videos=self.cb_videos.isChecked())
        self._filter_timer.start()

    def _apply_filters(self) -> None:
        for w in (self.date_from, self.date_to):
            w.edit.setStyleSheet("" if not w.text().strip() or w.value() else "color: #b00020;")
        self._refresh_lists()
        self._send_view_data()

    # ══ Listy ══════════════════════════════════════════════════
    def _refresh_lists(self) -> None:
        flt = self.filters()
        for tree, located in ((self.list_located, True), (self.list_missing, False)):
            rows = self.catalog.query_list(flt, located=located, limit=LIST_LIMIT)
            tree.blockSignals(True)
            tree.clear()
            items = []
            for r in rows:
                date_txt = (r["captured_at"] or "").replace("T", " ")[:16] or "nieznana"
                state = r["status"]
                if r["kind"] == cat.KIND_VIDEO and r["lat"] is None:
                    state = f"wymaga danych: {r['track_status'] or 'brak SRT'}"
                elif r["kind"] == cat.KIND_PHOTO and r["lat"] is None and state == cat.STATUS_OK:
                    state = "brak GPS w EXIF"
                item = QTreeWidgetItem([r["name"], r["kind"], date_txt,
                                        cat.CATEGORY_LABELS.get(r["category"], "—") if r["category"] else "—", state])
                item.setData(0, Qt.ItemDataRole.UserRole, r["id"])
                item.setToolTip(0, r["main_path"])
                items.append(item)
            tree.addTopLevelItems(items)
            tree.blockSignals(False)
        n_loc, n_miss = self.catalog.count(flt, True), self.catalog.count(flt, False)
        self.lists.setTabText(0, f"Na mapie ({n_loc})")
        self.lists.setTabText(1, f"Bez lokalizacji ({n_miss})")
        note = []
        if n_loc > LIST_LIMIT or n_miss > LIST_LIMIT:
            note.append(f"Listy pokazują najnowsze {LIST_LIMIT} pozycji – zawęź filtry, aby zobaczyć resztę.")
        if not self.catalog.totals().get("razem"):
            note.append("Katalog jest pusty – kliknij „Skanuj komputer”.")
        self.list_info.setText(" ".join(note))

    def _on_list_select(self, tree: QTreeWidget) -> None:
        items = tree.selectedItems()
        if items:
            self.select(items[0].data(0, Qt.ItemDataRole.UserRole), fit=False, from_list=True)

    # ══ Mapa ═══════════════════════════════════════════════════
    def _on_map_message(self, m: dict) -> None:
        kind = m.get("type")
        if kind == "ready":
            p = provider_from_settings(self.settings)
            self.map.send("init", tileUrl=p.url, attribution=p.attribution, maxZoom=p.max_zoom,
                          theme="dark" if self.dark else "light", bbox=self.catalog.bounds(self.filters()))
            self.map.flush()
            self.map.send("layers", photos=self.cb_photos.isChecked(), videos=self.cb_videos.isChecked())
        elif kind == "view":
            try:
                self.view = {k: float(m[k]) for k in ("s", "w", "n", "e")}
                self.view["zoom"] = int(m["zoom"])
            except (KeyError, TypeError, ValueError):
                return
            self._send_view_data()
        elif kind == "needTracks":
            ids = [int(i) for i in (m.get("ids") or []) if isinstance(i, (int, float))][:5000]
            for i in range(0, len(ids), MAX_TRACKS_PER_MESSAGE):
                tracks = self.catalog.display_tracks(ids[i:i + MAX_TRACKS_PER_MESSAGE])
                self.map.send("tracks", tracks={str(k): v for k, v in tracks.items()})
        elif kind == "pick":
            try:
                self._pick(float(m["lat"]), float(m["lon"]), int(m["zoom"]))
            except (KeyError, TypeError, ValueError):
                pass
        elif kind == "tiles":
            self.map_status.setText("" if m.get("ok") else "Podkład mapy niedostępny – materiały działają lokalnie.")

    def _send_view_data(self) -> None:
        if not self.view or not self.map.ready:
            return
        v = self.view
        flt = self.filters()
        # zapas wokół widoku, by przesuwanie nie pokazywało pustych brzegów
        dl, dn = (v["n"] - v["s"]) * 0.25, (v["e"] - v["w"]) * 0.25
        rows = self.catalog.query_points(flt, (v["s"] - dl, v["w"] - dn, v["n"] + dl, v["e"] + dn), limit=200000)
        photos = [(r["id"], r["lat"], r["lon"]) for r in rows if r["kind"] == cat.KIND_PHOTO]
        videos = [(r["id"], r["lat"], r["lon"], 1 if r["track_points"] else 0) for r in rows if r["kind"] == cat.KIND_VIDEO]
        thinned = False
        if len(photos) > MAX_POINTS_IN_VIEW:
            photos = thin_latlon(photos, v["zoom"], MAX_POINTS_IN_VIEW)
            thinned = True
        if len(videos) > MAX_POINTS_IN_VIEW:
            keep = {r[0] for r in thin_latlon([(i, a, b) for i, a, b, _t in videos], v["zoom"], MAX_POINTS_IN_VIEW)}
            videos = [x for x in videos if x[0] in keep]
            thinned = True
        self.map.send("data", photos=photos, videos=videos)
        txt = f"W widoku: {len([r for r in rows if r['kind'] == cat.KIND_PHOTO])} zdjęć, " \
              f"{len([r for r in rows if r['kind'] == cat.KIND_VIDEO])} filmów"
        if thinned:
            txt += " (gęste skupiska rysowane uproszczone – kliknięcie pokaże wszystkie)"
        if not self.map_status.text().startswith("Podkład"):
            self.map_status.setText(txt)

    def _pick(self, lat: float, lon: float, zoom: int) -> None:
        tol = max(1.0, 8 * meters_per_px(lat, zoom))
        flt = self.filters()
        hits = self.catalog.pick(flt, lat, lon, tol)
        if not hits:
            return
        if len(hits) == 1:
            self.select(hits[0]["id"])
            return
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        title = QAction(f"W tym miejscu: {materials_word(len(hits))} – wybierz", menu)
        title.setEnabled(False)
        menu.addAction(title)
        menu.addSeparator()
        for h in hits:
            icon = "🎬" if h["kind"] == cat.KIND_VIDEO else "📷"
            extra = " (trasa)" if h["on_track"] else ""
            act = QAction(menu_text(f"{icon} {h['name']}{extra}"), menu)
            act.triggered.connect(lambda _c=False, mid=h["id"]: self.select(mid))
            menu.addAction(act)
        menu.exec(QCursor.pos())

    def select(self, material_id: int | None, fit: bool = False, from_list: bool = False) -> None:
        if material_id is None:
            return
        m = self.catalog.material(int(material_id))
        if m is None:
            return
        self.selected_id = m["id"]
        if not (self.preview.material and self.preview.material.get("id") == m["id"]
                and self.preview.material.get("main_path") == m["main_path"]):
            self.preview.show_material(m)
        if m.get("lat") is not None:
            payload = {"id": m["id"], "kind": m["kind"], "lat": m["lat"], "lon": m["lon"],
                       "bbox": [m["min_lat"], m["min_lon"], m["max_lat"], m["max_lon"]]}
            if m["kind"] == cat.KIND_VIDEO:
                payload["segments"] = self.catalog.display_tracks([m["id"]]).get(m["id"], [])
            self.map.send("select", material=payload, fit=fit)
        if not from_list:
            self._select_in_lists(m["id"])

    def _select_in_lists(self, mid: int) -> None:
        for tree in (self.list_located, self.list_missing):
            tree.blockSignals(True)
            tree.clearSelection()
            for i in range(tree.topLevelItemCount()):
                item = tree.topLevelItem(i)
                if item.data(0, Qt.ItemDataRole.UserRole) == mid:
                    item.setSelected(True)
                    tree.scrollToItem(item)
                    self.lists.setCurrentWidget(tree)
                    break
            tree.blockSignals(False)

    def fit_all(self) -> None:
        bbox = self.catalog.bounds(self.filters())
        if bbox:
            self.map.send("fit", bbox=list(bbox))
        else:
            QMessageBox.information(self, "Mapa", "Brak materiałów z lokalizacją dla aktywnych filtrów.")

    def set_theme(self, dark: bool) -> None:
        self.dark = dark
        self.map.send("theme", theme="dark" if dark else "light")

    def refresh_after_change(self, ids: list[int] | None = None) -> None:
        """Po sortowaniu: odśwież listy, mapę i podgląd bez skanowania dysków."""
        self._load_filter_values()
        self._refresh_lists()
        self.map.send("reset")
        if self.selected_id is not None and (ids is None or self.selected_id in ids):
            self.preview.material = None
            self.select(self.selected_id)

    # ══ Skanowanie ═════════════════════════════════════════════
    def start_scan(self, resume: bool = False) -> None:
        if self.scanner and self.scanner.running:
            return
        self.scanner = Scanner(self.catalog, self.settings, on_progress=lambda p: self._poster.post(p))
        self.scanner.start(resume=resume)
        self.scan_status.setText("Wznawianie skanowania…" if resume else "Rozpoczynanie skanowania…")
        self.scan_progress.setRange(0, 0)
        self._update_scan_buttons(running=True)

    def cancel_scan(self) -> None:
        if self.scanner:
            self.scanner.cancel()
            self.scan_status.setText("Anulowanie – kończę bieżący folder…")

    def _update_scan_buttons(self, running: bool = False) -> None:
        can_resume = not running and self.catalog.get_state("scan_status") in ("przerwane", "trwa") \
            and self.catalog.queue_pending() > 0
        self.btn_scan.setEnabled(not running)
        self.btn_cancel.setEnabled(running)
        self.btn_resume.setEnabled(can_resume)
        self.btn_scan_settings.setEnabled(not running)
        if not running and not self.scan_status.text():
            t = self.catalog.totals()
            self.scan_status.setText(f"W katalogu: {materials_word(t.get('razem', 0))}"
                                     + (" · skanowanie przerwane – można wznowić" if can_resume else ""))

    def _on_scan_event(self, prog: ScanProgress) -> None:
        self._last_run = prog.run
        done, pending = prog.dirs_done, prog.dirs_pending
        if prog.finished or prog.cancelled:
            self.scan_progress.setRange(0, 1)
            self.scan_progress.setValue(1 if prog.finished else 0)
            head = "Skanowanie zakończone" if prog.finished else "Skanowanie przerwane (można wznowić)"
            missing = prog.finalize.get("brak_pliku", 0) if prog.finalize else 0
            self.scan_status.setText(
                f"{head}: folderów {done}, nowe {prog.new}, zmienione {prog.changed}, bez zmian {prog.unchanged}, "
                f"pominięte {prog.skipped}" + (f", zniknęło {missing}" if missing else "") + ".")
            self.scan_folder.setText("")
            self._update_scan_buttons(running=False)
            self._load_filter_values()
            self._refresh_lists()
            self.map.send("reset")
            if not self._had_located:            # pierwsze materiały na mapie – pokaż je
                bbox = self.catalog.bounds(self.filters())
                if bbox:
                    self.map.send("fit", bbox=list(bbox))
                    self._had_located = True
            self.scanFinished.emit()
            return
        if pending:
            self.scan_progress.setRange(0, done + pending)
            self.scan_progress.setValue(done)
        self.scan_status.setText(f"Folderów: {done} (w kolejce {pending}) · znalezione materiały: "
                                 f"{prog.materials_seen} (nowe {prog.new}, zmienione {prog.changed}) · "
                                 f"pominięte: {prog.skipped}")
        self.scan_folder.setText(prog.current)
        if prog.new or prog.changed:
            if done % 200 == 0:
                self._refresh_lists()
                self._send_view_data()

    def _scan_settings(self) -> None:
        ScanSettingsDialog(self, self.settings).exec()

    def _show_skips(self) -> None:
        run = self._last_run or self.catalog.current_run()
        SkipsDialog(self, self.catalog.skip_summary(run), self.catalog.skips(run, 2000)).exec()

    def shutdown(self) -> None:
        if self.scanner and self.scanner.running:
            self.scanner.cancel()
            self.scanner.wait(5.0)     # zapis stanu „przerwane”, aby można było wznowić
        self.preview.show_material(None)
