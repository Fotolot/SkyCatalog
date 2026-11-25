"""Okna ustawień (Qt)."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QPushButton, QSpinBox, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

from ..config import Settings
from ..osm import ATTRIBUTION, ATTRIBUTION_URL
from ..overpass import ENV_ENDPOINT
from ..scanner import list_drives
from ..tiles import validate_url
from .common import plain_label

THEMES = {"systemowy": "Systemowy", "jasny": "Jasny", "ciemny": "Ciemny"}


class SettingsDialog(QDialog):
    """Overpass (rozpoznawanie miejscowości), podkład mapy i motyw."""

    def __init__(self, parent, settings: Settings, cache_summary: str, decisions_count: int,
                 on_clear_cache, on_clear_decisions) -> None:
        super().__init__(parent)
        self.setWindowTitle("Ustawienia")
        self.settings = settings
        s = settings.as_dict()

        over = QGroupBox("Rozpoznawanie miejscowości – Overpass API (OpenStreetMap)", self)
        form = QFormLayout(over)
        self.overpass = QLineEdit(s["overpass_url"])
        self.cache_days = QSpinBox()
        self.cache_days.setRange(1, 3650)
        self.cache_days.setValue(int(s["cache_days"]))
        self.contact = QLineEdit(s["contact_email"])
        self.offline = QCheckBox("Tryb offline – tylko zapisane dane i decyzje (bez internetu)")
        self.offline.setChecked(bool(s["offline"]))
        form.addRow("Endpoint Overpass API:", self.overpass)
        form.addRow("Odświeżaj dane OSM po (dniach):", self.cache_days)
        form.addRow("Kontakt w User-Agent (opcjonalnie):", self.contact)
        form.addRow(self.offline)
        env = os.environ.get(ENV_ENDPOINT, "").strip()
        if env:
            form.addRow(plain_label(f"Zmienna {ENV_ENDPOINT} ma pierwszeństwo: {env}", wrap=True))
        form.addRow(plain_label(
            "Do serwera wysyłane są wyłącznie współrzędne. Overpass służy tylko do rozpoznania miejscowości "
            "w zakładce sortowania – mapa materiałów go nie używa. Program nie wyszukuje pobliskich obiektów.",
            wrap=True, hint=True))
        cache_row = QHBoxLayout()
        self.cache_label = plain_label(f"Cache OSM: {cache_summary}")
        btn_cache = QPushButton("Wyczyść")
        btn_cache.clicked.connect(lambda: self._confirm("Usunąć zapisane dane OSM?", on_clear_cache,
                                                        self.cache_label, "Cache OSM: pusty"))
        cache_row.addWidget(self.cache_label, 1)
        cache_row.addWidget(btn_cache)
        form.addRow(cache_row)
        dec_row = QHBoxLayout()
        self.dec_label = plain_label(f"Zapamiętane wybory miejscowości: {decisions_count}")
        btn_dec = QPushButton("Wyczyść")
        btn_dec.clicked.connect(lambda: self._confirm("Usunąć zapamiętane wybory miejscowości?", on_clear_decisions,
                                                      self.dec_label, "Zapamiętane wybory miejscowości: 0"))
        dec_row.addWidget(self.dec_label, 1)
        dec_row.addWidget(btn_dec)
        form.addRow(dec_row)

        tiles = QGroupBox("Podkład mapy (kafelki)", self)
        tform = QFormLayout(tiles)
        self.tile_url = QLineEdit(s["tile_url"])
        self.tile_attr = QLineEdit(s["tile_attribution"])
        self.tile_zoom = QSpinBox()
        self.tile_zoom.setRange(2, 22)
        self.tile_zoom.setValue(int(s["tile_max_zoom"]))
        tform.addRow("Adres kafelków:", self.tile_url)
        tform.addRow("Oznaczenie źródła:", self.tile_attr)
        tform.addRow("Maks. przybliżenie:", self.tile_zoom)
        tform.addRow(plain_label(
            "Domyślnie standardowe kafelki OpenStreetMap – zgodnie z zasadami OSMF pobierane są tylko "
            "kafelki oglądanego widoku (bez pobierania obszarów i trybu offline) i zapisywane w cache. "
            "Przy innym dostawcy sprawdź jego warunki i wpisz wymagane oznaczenie źródła.", wrap=True, hint=True))
        btn_default = QPushButton("Przywróć domyślny podkład OSM")
        btn_default.clicked.connect(self._default_tiles)
        tform.addRow(btn_default)

        look = QGroupBox("Wygląd", self)
        lform = QFormLayout(look)
        self.theme = QComboBox()
        for key, label in THEMES.items():
            self.theme.addItem(label, key)
        self.theme.setCurrentIndex(max(0, self.theme.findData(s.get("theme", "systemowy"))))
        lform.addRow("Motyw aplikacji i mapy:", self.theme)

        credit = QLabel(f'Dane: <a href="{ATTRIBUTION_URL}">{ATTRIBUTION}</a>')
        credit.setOpenExternalLinks(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(over)
        lay.addWidget(tiles)
        lay.addWidget(look)
        lay.addWidget(credit)
        lay.addWidget(buttons)
        self.resize(640, self.sizeHint().height())

    def _default_tiles(self) -> None:
        self.tile_url.setText("https://tile.openstreetmap.org/{z}/{x}/{y}.png")
        self.tile_attr.setText("© OpenStreetMap contributors")
        self.tile_zoom.setValue(19)

    def _confirm(self, question, action, label, text) -> None:
        if QMessageBox.question(self, "Ustawienia", question) == QMessageBox.StandardButton.Yes:
            action()
            label.setText(text)

    def _save(self) -> None:
        url = self.overpass.text().strip()
        if not url.startswith(("https://", "http://")):
            QMessageBox.warning(self, "Ustawienia", f"Nieprawidłowy adres Overpass: {url}")
            return
        tile_error = validate_url(self.tile_url.text().strip())
        if tile_error:
            QMessageBox.warning(self, "Ustawienia", tile_error)
            return
        if not self.tile_attr.text().strip():
            QMessageBox.warning(self, "Ustawienia", "Wpisz oznaczenie źródła wymagane przez dostawcę mapy.")
            return
        self.settings.update({
            "overpass_url": url, "cache_days": self.cache_days.value(),
            "contact_email": self.contact.text().strip(), "offline": self.offline.isChecked(),
            "tile_url": self.tile_url.text().strip(), "tile_attribution": self.tile_attr.text().strip(),
            "tile_max_zoom": self.tile_zoom.value(), "theme": self.theme.currentData(),
        })
        self.settings.save()
        self.accept()


class ScanSettingsDialog(QDialog):
    """Dyski do skanowania, dyski sieciowe i wykluczenia."""

    def __init__(self, parent, settings: Settings) -> None:
        super().__init__(parent)
        self.setWindowTitle("Ustawienia skanowania")
        self.settings = settings
        chosen = set(settings.get("scan_drives") or [])
        self.drives = QListWidget(self)
        for d in list_drives():
            label = f"{d.root}  ({d.kind}{', ' + d.label if d.label else ''})"
            if not d.available:
                label += " – niedostępny"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, d.root)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            default_on = d.kind in ("lokalny", "wymienny")
            item.setCheckState(Qt.CheckState.Checked if (d.root in chosen if chosen else default_on)
                               else Qt.CheckState.Unchecked)
            self.drives.addItem(item)
        for root in sorted(chosen):     # foldery dodane ręcznie lub odłączone dyski
            if not any(self.drives.item(i).data(Qt.ItemDataRole.UserRole) == root for i in range(self.drives.count())):
                item = QListWidgetItem(f"{root}  (niedostępny lub folder)")
                item.setData(Qt.ItemDataRole.UserRole, root)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
                self.drives.addItem(item)
        self.network = QCheckBox("Uwzględniaj dyski sieciowe (gdy nie wybrano dysków ręcznie)")
        self.network.setChecked(bool(settings.get("scan_network_drives")))
        self.exclusions = QPlainTextEdit(self)
        self.exclusions.setPlainText("\n".join(settings.get("scan_exclusions") or []))
        self.exclusions.setPlaceholderText("Jedna pozycja w wierszu: pełna ścieżka folderu (np. D:\\Kopie) "
                                           "albo nazwa folderu pomijana wszędzie (np. Backup).")
        add = QPushButton("Dodaj folder do skanowania…")
        add.clicked.connect(self._add_folder)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(plain_label("Dyski i foldery do skanowania (podfoldery są uwzględniane):"))
        lay.addWidget(self.drives)
        lay.addWidget(add)
        lay.addWidget(self.network)
        lay.addWidget(plain_label("Wykluczenia (oprócz folderów systemowych, Program Files, AppData i kosza):"))
        lay.addWidget(self.exclusions)
        lay.addWidget(plain_label("Skanowanie nie wymaga uprawnień administratora – niedostępne foldery są pomijane. "
                                  "Pliki dostępne tylko w chmurze nie są pobierane.", wrap=True, hint=True))
        lay.addWidget(buttons)
        self.resize(560, 520)

    def _add_folder(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        folder = QFileDialog.getExistingDirectory(self, "Wybierz folder do skanowania")
        if folder:
            item = QListWidgetItem(os.path.normpath(folder))
            item.setData(Qt.ItemDataRole.UserRole, os.path.normpath(folder))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.drives.addItem(item)

    def _save(self) -> None:
        checked = [self.drives.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.drives.count())
                   if self.drives.item(i).checkState() == Qt.CheckState.Checked]
        defaults = [d.root for d in list_drives() if d.kind in ("lokalny", "wymienny")]
        self.settings.update({
            # domyślny wybór zapisujemy jako pustą listę = „wszystkie lokalne dyski” (także nowe)
            "scan_drives": [] if sorted(checked) == sorted(defaults) else checked,
            "scan_network_drives": self.network.isChecked(),
            "scan_exclusions": [line.strip() for line in self.exclusions.toPlainText().splitlines() if line.strip()],
        })
        self.settings.save()
        self.accept()


class SkipsDialog(QDialog):
    def __init__(self, parent, summary: list[tuple[str, int]], items: list[tuple[str, str]]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pominięte podczas skanowania")
        tree = QTreeWidget(self)
        tree.setHeaderLabels(["Ścieżka", "Powód"])
        for path, reason in items:
            tree.addTopLevelItem(QTreeWidgetItem([path, reason]))
        tree.setColumnWidth(0, 420)
        lay = QVBoxLayout(self)
        lay.addWidget(plain_label("\n".join(f"{reason}: {n}" for reason, n in summary) or "Nic nie pominięto."))
        lay.addWidget(tree)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        lay.addWidget(close)
        self.resize(720, 460)
