"""Główne okno aplikacji – import materiałów z lotów i ręczny wybór kategorii."""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import APP_NAME, __version__, core
from . import planner as pl
from .config import Settings
from .geo import GeoCache, LocationService, Place, SOURCE_NONE
from .media import MediaGroup, scan_source
from .widgets import DateField, SettingsDialog

NO_CATEGORY = "-"  # wartość „odznaczona” dla pól wyboru kategorii
SCOPE_ALL = "all"
SCOPE_SELECTED = "selected"
NEW_TARGET = "— nowe —"


def _enable_high_dpi() -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n == 1:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _groups_word(n: int) -> str:
    return f"{n} {_plural(n, 'grupa', 'grupy', 'grup')}"


def _files_word(n: int) -> str:
    if n == 1:
        return "plik"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "pliki"
    return "plików"


class GeoWorker(threading.Thread):
    """Wątek rozpoznający lokalizacje w tle (kolejka zadań, limit zapytań w HttpClient)."""

    def __init__(self, service: LocationService, events: queue.Queue) -> None:
        super().__init__(daemon=True)
        self.service = service
        self.events = events
        self.jobs: queue.Queue = queue.Queue()

    def submit(self, import_id: int, kind: str, key: str, gps: tuple[float, float]) -> None:
        self.jobs.put((import_id, kind, key, gps))

    def run(self) -> None:
        while True:
            import_id, kind, key, (lat, lon) = self.jobs.get()
            try:
                if kind == "town":
                    result, source, error = self.service.town(lat, lon)
                else:
                    result, source, error = self.service.nearby_object(lat, lon)
            except Exception as exc:  # nie dopuszczamy do zatrzymania wątku
                result, source, error = None, SOURCE_NONE, str(exc)
            self.events.put((kind, import_id, key, result, source, error))


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {__version__} – archiwizacja materiałów z lotów")
        self.geometry("1320x820")
        self.minsize(1050, 680)

        self.settings = Settings()
        self.cache = GeoCache()
        self.geo = LocationService(self.settings, self.cache)
        self.events: queue.Queue = queue.Queue()
        self.worker = GeoWorker(self.geo, self.events)
        self.worker.start()

        # Stan importu
        self.import_id = 0
        self.groups: list[MediaGroup] = []
        self.group_by_key: dict[str, MediaGroup] = {}
        self.assignment_of: dict[str, pl.Assignment | None] = {}
        self.places: dict[str, Place] = {}
        self.poi_requested: set[str] = set()
        self.poi_failed: set[str] = set()   # obiektu nie sprawdzono (brak internetu i cache)
        self.current: pl.Assignment | None = None
        self.plan: pl.ImportPlan | None = None
        self.busy = False

        # Zmienne interfejsu
        self.source_var = tk.StringVar(value=self.settings.get("last_source") or "")
        self.target_var = tk.StringVar(value=self.settings.get("target_root") or "")
        self.scope_var = tk.StringVar(value=SCOPE_ALL)
        self.category_var = tk.StringVar(value=NO_CATEGORY)
        self.name_var = tk.StringVar()
        self.date_var = tk.StringVar()
        self.place_var = tk.StringVar()
        self.existing_var = tk.StringVar(value=NEW_TARGET)
        self.status_var = tk.StringVar(value="Wybierz folder importu (np. kartę pamięci) i kliknij „Wczytaj”.")
        self.geo_status_var = tk.StringVar()
        self.selection_info_var = tk.StringVar()

        self._suspend = False         # blokada reakcji na programowe zmiany pól
        self._suspend_select = False
        self._refresh_job: str | None = None
        self._existing: list[pl.ExistingTarget] = []

        self._build_ui()
        for var, field_name in ((self.name_var, "name"), (self.date_var, "date"), (self.place_var, "place")):
            var.trace_add("write", lambda *_a, f=field_name, v=var: self._on_field_changed(f, v))
        self.target_var.trace_add("write", lambda *_a: self._on_target_changed())

        self._sync_form()
        self._refresh_undo_button()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_events)

    # ══ Budowa interfejsu ══════════════════════════════════════

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Error.TLabel", foreground="#b00020")
        style.configure("Hint.TLabel", foreground="#555555")
        style.configure("Ok.TLabel", foreground="#1b5e20")
        style.configure("Category.TCheckbutton", font=("Segoe UI", 10, "bold"))

        main = ttk.Frame(self, padding=10)
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        # ── Foldery
        folders = ttk.LabelFrame(main, text="Foldery", padding=8)
        folders.grid(row=0, column=0, sticky="ew")
        folders.columnconfigure(1, weight=1)
        ttk.Label(folders, text="Import (karta / folder):").grid(row=0, column=0, sticky="w")
        ttk.Entry(folders, textvariable=self.source_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(folders, text="Wybierz…", command=self._choose_source).grid(row=0, column=2)
        self.load_btn = ttk.Button(folders, text="Wczytaj", command=self._load_source)
        self.load_btn.grid(row=0, column=3, padx=(6, 0))
        ttk.Label(folders, text="Archiwum (folder docelowy):").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(folders, textvariable=self.target_var).grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Button(folders, text="Wybierz…", command=self._choose_target).grid(row=1, column=2, pady=(6, 0))
        ttk.Button(folders, text="Ustawienia…", command=self._open_settings).grid(row=1, column=3, padx=(6, 0), pady=(6, 0))

        panes = ttk.PanedWindow(main, orient=tk.HORIZONTAL)
        panes.grid(row=1, column=0, sticky="nsew", pady=8)

        # ── Lista grup materiałów
        left = ttk.LabelFrame(panes, text="Materiały z importu", padding=8)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        cols = ("typ", "pliki", "data", "lokalizacja", "kategoria")
        self.tree = ttk.Treeview(left, columns=cols, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Nazwa")
        self.tree.column("#0", width=200, stretch=False)
        for col, title, width in (("typ", "Typ", 70), ("pliki", "Pliki", 110), ("data", "Data wykonania", 140),
                                  ("lokalizacja", "Lokalizacja z GPS", 220), ("kategoria", "Kategoria", 220)):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, stretch=col in ("lokalizacja", "kategoria"))
        self.tree.tag_configure("unassigned", foreground="#b00020")
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Control-a>", lambda _e: (self._select_all(), "break")[1])

        left_bottom = ttk.Frame(left)
        left_bottom.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(left_bottom, text="Zaznacz wszystkie", command=self._select_all).pack(side=tk.LEFT)
        ttk.Button(left_bottom, text="Zaznacz bez kategorii", command=self._select_unassigned).pack(side=tk.LEFT, padx=6)
        self.retry_btn = ttk.Button(left_bottom, text="Ponów rozpoznawanie lokalizacji", command=self._retry_geo)
        self.retry_btn.pack(side=tk.RIGHT)
        ttk.Label(left, textvariable=self.geo_status_var, style="Hint.TLabel", wraplength=640,
                  justify="left").grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        panes.add(left, weight=3)

        # ── Prawa kolumna: kategoria, formularz, podgląd
        right = ttk.Frame(panes)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        panes.add(right, weight=2)

        cat = ttk.LabelFrame(right, text="Kategoria lotu (wybierz ręcznie)", padding=8)
        cat.grid(row=0, column=0, sticky="ew")
        scope = ttk.Frame(cat)
        scope.pack(fill=tk.X)
        ttk.Label(scope, text="Przypisz do:").pack(side=tk.LEFT)
        ttk.Radiobutton(scope, text="wszystkich materiałów z importu", value=SCOPE_ALL,
                        variable=self.scope_var, command=self._sync_form).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Radiobutton(scope, text="zaznaczonych grup", value=SCOPE_SELECTED,
                        variable=self.scope_var, command=self._sync_form).pack(side=tk.LEFT, padx=(6, 0))

        boxes = ttk.Frame(cat)
        boxes.pack(fill=tk.X, pady=(8, 0))
        self.category_buttons: list[ttk.Checkbutton] = []
        for key, label in pl.CATEGORY_LABELS.items():
            # Wspólna zmienna => wygląd pola wyboru, ale tylko jedna kategoria naraz
            cb = ttk.Checkbutton(boxes, text=label, variable=self.category_var, onvalue=key,
                                 offvalue=NO_CATEGORY, command=self._on_category_clicked,
                                 style="Category.TCheckbutton")
            cb.pack(side=tk.LEFT, padx=(0, 18))
            self.category_buttons.append(cb)
        info = ttk.Frame(cat)
        info.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(info, textvariable=self.selection_info_var, style="Hint.TLabel",
                  wraplength=430, justify="left").pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.split_btn = ttk.Button(info, text="Wydziel jako osobne", command=self._split_selection)

        # Formularze
        self.form_box = ttk.LabelFrame(right, text="Dane", padding=8)
        self.form_box.grid(row=1, column=0, sticky="ew", pady=8)
        self.form_box.columnconfigure(0, weight=1)
        self._build_forms(self.form_box)

        # Podgląd
        prev = ttk.LabelFrame(right, text="Podgląd docelowej struktury folderów", padding=8)
        prev.grid(row=2, column=0, sticky="nsew")
        prev.columnconfigure(0, weight=1)
        prev.rowconfigure(0, weight=1)
        self.preview = ttk.Treeview(prev, columns=("ile",), show="tree headings", height=8)
        self.preview.heading("#0", text="Folder")
        self.preview.heading("ile", text="Zawartość")
        self.preview.column("ile", width=150, stretch=False, anchor="e")
        self.preview.tag_configure("warn", foreground="#b00020")
        self.preview.grid(row=0, column=0, sticky="nsew")
        psb = ttk.Scrollbar(prev, orient=tk.VERTICAL, command=self.preview.yview)
        psb.grid(row=0, column=1, sticky="ns")
        self.preview.configure(yscrollcommand=psb.set)
        self.errors_label = ttk.Label(prev, style="Error.TLabel", wraplength=480, justify="left")
        self.errors_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # ── Dół okna
        bottom = ttk.Frame(main)
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        self.undo_btn = ttk.Button(bottom, text="Cofnij ostatni import", command=self._undo)
        self.undo_btn.grid(row=0, column=1)
        self.move_btn = ttk.Button(bottom, text="Przenieś materiały", command=self._move, state=tk.DISABLED)
        self.move_btn.grid(row=0, column=2, padx=(8, 0))
        ttk.Label(main, textvariable=self.status_var, anchor="w").grid(row=3, column=0, sticky="ew", pady=(6, 0))

    def _build_forms(self, parent: ttk.Frame) -> None:
        # Brak kategorii
        self.empty_form = ttk.Frame(parent)
        self.empty_label = ttk.Label(self.empty_form, style="Hint.TLabel", wraplength=480, justify="left",
                                     text="Wybierz kategorię powyżej, aby wyświetlić pola formularza.")
        self.empty_label.pack(anchor="w")

        # Hobby
        self.hobby_form = ttk.Frame(parent)
        self.hobby_form.columnconfigure(1, weight=1)
        ttk.Label(self.hobby_form, style="Hint.TLabel", wraplength=480, justify="left", text=(
            "Miejscowość i pobliski obiekt są rozpoznawane z GPS każdego materiału osobno. "
            "Jeśli w promieniu nie ma charakterystycznego obiektu, jego poziom jest pomijany.\n"
            "Struktura: Hobby / Miejscowość / [Obiekt] / WIDEO | Zdjęcia")
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(self.hobby_form, text="Miejscowość ręcznie:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(self.hobby_form, textvariable=self.place_var).grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=(8, 0))
        ttk.Label(self.hobby_form, style="Hint.TLabel", text=(
            "Używana tylko dla materiałów bez GPS lub gdy lokalizacji nie udało się rozpoznać.")
        ).grid(row=2, column=1, sticky="w", padx=(6, 0))
        ttk.Label(self.hobby_form, text="Obiekt ręcznie:").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(self.hobby_form, textvariable=self.name_var).grid(row=3, column=1, sticky="ew", padx=(6, 0), pady=(8, 0))
        ttk.Label(self.hobby_form, style="Hint.TLabel", wraplength=400, justify="left", text=(
            "Opcjonalnie – dla materiałów, przy których nie rozpoznano obiektu (np. bez internetu). "
            "Pozostaw puste, aby pominąć poziom obiektu.")
        ).grid(row=4, column=1, sticky="w", padx=(6, 0))
        self.hobby_summary = ttk.Label(self.hobby_form, wraplength=480, justify="left")
        self.hobby_summary.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # Eventy / Zlecenia – wspólny układ, różne etykiety
        self.detail_form = ttk.Frame(parent)
        f = self.detail_form
        f.columnconfigure(1, weight=1)
        self.existing_label = ttk.Label(f, text="Dołącz do istniejącego:")
        self.existing_label.grid(row=0, column=0, sticky="w")
        self.existing_combo = ttk.Combobox(f, textvariable=self.existing_var, state="readonly")
        self.existing_combo.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(6, 0))
        self.existing_combo.bind("<<ComboboxSelected>>", self._on_existing_selected)

        self.name_label = ttk.Label(f, text="Nazwa *")
        self.name_label.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.name_entry = ttk.Entry(f, textvariable=self.name_var)
        self.name_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(6, 0), pady=(8, 0))

        self.date_label = ttk.Label(f, text="Data *")
        self.date_label.grid(row=2, column=0, sticky="w", pady=(8, 0))
        DateField(f, self.date_var).grid(row=2, column=1, sticky="ew", padx=(6, 0), pady=(8, 0))
        self.date_suggest_btn = ttk.Button(f, command=self._use_date_suggestion)
        self.date_suggest_btn.grid(row=2, column=2, sticky="e", padx=(6, 0), pady=(8, 0))

        self.place_label = ttk.Label(f, text="Miejscowość *")
        self.place_label.grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(f, textvariable=self.place_var).grid(row=3, column=1, sticky="ew", padx=(6, 0), pady=(8, 0))
        self.place_suggest_btn = ttk.Button(f, command=self._use_place_suggestion)
        self.place_suggest_btn.grid(row=3, column=2, sticky="e", padx=(6, 0), pady=(8, 0))

        self.detail_hint = ttk.Label(f, style="Hint.TLabel", wraplength=480, justify="left")
        self.detail_hint.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

    # ══ Wybór folderów i wczytywanie ═══════════════════════════

    def _choose_source(self) -> None:
        folder = filedialog.askdirectory(title="Wybierz kartę pamięci lub folder z materiałami",
                                         initialdir=self.source_var.get() or None)
        if folder:
            self.source_var.set(str(Path(folder)))
            self._load_source()

    def _choose_target(self) -> None:
        folder = filedialog.askdirectory(title="Wybierz folder archiwum",
                                         initialdir=self.target_var.get() or None)
        if folder:
            self.target_var.set(str(Path(folder)))

    def _on_target_changed(self) -> None:
        self._load_existing()
        self._schedule_refresh()

    def _load_source(self) -> None:
        source = self.source_var.get().strip()
        if not source:
            messagebox.showwarning(APP_NAME, "Najpierw wybierz folder importu.")
            return
        if self.busy:
            return
        if self.groups and any(self.assignment_of.values()):
            if not messagebox.askyesno(APP_NAME, "Wczytać nowy import? Bieżące przypisania kategorii "
                                                 "(nieprzeniesione) zostaną utracone."):
                return
        self.settings.set("last_source", source)
        self._set_busy(True, "Wczytywanie materiałów i odczyt metadanych…")
        self.import_id += 1
        threading.Thread(target=self._scan_worker, args=(self.import_id, source), daemon=True).start()

    def _scan_worker(self, import_id: int, source: str) -> None:
        try:
            groups = scan_source(source, lambda i, n: self.events.put(("scan_progress", import_id, i, n)))
            self.events.put(("scanned", import_id, groups, None))
        except OSError as exc:
            self.events.put(("scanned", import_id, [], str(exc)))

    def _on_scanned(self, groups: list[MediaGroup], error: str | None) -> None:
        self._set_busy(False)
        if error:
            messagebox.showerror(APP_NAME, f"Nie można wczytać folderu:\n{error}")
            return
        self.groups = groups
        self.group_by_key = {g.key: g for g in groups}
        self.assignment_of = {g.key: None for g in groups}
        self.places = {}
        self.poi_requested = set()
        self.poi_failed = set()
        self.current = None
        self.scope_var.set(SCOPE_ALL)
        self._fill_tree()
        for g in groups:
            if g.gps:
                self.worker.submit(self.import_id, "town", g.key, g.gps)
        total_files = sum(len(g.files) for g in groups)
        self.status_var.set(f"Wczytano {_groups_word(len(groups))} materiałów ({total_files} {_files_word(total_files)}, "
                            f"{_human_size(sum(g.size for g in groups))}). Wybierz kategorię.")
        self._sync_form()

    # ══ Lista grup ═════════════════════════════════════════════

    def _fill_tree(self) -> None:
        self._suspend_select = True
        self.tree.delete(*self.tree.get_children())
        for g in self.groups:
            self.tree.insert("", tk.END, iid=g.key, text=g.name, values=self._row_values(g),
                             tags=("unassigned",) if self.assignment_of.get(g.key) is None else ())
        self._suspend_select = False
        self._update_geo_status()

    def _row_values(self, g: MediaGroup) -> tuple:
        if g.captured:
            date_txt = f"{g.captured:%Y-%m-%d %H:%M} ({g.captured_source})"
        else:
            date_txt = "nieznana"
        if g.gps is None:
            loc = "brak GPS"
        elif g.key not in self.places:
            loc = "rozpoznawanie…"
        else:
            p = self.places[g.key]
            if not p.town:
                loc = "nie rozpoznano" + (" (offline)" if p.error else "")
            else:
                loc = p.town
                if p.object_name:
                    loc += f" / {p.object_name}"
                elif g.key in self.poi_failed:
                    loc += " / obiekt: nie sprawdzono (offline)"
                elif g.key in self.poi_requested and not p.object_checked:
                    loc += " / szukanie obiektu…"
                if p.source.startswith("cache"):
                    loc += " [cache]"
        a = self.assignment_of.get(g.key)
        if a is None:
            cat = "— brak —"
        else:
            cat = pl.CATEGORY_LABELS[a.category]
            if a.category != pl.CATEGORY_HOBBY and a.name:
                cat += f": {a.name}"
        return (g.kind, g.extensions, date_txt, loc, cat)

    def _update_rows(self, keys=None) -> None:
        for key in (keys if keys is not None else self.group_by_key):
            if self.tree.exists(key):
                g = self.group_by_key[key]
                self.tree.item(key, values=self._row_values(g),
                               tags=("unassigned",) if self.assignment_of.get(key) is None else ())

    def _selected_keys(self) -> list[str]:
        return [k for k in self.tree.selection() if k in self.group_by_key]

    def _target_keys(self) -> list[str]:
        if self.scope_var.get() == SCOPE_ALL:
            return [g.key for g in self.groups]
        return self._selected_keys()

    def _on_tree_select(self, _event=None) -> None:
        if self._suspend_select:
            return
        if self._selected_keys() and self.scope_var.get() != SCOPE_SELECTED:
            self.scope_var.set(SCOPE_SELECTED)
        self._sync_form()

    def _select_all(self) -> None:
        self.tree.selection_set(self.tree.get_children())

    def _select_unassigned(self) -> None:
        keys = [g.key for g in self.groups if self.assignment_of.get(g.key) is None]
        self.tree.selection_set(keys)
        if keys:
            self.tree.see(keys[0])

    # ══ Kategoria i przypisania ════════════════════════════════

    def _members(self, a: pl.Assignment) -> list[MediaGroup]:
        return [g for g in self.groups if self.assignment_of.get(g.key) is a]

    def _on_category_clicked(self) -> None:
        keys = self._target_keys()
        cat = self.category_var.get()
        if not keys:
            self.category_var.set(NO_CATEGORY)
            messagebox.showinfo(APP_NAME, "Zaznacz grupy materiałów na liście albo wybierz "
                                          "„wszystkich materiałów z importu”.")
            return
        if cat == NO_CATEGORY:
            for k in keys:
                self.assignment_of[k] = None
        else:
            current = self.current
            members = {g.key for g in self._members(current)} if current else set()
            if current is not None and members == set(keys):
                current.category = cat          # zmiana kategorii całego przypisania
            else:
                # Nowe przypisanie zaczyna z pustym formularzem – pola innej kategorii
                # (np. nazwa wydarzenia) nie mogą trafić do nowej kategorii.
                new = pl.Assignment(cat)
                for k in keys:
                    self.assignment_of[k] = new
            self._request_objects()
        self._sync_form()
        self._update_rows(keys)

    def _split_selection(self) -> None:
        """Wydziela zaznaczone grupy z większego przypisania jako osobne (np. drugie wydarzenie)."""
        if self.current is None:
            return
        keys = self._selected_keys()
        new = pl.Assignment(self.current.category)
        for k in keys:
            self.assignment_of[k] = new
        self._sync_form()
        self._update_rows()

    def _sync_form(self) -> None:
        """Ustawia pola wyboru i formularz zgodnie z przypisaniem docelowych grup."""
        keys = self._target_keys()
        assignments = {id(self.assignment_of.get(k)): self.assignment_of.get(k) for k in keys}
        self.split_btn.pack_forget()
        state = tk.NORMAL if keys else tk.DISABLED
        for cb in self.category_buttons:
            cb.configure(state=state)

        if not self.groups:
            info = "Najpierw wczytaj materiały."
            self.current = None
        elif not keys:
            info = "Zaznacz grupy na liście (Ctrl/Shift + klik), aby przypisać im kategorię."
            self.current = None
        elif len(assignments) == 1:
            self.current = next(iter(assignments.values()))
            n = len(keys)
            info = (f"Wszystkie materiały: {_groups_word(n)}." if self.scope_var.get() == SCOPE_ALL
                    else f"Zaznaczono: {_groups_word(n)}.")
            if self.current is not None:
                total = len(self._members(self.current))
                if total > n:
                    info += (f" Należą do przypisania obejmującego {_groups_word(total)} – zmiany w formularzu "
                             "dotyczą wszystkich. Aby utworzyć osobne przypisanie (np. inne wydarzenie), użyj przycisku obok.")
                    self.split_btn.pack(side=tk.RIGHT)
        else:
            self.current = None
            info = (f"Zaznaczone grupy ({len(keys)}) mają różne kategorie. Wybierz kategorię, "
                    "aby przypisać im wspólną.")
        self.selection_info_var.set(info)

        self._suspend = True
        try:
            a = self.current
            self.category_var.set(a.category if a else NO_CATEGORY)
            if a is not None:
                self._apply_suggestions(a)
            self.name_var.set(a.name if a else "")
            self.date_var.set(a.date if a else "")
            self.place_var.set(a.place if a else "")
            self.existing_var.set(NEW_TARGET)
        finally:
            self._suspend = False
        self._show_form()
        self._schedule_refresh()

    def _show_form(self) -> None:
        for frame in (self.empty_form, self.hobby_form, self.detail_form):
            frame.grid_forget()
        a = self.current
        if a is None:
            self.form_box.configure(text="Dane")
            self.empty_form.grid(row=0, column=0, sticky="ew")
            return
        if a.category == pl.CATEGORY_HOBBY:
            self.form_box.configure(text="Hobby / Luźne loty")
            self.hobby_form.grid(row=0, column=0, sticky="ew")
            self._update_hobby_summary()
            return

        is_event = a.category == pl.CATEGORY_EVENT
        self.form_box.configure(text="Eventy" if is_event else "Zlecenia")
        self.existing_label.configure(text="Dołącz do istniejącego wydarzenia:" if is_event
                                      else "Dołącz do istniejącego zlecenia:")
        self.name_label.configure(text="Nazwa wydarzenia *" if is_event else "Nazwa / opis zlecenia *")
        self.date_label.configure(text="Data wydarzenia *" if is_event else "Data realizacji *")
        self.place_label.configure(text="Miejscowość *" if is_event else "Lokalizacja zlecenia *")
        self.detail_hint.configure(text=(
            "Miejscowość rozpoznana z GPS – możesz ją poprawić. Data zaproponowana na podstawie daty "
            "wykonania materiałów (nie daty modyfikacji pliku) – sprawdź ją.\n"
            "Struktura: Eventy / Miejscowość / RRRR-MM-DD Nazwa / WIDEO | Zdjęcia" if is_event else
            "Lokalizacja może być miejscowością, adresem lub własnym określeniem miejsca. "
            "Wszystkie materiały zlecenia trafią do jednego folderu (bez podziału według GPS).\n"
            "Struktura: Zlecenia / Lokalizacja / RRRR-MM-DD Nazwa / WIDEO | Zdjęcia"))
        self._load_existing()
        self._update_suggestion_buttons()
        self.detail_form.grid(row=0, column=0, sticky="ew")

    def _apply_suggestions(self, a: pl.Assignment) -> None:
        """Propozycje dla Eventów (data z materiałów, miejscowość z GPS) – tylko gdy użytkownik
        nie edytował pola. Zlecenia nie są uzupełniane automatycznie."""
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
        self.date_suggest_btn.configure(text=f"Z materiałów: {d}" if d else "Brak daty w materiałach",
                                        state=tk.NORMAL if d else tk.DISABLED)
        self.place_suggest_btn.configure(text=f"Z GPS: {t}" if t else "Brak propozycji z GPS",
                                         state=tk.NORMAL if t else tk.DISABLED)

    def _use_date_suggestion(self) -> None:
        if self.current:
            self.date_var.set(pl.suggest_date(self._members(self.current)))

    def _use_place_suggestion(self) -> None:
        if self.current:
            self.place_var.set(pl.suggest_town(self._members(self.current), self.places))

    def _on_field_changed(self, field_name: str, var: tk.StringVar) -> None:
        if self._suspend or self.current is None:
            return
        setattr(self.current, field_name, var.get())
        self.current.edited.add(field_name)
        if field_name == "name":
            self._update_rows([g.key for g in self._members(self.current)])
        self._schedule_refresh()

    # ══ Istniejące wydarzenia / zlecenia ═══════════════════════

    def _load_existing(self) -> None:
        a = self.current
        if a is None or a.category == pl.CATEGORY_HOBBY:
            self._existing = []
        else:
            self._existing = pl.list_existing(self.target_var.get().strip(), a.category)
        self.existing_combo.configure(values=[NEW_TARGET] + [e.label for e in self._existing])

    def _on_existing_selected(self, _event=None) -> None:
        label = self.existing_var.get()
        match = next((e for e in self._existing if e.label == label), None)
        if match is None or self.current is None:
            return
        self.name_var.set(match.name)
        self.date_var.set(match.date)
        self.place_var.set(match.place)
        self.current.edited.update({"name", "date", "place"})

    # ══ Lokalizacje (wątek w tle) ══════════════════════════════

    def _request_objects(self) -> None:
        """Wyszukiwanie obiektów tylko dla grup Hobby z rozpoznaną miejscowością."""
        for g in self.groups:
            a = self.assignment_of.get(g.key)
            p = self.places.get(g.key)
            if (a is not None and a.category == pl.CATEGORY_HOBBY and g.gps and p and p.town
                    and not p.object_checked and g.key not in self.poi_requested):
                self.poi_requested.add(g.key)
                self.worker.submit(self.import_id, "poi", g.key, g.gps)
        self._update_geo_status()

    def _on_town(self, key: str, town: str | None, source: str, error: str) -> None:
        self.places[key] = Place(town=town, source=source, error=error)
        self._request_objects()
        self._after_geo_update(key)

    def _on_poi(self, key: str, poi: dict | None, source: str, error: str) -> None:
        place = self.places.get(key)
        if place is None:
            return
        place.object_checked = True
        if source == SOURCE_NONE and error:
            self.poi_failed.add(key)
        else:
            self.poi_failed.discard(key)
        if poi:
            place.object_name = poi.get("name")
            place.object_distance_m = poi.get("distance")
        if error and not place.error:
            place.error = error
        self._after_geo_update(key)

    def _after_geo_update(self, key: str) -> None:
        self._update_rows([key])
        a = self.assignment_of.get(key)
        if a is not None and a is self.current:
            # Odśwież propozycje w formularzu (bez nadpisywania edycji użytkownika)
            self._suspend = True
            try:
                self._apply_suggestions(a)
                if self.date_var.get() != a.date:
                    self.date_var.set(a.date)
                if self.place_var.get() != a.place:
                    self.place_var.set(a.place)
            finally:
                self._suspend = False
            if a.category == pl.CATEGORY_HOBBY:
                self._update_hobby_summary()
            else:
                self._update_suggestion_buttons()
        elif a is not None:
            self._apply_suggestions(a)
        self._update_geo_status()
        self._schedule_refresh()

    def _update_geo_status(self) -> None:
        with_gps = [g for g in self.groups if g.gps]
        if not self.groups:
            self.geo_status_var.set("")
            return
        pending = sum(1 for g in with_gps if g.key not in self.places)
        pending_poi = sum(1 for k in self.poi_requested
                          if k in self.places and not self.places[k].object_checked)
        unresolved = sum(1 for g in with_gps if g.key in self.places and not self.places[g.key].town)
        no_gps = len(self.groups) - len(with_gps)
        parts = [f"GPS: {len(with_gps)} z {_groups_word(len(self.groups))}"]
        if pending or pending_poi:
            parts.append(f"rozpoznawanie: {pending} miejscowości, {pending_poi} obiektów…")
        if unresolved:
            parts.append(f"nie rozpoznano: {unresolved}")
        if no_gps:
            parts.append(f"bez GPS: {no_gps}")
        if self.settings.get("offline"):
            parts.append("tryb offline – używane są tylko zapisane wyniki")
        elif self.geo.network_down:
            parts.append("brak internetu – używane są zapisane wyniki; możesz uzupełnić lokalizację ręcznie")
        self.geo_status_var.set(" · ".join(parts))

    def _update_hobby_summary(self) -> None:
        a = self.current
        if a is None or a.category != pl.CATEGORY_HOBBY:
            return
        members = self._members(a)
        ok = sum(1 for g in members if (self.places.get(g.key) or Place()).town)
        pending = sum(1 for g in members if pl.is_pending(g, self.places, need_object=True))
        missing = len(members) - ok - pending
        unchecked = sum(1 for g in members if g.key in self.poi_failed)
        text = f"Rozpoznano lokalizację: {ok} z {_groups_word(len(members))}."
        if pending:
            text += f" Trwa rozpoznawanie: {pending}."
        if missing:
            text += f" Bez lokalizacji: {missing} – zostanie użyta miejscowość wpisana ręcznie."
        if unchecked:
            text += (f" Liczba grup bez sprawdzonych obiektów: {unchecked} – wpisz obiekt ręcznie, "
                     "kliknij „Ponów rozpoznawanie” albo pozostaw bez poziomu obiektu.")
        warn = (missing and not a.place.strip()) or unchecked
        self.hobby_summary.configure(text=text, style="Error.TLabel" if warn else "Hint.TLabel")

    def _retry_geo(self) -> None:
        self.geo.reset_network_state()
        for g in self.groups:
            p = self.places.get(g.key)
            if g.gps and p is not None and not p.town:
                del self.places[g.key]
                self.worker.submit(self.import_id, "town", g.key, g.gps)
            elif g.key in self.poi_failed and p is not None:
                p.object_checked = False
                self.poi_failed.discard(g.key)
                self.poi_requested.discard(g.key)
        self._request_objects()
        self._update_rows()
        self._schedule_refresh()

    # ══ Podgląd na żywo ════════════════════════════════════════

    def _schedule_refresh(self) -> None:
        if self._refresh_job is not None:
            self.after_cancel(self._refresh_job)
        self._refresh_job = self.after(150, self._refresh_preview)

    def _refresh_preview(self) -> None:
        self._refresh_job = None
        target = self.target_var.get().strip()
        self.plan = pl.build_plan(self.groups, self.assignment_of, self.places, target)
        folder_error = pl.check_folders(self.source_var.get().strip(), target)
        errors = list(self.plan.errors)
        if folder_error:
            errors.insert(0, folder_error)

        self.preview.delete(*self.preview.get_children())
        root_label = Path(target).name or target if target else "(wybierz archiwum)"
        root_id = self.preview.insert("", tk.END, text=root_label, open=True)
        counts: dict[Path, int] = {}
        for g in self.groups:
            rel = self.plan.group_dirs.get(g.key)
            if rel is not None:
                counts[rel] = counts.get(rel, 0) + len(g.files)
        nodes: dict[Path, str] = {}
        for rel in sorted(counts, key=lambda p: [s.lower() for s in p.parts]):
            parent = root_id
            for i in range(1, len(rel.parts) + 1):
                sub = Path(*rel.parts[:i])
                if sub not in nodes:
                    exists = target and (Path(target) / sub).is_dir()
                    nodes[sub] = self.preview.insert(parent, tk.END, text=rel.parts[i - 1], open=True,
                                                     values=("istnieje" if exists else "",))
                parent = nodes[sub]
            n = counts[rel]
            self.preview.item(parent, values=(f"+{n} {_files_word(n)}",))
        unassigned = len(self.plan.unassigned)
        if unassigned:
            self.preview.insert("", tk.END, text=f"Bez kategorii: {_groups_word(unassigned)}", tags=("warn",))
        incomplete = len(self.groups) - unassigned - len(self.plan.group_dirs)
        if incomplete > 0:
            self.preview.insert("", tk.END, text=f"Niekompletne dane: {_groups_word(incomplete)}", tags=("warn",))

        shown = errors[:6] + ([f"… i {len(errors) - 6} więcej"] if len(errors) > 6 else [])
        self.errors_label.configure(text="\n".join(f"• {e}" for e in shown), style="Error.TLabel")
        if not errors and self.plan.ready:
            n = len(self.plan.moves)
            self.errors_label.configure(text=f"Gotowe do przeniesienia: {n} {_files_word(n)}.", style="Ok.TLabel")
        ready = self.plan.ready and not folder_error and not self.busy
        self.move_btn.configure(state=tk.NORMAL if ready else tk.DISABLED)

    # ══ Przenoszenie i cofanie ═════════════════════════════════

    def _move(self) -> None:
        self._refresh_preview()
        plan = self.plan
        if plan is None or not plan.ready or self.busy:
            messagebox.showwarning(APP_NAME, "Uzupełnij kategorię i wymagane pola przed przeniesieniem.")
            return
        size = sum(g.size for g in self.groups if g.key in plan.group_dirs)
        if not messagebox.askyesno(APP_NAME, f"Przenieść {len(plan.moves)} {_files_word(len(plan.moves))} "
                                             f"({_human_size(size)}) do archiwum?\n\n{self.target_var.get()}"):
            return
        self.settings.set("target_root", self.target_var.get().strip())
        self.settings.save()
        self._set_busy(True, "Przenoszenie materiałów…")
        threading.Thread(target=self._move_worker, args=(list(plan.moves), Path(self.target_var.get().strip())),
                         daemon=True).start()

    def _move_worker(self, moves: list[core.Move], root: Path) -> None:
        done, failed = core.execute_moves(moves, lambda i, n, _m: self.events.put(("move_progress", i, n)))
        if done:
            core.save_history(done, root)
        self.events.put(("moved", done, failed))

    def _on_moved(self, done: list[core.Move], failed: list[tuple[core.Move, str]]) -> None:
        self._set_busy(False)
        moved_sources = {m.source for m in done}
        # Usuń z listy grupy, których wszystkie pliki zostały przeniesione
        remaining = [g for g in self.groups if not all(p in moved_sources for p in g.files)]
        for g in remaining:
            g.files = [p for p in g.files if p not in moved_sources]
        self.groups = remaining
        self.group_by_key = {g.key: g for g in remaining}
        self.assignment_of = {k: v for k, v in self.assignment_of.items() if k in self.group_by_key}
        self._fill_tree()
        self._sync_form()
        self._refresh_undo_button()
        msg = f"Przeniesiono {len(done)} {_files_word(len(done))}."
        if failed:
            details = "\n".join(f"• {m.source.name}: {err}" for m, err in failed[:10])
            more = f"\n… i {len(failed) - 10} więcej" if len(failed) > 10 else ""
            messagebox.showwarning(APP_NAME, f"{msg}\nNie udało się: {len(failed)}\n\n{details}{more}")
        else:
            messagebox.showinfo(APP_NAME, msg + "\n\nMożesz wczytać kolejną kartę i dołączyć ją do "
                                                "tego samego wydarzenia lub zlecenia.")
        self.status_var.set(msg)

    def _undo(self) -> None:
        if self.busy:
            return
        moves, root = core.load_history()
        if not moves:
            messagebox.showinfo(APP_NAME, "Brak importu do cofnięcia.")
            return
        if not messagebox.askyesno(APP_NAME, f"Przywrócić {len(moves)} {_files_word(len(moves))} "
                                             "do poprzednich lokalizacji?"):
            return
        self._set_busy(True, "Cofanie ostatniego importu…")
        threading.Thread(target=self._undo_worker, args=(moves, root), daemon=True).start()

    def _undo_worker(self, moves: list[core.Move], root: Path | None) -> None:
        done, failed = core.undo_moves(moves, stop_at=root)
        if not failed:
            core.clear_history()
        else:
            # Zachowaj w historii tylko to, czego nie udało się cofnąć
            undone_targets = {m.source for m in done}
            core.save_history([m for m in moves if m.target not in undone_targets], root)
        self.events.put(("undone", done, failed))

    def _on_undone(self, done: list, failed: list) -> None:
        self._set_busy(False)
        self._refresh_undo_button()
        self._sync_form()
        msg = f"Przywrócono {len(done)} {_files_word(len(done))}."
        if failed:
            messagebox.showwarning(APP_NAME, f"{msg}\nNie udało się przywrócić: {len(failed)}.")
        self.status_var.set(msg + " Wczytaj import ponownie, aby przypisać kategorie.")

    def _refresh_undo_button(self) -> None:
        moves, _root = core.load_history()
        self.undo_btn.configure(state=tk.NORMAL if moves and not self.busy else tk.DISABLED)

    # ══ Ustawienia ═════════════════════════════════════════════

    def _open_settings(self) -> None:
        def saved() -> None:
            self.geo.reset_network_state()
            self._update_geo_status()

        SettingsDialog(self, self.settings, len(self.cache), saved, self.cache.clear)

    # ══ Pętla zdarzeń ══════════════════════════════════════════

    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        self.busy = busy
        self.load_btn.configure(state=tk.DISABLED if busy else tk.NORMAL)
        if busy:
            self.move_btn.configure(state=tk.DISABLED)
            self.undo_btn.configure(state=tk.DISABLED)
            self.progress.configure(value=0)
        else:
            self._refresh_undo_button()
            self._schedule_refresh()
        if status:
            self.status_var.set(status)

    def _poll_events(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "scan_progress" and ev[1] == self.import_id:
                    self.progress.configure(maximum=max(ev[3], 1), value=ev[2])
                elif kind == "scanned" and ev[1] == self.import_id:
                    self._on_scanned(ev[2], ev[3])
                elif kind == "town" and ev[1] == self.import_id and ev[2] in self.group_by_key:
                    self._on_town(ev[2], ev[3], ev[4], ev[5])
                elif kind == "poi" and ev[1] == self.import_id and ev[2] in self.group_by_key:
                    self._on_poi(ev[2], ev[3], ev[4], ev[5])
                elif kind == "move_progress":
                    self.progress.configure(maximum=max(ev[2], 1), value=ev[1])
                elif kind == "moved":
                    self._on_moved(ev[1], ev[2])
                elif kind == "undone":
                    self._on_undone(ev[1], ev[2])
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno(APP_NAME, "Trwa operacja na plikach. Zamknąć mimo to?"):
            return
        self.settings.set("target_root", self.target_var.get().strip())
        self.settings.set("last_source", self.source_var.get().strip())
        self.settings.save()
        self.destroy()


def main() -> None:
    _enable_high_dpi()
    App().mainloop()
