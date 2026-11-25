"""Zakładka „Sortowanie” – import materiałów z lotów i ręczny wybór kategorii (Qt)."""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame,
                               QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
                               QRadioButton, QSplitter, QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from .. import catalog as cat
from .. import core
from ..cards import Card, CardWatcher
from .. import planner as pl
from ..config import Settings
from ..geo import DecisionStore, LocationService, OsmCache, Place, SOURCE_DECISION, SOURCE_NONE
from ..media import MediaGroup, scan_source
from ..osm import ATTRIBUTION, ATTRIBUTION_URL, TownResult
from .common import (DateField, Poster, files_word, flow, groups_word, human_size, plain_label, scrollable, shrinkable,
                     splitter)

NO_CATEGORY = None
SCOPE_ALL, SCOPE_SELECTED = "all", "selected"
NEW_TARGET = "— nowe —"
NO_OBJECT = "— bez obiektu —"
ERROR_COLOR = QColor("#c62828")


class GeoWorker(threading.Thread):
    """Wątek lokalizacji (Overpass) – zadania zbierane w partie, jedno zapytanie naraz."""

    BATCH_DELAY = 0.3

    def __init__(self, service: LocationService, post) -> None:
        super().__init__(daemon=True)
        self.service = service
        self.post = post
        self.jobs: queue.Queue = queue.Queue()
        service.client.on_status = lambda text: post(("net_status", text))

    def submit(self, import_id: int, kind: str, key: str, gps: tuple[float, float]) -> None:
        self.jobs.put((import_id, kind, key, gps))

    def _drain(self) -> list:
        jobs = [self.jobs.get()]
        time.sleep(self.BATCH_DELAY)
        while True:
            try:
                jobs.append(self.jobs.get_nowait())
            except queue.Empty:
                return jobs

    def run(self) -> None:
        while True:
            jobs = self._drain()
            for import_id, kind, key, (lat, lon) in jobs:
                if kind != "town":
                    continue
                try:
                    result, source, error = self.service.town(lat, lon)
                except Exception as exc:
                    result, source, error = TownResult(None, [], "błąd"), SOURCE_NONE, str(exc)
                self.post(("town", import_id, key, result, source, error))
            by_import: dict[int, list] = {}
            for import_id, kind, key, (lat, lon) in jobs:
                if kind == "poi":
                    by_import.setdefault(import_id, []).append((key, lat, lon))
            for import_id, points in by_import.items():
                def deliver(key, cands, source, error, remembered, i=import_id):
                    self.post(("poi", i, key, cands, source, error, remembered))
                try:
                    self.service.objects(points, deliver)
                except Exception as exc:
                    for key, _lat, _lon in points:
                        deliver(key, [], SOURCE_NONE, str(exc), None)
            if self.jobs.empty():
                self.post(("net_status", ""))


class SortTab(QWidget):
    catalogChanged = Signal(list)       # identyfikatory materiałów zmienionych w katalogu
    statusMessage = Signal(str)

    def __init__(self, settings: Settings, catalog: cat.Catalog, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.catalog = catalog
        self.cache = OsmCache()
        self.decisions = DecisionStore()
        self.geo = LocationService(self.settings, self.cache, self.decisions)
        self.poster = Poster(self)
        self.poster.event.connect(self._on_event)
        self.worker = GeoWorker(self.geo, self.poster.post)
        self.worker.start()

        self.import_id = 0
        self.groups: list[MediaGroup] = []
        self.group_by_key: dict[str, MediaGroup] = {}
        self.items: dict[str, QTreeWidgetItem] = {}
        self.assignment_of: dict[str, pl.Assignment | None] = {}
        self.places: dict[str, Place] = {}
        self.poi_requested: set[str] = set()
        self.current: pl.Assignment | None = None
        self.plan: pl.ImportPlan | None = None
        self.busy = False
        self._suspend = False
        self._existing: list[pl.ExistingTarget] = []
        self._decision_rows: dict[int, tuple[str, list[str]]] = {}
        self._choice_ids: list[str] = []
        self._refresh_timer = QTimer(self, singleShot=True, interval=150, timeout=self._refresh_preview)

        self._build()
        self.source.setText(settings.get("last_source") or "")
        self.target.setText(settings.get("target_root") or "")
        self.target.textChanged.connect(self._on_target_changed)
        self._sync_form()
        self._refresh_undo_button()
        self.all_groups: list[MediaGroup] = []
        self.imported_keys: set[str] = set()
        self.card_watcher = CardWatcher()
        self._card_timer = QTimer(self, interval=3000, timeout=self._poll_cards)
        self._card_timer.start()
        QTimer.singleShot(1500, self._offer_present_cards)
        self._on_mode_changed()

    # ══ Budowa ═════════════════════════════════════════════════
    def _build(self) -> None:
        folders = QGroupBox("Foldery", self)
        fl = QGridLayout(folders)
        self.source = QLineEdit()
        self.target = QLineEdit()
        b_src = QPushButton("Wybierz…")
        b_src.clicked.connect(self._choose_source)
        self.load_btn = QPushButton("Wczytaj")
        self.load_btn.clicked.connect(self.load_source)
        b_tgt = QPushButton("Wybierz…")
        b_tgt.clicked.connect(self._choose_target)
        fl.addWidget(QLabel("Import (karta / folder):"), 0, 0)
        fl.addWidget(self.source, 0, 1)
        fl.addWidget(b_src, 0, 2)
        fl.addWidget(self.load_btn, 0, 3)
        fl.addWidget(QLabel("Archiwum (folder docelowy):"), 1, 0)
        fl.addWidget(self.target, 1, 1)
        fl.addWidget(b_tgt, 1, 2)
        self.mode_move = QRadioButton("Przenieś pliki")
        self.mode_copy = QRadioButton("Kopiuj (oryginały zostają na karcie / w folderze importu)")
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.mode_move)
        self.mode_group.addButton(self.mode_copy)
        (self.mode_copy if self.settings.get("import_mode") == core.MODE_COPY else self.mode_move).setChecked(True)
        self.mode_copy.toggled.connect(lambda _c: self._on_mode_changed())
        self.show_imported = QCheckBox("Pokaż także materiały już zaimportowane")
        self.show_imported.toggled.connect(lambda _c: self._apply_imported_filter())
        fl.addWidget(flow(QLabel("Tryb:"), self.mode_move, self.mode_copy, self.show_imported), 2, 0, 1, 4)

        # Pasek wykrytej karty pamięci
        self.card_banner = QFrame(self)
        self.card_banner.setFrameShape(QFrame.Shape.StyledPanel)
        self.card_banner.setStyleSheet("QFrame { background: palette(alternate-base); border: 1px solid palette(highlight); "
                                       "border-radius: 4px; }")
        cb_lay = QHBoxLayout(self.card_banner)
        self.card_label = plain_label("", wrap=True)
        self.card_import_btn = QPushButton("Importuj z karty")
        self.card_import_btn.clicked.connect(self._import_card)
        card_ignore = QPushButton("Ignoruj")
        card_ignore.clicked.connect(self.card_banner.hide)
        cb_lay.addWidget(self.card_label, 1)
        cb_lay.addWidget(self.card_import_btn)
        cb_lay.addWidget(card_ignore)
        self.card_banner.hide()
        self._card: Card | None = None

        # Lista grup
        left = QGroupBox("Materiały z importu", self)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Nazwa", "Typ", "Pliki", "Data wykonania", "Lokalizacja z GPS", "Kategoria"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        for i, w in enumerate((130, 60, 95, 140, 230, 150)):
            self.tree.setColumnWidth(i, w)
        self.tree.itemSelectionChanged.connect(self._on_tree_select)
        b_all = QPushButton("Zaznacz wszystkie")
        b_all.clicked.connect(self.tree.selectAll)
        b_un = QPushButton("Zaznacz bez kategorii")
        b_un.clicked.connect(self._select_unassigned)
        self.retry_btn = QPushButton("Ponów rozpoznawanie lokalizacji")
        self.retry_btn.clicked.connect(self._retry_geo)
        self.geo_status = plain_label("", wrap=True, hint=True)
        ll = QVBoxLayout(left)
        ll.addWidget(self.tree, 1)
        ll.addWidget(flow(b_all, b_un, self.retry_btn))
        ll.addWidget(shrinkable(self.geo_status))

        # Kategoria
        catbox = QGroupBox("Kategoria lotu (wybierz ręcznie)", self)
        cl = QVBoxLayout(catbox)
        self.scope_all = QRadioButton("wszystkich materiałów z importu")
        self.scope_sel = QRadioButton("zaznaczonych grup na liście")
        self.scope_all.setChecked(True)
        self.scope_all.toggled.connect(lambda _c: self._sync_form())
        scope_row = QGridLayout()
        scope_row.addWidget(QLabel("Przypisz do:"), 0, 0)
        scope_row.addWidget(self.scope_all, 0, 1)
        scope_row.addWidget(self.scope_sel, 1, 1)
        cl.addLayout(scope_row)
        bold = QFont()
        bold.setBold(True)
        self.category_boxes: dict[str, QCheckBox] = {}
        for key, label in pl.CATEGORY_LABELS.items():
            # Pola wyboru (checkbox), ale wzajemnie wykluczające się – jedna kategoria naraz
            cb = QCheckBox(label)
            cb.setFont(bold)
            cb.clicked.connect(lambda checked, k=key: self._on_category_clicked(k, checked))
            self.category_boxes[key] = cb
            cl.addWidget(cb)
        info_row = QHBoxLayout()
        self.selection_info = plain_label("", wrap=True, hint=True)
        self.split_btn = QPushButton("Wydziel jako osobne")
        self.split_btn.clicked.connect(self._split_selection)
        info_row.addWidget(self.selection_info, 1)
        info_row.addWidget(self.split_btn)
        cl.addLayout(info_row)

        # Formularze
        self.form_box = QGroupBox("Dane", self)
        self.forms = QStackedWidget(self.form_box)
        fbl = QVBoxLayout(self.form_box)
        fbl.addWidget(self.forms)
        self.empty_form = plain_label("Wybierz kategorię powyżej, aby wyświetlić pola formularza.", wrap=True, hint=True)
        self.forms.addWidget(self.empty_form)
        self.forms.addWidget(self._build_hobby())
        self.forms.addWidget(self._build_detail())

        # Podgląd
        prev = QGroupBox("Podgląd struktury folderów", self)
        self.preview = QTreeWidget()
        self.preview.setHeaderLabels(["Folder", "Zawartość"])
        self.preview.setColumnWidth(0, 300)
        self.errors_label = plain_label("", wrap=True)
        pl_ = QVBoxLayout(prev)
        pl_.addWidget(self.preview, 1)
        pl_.addWidget(self.errors_label)

        # Każdy panel można poszerzać/zwężać przeciągając uchwyty; za mało miejsca = pasek przewijania
        right = splitter(Qt.Orientation.Vertical, "sort_right", scrollable(catbox), scrollable(self.form_box), prev,
                         sizes=[190, 260, 260], stretch=[0, 1, 1])
        split = splitter(Qt.Orientation.Horizontal, "sort_columns", left, right,
                         sizes=[760, 540], stretch=[3, 2])

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.undo_btn = QPushButton("Cofnij ostatni import")
        self.undo_btn.clicked.connect(self._undo)
        self.move_btn = QPushButton("Przenieś materiały")
        self.move_btn.setEnabled(False)
        self.move_btn.clicked.connect(self._move)
        self.progress.setMinimumWidth(60)
        bottom = QHBoxLayout()
        bottom.addWidget(self.progress, 1)
        bottom.addWidget(flow(self.undo_btn, self.move_btn), 0)
        status_row = QHBoxLayout()
        self.status = shrinkable(plain_label("Wybierz folder importu (np. kartę pamięci) i kliknij „Wczytaj”."))
        self.net_status = shrinkable(plain_label("", hint=True))
        credit = shrinkable(QLabel(f'Dane lokalizacji: <a href="{ATTRIBUTION_URL}">{ATTRIBUTION}</a>'))
        credit.setOpenExternalLinks(True)
        credit.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        status_row.addWidget(self.status, 3)
        status_row.addWidget(self.net_status, 1)
        status_row.addWidget(credit, 2)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(self.card_banner)
        lay.addWidget(splitter(Qt.Orientation.Vertical, "sort_main", scrollable(folders), split,
                               sizes=[130, 700], stretch=[0, 1]), 1)
        lay.addLayout(bottom)
        lay.addLayout(status_row)
        QShortcut(QKeySequence.StandardKey.SelectAll, self.tree, activated=self.tree.selectAll)

    def _build_hobby(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        f.addRow(plain_label("Miejscowość i pobliski obiekt są rozpoznawane z GPS każdego materiału osobno. "
                             "Jeśli w promieniu nie ma charakterystycznego obiektu, jego poziom jest pomijany.\n"
                             "Struktura: Hobby / Miejscowość / [Obiekt] / WIDEO | Zdjęcia", wrap=True, hint=True))
        self.hobby_town = QLineEdit()
        self.hobby_town.textChanged.connect(lambda t: self._on_field_changed("place", t))
        self.hobby_object = QLineEdit()
        self.hobby_object.textChanged.connect(lambda t: self._on_field_changed("name", t))
        f.addRow("Miejscowość ręcznie:", self.hobby_town)
        f.addRow("", plain_label("Tylko dla materiałów bez GPS lub gdy lokalizacji nie rozpoznano.", hint=True))
        f.addRow("Obiekt ręcznie:", self.hobby_object)
        f.addRow("", plain_label("Opcjonalnie – gdy nie rozpoznano obiektu (np. bez internetu).", hint=True))
        self.hobby_summary = plain_label("", wrap=True)
        f.addRow(self.hobby_summary)
        dec = QGroupBox("Do zatwierdzenia")
        dl = QVBoxLayout(dec)
        dl.addWidget(plain_label("Obiekt w pobliżu jest tylko propozycją – GPS drona nie dowodzi, że obiekt jest "
                                 "w kadrze. Zatwierdź go, wybierz inny lub „bez obiektu”. Przy niejednoznacznej "
                                 "miejscowości wybierz właściwą. Decyzje są zapamiętywane dla tego miejsca.",
                                 wrap=True, hint=True))
        self.decision_tree = QTreeWidget()
        self.decision_tree.setHeaderLabels(["Co", "Propozycja", "Grupy", "Status"])
        self.decision_tree.setRootIsDecorated(False)
        self.decision_tree.setColumnWidth(0, 85)
        self.decision_tree.setColumnWidth(1, 210)
        self.decision_tree.setMaximumHeight(130)
        self.decision_tree.itemSelectionChanged.connect(self._on_decision_selected)
        dl.addWidget(self.decision_tree)
        row = QHBoxLayout()
        self.choice_combo = QComboBox()
        b_ok = QPushButton("Zatwierdź wybór")
        b_ok.clicked.connect(lambda: self._apply_decision(False))
        b_none = QPushButton("Bez obiektu")
        b_none.clicked.connect(lambda: self._apply_decision(True))
        row.addWidget(self.choice_combo, 1)
        row.addWidget(b_ok)
        row.addWidget(b_none)
        dl.addLayout(row)
        f.addRow(dec)
        return w

    def _build_detail(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.existing_label = QLabel("Dołącz do istniejącego:")
        self.existing_combo = QComboBox()
        self.existing_combo.activated.connect(self._on_existing_selected)
        self.name_label = QLabel("Nazwa *")
        self.name_edit = QLineEdit()
        self.name_edit.textChanged.connect(lambda t: self._on_field_changed("name", t))
        self.date_label = QLabel("Data *")
        self.date_field = DateField()
        self.date_field.changed.connect(lambda t: self._on_field_changed("date", t))
        self.date_suggest = QPushButton()
        self.date_suggest.clicked.connect(self._use_date_suggestion)
        self.place_label = QLabel("Miejscowość *")
        self.place_combo = QComboBox()
        self.place_combo.setEditable(True)
        self.place_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.place_combo.editTextChanged.connect(lambda t: self._on_field_changed("place", t))
        self.place_suggest = QPushButton()
        self.place_suggest.clicked.connect(self._use_place_suggestion)
        f.addRow(self.existing_label, self.existing_combo)
        f.addRow(self.name_label, self.name_edit)
        drow = QHBoxLayout()
        drow.addWidget(self.date_field, 1)
        drow.addWidget(self.date_suggest)
        f.addRow(self.date_label, drow)
        prow = QHBoxLayout()
        prow.addWidget(self.place_combo, 1)
        prow.addWidget(self.place_suggest)
        f.addRow(self.place_label, prow)
        self.detail_hint = plain_label("", wrap=True, hint=True)
        f.addRow(self.detail_hint)
        return w

    # ══ Foldery i wczytywanie ══════════════════════════════════
    def _choose_source(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Wybierz kartę pamięci lub folder z materiałami",
                                                  self.source.text())
        if folder:
            self.source.setText(str(Path(folder)))
            self.load_source()

    def _choose_target(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Wybierz folder archiwum", self.target.text())
        if folder:
            self.target.setText(str(Path(folder)))

    def _on_target_changed(self, _t: str) -> None:
        self._load_existing()
        self._schedule_refresh()

    def load_source(self) -> None:
        source = self.source.text().strip()
        if not source:
            QMessageBox.warning(self, "Sortowanie", "Najpierw wybierz folder importu.")
            return
        if self.busy:
            return
        if self.groups and any(self.assignment_of.values()):
            if QMessageBox.question(self, "Sortowanie", "Wczytać nowy import? Bieżące przypisania kategorii "
                                    "(nieprzeniesione) zostaną utracone.") != QMessageBox.StandardButton.Yes:
                return
        self.settings.set("last_source", source)
        self._set_busy(True, "Wczytywanie materiałów i odczyt metadanych…")
        self.import_id += 1
        threading.Thread(target=self._scan_worker, args=(self.import_id, source), daemon=True).start()

    def _scan_worker(self, import_id: int, source: str) -> None:
        try:
            groups = scan_source(source, lambda i, n: self.poster.post(("scan_progress", import_id, i, n)))
            imported = self._find_imported(groups, source)
            self.poster.post(("scanned", import_id, groups, None, imported))
        except OSError as exc:
            self.poster.post(("scanned", import_id, [], str(exc), set()))

    def _find_imported(self, groups: list[MediaGroup], source: str) -> set[str]:
        """Grupy, których pliki zdjęć/filmów są już w archiwum (zaimportowane lub w katalogu)."""
        from ..media import SIDECAR_EXT
        out = set()
        for g in groups:
            media = [p for p in g.files if p.suffix.lower() not in SIDECAR_EXT]
            try:
                if media and all(self.catalog.is_imported(p.name, p.stat().st_size, p.stat().st_mtime, source)
                                 for p in media):
                    out.add(g.key)
            except OSError:
                continue
        return out

    def _on_scanned(self, groups: list[MediaGroup], error: str | None, imported: set[str]) -> None:
        self._set_busy(False)
        if error:
            QMessageBox.critical(self, "Sortowanie", f"Nie można wczytać folderu:\n{error}")
            return
        self.all_groups = groups
        self.imported_keys = imported
        if not self.show_imported.isChecked():
            groups = [g for g in groups if g.key not in imported]
        self.groups = groups
        self.group_by_key = {g.key: g for g in groups}
        self.assignment_of = {g.key: None for g in groups}
        self.places = {}
        self.poi_requested = set()
        self.current = None
        self.scope_all.setChecked(True)
        self._fill_tree()
        for g in groups:
            if g.gps:
                self.worker.submit(self.import_id, "town", g.key, g.gps)
        total = sum(len(g.files) for g in groups)
        hidden = len(self.all_groups) - len(groups)
        self.status.setText(f"Wczytano {groups_word(len(groups))} materiałów ({total} {files_word(total)}, "
                            f"{human_size(sum(g.size for g in groups))}). Wybierz kategorię."
                            + (f" Ukryto {groups_word(hidden)} już zaimportowanych." if hidden else ""))
        self._sync_form()

    # ══ Karta pamięci ══════════════════════════════════════════
    def _poll_cards(self) -> None:
        if self.busy:
            return
        new = self.card_watcher.poll()
        if new:
            self._show_card(new[0])
        elif self._card and self._card.root not in self.card_watcher.known:
            self._card = None
            self.card_banner.hide()             # karta wyjęta

    def _offer_present_cards(self) -> None:
        if not self.groups:
            cards = self.card_watcher.current()
            if cards:
                self._show_card(cards[0])

    def _show_card(self, card: Card) -> None:
        self._card = card
        self.card_label.setText(f"Wykryto kartę pamięci {card.title} z folderem DCIM. Kliknij „Importuj z karty”, "
                                "aby wczytać nowe materiały – zostaną skopiowane do archiwum po wybraniu kategorii, "
                                "a karta pozostanie bez zmian.")
        self.card_banner.show()

    def _import_card(self) -> None:
        if self._card is None or self.busy:
            return
        self.card_banner.hide()
        self.mode_copy.setChecked(True)
        self.show_imported.setChecked(False)
        self.source.setText(self._card.dcim)
        self.load_source()

    def _on_mode_changed(self) -> None:
        self.settings.set("import_mode", core.MODE_COPY if self.mode_copy.isChecked() else core.MODE_MOVE)
        self.move_btn.setText("Kopiuj do archiwum" if self.mode_copy.isChecked() else "Przenieś materiały")

    def _apply_imported_filter(self) -> None:
        if not self.all_groups:
            return
        show_all = self.show_imported.isChecked()
        groups = self.all_groups if show_all else [g for g in self.all_groups if g.key not in self.imported_keys]
        self.groups = groups
        self.group_by_key = {g.key: g for g in groups}
        for g in groups:
            self.assignment_of.setdefault(g.key, None)
        self.assignment_of = {k: v for k, v in self.assignment_of.items() if k in self.group_by_key}
        self._fill_tree()
        self._sync_form()

    # ══ Lista grup ═════════════════════════════════════════════
    def _fill_tree(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        self.items = {}
        for g in self.groups:
            item = QTreeWidgetItem()
            item.setData(0, Qt.ItemDataRole.UserRole, g.key)
            self.items[g.key] = item
            self.tree.addTopLevelItem(item)
            self._set_row(g)
        self.tree.blockSignals(False)
        self._update_geo_status()

    def _row_values(self, g: MediaGroup) -> list[str]:
        date_txt = f"{g.captured:%Y-%m-%d %H:%M} ({g.captured_source})" if g.captured else "nieznana"
        if g.gps is None:
            loc = "brak GPS"
        elif g.key not in self.places:
            loc = "rozpoznawanie…"
        else:
            p = self.places[g.key]
            if p.town:
                loc = p.town + (" (wybrano)" if p.town_decided else "")
                if p.object_choice:
                    loc += f" / {p.object_name} ✓"
                elif p.object_choice == "" and p.objects:
                    loc += " / bez obiektu"
                elif p.objects:
                    prop = p.proposed_object
                    loc += f" / {prop.name}? ({prop.distance_m:.0f} m, do zatwierdzenia)"
                elif p.object_failed:
                    loc += " / obiekty: nie sprawdzono"
                elif g.key in self.poi_requested and not p.object_checked:
                    loc += " / szukanie obiektów…"
            elif p.ambiguous:
                loc = "do wyboru: " + " / ".join(p.town_candidates[:4])
            else:
                loc = "nie rozpoznano" + (" (tryb offline)" if self.settings.get("offline")
                                          else " (brak połączenia)" if p.error else "")
            if p.town_source.startswith("cache"):
                loc += " [cache]"
        a = self.assignment_of.get(g.key)
        if a is None:
            category = "— brak —"
        else:
            category = pl.CATEGORY_LABELS[a.category]
            if a.category != pl.CATEGORY_HOBBY and a.name:
                category += f": {a.name}"
        name = g.name + ("  (już w archiwum)" if g.key in self.imported_keys else "")
        return [name, g.kind, g.extensions, date_txt, loc, category]

    def _set_row(self, g: MediaGroup) -> None:
        item = self.items.get(g.key)
        if item is None:
            return
        brush = QBrush(ERROR_COLOR) if self.assignment_of.get(g.key) is None else QBrush()
        for i, v in enumerate(self._row_values(g)):
            item.setText(i, v)
            item.setForeground(i, brush)

    def _update_rows(self, keys=None) -> None:
        for key in (keys if keys is not None else list(self.group_by_key)):
            if key in self.group_by_key:
                self._set_row(self.group_by_key[key])

    def _selected_keys(self) -> list[str]:
        return [i.data(0, Qt.ItemDataRole.UserRole) for i in self.tree.selectedItems()
                if i.data(0, Qt.ItemDataRole.UserRole) in self.group_by_key]

    def _target_keys(self) -> list[str]:
        return [g.key for g in self.groups] if self.scope_all.isChecked() else self._selected_keys()

    def _on_tree_select(self) -> None:
        if self._selected_keys() and not self.scope_sel.isChecked():
            self.scope_sel.setChecked(True)      # wywoła _sync_form
        else:
            self._sync_form()

    def _select_unassigned(self) -> None:
        self.tree.clearSelection()
        first = None
        for g in self.groups:
            if self.assignment_of.get(g.key) is None:
                self.items[g.key].setSelected(True)
                first = first or self.items[g.key]
        if first:
            self.tree.scrollToItem(first)

    # ══ Kategoria i przypisania ════════════════════════════════
    def _members(self, a: pl.Assignment) -> list[MediaGroup]:
        return [g for g in self.groups if self.assignment_of.get(g.key) is a]

    def _set_category_boxes(self, key: str | None) -> None:
        for k, cb in self.category_boxes.items():
            cb.blockSignals(True)
            cb.setChecked(k == key)
            cb.blockSignals(False)

    def _on_category_clicked(self, key: str, checked: bool) -> None:
        keys = self._target_keys()
        cat_key = key if checked else NO_CATEGORY
        self._set_category_boxes(cat_key)
        if not keys:
            self._set_category_boxes(None)
            QMessageBox.information(self, "Sortowanie", "Zaznacz grupy materiałów na liście albo wybierz "
                                                        "„wszystkich materiałów z importu”.")
            return
        if cat_key is NO_CATEGORY:
            for k in keys:
                self.assignment_of[k] = None
        else:
            current = self.current
            members = {g.key for g in self._members(current)} if current else set()
            if current is not None and members == set(keys):
                # Pola Eventów i Zleceń znaczą to samo; w Hobby co innego – przy zmianie z/do Hobby czyścimy
                if pl.CATEGORY_HOBBY in (current.category, cat_key):
                    current.name = current.date = current.place = ""
                    current.edited = set()
                current.category = cat_key
            else:
                new = pl.Assignment(cat_key)     # nowe przypisanie – pusty formularz
                for k in keys:
                    self.assignment_of[k] = new
            self._request_objects()
        self._sync_form()
        self._update_rows(keys)

    def _split_selection(self) -> None:
        if self.current is None:
            return
        new = pl.Assignment(self.current.category)
        for k in self._selected_keys():
            self.assignment_of[k] = new
        self._sync_form()
        self._update_rows()

    def _sync_form(self) -> None:
        keys = self._target_keys()
        assignments = {id(self.assignment_of.get(k)): self.assignment_of.get(k) for k in keys}
        self.split_btn.hide()
        for cb in self.category_boxes.values():
            cb.setEnabled(bool(keys))
        if not self.groups:
            info, self.current = "Najpierw wczytaj materiały.", None
        elif not keys:
            info, self.current = "Zaznacz grupy na liście (Ctrl/Shift + klik), aby przypisać im kategorię.", None
        elif len(assignments) == 1:
            self.current = next(iter(assignments.values()))
            n = len(keys)
            info = f"Wszystkie materiały: {groups_word(n)}." if self.scope_all.isChecked() else f"Zaznaczono: {groups_word(n)}."
            if self.current is not None:
                total = len(self._members(self.current))
                if total > n:
                    info += (f" Należą do przypisania obejmującego {groups_word(total)} – zmiany w formularzu "
                             "dotyczą wszystkich. Aby utworzyć osobne przypisanie, użyj przycisku obok.")
                    self.split_btn.show()
        else:
            self.current = None
            info = f"Zaznaczone grupy ({len(keys)}) mają różne kategorie. Wybierz kategorię, aby przypisać im wspólną."
        self.selection_info.setText(info)

        self._suspend = True
        try:
            a = self.current
            self._set_category_boxes(a.category if a else None)
            if a is not None:
                self._apply_suggestions(a)
            self.hobby_town.setText(a.place if a else "")
            self.hobby_object.setText(a.name if a else "")
            self.name_edit.setText(a.name if a else "")
            self.date_field.setText(a.date if a else "")
            self.place_combo.setEditText(a.place if a else "")
        finally:
            self._suspend = False
        self._show_form()
        self._schedule_refresh()

    def _show_form(self) -> None:
        a = self.current
        if a is None:
            self.form_box.setTitle("Dane")
            self.forms.setCurrentIndex(0)
            return
        if a.category == pl.CATEGORY_HOBBY:
            self.form_box.setTitle("Hobby / Luźne loty")
            self.forms.setCurrentIndex(1)
            self._update_hobby_summary()
            self._refresh_decisions()
            return
        is_event = a.category == pl.CATEGORY_EVENT
        self.form_box.setTitle("Eventy" if is_event else "Zlecenia")
        self.existing_label.setText("Dołącz do istniejącego wydarzenia:" if is_event else "Dołącz do istniejącego zlecenia:")
        self.name_label.setText("Nazwa wydarzenia *" if is_event else "Nazwa / opis zlecenia *")
        self.date_label.setText("Data wydarzenia *" if is_event else "Data realizacji *")
        self.place_label.setText("Miejscowość *" if is_event else "Lokalizacja zlecenia *")
        self.detail_hint.setText(
            "Miejscowość rozpoznana z GPS – możesz ją poprawić. Data zaproponowana na podstawie daty wykonania "
            "materiałów (nie daty modyfikacji pliku) – sprawdź ją.\n"
            "Struktura: Eventy / Miejscowość / RRRR-MM-DD Nazwa / WIDEO | Zdjęcia" if is_event else
            "Lokalizacja może być miejscowością, adresem lub własnym określeniem miejsca. Wszystkie materiały "
            "zlecenia trafią do jednego folderu (bez podziału według GPS).\n"
            "Struktura: Zlecenia / Lokalizacja / RRRR-MM-DD Nazwa / WIDEO | Zdjęcia")
        self._load_existing()
        self._update_suggestion_buttons()
        self.forms.setCurrentIndex(2)

    def _apply_suggestions(self, a: pl.Assignment) -> None:
        if a.category != pl.CATEGORY_EVENT:
            return
        members = self._members(a)
        if "date" not in a.edited:
            a.date = pl.suggest_date(members) or a.date
        if "place" not in a.edited:
            a.place = pl.suggest_town(members, self.places) or a.place

    def _update_suggestion_buttons(self) -> None:
        a = self.current
        if a is None or a.category == pl.CATEGORY_HOBBY:
            return
        members = self._members(a)
        d = pl.suggest_date(members)
        t = pl.suggest_town(members, self.places)
        self._suspend = True
        text = self.place_combo.currentText()
        self.place_combo.clear()
        self.place_combo.addItems(pl.town_options(members, self.places))
        self.place_combo.setEditText(text)
        self._suspend = False
        self.date_suggest.setText(f"Z materiałów: {d}" if d else "Brak daty w materiałach")
        self.date_suggest.setEnabled(bool(d))
        self.place_suggest.setText(f"Z GPS: {t}" if t else "Brak propozycji z GPS")
        self.place_suggest.setEnabled(bool(t))

    def _use_date_suggestion(self) -> None:
        if self.current:
            self.date_field.setText(pl.suggest_date(self._members(self.current)))

    def _use_place_suggestion(self) -> None:
        if self.current:
            self.place_combo.setEditText(pl.suggest_town(self._members(self.current), self.places))

    def _on_field_changed(self, field_name: str, value: str) -> None:
        if self._suspend or self.current is None:
            return
        # pola Hobby i Eventów/Zleceń dzielą atrybuty przypisania – aktualizujemy tylko widoczny formularz
        hobby = self.current.category == pl.CATEGORY_HOBBY
        if hobby != (self.forms.currentIndex() == 1):
            return
        setattr(self.current, field_name, value)
        self.current.edited.add(field_name)
        if field_name == "name":
            self._update_rows([g.key for g in self._members(self.current)])
        self._schedule_refresh()

    # ══ Istniejące wydarzenia / zlecenia ═══════════════════════
    def _load_existing(self) -> None:
        a = self.current
        self._existing = [] if a is None or a.category == pl.CATEGORY_HOBBY else \
            pl.list_existing(self.target.text().strip(), a.category)
        self.existing_combo.clear()
        self.existing_combo.addItem(NEW_TARGET)
        for e in self._existing:
            self.existing_combo.addItem(e.label)

    def _on_existing_selected(self, index: int) -> None:
        if index <= 0 or self.current is None or index - 1 >= len(self._existing):
            return
        e = self._existing[index - 1]
        self.name_edit.setText(e.name)
        self.date_field.setText(e.date)
        self.place_combo.setEditText(e.place)
        self.current.edited.update({"name", "date", "place"})

    # ══ Lokalizacje (Overpass w tle) ═══════════════════════════
    def _request_objects(self) -> None:
        for g in self.groups:
            a = self.assignment_of.get(g.key)
            p = self.places.get(g.key)
            if (a is not None and a.category == pl.CATEGORY_HOBBY and g.gps and p and p.town
                    and not p.object_checked and g.key not in self.poi_requested):
                self.poi_requested.add(g.key)
                self.worker.submit(self.import_id, "poi", g.key, g.gps)
        self._update_geo_status()

    def _on_town(self, key: str, result: TownResult, source: str, error: str) -> None:
        self.places[key] = Place(town=result.town, town_candidates=list(result.candidates),
                                 town_reason=result.reason, town_source=source,
                                 town_decided=source == SOURCE_DECISION, error=error)
        self._request_objects()
        self._after_geo_update(key)

    def _on_poi(self, key: str, cands: list, source: str, error: str, remembered: str | None) -> None:
        place = self.places.get(key)
        if place is None:
            return
        place.objects = list(cands)
        place.object_checked = True
        place.object_failed = source == SOURCE_NONE and bool(error)
        if remembered is not None and (remembered == "" or any(c.osm_id == remembered for c in cands)):
            place.object_choice = remembered
            place.object_choice_source = SOURCE_DECISION
        if error and not place.error:
            place.error = error
        self._after_geo_update(key)

    def _after_geo_update(self, key: str) -> None:
        self._update_rows([key])
        a = self.assignment_of.get(key)
        if a is not None and a is self.current:
            self._suspend = True
            try:
                self._apply_suggestions(a)
                if a.category == pl.CATEGORY_EVENT:
                    if self.date_field.text() != a.date:
                        self.date_field.setText(a.date)
                    if self.place_combo.currentText() != a.place:
                        self.place_combo.setEditText(a.place)
            finally:
                self._suspend = False
            if a.category == pl.CATEGORY_HOBBY:
                self._update_hobby_summary()
                self._refresh_decisions()
            else:
                self._update_suggestion_buttons()
        elif a is not None:
            self._apply_suggestions(a)
        self._update_geo_status()
        self._schedule_refresh()

    def _update_geo_status(self) -> None:
        with_gps = [g for g in self.groups if g.gps]
        if not self.groups:
            self.geo_status.setText("")
            return
        pending = sum(1 for g in with_gps if g.key not in self.places)
        pending_poi = sum(1 for k in self.poi_requested if k in self.places and not self.places[k].object_checked)
        unresolved = sum(1 for g in with_gps if g.key in self.places and not self.places[g.key].town
                         and not self.places[g.key].ambiguous)
        ambiguous = sum(1 for g in with_gps if g.key in self.places and self.places[g.key].ambiguous)
        parts = [f"GPS: {len(with_gps)} z {groups_word(len(self.groups))}"]
        if pending or pending_poi:
            parts.append(f"rozpoznawanie: {pending} miejscowości, {pending_poi} obiektów…")
        if ambiguous:
            parts.append(f"niejednoznaczna miejscowość: {ambiguous}")
        if unresolved:
            parts.append(f"nie rozpoznano: {unresolved}")
        if len(self.groups) - len(with_gps):
            parts.append(f"bez GPS: {len(self.groups) - len(with_gps)}")
        if self.settings.get("offline"):
            parts.append("tryb offline – używane są tylko zapisane wyniki")
        elif self.geo.network_down:
            parts.append("brak połączenia z Overpass – używane są zapisane dane; możesz uzupełnić lokalizację ręcznie")
        self.geo_status.setText(" · ".join(parts))

    def _update_hobby_summary(self) -> None:
        a = self.current
        if a is None or a.category != pl.CATEGORY_HOBBY:
            return
        members = self._members(a)
        ok = sum(1 for g in members if (self.places.get(g.key) or Place()).town)
        pending = sum(1 for g in members if pl.is_pending(g, self.places, need_object=True))
        missing = len(members) - ok - pending
        unchecked = sum(1 for g in members if (self.places.get(g.key) or Place()).object_failed)
        to_decide = sum(1 for g in members if (p := self.places.get(g.key)) is not None
                        and (p.ambiguous or p.needs_object_decision))
        text = f"Rozpoznano lokalizację: {ok} z {groups_word(len(members))}."
        if pending:
            text += f" Trwa rozpoznawanie: {pending}."
        if missing:
            text += f" Bez lokalizacji: {missing} – zostanie użyta miejscowość wpisana ręcznie."
        if unchecked:
            text += (f" Grupy bez sprawdzonych obiektów: {unchecked} – wpisz obiekt ręcznie, kliknij "
                     "„Ponów rozpoznawanie” albo pozostaw bez poziomu obiektu.")
        if to_decide:
            text += f" Czeka na Twoją decyzję: {groups_word(to_decide)} (lista poniżej)."
        warn = (missing and not a.place.strip()) or unchecked or to_decide
        self.hobby_summary.setText(text)
        self.hobby_summary.setStyleSheet("color: #c62828;" if warn else "")

    def _retry_geo(self) -> None:
        self.geo.reset_network_state()
        for g in self.groups:
            p = self.places.get(g.key)
            if g.gps and p is not None and not p.town and not p.ambiguous:
                del self.places[g.key]
                self.worker.submit(self.import_id, "town", g.key, g.gps)
            elif p is not None and p.object_failed:
                p.object_checked = False
                p.object_failed = False
                self.poi_requested.discard(g.key)
        self._request_objects()
        self._update_rows()
        self._schedule_refresh()

    # ══ Decyzje ═══════════════════════════════════════════════
    def _refresh_decisions(self) -> None:
        a = self.current
        selected = self.decision_tree.currentItem()
        selected_key = selected.data(0, Qt.ItemDataRole.UserRole) if selected else None
        self.decision_tree.blockSignals(True)
        self.decision_tree.clear()
        self._decision_rows = {}
        if a is None or a.category != pl.CATEGORY_HOBBY:
            self.decision_tree.blockSignals(False)
            return
        towns: dict[tuple, list[str]] = {}
        objects: dict[str, list[str]] = {}
        for g in self._members(a):
            p = self.places.get(g.key)
            if p is None:
                continue
            if p.town_candidates and (p.ambiguous or p.town_decided):
                towns.setdefault(tuple(sorted(p.town_candidates)), []).append(g.key)
            if p.town and p.objects:
                objects.setdefault(p.objects[0].osm_id, []).append(g.key)
        rows = []
        for cands, keys in towns.items():
            chosen = {self.places[k].town for k in keys if self.places[k].town}
            todo = any(self.places[k].ambiguous for k in keys)
            status = "wybierz miejscowość" if todo else f"wybrano: {', '.join(sorted(chosen))}"
            rows.append(("town|" + "|".join(cands), "town", keys, ["Miejscowość", " / ".join(cands), str(len(keys)), status], todo))
        for oid, keys in objects.items():
            prop = self.places[keys[0]].objects[0]
            dist = min(next(o.distance_m for o in self.places[k].objects if o.osm_id == oid) for k in keys)
            choices = {self.places[k].object_choice for k in keys}
            if None in choices:
                status, todo = ("do zatwierdzenia" if choices == {None} else "częściowo zatwierdzone"), True
            else:
                status = "zatwierdzono: " + ", ".join(sorted({self.places[k].object_name or NO_OBJECT for k in keys}))
                todo = False
            rows.append(("obj|" + oid, "obj", keys, ["Obiekt", f"{prop.name} ({prop.category}, {dist:.0f} m)",
                                                      str(len(keys)), status], todo))
        target = None
        for i, (rkey, kind, keys, values, todo) in enumerate(rows):
            item = QTreeWidgetItem(values)
            item.setData(0, Qt.ItemDataRole.UserRole, rkey)
            if todo:
                for c in range(4):
                    item.setForeground(c, QBrush(ERROR_COLOR))
            self.decision_tree.addTopLevelItem(item)
            self._decision_rows[id(item)] = (kind, keys)
            item.setData(1, Qt.ItemDataRole.UserRole, i)
            if rkey == selected_key or (target is None and todo):
                target = item
        target = target or self.decision_tree.topLevelItem(0)
        self.decision_tree.blockSignals(False)
        if target is not None:
            self.decision_tree.setCurrentItem(target)
            self._on_decision_selected()
        else:
            self.choice_combo.clear()

    def _decision_for(self, item) -> tuple[str, list[str]] | None:
        return self._decision_rows.get(id(item)) if item is not None else None

    def _on_decision_selected(self) -> None:
        row = self._decision_for(self.decision_tree.currentItem())
        if not row:
            return
        kind, keys = row
        self.choice_combo.clear()
        if kind == "town":
            p = self.places[keys[0]]
            self._choice_ids = list(p.town_candidates)
            self.choice_combo.addItems(self._choice_ids)
            current = p.town if p.town in p.town_candidates else p.town_candidates[0]
            self.choice_combo.setCurrentIndex(self._choice_ids.index(current))
        else:
            best: dict[str, object] = {}
            for k in keys:
                for o in self.places[k].objects:
                    if o.osm_id not in best or o.distance_m < best[o.osm_id].distance_m:
                        best[o.osm_id] = o
            ordered = sorted(best.values(), key=lambda o: (o.priority, o.distance_m))
            self._choice_ids = [o.osm_id for o in ordered] + [""]
            self.choice_combo.addItems([f"{o.name} ({o.category}, {o.distance_m:.0f} m)" for o in ordered] + [NO_OBJECT])
            choice = self.places[keys[0]].object_choice
            current_id = choice if choice is not None else ordered[0].osm_id
            self.choice_combo.setCurrentIndex(self._choice_ids.index(current_id) if current_id in self._choice_ids else 0)

    def _apply_decision(self, no_object: bool) -> None:
        row = self._decision_for(self.decision_tree.currentItem())
        if not row:
            QMessageBox.information(self, "Sortowanie", "Wybierz pozycję z listy „Do zatwierdzenia”.")
            return
        kind, keys = row
        idx = self.choice_combo.currentIndex()
        if kind == "town":
            if no_object or idx < 0:
                return
            choice = self._choice_ids[idx]
            for k in keys:
                p, g = self.places[k], self.group_by_key[k]
                p.town, p.town_decided, p.town_source = choice, True, SOURCE_DECISION
                self.decisions.remember_town(g.gps[0], g.gps[1], p.town_candidates, choice)
            self._request_objects()
        else:
            choice = "" if no_object or idx < 0 else self._choice_ids[idx]
            skipped = 0
            for k in keys:
                p, g = self.places[k], self.group_by_key[k]
                if choice and all(o.osm_id != choice for o in p.objects):
                    skipped += 1
                    continue
                p.object_choice, p.object_choice_source = choice, "użytkownik"
                self.decisions.remember_object(g.gps[0], g.gps[1], [o.osm_id for o in p.objects], choice)
            if skipped:
                QMessageBox.information(self, "Sortowanie", f"Wybrany obiekt jest poza promieniem dla "
                                        f"{groups_word(skipped)} – te grupy nadal czekają na decyzję.")
        self._update_rows(keys)
        self._update_hobby_summary()
        self._refresh_decisions()
        self._schedule_refresh()

    # ══ Podgląd na żywo ════════════════════════════════════════
    def _schedule_refresh(self) -> None:
        self._refresh_timer.start()

    def _refresh_preview(self) -> None:
        target = self.target.text().strip()
        self.plan = pl.build_plan(self.groups, self.assignment_of, self.places, target)
        folder_error = pl.check_folders(self.source.text().strip(), target)
        errors = list(self.plan.errors)
        if folder_error:
            errors.insert(0, folder_error)
        self.preview.clear()
        root = QTreeWidgetItem([(Path(target).name or target) if target else "(wybierz archiwum)", ""])
        self.preview.addTopLevelItem(root)
        counts: dict[Path, int] = {}
        for g in self.groups:
            rel = self.plan.group_dirs.get(g.key)
            if rel is not None:
                counts[rel] = counts.get(rel, 0) + len(g.files)
        nodes: dict[Path, QTreeWidgetItem] = {}
        for rel in sorted(counts, key=lambda p: [s.lower() for s in p.parts]):
            parent = root
            for i in range(1, len(rel.parts) + 1):
                sub = Path(*rel.parts[:i])
                if sub not in nodes:
                    exists = bool(target) and (Path(target) / sub).is_dir()
                    nodes[sub] = QTreeWidgetItem(parent, [rel.parts[i - 1], "istnieje" if exists else ""])
                parent = nodes[sub]
            parent.setText(1, f"+{counts[rel]} {files_word(counts[rel])}")
        unassigned = len(self.plan.unassigned)
        if unassigned:
            it = QTreeWidgetItem([f"Bez kategorii: {groups_word(unassigned)}", ""])
            it.setForeground(0, QBrush(ERROR_COLOR))
            self.preview.addTopLevelItem(it)
        incomplete = len(self.groups) - unassigned - len(self.plan.group_dirs)
        if incomplete > 0:
            it = QTreeWidgetItem([f"Niekompletne dane: {groups_word(incomplete)}", ""])
            it.setForeground(0, QBrush(ERROR_COLOR))
            self.preview.addTopLevelItem(it)
        self.preview.expandAll()
        shown = errors[:6] + ([f"… i {len(errors) - 6} więcej"] if len(errors) > 6 else [])
        if not errors and self.plan.ready:
            n = len(self.plan.moves)
            self.errors_label.setText(f"Gotowe do przeniesienia: {n} {files_word(n)}.")
            self.errors_label.setStyleSheet("color: #2e7d32;")
        else:
            self.errors_label.setText("\n".join(f"• {e}" for e in shown))
            self.errors_label.setStyleSheet("color: #c62828;")
        self.move_btn.setEnabled(self.plan.ready and not folder_error and not self.busy)

    # ══ Przenoszenie i cofanie ═════════════════════════════════
    def _catalog_info(self) -> dict[str, dict]:
        """Dane kategorii dla katalogu materiałów (wspólnego z zakładką mapy)."""
        info: dict[str, dict] = {}
        for g in self.groups:
            a = self.assignment_of.get(g.key)
            if a is None or g.key not in self.plan.group_dirs:
                continue
            place = self.places.get(g.key)
            if a.category == pl.CATEGORY_HOBBY:
                town, obj = pl.hobby_location(g, place, a)
                data = {"category": "hobby", "town": town, "object": obj}
            else:
                d = pl.parse_date(a.date)
                data = {"category": a.category, "town": a.place.strip(), "event_name": a.name.strip(),
                        "event_date": d.isoformat() if d else None}
            for f in g.files:
                info[str(f)] = data
        return info

    def _move(self) -> None:
        self._refresh_preview()
        plan = self.plan
        if plan is None or not plan.ready or self.busy:
            QMessageBox.warning(self, "Sortowanie", "Uzupełnij kategorię i wymagane pola przed przeniesieniem.")
            return
        size = sum(g.size for g in self.groups if g.key in plan.group_dirs)
        copy = self.mode_copy.isChecked()
        verb = "Skopiować" if copy else "Przenieść"
        extra = "\n\nOryginały pozostaną w miejscu importu (np. na karcie)." if copy else ""
        if QMessageBox.question(self, "Sortowanie", f"{verb} {len(plan.moves)} {files_word(len(plan.moves))} "
                                f"({human_size(size)}) do archiwum?\n\n{self.target.text()}{extra}") != QMessageBox.StandardButton.Yes:
            return
        self.settings.set("target_root", self.target.text().strip())
        self.settings.save()
        info = self._catalog_info()
        mode = core.MODE_COPY if copy else core.MODE_MOVE
        self._set_busy(True, "Kopiowanie materiałów do archiwum…" if copy else "Przenoszenie materiałów…")
        threading.Thread(target=self._move_worker, args=(list(plan.moves), Path(self.target.text().strip()), info, mode),
                         daemon=True).start()

    def _move_worker(self, moves: list[core.Move], root: Path, info: dict, mode: str = core.MODE_MOVE) -> None:
        paths = [m.source for m in moves] + [m.target for m in moves]
        cat.mark_in_flight(paths)                 # skaner mapy nie indeksuje plików w trakcie operacji
        ids: list[int] = []
        progress = lambda i, n, _m: self.poster.post(("move_progress", i, n))  # noqa: E731
        try:
            if mode == core.MODE_COPY:
                done, failed = core.execute_copies(moves, progress)
            else:
                done, failed = core.execute_moves(moves, progress)
            op_id = None
            if done:
                pairs = [(str(m.source), str(m.target)) for m in done]
                try:
                    if mode == core.MODE_COPY:
                        op_id, ids = self.catalog.apply_copies(pairs, info)
                    else:
                        op_id, ids = self.catalog.apply_moves(pairs, info)
                except Exception as exc:          # katalog nie może zablokować operacji na plikach
                    self.poster.post(("net_status", f"Nie zaktualizowano katalogu mapy: {exc}"))
                core.save_history(done, root, op_id=op_id, mode=mode)
        finally:
            cat.clear_in_flight(paths)
        self.poster.post(("moved", done, failed, ids, mode))

    def _on_moved(self, done: list[core.Move], failed: list, ids: list[int], mode: str = core.MODE_MOVE) -> None:
        self._set_busy(False)
        moved = {m.source for m in done}
        if mode == core.MODE_COPY:
            # Pliki zostały na karcie: skopiowane grupy oznaczamy jako zaimportowane i ukrywamy
            copied = {g.key for g in self.all_groups if g.files and all(p in moved for p in g.files)}
            self.imported_keys |= copied
            for k in copied:
                self.assignment_of.pop(k, None)
            self._apply_imported_filter()
        else:
            remaining = [g for g in self.groups if not all(p in moved for p in g.files)]
            for g in remaining:
                g.files = [p for p in g.files if p not in moved]
            self.groups = remaining
            self.all_groups = remaining
            self.group_by_key = {g.key: g for g in remaining}
            self.assignment_of = {k: v for k, v in self.assignment_of.items() if k in self.group_by_key}
            self._fill_tree()
            self._sync_form()
        self._refresh_undo_button()
        msg = f"{'Skopiowano' if mode == core.MODE_COPY else 'Przeniesiono'} {len(done)} {files_word(len(done))}."
        if mode == core.MODE_COPY and done and self._card is not None and not failed:
            msg += f" Oryginały są nadal na karcie {self._card.title} – możesz ją bezpiecznie wysunąć."
        if failed:
            details = "\n".join(f"• {m.source.name}: {err}" for m, err in failed[:10])
            more = f"\n… i {len(failed) - 10} więcej" if len(failed) > 10 else ""
            QMessageBox.warning(self, "Sortowanie", f"{msg}\nNie udało się: {len(failed)}\n\n{details}{more}")
        else:
            QMessageBox.information(self, "Sortowanie", msg + "\n\nMożesz wczytać kolejną kartę i dołączyć ją do "
                                                              "tego samego wydarzenia lub zlecenia.")
        self.status.setText(msg)
        if ids:
            self.catalogChanged.emit(ids)

    def _undo(self) -> None:
        if self.busy:
            return
        moves, root = core.load_history()
        if not moves:
            QMessageBox.information(self, "Sortowanie", "Brak importu do cofnięcia.")
            return
        mode = core.load_history_mode()
        question = (f"Usunąć {len(moves)} {files_word(len(moves))} skopiowanych do archiwum w ostatnim imporcie?\n"
                    "Oryginały (np. na karcie) nie zostaną zmienione." if mode == core.MODE_COPY else
                    f"Przywrócić {len(moves)} {files_word(len(moves))} do poprzednich lokalizacji?")
        if QMessageBox.question(self, "Sortowanie", question) != QMessageBox.StandardButton.Yes:
            return
        self._undo_mode = mode
        self._set_busy(True, "Cofanie ostatniego importu…")
        threading.Thread(target=self._undo_worker, args=(moves, root, core.load_history_op(), core.load_history_mode()),
                         daemon=True).start()

    def _undo_worker(self, moves: list[core.Move], root: Path | None, op_id: int | None,
                     mode: str = core.MODE_MOVE) -> None:
        paths = [m.source for m in moves] + [m.target for m in moves]
        cat.mark_in_flight(paths)
        ids: list[int] = []
        try:
            if mode == core.MODE_COPY:
                done, failed = core.undo_copies(moves, stop_at=root)   # usuwa tylko kopie
                try:
                    ids = self.catalog.remove_copies([str(m.target) for m in done])
                except Exception as exc:
                    self.poster.post(("net_status", f"Nie zaktualizowano katalogu mapy: {exc}"))
                remaining = [m for m in moves if m not in done]
            else:
                done, failed = core.undo_moves(moves, stop_at=root)
                try:
                    ids = self.catalog.undo_moves([(str(m.source), str(m.target)) for m in done], op_id)
                except Exception as exc:
                    self.poster.post(("net_status", f"Nie zaktualizowano katalogu mapy: {exc}"))
                undone = {m.source for m in done}
                remaining = [m for m in moves if m.target not in undone]
            if not failed:
                core.clear_history()
            else:
                core.save_history(remaining, root, op_id=op_id, mode=mode)
        finally:
            cat.clear_in_flight(paths)
        self.poster.post(("undone", done, failed, ids))

    def _on_undone(self, done: list, failed: list, ids: list[int]) -> None:
        self._set_busy(False)
        self._refresh_undo_button()
        self._sync_form()
        copy = getattr(self, "_undo_mode", core.MODE_MOVE) == core.MODE_COPY
        msg = (f"Usunięto {len(done)} {files_word(len(done))} skopiowanych do archiwum." if copy
               else f"Przywrócono {len(done)} {files_word(len(done))}.")
        if copy:
            removed = {str(m.source) for m in done}
            self.imported_keys = {k for k in self.imported_keys
                                  if not any(str(p) in removed for g in self.all_groups if g.key == k for p in g.files)}
            self._apply_imported_filter()
        if failed:
            details = "\n".join(f"• {m.target.name}: {err}" for m, err in failed[:10])
            QMessageBox.warning(self, "Sortowanie", f"{msg}\nNie udało się: {len(failed)}.\n\n{details}")
        self.status.setText(msg if copy else msg + " Wczytaj import ponownie, aby przypisać kategorie.")
        if ids:
            self.catalogChanged.emit(ids)

    def _refresh_undo_button(self) -> None:
        moves, _root = core.load_history()
        self.undo_btn.setEnabled(bool(moves) and not self.busy)

    # ══ Zdarzenia ══════════════════════════════════════════════
    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        self.busy = busy
        self.load_btn.setEnabled(not busy)
        if busy:
            self.move_btn.setEnabled(False)
            self.undo_btn.setEnabled(False)
            self.progress.setValue(0)
        else:
            self._refresh_undo_button()
            self._schedule_refresh()
        if status:
            self.status.setText(status)

    def _on_event(self, ev) -> None:
        kind = ev[0]
        if kind == "scan_progress" and ev[1] == self.import_id:
            self.progress.setMaximum(max(ev[3], 1))
            self.progress.setValue(ev[2])
        elif kind == "scanned" and ev[1] == self.import_id:
            self._on_scanned(ev[2], ev[3], ev[4])
        elif kind == "town" and ev[1] == self.import_id and ev[2] in self.group_by_key:
            self._on_town(ev[2], ev[3], ev[4], ev[5])
        elif kind == "poi" and ev[1] == self.import_id and ev[2] in self.group_by_key:
            self._on_poi(ev[2], ev[3], ev[4], ev[5], ev[6])
        elif kind == "net_status":
            self.net_status.setText(ev[1])
        elif kind == "move_progress":
            self.progress.setMaximum(max(ev[2], 1))
            self.progress.setValue(ev[1])
        elif kind == "moved":
            self._on_moved(ev[1], ev[2], ev[3], ev[4])
        elif kind == "undone":
            self._on_undone(ev[1], ev[2], ev[3])

    def save_state(self) -> None:
        self.settings.set("target_root", self.target.text().strip())
        self.settings.set("last_source", self.source.text().strip())
