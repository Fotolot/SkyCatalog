"""Okno „Pliki o takich samych nazwach” – decyzja użytkownika przed kopiowaniem/przenoszeniem."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QPushButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from .. import core
from .. import planner as pl
from .common import human_size, plain_label

ACTIONS = [pl.ACTION_RENAME, pl.ACTION_OVERWRITE, pl.ACTION_SKIP]
SHORT_LABELS = {pl.ACTION_RENAME: "Zmień nazwę (zachowaj oba)", pl.ACTION_OVERWRITE: "Nadpisz",
                pl.ACTION_SKIP: "Pomiń"}


def _describe(path: Path) -> tuple[str, int | None, float | None]:
    try:
        st = path.stat()
    except OSError:
        return "niedostępny", None, None
    when = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
    return f"{human_size(st.st_size)}, {when}", st.st_size, st.st_mtime


class ConflictDialog(QDialog):
    """Lista grup z zajętymi nazwami; dla każdej: Zmień nazwę / Nadpisz / Pomiń.

    Decyzja dotyczy całej grupy (film + SRT + pliki towarzyszące), aby ich nazwy nadal pasowały.
    Domyślnie zaznaczona jest bezpieczna opcja „Zmień nazwę” – nic nie jest nadpisywane bez wyboru.
    """

    def __init__(self, parent, conflicts: list[pl.Conflict], archive: str, copy: bool) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pliki o takich samych nazwach")
        self.conflicts = conflicts
        verb = "skopiowania" if copy else "przeniesienia"
        n_files = sum(len(c.pairs) for c in conflicts)
        intro = QLabel(f"<b>W folderze docelowym są już pliki o takich samych nazwach ({n_files}).</b>")
        intro.setTextFormat(Qt.TextFormat.RichText)
        info = plain_label(f"Wybierz, co zrobić z każdym materiałem do {verb}. Decyzja dotyczy całej grupy "
                           "(np. film i jego plik SRT), aby ich nazwy nadal do siebie pasowały.", wrap=True)

        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(["Plik", "Folder w archiwum", "W archiwum", "Nowy plik", "Uwagi", "Co zrobić"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(False)
        self.combos: dict[str, QComboBox] = {}
        for c in conflicts:
            names = ", ".join(src.name for src, _dst in c.pairs)
            folder = c.pairs[0][1].parent
            try:
                folder_txt = str(folder.relative_to(archive))
            except ValueError:
                folder_txt = str(folder)
            old_txt, old_size, old_mtime = _describe(c.pairs[0][1])
            new_txt, new_size, new_mtime = _describe(c.pairs[0][0])
            same = (old_size is not None and old_size == new_size and old_mtime is not None
                    and new_mtime is not None and abs(old_mtime - new_mtime) <= 2)
            item = QTreeWidgetItem([names, folder_txt, old_txt, new_txt, "ten sam rozmiar i data" if same else "", ""])
            item.setToolTip(0, "\n".join(str(dst) for _src, dst in c.pairs))
            item.setToolTip(1, str(folder))
            self.tree.addTopLevelItem(item)
            combo = QComboBox(self.tree)
            for action in ACTIONS:
                combo.addItem(SHORT_LABELS[action], action)
                combo.setItemData(combo.count() - 1, pl.ACTION_LABELS[action], Qt.ItemDataRole.ToolTipRole)
            combo.setCurrentIndex(ACTIONS.index(pl.ACTION_RENAME))
            self.tree.setItemWidget(item, 5, combo)
            self.combos[c.key] = combo
        header = self.tree.header()
        for col in (0, 2, 3, 4, 5):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(60)

        all_row = QHBoxLayout()
        all_row.addWidget(QLabel("Dla wszystkich:"))
        for action, text in ((pl.ACTION_RENAME, "Zmień nazwę wszystkim"), (pl.ACTION_OVERWRITE, "Nadpisz wszystkie"),
                             (pl.ACTION_SKIP, "Pomiń wszystkie")):
            b = QPushButton(text)
            b.clicked.connect(lambda _c=False, a=action: self.set_all(a))
            all_row.addWidget(b)
        all_row.addStretch(1)

        explain = plain_label(
            "• Zmień nazwę – oba pliki zostają, nowy dostaje dopisek, np. DJI_0001 (1).MP4 (film i SRT ten sam).\n"
            f"• Nadpisz – nowy plik zajmie miejsce starego. Poprzednia wersja nie jest kasowana: trafia do folderu "
            f"{core.BACKUP_FOLDER} w archiwum. Przywrócisz ją przyciskiem „Cofnij ostatni import” "
            "albo usuniesz ten folder ręcznie, gdy nie będzie potrzebny.\n"
            f"• Pomiń – ten materiał nie zostanie {'skopiowany' if copy else 'przeniesiony'}.",
            wrap=True, hint=True)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        box.button(QDialogButtonBox.StandardButton.Ok).setText("Dalej")
        box.button(QDialogButtonBox.StandardButton.Cancel).setText("Anuluj")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addWidget(info)
        lay.addWidget(self.tree, 1)
        lay.addLayout(all_row)
        lay.addWidget(explain)
        lay.addWidget(box)
        self.resize(1180, min(620, 300 + 34 * len(conflicts)))

    def set_all(self, action: str) -> None:
        for combo in self.combos.values():
            combo.setCurrentIndex(ACTIONS.index(action))

    def resolutions(self) -> dict[str, str]:
        return {key: combo.currentData() for key, combo in self.combos.items()}
