"""Ustawienia aplikacji przechowywane lokalnie w %APPDATA%\\Sortowanie."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

DEFAULT_SETTINGS: dict = {
    # Rozpoznawanie miejscowości: "nominatim", "photon" lub "brak"
    "reverse_provider": "nominatim",
    "nominatim_url": "https://nominatim.openstreetmap.org",
    "photon_url": "https://photon.komoot.io",
    # Wyszukiwanie obiektów w pobliżu: "overpass" lub "brak"
    "poi_provider": "overpass",
    "overpass_url": "https://overpass-api.de/api/interpreter",
    "poi_radius_m": 250,
    # Opcjonalny e-mail kontaktowy dołączany do nagłówka User-Agent
    # (zalecane przez zasady korzystania z Nominatim). Domyślnie pusty.
    "contact_email": "",
    # Tryb offline: tylko zapisane wyniki z cache, bez zapytań do internetu
    "offline": False,
    "language": "pl",
    "cache_days": 365,
    # Ostatnio używane foldery
    "last_source": "",
    "target_root": "",
}

PROVIDERS_REVERSE = ("nominatim", "photon", "brak")
PROVIDERS_POI = ("overpass", "brak")


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / "Sortowanie"


def settings_path() -> Path:
    return app_data_dir() / "ustawienia.json"


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class Settings:
    """Prosty, bezpieczny wątkowo słownik ustawień zapisywany do JSON."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings_path()
        self._lock = threading.Lock()
        self._data = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self) -> None:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                self._data.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        with self._lock:
            data = dict(self._data)
        try:
            atomic_write_json(self.path, data)
        except OSError:
            pass

    def get(self, key: str):
        with self._lock:
            return self._data.get(key, DEFAULT_SETTINGS.get(key))

    def set(self, key: str, value) -> None:
        with self._lock:
            self._data[key] = value

    def update(self, values: dict) -> None:
        with self._lock:
            self._data.update(values)

    def as_dict(self) -> dict:
        with self._lock:
            return dict(self._data)
