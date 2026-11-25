"""Widżety pomocnicze: pole daty z kalendarzem i okno ustawień."""

from __future__ import annotations

import calendar
import os
import tkinter as tk
import webbrowser
from datetime import date
from tkinter import messagebox, ttk
from typing import Callable

from .config import Settings
from .osm import ATTRIBUTION, ATTRIBUTION_URL
from .overpass import ENV_ENDPOINT
from .planner import parse_date

MONTHS = ["styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec", "lipiec",
          "sierpień", "wrzesień", "październik", "listopad", "grudzień"]
WEEKDAYS = ["Pn", "Wt", "Śr", "Cz", "Pt", "So", "Nd"]


def _safe_grab(window: tk.Toplevel) -> None:
    """Okno modalne; grab może się nie udać, gdy okno nie jest jeszcze widoczne."""
    try:
        window.grab_set()
    except tk.TclError:
        pass


class CalendarPopup(tk.Toplevel):
    """Małe okno z kalendarzem miesięcznym. Wybrana data trafia do on_pick."""

    def __init__(self, master: tk.Misc, initial: date | None, on_pick: Callable[[date], None]) -> None:
        super().__init__(master)
        self.title("Wybierz datę")
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())
        self.on_pick = on_pick
        self.selected = initial
        start = initial or date.today()
        self.year, self.month = start.year, start.month

        header = ttk.Frame(self, padding=(6, 6, 6, 0))
        header.pack(fill=tk.X)
        ttk.Button(header, text="◀", width=3, command=lambda: self._shift(-1)).pack(side=tk.LEFT)
        self.title_label = ttk.Label(header, anchor="center", font=("Segoe UI", 10, "bold"))
        self.title_label.pack(side=tk.LEFT, expand=True, fill=tk.X)
        ttk.Button(header, text="▶", width=3, command=lambda: self._shift(1)).pack(side=tk.RIGHT)

        self.grid_frame = ttk.Frame(self, padding=6)
        self.grid_frame.pack()
        footer = ttk.Frame(self, padding=(6, 0, 6, 6))
        footer.pack(fill=tk.X)
        ttk.Button(footer, text="Dzisiaj", command=lambda: self._pick(date.today())).pack(side=tk.LEFT)
        ttk.Button(footer, text="Anuluj", command=self.destroy).pack(side=tk.RIGHT)

        self.bind("<Escape>", lambda _e: self.destroy())
        self._render()
        self.after(10, self._place_near_pointer)
        self.after(50, self._grab)

    def _grab(self) -> None:
        _safe_grab(self)

    def _place_near_pointer(self) -> None:
        x, y = self.winfo_pointerx(), self.winfo_pointery()
        self.geometry(f"+{max(x - 120, 0)}+{max(y + 10, 0)}")

    def _shift(self, delta: int) -> None:
        m = self.month - 1 + delta
        self.year += m // 12
        self.month = m % 12 + 1
        self._render()

    def _render(self) -> None:
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self.title_label.configure(text=f"{MONTHS[self.month - 1]} {self.year}")
        for col, name in enumerate(WEEKDAYS):
            ttk.Label(self.grid_frame, text=name, width=4, anchor="center").grid(row=0, column=col)
        today = date.today()
        for row, week in enumerate(calendar.Calendar(firstweekday=0).monthdayscalendar(self.year, self.month), 1):
            for col, day in enumerate(week):
                if not day:
                    continue
                d = date(self.year, self.month, day)
                text = f"[{day}]" if d == self.selected else (f"·{day}·" if d == today else str(day))
                ttk.Button(self.grid_frame, text=text, width=4,
                           command=lambda d=d: self._pick(d)).grid(row=row, column=col, padx=1, pady=1)

    def _pick(self, d: date) -> None:
        self.on_pick(d)
        self.destroy()


class DateField(ttk.Frame):
    """Pole tekstowe RRRR-MM-DD z przyciskiem kalendarza."""

    def __init__(self, master: tk.Misc, variable: tk.StringVar, width: int = 14) -> None:
        super().__init__(master)
        self.variable = variable
        self.entry = ttk.Entry(self, textvariable=variable, width=width)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(self, text="Kalendarz…", command=self._open).pack(side=tk.LEFT, padx=(6, 0))

    def _open(self) -> None:
        CalendarPopup(self, parse_date(self.variable.get()),
                      lambda d: self.variable.set(d.strftime("%Y-%m-%d")))


class SettingsDialog(tk.Toplevel):
    """Konfiguracja Overpass API, promienia, cache i trybu offline."""

    def __init__(self, master: tk.Misc, settings: Settings, cache_summary: str, decisions_count: int,
                 on_save: Callable[[], None], on_clear_cache: Callable[[], None],
                 on_clear_decisions: Callable[[], None]) -> None:
        super().__init__(master)
        self.title("Ustawienia")
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())
        self.settings = settings
        self.on_save = on_save

        s = settings.as_dict()
        self.vars = {
            "overpass_url": tk.StringVar(value=s["overpass_url"]),
            "poi_radius_m": tk.StringVar(value=str(s["poi_radius_m"])),
            "cache_days": tk.StringVar(value=str(s["cache_days"])),
            "contact_email": tk.StringVar(value=s["contact_email"]),
            "offline": tk.BooleanVar(value=bool(s["offline"])),
        }

        body = ttk.Frame(self, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)
        rows = [
            ("Endpoint Overpass API:", ttk.Entry(body, textvariable=self.vars["overpass_url"], width=56)),
            ("Promień wyszukiwania obiektów (m):", ttk.Spinbox(body, from_=25, to=2000, increment=25,
                                                                textvariable=self.vars["poi_radius_m"], width=8)),
            ("Odświeżaj dane OSM po (dniach):", ttk.Spinbox(body, from_=1, to=3650, increment=30,
                                                             textvariable=self.vars["cache_days"], width=8)),
            ("Kontakt w User-Agent (opcjonalnie):", ttk.Entry(body, textvariable=self.vars["contact_email"], width=56)),
        ]
        for r, (label, widget) in enumerate(rows):
            ttk.Label(body, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 8))
            widget.grid(row=r, column=1, sticky="w" if isinstance(widget, ttk.Spinbox) else "ew", pady=3)
        r = len(rows)
        env = os.environ.get(ENV_ENDPOINT, "").strip()
        if env:
            ttk.Label(body, foreground="#b00020", text=f"Zmienna {ENV_ENDPOINT} ma pierwszeństwo: {env}"
                      ).grid(row=r, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(body, text="Tryb offline – tylko zapisane dane i decyzje (bez internetu)",
                        variable=self.vars["offline"]).grid(row=r + 1, column=0, columnspan=2, sticky="w", pady=(8, 3))
        ttk.Label(body, foreground="#555", wraplength=560, justify="left", text=(
            "Do serwera wysyłane są wyłącznie współrzędne – nigdy zdjęcia ani filmy. Publiczna instancja "
            "overpass-api.de jest bezpłatna, ale współdzielona (orientacyjnie do 10 000 zapytań i 1 GB dziennie). "
            "Aplikacja wysyła zapytania pojedynczo, zapisuje dane lokalnie i ponawia je po przerwie przy "
            f"błędach 429/504. Przy intensywnym użyciu wskaż własny serwer Overpass – w polu powyżej "
            f"lub zmienną środowiskową {ENV_ENDPOINT}.")
        ).grid(row=r + 2, column=0, columnspan=2, sticky="w", pady=(4, 8))

        cache_row = ttk.Frame(body)
        cache_row.grid(row=r + 3, column=0, columnspan=2, sticky="ew")
        self.cache_label = ttk.Label(cache_row, text=f"Cache danych OSM – {cache_summary}")
        self.cache_label.pack(side=tk.LEFT)
        ttk.Button(cache_row, text="Wyczyść", command=lambda: self._confirm(
            "Usunąć zapisane dane OSM?", on_clear_cache, self.cache_label, "Cache danych OSM – pusty")).pack(side=tk.RIGHT)
        dec_row = ttk.Frame(body)
        dec_row.grid(row=r + 4, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.dec_label = ttk.Label(dec_row, text=f"Zapamiętane decyzje (miejscowości i obiekty): {decisions_count}")
        self.dec_label.pack(side=tk.LEFT)
        ttk.Button(dec_row, text="Wyczyść", command=lambda: self._confirm(
            "Usunąć zapamiętane decyzje?", on_clear_decisions, self.dec_label,
            "Zapamiętane decyzje (miejscowości i obiekty): 0")).pack(side=tk.RIGHT)
        attribution = ttk.Label(body, text=f"Dane: {ATTRIBUTION}", foreground="#0b57d0", cursor="hand2")
        attribution.grid(row=r + 5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        attribution.bind("<Button-1>", lambda _e: webbrowser.open(ATTRIBUTION_URL))

        buttons = ttk.Frame(self, padding=(12, 0, 12, 12))
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Zapisz", command=self._save).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Anuluj", command=self.destroy).pack(side=tk.RIGHT, padx=(0, 8))
        self.bind("<Escape>", lambda _e: self.destroy())
        self.after(50, self._grab)

    def _grab(self) -> None:
        _safe_grab(self)

    def _confirm(self, question: str, action: Callable[[], None], label: ttk.Label, text: str) -> None:
        if messagebox.askyesno("Ustawienia", question, parent=self):
            action()
            label.configure(text=text)

    def _save(self) -> None:
        try:
            radius = int(self.vars["poi_radius_m"].get())
            days = int(self.vars["cache_days"].get())
            if not 10 <= radius <= 5000 or days < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Ustawienia", "Promień: 10–5000 m, odświeżanie: co najmniej 1 dzień.", parent=self)
            return
        url = self.vars["overpass_url"].get().strip()
        if not url.startswith(("https://", "http://")):
            messagebox.showerror("Ustawienia", f"Nieprawidłowy adres endpointu: {url}", parent=self)
            return
        self.settings.update({"overpass_url": url, "poi_radius_m": radius, "cache_days": days,
                              "contact_email": self.vars["contact_email"].get().strip(),
                              "offline": bool(self.vars["offline"].get())})
        self.settings.save()
        self.on_save()
        self.destroy()
