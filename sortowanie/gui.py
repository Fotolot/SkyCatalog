"""Okno aplikacji (Tkinter – wbudowany w Pythona dla Windows)."""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import APP_NAME, __version__, core


def _enable_high_dpi() -> None:
    """Ostry tekst na monitorach z powiększeniem (Windows)."""
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {__version__}")
        self.geometry("900x600")
        self.minsize(700, 450)

        self.folder_var = tk.StringVar()
        self.mode_var = tk.StringVar(value=core.MODE_TYPE)
        self.recursive_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Wybierz folder do posortowania.")

        self._planned: list[core.Move] = []
        self._events: queue.Queue = queue.Queue()
        self._busy = False

        self._build_ui()
        self._refresh_undo_button()
        self.after(100, self._poll_events)

    # ── Budowa interfejsu ───────────────────────────────────────

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        main = ttk.Frame(self, padding=12)
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        # Wybór folderu
        folder_box = ttk.LabelFrame(main, text="Folder", padding=8)
        folder_box.grid(row=0, column=0, sticky="ew")
        folder_box.columnconfigure(0, weight=1)
        ttk.Entry(folder_box, textvariable=self.folder_var).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(folder_box, text="Wybierz…", command=self._choose_folder).grid(row=0, column=1)

        # Opcje
        opts = ttk.LabelFrame(main, text="Sposób sortowania", padding=8)
        opts.grid(row=1, column=0, sticky="ew", pady=8)
        for i, (key, label) in enumerate(core.MODES.items()):
            ttk.Radiobutton(opts, text=label, value=key, variable=self.mode_var,
                            command=self._invalidate_plan).grid(row=0, column=i, sticky="w", padx=(0, 16))
        ttk.Checkbutton(opts, text="Uwzględnij podfoldery", variable=self.recursive_var,
                        command=self._invalidate_plan).grid(row=1, column=0, sticky="w", pady=(6, 0))

        # Podgląd
        preview = ttk.LabelFrame(main, text="Podgląd zmian", padding=8)
        preview.grid(row=2, column=0, sticky="nsew")
        preview.columnconfigure(0, weight=1)
        preview.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(preview, columns=("src", "dst"), show="headings")
        self.tree.heading("src", text="Plik")
        self.tree.heading("dst", text="Zostanie przeniesiony do")
        self.tree.column("src", width=380)
        self.tree.column("dst", width=380)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(preview, orient=tk.VERTICAL, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

        # Przyciski i postęp
        bottom = ttk.Frame(main)
        bottom.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        bottom.columnconfigure(3, weight=1)
        self.preview_btn = ttk.Button(bottom, text="Podgląd", command=self._preview)
        self.preview_btn.grid(row=0, column=0)
        self.sort_btn = ttk.Button(bottom, text="Sortuj", command=self._sort, state=tk.DISABLED)
        self.sort_btn.grid(row=0, column=1, padx=8)
        self.undo_btn = ttk.Button(bottom, text="Cofnij ostatnie sortowanie", command=self._undo)
        self.undo_btn.grid(row=0, column=2)
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.grid(row=0, column=3, sticky="ew", padx=(12, 0))

        ttk.Label(main, textvariable=self.status_var, anchor="w").grid(row=4, column=0, sticky="ew", pady=(8, 0))

    # ── Akcje ───────────────────────────────────────────────────

    def _choose_folder(self) -> None:
        folder = filedialog.askdirectory(title="Wybierz folder do posortowania")
        if folder:
            self.folder_var.set(str(Path(folder)))
            self._preview()

    def _invalidate_plan(self) -> None:
        self._planned = []
        self.tree.delete(*self.tree.get_children())
        self.sort_btn.configure(state=tk.DISABLED)
        if self.folder_var.get():
            self.status_var.set("Ustawienia zmienione – kliknij „Podgląd”.")

    def _preview(self) -> None:
        folder = self.folder_var.get().strip()
        if not folder:
            messagebox.showwarning(APP_NAME, "Najpierw wybierz folder.")
            return
        try:
            moves = core.plan_moves(folder, self.mode_var.get(), self.recursive_var.get())
        except (OSError, ValueError) as exc:
            messagebox.showerror(APP_NAME, f"Nie można odczytać folderu:\n{exc}")
            return

        self._planned = moves
        self.tree.delete(*self.tree.get_children())
        base = Path(folder).resolve()
        for m in moves:
            self.tree.insert("", tk.END, values=(self._rel(m.source, base), self._rel(m.target, base)))
        self.sort_btn.configure(state=tk.NORMAL if moves else tk.DISABLED)
        self.progress.configure(value=0)
        self.status_var.set(f"Do przeniesienia: {len(moves)} plików." if moves
                            else "Nic do zrobienia – folder jest już uporządkowany.")

    @staticmethod
    def _rel(path: Path, base: Path) -> str:
        try:
            return str(path.relative_to(base))
        except ValueError:
            return str(path)

    def _sort(self) -> None:
        if not self._planned or self._busy:
            return
        if not messagebox.askyesno(APP_NAME, f"Przenieść {len(self._planned)} plików?"):
            return
        self._run_in_background(self._sort_worker, list(self._planned))

    def _sort_worker(self, moves: list[core.Move]) -> None:
        def progress(i: int, total: int, _m: core.Move) -> None:
            self._events.put(("progress", i, total))

        done, failed = core.execute_moves(moves, progress)
        if done:
            core.save_history(done)
        self._events.put(("finished", "sort", done, failed))

    def _undo(self) -> None:
        if self._busy:
            return
        moves = core.load_history()
        if not moves:
            messagebox.showinfo(APP_NAME, "Brak operacji do cofnięcia.")
            return
        if not messagebox.askyesno(APP_NAME, f"Przywrócić {len(moves)} plików do poprzednich lokalizacji?"):
            return
        self._run_in_background(self._undo_worker, moves)

    def _undo_worker(self, moves: list[core.Move]) -> None:
        done, failed = core.undo_moves(moves)
        core.clear_history()
        self._events.put(("finished", "undo", done, failed))

    # ── Wątek roboczy i komunikacja z GUI ──────────────────────

    def _run_in_background(self, target, *args) -> None:
        self._busy = True
        for btn in (self.preview_btn, self.sort_btn, self.undo_btn):
            btn.configure(state=tk.DISABLED)
        self.status_var.set("Trwa przenoszenie plików…")
        threading.Thread(target=target, args=args, daemon=True).start()

    def _poll_events(self) -> None:
        try:
            while True:
                event = self._events.get_nowait()
                if event[0] == "progress":
                    _, i, total = event
                    self.progress.configure(maximum=total, value=i)
                elif event[0] == "finished":
                    self._on_finished(*event[1:])
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _on_finished(self, kind: str, done: list, failed: list) -> None:
        self._busy = False
        self.preview_btn.configure(state=tk.NORMAL)
        self._refresh_undo_button()
        verb = "Przeniesiono" if kind == "sort" else "Przywrócono"
        msg = f"{verb} {len(done)} plików."
        if failed:
            details = "\n".join(f"• {m.source.name}: {err}" for m, err in failed[:10])
            more = f"\n… i {len(failed) - 10} więcej" if len(failed) > 10 else ""
            messagebox.showwarning(APP_NAME, f"{msg}\nNie udało się: {len(failed)}\n\n{details}{more}")
        self.status_var.set(msg + (f" Błędy: {len(failed)}." if failed else ""))
        if self.folder_var.get():
            self._preview()
            self.status_var.set(msg + (f" Błędy: {len(failed)}." if failed else ""))

    def _refresh_undo_button(self) -> None:
        self.undo_btn.configure(state=tk.NORMAL if core.load_history() else tk.DISABLED)


def main() -> None:
    _enable_high_dpi()
    App().mainloop()
