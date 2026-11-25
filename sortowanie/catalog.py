"""Lokalny katalog materiałów (SQLite) wspólny dla zakładki mapy i sortowania.

* materiał = grupa plików o tej samej nazwie bazowej w tym samym folderze:
  zdjęcie (JPG/JPEG i/lub DNG) albo film (MP4/MOV + SRT + LRF…); każdy plik ma
  własny wiersz w ``files`` – dostęp do każdego z nich jest zachowany,
* identyfikator materiału jest stały między skanowaniami i przenoszeniem plików,
* kolejne skanowania parsują tylko nowe i zmienione grupy (rozmiar + czas
  modyfikacji, także samego SRT) – bez sum kontrolnych,
* kategoria, miejscowość i wydarzenie/zlecenie pochodzą WYŁĄCZNIE z operacji
  sortowania – nigdy z położenia pliku na dysku.

Baza leży w %LOCALAPPDATA%\\SkyCatalog\\katalog.sqlite i nie trafia do repozytorium.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from . import metadata, srt
from .config import local_data_dir

KIND_PHOTO = "zdjęcie"
KIND_VIDEO = "wideo"

PHOTO_EXT = {".jpg", ".jpeg", ".dng"}
VIDEO_EXT = {".mp4", ".mov"}
SIDE_EXT = {".srt", ".lrf"}
CATALOG_EXT = PHOTO_EXT | VIDEO_EXT | SIDE_EXT

STATUS_OK = "dostępny"
STATUS_OFFLINE = "niedostępny"          # odłączony dysk – wpis zachowany
STATUS_MISSING = "brak pliku"           # plik zniknął (usunięty/przeniesiony poza aplikacją)
STATUS_CLOUD = "tylko w chmurze"        # nie pobieramy automatycznie
STATUS_ERROR = "błąd odczytu"

ROLE_MAIN, ROLE_RAW, ROLE_SRT, ROLE_LRF, ROLE_OTHER = "główny", "RAW", "SRT", "LRF", "inny"

CATEGORY_LABELS = {"hobby": "Hobby", "eventy": "Eventy", "zlecenia": "Zlecenia"}
UNASSIGNED = "nieprzypisane"

TRACK_DISPLAY_TOLERANCE_M = 2.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    dir TEXT NOT NULL, dir_key TEXT NOT NULL,
    stem TEXT NOT NULL, stem_key TEXT NOT NULL,
    name TEXT NOT NULL, main_path TEXT NOT NULL, ext TEXT NOT NULL,
    size INTEGER, mtime REAL,
    captured_at TEXT, captured_source TEXT,
    lat REAL, lon REAL,
    min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL,
    srt_path TEXT, track_status TEXT, track_points INTEGER DEFAULT 0,
    track_samples BLOB, track_display BLOB,
    meta TEXT,
    category TEXT, town TEXT, event_name TEXT, event_date TEXT,
    status TEXT NOT NULL DEFAULT 'dostępny', error TEXT,
    root TEXT, last_seen_run INTEGER DEFAULT 0, indexed_at REAL,
    UNIQUE (dir_key, stem_key)
);
CREATE INDEX IF NOT EXISTS idx_mat_bbox ON materials (min_lat, max_lat, min_lon, max_lon);
CREATE INDEX IF NOT EXISTS idx_mat_captured ON materials (captured_at);
CREATE INDEX IF NOT EXISTS idx_mat_root ON materials (root);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    material_id INTEGER NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    path TEXT NOT NULL, path_key TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL, size INTEGER, mtime REAL
);
CREATE INDEX IF NOT EXISTS idx_files_mat ON files (material_id);
CREATE TABLE IF NOT EXISTS scan_queue (
    path_key TEXT PRIMARY KEY, path TEXT NOT NULL, root TEXT NOT NULL, done INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scan_skips (run INTEGER, path TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS imported (
    fingerprint TEXT PRIMARY KEY, target_path TEXT, imported_at REAL
);
CREATE INDEX IF NOT EXISTS idx_files_size ON files (size);
CREATE TABLE IF NOT EXISTS op_log (
    op_id INTEGER, material_id INTEGER, prev TEXT, PRIMARY KEY (op_id, material_id)
);
"""


def path_key(p: str | Path) -> str:
    """Klucz ścieżki: na Windows bez rozróżniania wielkości liter."""
    return os.path.normcase(os.path.normpath(str(p)))


def default_db_path() -> Path:
    return local_data_dir() / "katalog.sqlite"


def _pack(obj) -> bytes:
    return zlib.compress(json.dumps(obj, separators=(",", ":")).encode("utf-8"), 6)


def _unpack(blob):
    if not blob:
        return None
    return json.loads(zlib.decompress(blob).decode("utf-8"))


# ── Rejestr plików w trakcie przenoszenia (skaner ich nie indeksuje) ──

_in_flight: set[str] = set()
_in_flight_lock = threading.Lock()


def mark_in_flight(paths: Iterable[str | Path]) -> None:
    with _in_flight_lock:
        _in_flight.update(path_key(p) for p in paths)


def clear_in_flight(paths: Iterable[str | Path]) -> None:
    with _in_flight_lock:
        for p in paths:
            _in_flight.discard(path_key(p))


def is_in_flight(path: str | Path) -> bool:
    with _in_flight_lock:
        return bool(_in_flight) and path_key(path) in _in_flight


# ── Grupowanie i odczyt metadanych ─────────────────────────────

@dataclass
class FileEntry:
    path: str
    size: int
    mtime: float
    cloud_only: bool = False


def group_entries(entries: list[FileEntry]) -> dict[str, list[FileEntry]]:
    groups: dict[str, list[FileEntry]] = {}
    for e in entries:
        p = Path(e.path)
        if p.suffix.lower() in CATALOG_EXT:
            groups.setdefault(p.stem.casefold(), []).append(e)
    return groups


def _role(ext: str, kind: str, main_ext: str) -> str:
    if ext in SIDE_EXT:
        return ROLE_SRT if ext == ".srt" else ROLE_LRF
    if kind == KIND_PHOTO and ext == ".dng" and main_ext != ".dng":
        return ROLE_RAW
    return ROLE_MAIN if ext == main_ext else ROLE_OTHER


def classify_group(files: list[FileEntry]) -> tuple[str, FileEntry] | None:
    """(rodzaj, plik główny) albo None, gdy w grupie nie ma zdjęcia ani filmu."""
    by_ext = {Path(f.path).suffix.lower(): f for f in files}
    for ext in (".mp4", ".mov"):
        if ext in by_ext:
            return KIND_VIDEO, by_ext[ext]
    for ext in (".jpg", ".jpeg", ".dng"):
        if ext in by_ext:
            return KIND_PHOTO, by_ext[ext]
    return None


@dataclass
class ParsedMaterial:
    kind: str
    main: FileEntry
    files: list[tuple[FileEntry, str]]
    captured_at: datetime | None = None
    captured_source: str = ""
    lat: float | None = None
    lon: float | None = None
    bbox: tuple[float, float, float, float] | None = None
    srt_path: str | None = None
    track_status: str | None = None
    track_points: int = 0
    track_samples: bytes | None = None
    track_display: bytes | None = None
    meta: dict = field(default_factory=dict)
    status: str = STATUS_OK
    error: str | None = None


def parse_group(files: list[FileEntry]) -> ParsedMaterial | None:
    """Odczytuje metadane grupy (tylko lokalnie; nic nie jest wysyłane)."""
    cls = classify_group(files)
    if cls is None:
        return None
    kind, main = cls
    main_ext = Path(main.path).suffix.lower()
    pm = ParsedMaterial(kind, main, [(f, _role(Path(f.path).suffix.lower(), kind, main_ext)) for f in files])
    if any(f.cloud_only for f in files if Path(f.path).suffix.lower() in PHOTO_EXT | VIDEO_EXT | {".srt"}):
        pm.status = STATUS_CLOUD
        pm.meta["uwaga"] = "plik dostępny tylko w chmurze – nie pobrano go automatycznie"
        return pm
    errors = []
    stem = Path(main.path).stem
    if kind == KIND_PHOTO:
        info = metadata.read_photo_info(Path(main.path))
        if info.get("error"):
            errors.append(info["error"])
        if info.get("gps") is None and main_ext != ".dng":
            dng = next((f for f in files if Path(f.path).suffix.lower() == ".dng"), None)
            if dng:
                raw_info = metadata.read_photo_info(Path(dng.path))
                for k, v in raw_info.items():
                    if info.get(k) is None:
                        info[k] = v
        if info.get("gps"):
            pm.lat, pm.lon = info["gps"]
            pm.bbox = (pm.lat, pm.lon, pm.lat, pm.lon)
        if info.get("taken"):
            pm.captured_at, pm.captured_source = info["taken"], "EXIF"
        pm.meta = {k: v for k, v in info.items() if k not in ("gps", "taken", "error") and v is not None}
    else:
        info = metadata.read_video_info(Path(main.path))
        if info.get("error"):
            errors.append(info["error"])
        pm.meta = {k: v for k, v in info.items() if k not in ("gps", "taken", "error") and v is not None}
        srt_file = next((f for f in files if Path(f.path).suffix.lower() == ".srt"), None)
        if srt_file is None:
            pm.track_status = "brak SRT"
        else:
            pm.srt_path = srt_file.path
            track = srt.read_track(Path(srt_file.path))
            pm.track_status = track.status
            pm.track_points = track.valid_count
            if track.start:
                pm.lat, pm.lon = track.start
                pm.bbox = track.bbox or (pm.lat, pm.lon, pm.lat, pm.lon)
                pm.track_samples = _pack(track.compact_samples())
                pm.track_display = _pack(srt.simplify_segments(track.segments, TRACK_DISPLAY_TOLERANCE_M))
            if track.home:
                pm.meta["punkt_startowy_pilota"] = list(track.home)
            if track.breaks:
                pm.meta["podziały_trasy"] = track.breaks
            if track.invalid_count:
                pm.meta["próbki_bez_gps"] = track.invalid_count
            if track.started_at:
                pm.captured_at, pm.captured_source = track.started_at, "SRT"
        if pm.captured_at is None:
            name_dt = metadata.date_from_filename(stem)
            if name_dt:
                pm.captured_at, pm.captured_source = name_dt, "nazwa pliku"
            elif info.get("taken"):
                pm.captured_at, pm.captured_source = info["taken"], "metadane MP4"
    if pm.captured_at is None and kind == KIND_PHOTO:
        name_dt = metadata.date_from_filename(stem)
        if name_dt:
            pm.captured_at, pm.captured_source = name_dt, "nazwa pliku"
    if errors:
        pm.error = "; ".join(errors)[:500]
    return pm


# ── Filtry ─────────────────────────────────────────────────────

@dataclass
class Filters:
    photos: bool = True
    videos: bool = True
    date_from: date | None = None
    date_to: date | None = None
    category: str | None = None          # 'hobby' / 'eventy' / 'zlecenia' / 'nieprzypisane' / None = wszystkie
    town: str | None = None
    event: str | None = None
    text: str = ""
    include_missing: bool = False

    def where(self) -> tuple[str, list]:
        sql, args = [], []
        kinds = [k for k, on in ((KIND_PHOTO, self.photos), (KIND_VIDEO, self.videos)) if on]
        if not kinds:
            return "0", []
        sql.append(f"kind IN ({','.join('?' * len(kinds))})")
        args += kinds
        if self.date_from:
            sql.append("captured_at >= ?")
            args.append(self.date_from.isoformat())
        if self.date_to:
            sql.append("captured_at < ?")
            args.append(date.fromordinal(self.date_to.toordinal() + 1).isoformat())
        if self.category == UNASSIGNED:
            sql.append("category IS NULL")
        elif self.category:
            sql.append("category = ?")
            args.append(self.category)
        if self.town:
            sql.append("town = ?")
            args.append(self.town)
        if self.event:
            sql.append("event_name = ?")
            args.append(self.event)
        if self.text.strip():
            needle = "%" + self.text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            sql.append("(name LIKE ? ESCAPE '\\' OR main_path LIKE ? ESCAPE '\\')")
            args += [needle, needle]
        if not self.include_missing:
            sql.append("status != ?")
            args.append(STATUS_MISSING)
        return " AND ".join(sql) or "1", args


# ── Katalog ────────────────────────────────────────────────────

class Catalog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        with self.conn() as c:
            c.executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # Stan
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.conn().execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._write_lock, self.conn() as c:
            c.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))

    # ── Indeksowanie folderu ──
    def process_directory(self, directory: str, entries: list[FileEntry], root: str, run: int,
                          force: bool = False) -> dict[str, int]:
        """Aktualizuje materiały z jednego folderu. Parsuje tylko nowe i zmienione grupy."""
        stats = {"nowe": 0, "zmienione": 0, "bez_zmian": 0}
        dir_key = path_key(directory)
        groups = group_entries(entries)
        if not groups:
            return stats
        c = self.conn()
        existing = {r["stem_key"]: r for r in c.execute(
            "SELECT id, stem_key, status FROM materials WHERE dir_key=?", (dir_key,))}
        for stem_key, files in groups.items():
            row = existing.get(stem_key)
            if row is not None and not force:
                stored = {(r["path_key"], r["size"], r["mtime"]) for r in c.execute(
                    "SELECT path_key, size, mtime FROM files WHERE material_id=?", (row["id"],))}
                current = {(path_key(f.path), f.size, f.mtime) for f in files}
                # pobranie z chmury / zwolnienie miejsca nie zmienia rozmiaru ani daty – sprawdzamy osobno
                cloud_now = any(f.cloud_only for f in files)
                cloud_changed = cloud_now != (row["status"] == STATUS_CLOUD)
                if stored == current and not cloud_changed:
                    with self._write_lock, c:
                        c.execute("UPDATE materials SET last_seen_run=?, root=?, "
                                  "status=CASE WHEN status IN (?, ?) THEN ? ELSE status END WHERE id=?",
                                  (run, root, STATUS_OFFLINE, STATUS_MISSING, STATUS_OK, row["id"]))
                    stats["bez_zmian"] += 1
                    continue
            pm = parse_group(files)
            if pm is None:
                continue
            self._upsert(pm, directory, dir_key, stem_key, root, run, row["id"] if row else None)
            stats["zmienione" if row else "nowe"] += 1
        return stats

    def _upsert(self, pm: ParsedMaterial, directory: str, dir_key: str, stem_key: str, root: str,
                run: int, material_id: int | None) -> int:
        main = Path(pm.main.path)
        b = pm.bbox or (None, None, None, None)
        values = dict(
            kind=pm.kind, dir=directory, dir_key=dir_key, stem=main.stem, stem_key=stem_key,
            name=main.name, main_path=str(main), ext=main.suffix.lower().lstrip("."),
            size=pm.main.size, mtime=pm.main.mtime,
            captured_at=pm.captured_at.isoformat(timespec="seconds") if pm.captured_at else None,
            captured_source=pm.captured_source or None, lat=pm.lat, lon=pm.lon,
            min_lat=b[0], min_lon=b[1], max_lat=b[2], max_lon=b[3],
            srt_path=pm.srt_path, track_status=pm.track_status, track_points=pm.track_points,
            track_samples=pm.track_samples, track_display=pm.track_display,
            meta=json.dumps(pm.meta, ensure_ascii=False, default=str), status=pm.status, error=pm.error,
            root=root, last_seen_run=run, indexed_at=time.time(),
        )
        c = self.conn()
        with self._write_lock, c:
            if material_id is None:
                cols = ",".join(values)
                cur = c.execute(f"INSERT INTO materials ({cols}) VALUES ({','.join('?' * len(values))})",
                                list(values.values()))
                material_id = cur.lastrowid
            else:
                sets = ",".join(f"{k}=?" for k in values)
                c.execute(f"UPDATE materials SET {sets} WHERE id=?", [*values.values(), material_id])
                c.execute("DELETE FROM files WHERE material_id=?", (material_id,))
            for f, role in pm.files:
                c.execute("INSERT OR REPLACE INTO files (material_id, path, path_key, role, size, mtime) "
                          "VALUES (?,?,?,?,?,?)", (material_id, f.path, path_key(f.path), role, f.size, f.mtime))
        return material_id

    def index_paths(self, paths: Iterable[str | Path], run: int = 0) -> list[int]:
        """Natychmiastowe zaindeksowanie konkretnych plików (np. po sortowaniu) – bez skanowania dysku."""
        by_dir: dict[str, list[FileEntry]] = {}
        for p in paths:
            p = Path(p)
            try:
                st = p.stat()
            except OSError:
                continue
            by_dir.setdefault(str(p.parent), []).append(FileEntry(str(p), st.st_size, st.st_mtime))
        ids = []
        for d, entries in by_dir.items():
            # uzupełnij o pozostałe pliki tej samej grupy już leżące w folderze
            stems = {Path(e.path).stem.casefold() for e in entries}
            known = {path_key(e.path) for e in entries}
            try:
                for name in os.listdir(d):
                    fp = Path(d) / name
                    if fp.stem.casefold() in stems and path_key(fp) not in known and fp.suffix.lower() in CATALOG_EXT:
                        st = fp.stat()
                        entries.append(FileEntry(str(fp), st.st_size, st.st_mtime))
            except OSError:
                pass
            root = self.root_for(d)
            self.process_directory(d, entries, root, run or self.current_run(), force=False)
            ids += [r[0] for r in self.conn().execute(
                f"SELECT id FROM materials WHERE dir_key=? AND stem_key IN ({','.join('?' * len(stems))})",
                (path_key(d), *stems))]
        return ids

    @staticmethod
    def root_for(directory: str) -> str:
        p = Path(directory)
        return str(Path(p.anchor)) if p.anchor else str(p.parts[0] if p.parts else directory)

    def current_run(self) -> int:
        return int(self.get_state("run", "0") or 0)

    # ── Dostępność ──
    @staticmethod
    def _prefix_clause(root: str) -> tuple[str, list]:
        key = path_key(root).rstrip("\\/")
        like = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + os.sep.replace("\\", "\\\\") + "%"
        return "(dir_key = ? OR dir_key LIKE ? ESCAPE '\\')", [key, like]

    def mark_root_unavailable(self, root: str) -> int:
        """Odłączony dysk / folder: wpisy zostają, ale są oznaczone jako niedostępne."""
        clause, args = self._prefix_clause(root)
        with self._write_lock, self.conn() as c:
            cur = c.execute(f"UPDATE materials SET status=? WHERE {clause} AND status=?",
                            (STATUS_OFFLINE, *args, STATUS_OK))
            return cur.rowcount

    def finalize_scan(self, run: int, roots: list[str]) -> dict[str, int]:
        """Po PEŁNYM przejściu katalogów: materiały niewidziane w tym przebiegu → 'brak pliku'.
        Przeniesienia poza aplikacją (ta sama nazwa, rozmiar i czas) zachowują kategorię."""
        c = self.conn()
        result = {"brak_pliku": 0, "rozpoznane_przeniesienia": 0, "pominięte_niesprawdzone": 0}
        # Foldery, których w tym przebiegu nie udało się sprawdzić (brak uprawnień, błąd,
        # wykluczenie) – ich materiałów NIE uznajemy za usunięte.
        unchecked = [path_key(p).rstrip("\\/") for (p,) in c.execute(
            "SELECT path FROM scan_skips WHERE run=? AND reason NOT LIKE ?", (run, "folder zniknął%"))]

        def under_unchecked(dk: str) -> bool:
            return any(dk == u or dk.startswith(u + os.sep) for u in unchecked)

        for root in roots:
            clause, args = self._prefix_clause(root)
            with self._write_lock, c:
                missing = c.execute(f"SELECT * FROM materials WHERE {clause} AND last_seen_run<? AND status NOT IN (?, ?)",
                                    (*args, run, STATUS_MISSING, STATUS_CLOUD)).fetchall()
                for m in missing:
                    if is_in_flight(m["main_path"]):
                        continue
                    if under_unchecked(m["dir_key"]):
                        result["pominięte_niesprawdzone"] += 1
                        continue
                    twin = c.execute(
                        "SELECT id FROM materials WHERE stem_key=? AND kind=? AND size=? AND mtime=? "
                        "AND last_seen_run=? AND id!=? AND category IS NULL",
                        (m["stem_key"], m["kind"], m["size"], m["mtime"], run, m["id"])).fetchone()
                    if twin and m["category"]:
                        c.execute("UPDATE materials SET category=?, town=?, event_name=?, event_date=? WHERE id=?",
                                  (m["category"], m["town"], m["event_name"], m["event_date"], twin["id"]))
                        result["rozpoznane_przeniesienia"] += 1
                    c.execute("UPDATE materials SET status=? WHERE id=?", (STATUS_MISSING, m["id"]))
                    result["brak_pliku"] += 1
        return result

    # ── Zapytania mapy i listy ──
    def query_points(self, flt: Filters, bbox: tuple[float, float, float, float] | None = None,
                     limit: int = 50000) -> list[sqlite3.Row]:
        """Materiały z lokalizacją (punkt lub trasa przecinająca widok)."""
        where, args = flt.where()
        sql = (f"SELECT id, kind, lat, lon, min_lat, min_lon, max_lat, max_lon, track_points, status "
               f"FROM materials WHERE lat IS NOT NULL AND {where}")
        if bbox:
            s, w, n, e = bbox
            sql += " AND max_lat >= ? AND min_lat <= ? AND max_lon >= ? AND min_lon <= ?"
            args = [*args, s, n, w, e]
        return self.conn().execute(sql + " LIMIT ?", [*args, limit]).fetchall()

    def query_list(self, flt: Filters, located: bool | None = None, limit: int = 5000,
                   bbox: tuple[float, float, float, float] | None = None) -> list[sqlite3.Row]:
        where, args = flt.where()
        sql = (f"SELECT id, kind, name, main_path, captured_at, captured_source, lat, lon, category, town, "
               f"event_name, status, track_status, error FROM materials WHERE {where}")
        if located is True:
            sql += " AND lat IS NOT NULL"
        elif located is False:
            sql += " AND lat IS NULL"
        if bbox:
            s, w, n, e = bbox
            sql += " AND max_lat >= ? AND min_lat <= ? AND max_lon >= ? AND min_lon <= ?"
            args = [*args, s, n, w, e]
        sql += " ORDER BY captured_at IS NULL, captured_at DESC, name LIMIT ?"
        return self.conn().execute(sql, [*args, limit]).fetchall()

    def count(self, flt: Filters, located: bool | None = None) -> int:
        where, args = flt.where()
        extra = " AND lat IS NOT NULL" if located is True else " AND lat IS NULL" if located is False else ""
        return self.conn().execute(f"SELECT COUNT(*) FROM materials WHERE {where}{extra}", args).fetchone()[0]

    def bounds(self, flt: Filters) -> tuple[float, float, float, float] | None:
        where, args = flt.where()
        row = self.conn().execute(f"SELECT MIN(min_lat), MIN(min_lon), MAX(max_lat), MAX(max_lon) "
                                  f"FROM materials WHERE lat IS NOT NULL AND {where}", args).fetchone()
        return tuple(row) if row and row[0] is not None else None

    def pick(self, flt: Filters, lat: float, lon: float, tol_m: float, limit: int = 40) -> list[dict]:
        """Materiały pod kliknięciem: punkty zdjęć, punkty startowe i linie tras w promieniu ``tol_m``.

        Zwraca listę od najbliższego – przy nakładających się obiektach użytkownik wybiera z listy.
        """
        import math
        dlat = tol_m / 111_320
        dlon = tol_m / (111_320 * max(0.05, math.cos(math.radians(lat))))
        where, args = flt.where()
        rows = self.conn().execute(
            f"SELECT id, kind, name, lat, lon, track_display FROM materials WHERE lat IS NOT NULL AND {where} "
            f"AND max_lat >= ? AND min_lat <= ? AND max_lon >= ? AND min_lon <= ? LIMIT 3000",
            [*args, lat - dlat, lat + dlat, lon - dlon, lon + dlon]).fetchall()
        kx, ky = 111_320 * math.cos(math.radians(lat)), 110_540

        def dist_pt(a, b):
            return math.hypot((b - lon) * kx, (a - lat) * ky)

        out = []
        for r in rows:
            d = dist_pt(r["lat"], r["lon"])
            on_track = False
            segs = _unpack(r["track_display"]) if r["kind"] == KIND_VIDEO else None
            for seg in segs or ():
                pts = [((p[1] - lon) * kx, (p[0] - lat) * ky) for p in seg]
                for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                    dx, dy = bx - ax, by - ay
                    L = dx * dx + dy * dy
                    t = 0.0 if L == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L))
                    ds = math.hypot(ax + t * dx, ay + t * dy)
                    if ds < d:
                        d, on_track = ds, True
            if d <= tol_m:
                out.append({"id": r["id"], "kind": r["kind"], "name": r["name"], "distance_m": round(d, 1),
                            "on_track": on_track})
        out.sort(key=lambda x: (x["distance_m"] + (0 if not x["on_track"] else tol_m * 0.25)))
        return out[:limit]

    def display_tracks(self, ids: list[int]) -> dict[int, list[list[tuple[float, float]]]]:
        out: dict[int, list] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in self.conn().execute(
                    f"SELECT id, track_display FROM materials WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                segs = _unpack(r["track_display"])
                if segs:
                    out[r["id"]] = [[tuple(p) for p in seg] for seg in segs]
        return out

    def track_samples(self, material_id: int) -> list | None:
        row = self.conn().execute("SELECT track_samples FROM materials WHERE id=?", (material_id,)).fetchone()
        return _unpack(row[0]) if row else None

    def material(self, material_id: int) -> dict | None:
        row = self.conn().execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d.pop("track_samples", None)
        d.pop("track_display", None)
        try:
            d["meta"] = json.loads(d["meta"] or "{}")
        except ValueError:
            d["meta"] = {}
        d["files"] = [dict(f) for f in self.conn().execute(
            "SELECT path, role, size, mtime FROM files WHERE material_id=? ORDER BY role != ?, path",
            (material_id, ROLE_MAIN))]
        return d

    def material_id_for_path(self, path: str | Path) -> int | None:
        row = self.conn().execute("SELECT material_id FROM files WHERE path_key=?", (path_key(path),)).fetchone()
        return row[0] if row else None

    def is_indexed_file(self, path: str | Path) -> bool:
        """Podgląd udostępniamy wyłącznie dla plików zapisanych w katalogu."""
        return self.material_id_for_path(path) is not None

    def distinct(self, column: str) -> list[str]:
        if column not in ("town", "event_name"):
            raise ValueError(column)
        return [r[0] for r in self.conn().execute(
            f"SELECT DISTINCT {column} FROM materials WHERE {column} IS NOT NULL ORDER BY {column}")]

    def totals(self) -> dict[str, int]:
        c = self.conn()
        rows = c.execute("SELECT kind, status, lat IS NOT NULL AS located, COUNT(*) FROM materials "
                         "GROUP BY kind, status, located").fetchall()
        out = {"razem": 0}
        for kind, status, located, n in rows:
            out["razem"] += n
            out[f"{kind}"] = out.get(kind, 0) + n
            out[f"status:{status}"] = out.get(f"status:{status}", 0) + n
            if not located:
                out["bez_lokalizacji"] = out.get("bez_lokalizacji", 0) + n
        return out

    # ── Powiązanie z sortowaniem ──
    def apply_moves(self, moves: list[tuple[str, str]], info: dict[str, dict] | None = None,
                    op_id: int | None = None) -> tuple[int, list[int]]:
        """Aktualizuje ścieżki po faktycznie wykonanych przeniesieniach (źródło → cel).

        ``info``: ścieżka źródłowa → {category, town, event_name, event_date, object} –
        dane z sortowania. Poprzedni stan materiału trafia do ``op_log`` (do cofnięcia).
        Pliki nieznane katalogowi są indeksowane w miejscu docelowym (bez skanowania dysku).
        """
        info = info or {}
        c = self.conn()
        op_id = op_id or int(time.time() * 1000)
        touched: dict[int, dict] = {}
        unknown_targets: list[str] = []
        with self._write_lock:
            with c:
                for src, dst in moves:
                    row = c.execute("SELECT id, material_id FROM files WHERE path_key=?", (path_key(src),)).fetchone()
                    if row is None:
                        unknown_targets.append(dst)
                        if src in info:
                            touched.setdefault(-len(unknown_targets), {"target": dst, **info[src]})
                        continue
                    c.execute("UPDATE files SET path=?, path_key=? WHERE id=?", (dst, path_key(dst), row["id"]))
                    t = touched.setdefault(row["material_id"], {})
                    if src in info:
                        t.update(info[src])
                for mid, data in list(touched.items()):
                    if mid < 0:
                        continue
                    m = c.execute("SELECT * FROM materials WHERE id=?", (mid,)).fetchone()
                    c.execute("INSERT OR REPLACE INTO op_log (op_id, material_id, prev) VALUES (?,?,?)",
                              (op_id, mid, json.dumps({k: m[k] for k in ("category", "town", "event_name", "event_date",
                                                                         "dir", "dir_key", "stem", "stem_key", "name",
                                                                         "main_path", "srt_path", "root")})))
                    self._refresh_material_paths(c, mid)
                    if data:
                        c.execute("UPDATE materials SET category=?, town=?, event_name=?, event_date=?, "
                                  "meta=json_set(COALESCE(meta,'{}'), '$.obiekt', ?) WHERE id=?",
                                  (data.get("category"), data.get("town"), data.get("event_name"),
                                   data.get("event_date"), data.get("object"), mid))
        new_ids = []
        if unknown_targets:
            new_ids = self.index_paths(unknown_targets)
            with self._write_lock, c:
                for key, data in touched.items():
                    if key >= 0:
                        continue
                    mid = self.material_id_for_path(data["target"])
                    if mid is None:
                        continue
                    c.execute("INSERT OR IGNORE INTO op_log (op_id, material_id, prev) VALUES (?,?,?)",
                              (op_id, mid, json.dumps({"category": None, "town": None, "event_name": None,
                                                       "event_date": None, "_nowy": True})))
                    c.execute("UPDATE materials SET category=?, town=?, event_name=?, event_date=?, "
                              "meta=json_set(COALESCE(meta,'{}'), '$.obiekt', ?) WHERE id=?",
                              (data.get("category"), data.get("town"), data.get("event_name"),
                               data.get("event_date"), data.get("object"), mid))
        return op_id, sorted({k for k in touched if k >= 0} | set(new_ids))

    def _refresh_material_paths(self, c: sqlite3.Connection, mid: int) -> None:
        files = c.execute("SELECT path, role FROM files WHERE material_id=?", (mid,)).fetchall()
        main = next((f["path"] for f in files if f["role"] == ROLE_MAIN), files[0]["path"] if files else None)
        if main is None:
            return
        p = Path(main)
        srt_path = next((f["path"] for f in files if f["role"] == ROLE_SRT), None)
        new_dir_key, new_stem_key = path_key(p.parent), p.stem.casefold()
        clash = c.execute("SELECT id FROM materials WHERE dir_key=? AND stem_key=? AND id!=?",
                          (new_dir_key, new_stem_key, mid)).fetchone()
        if clash:   # nazwa zajęta (np. sufiks kolizji) – unikalny klucz z identyfikatorem
            new_stem_key = f"{new_stem_key}#{mid}"
        c.execute("UPDATE materials SET dir=?, dir_key=?, stem=?, stem_key=?, name=?, main_path=?, srt_path=?, "
                  "root=?, status=? WHERE id=?",
                  (str(p.parent), new_dir_key, p.stem, new_stem_key, p.name, str(p), srt_path,
                   self.root_for(str(p.parent)), STATUS_OK, mid))

    def undo_moves(self, moves: list[tuple[str, str]], op_id: int | None) -> list[int]:
        """Cofnięcie: ``moves`` to faktycznie wykonane przeniesienia powrotne (cel → źródło)."""
        c = self.conn()
        touched = set()
        with self._write_lock, c:
            for src, dst in moves:
                row = c.execute("SELECT id, material_id FROM files WHERE path_key=?", (path_key(src),)).fetchone()
                if row is None:
                    continue
                c.execute("UPDATE files SET path=?, path_key=? WHERE id=?", (dst, path_key(dst), row["id"]))
                touched.add(row["material_id"])
            for mid in touched:
                self._refresh_material_paths(c, mid)
                if op_id is not None:
                    prev = c.execute("SELECT prev FROM op_log WHERE op_id=? AND material_id=?", (op_id, mid)).fetchone()
                    if prev:
                        p = json.loads(prev[0])
                        c.execute("UPDATE materials SET category=?, town=?, event_name=?, event_date=? WHERE id=?",
                                  (p.get("category"), p.get("town"), p.get("event_name"), p.get("event_date"), mid))
        return sorted(touched)

    # ── Import z karty (kopiowanie) ──
    @staticmethod
    def fingerprint(name: str, size: int, mtime: float) -> str:
        """Odcisk pliku z karty: nazwa + rozmiar + czas (z dokładnością do 2 s – system plików FAT)."""
        return f"{name.casefold()}|{int(size)}|{int(mtime) // 2}"

    def is_imported(self, name: str, size: int, mtime: float, exclude_root: str | None = None) -> bool:
        """Czy plik był już zaimportowany albo jest w katalogu (np. skopiowany wcześniej poza programem)."""
        c = self.conn()
        if c.execute("SELECT 1 FROM imported WHERE fingerprint=?", (self.fingerprint(name, size, mtime),)).fetchone():
            return True
        ex = path_key(exclude_root).rstrip("\\/") + os.sep if exclude_root else None
        for r in c.execute("SELECT path_key, mtime FROM files WHERE size=?", (int(size),)):
            if ex and r["path_key"].startswith(ex):
                continue        # ten sam plik na samej karcie (np. zeskanowany w zakładce mapy)
            if os.path.basename(r["path_key"]) == os.path.normcase(name) and r["mtime"] is not None \
                    and abs(r["mtime"] - mtime) <= 2:
                return True
        return False

    def apply_copies(self, copies: list[tuple[str, str]], info: dict[str, dict] | None = None,
                     op_id: int | None = None) -> tuple[int, list[int]]:
        """Po skopiowaniu z karty: indeksuje kopie w archiwum, zapisuje kategorię i rejestr importu.

        Wpisy plików źródłowych (karty) nie są zmieniane – karta pozostaje bez zmian.
        """
        info = info or {}
        op_id = op_id or int(time.time() * 1000)
        targets = [dst for _src, dst in copies]
        ids = self.index_paths(targets)
        c = self.conn()
        with self._write_lock, c:
            for src, dst in copies:
                try:
                    st = Path(src).stat()
                    c.execute("INSERT OR REPLACE INTO imported (fingerprint, target_path, imported_at) VALUES (?,?,?)",
                              (self.fingerprint(Path(src).name, st.st_size, st.st_mtime), dst, time.time()))
                except OSError:
                    pass
                mid = c.execute("SELECT material_id FROM files WHERE path_key=?", (path_key(dst),)).fetchone()
                data = info.get(src)
                if mid is None or not data:
                    continue
                c.execute("INSERT OR IGNORE INTO op_log (op_id, material_id, prev) VALUES (?,?,?)",
                          (op_id, mid[0], json.dumps({"category": None, "_nowy": True})))
                c.execute("UPDATE materials SET category=?, town=?, event_name=?, event_date=?, "
                          "meta=json_set(COALESCE(meta,'{}'), '$.obiekt', ?) WHERE id=?",
                          (data.get("category"), data.get("town"), data.get("event_name"),
                           data.get("event_date"), data.get("object"), mid[0]))
        return op_id, ids

    def remove_copies(self, targets: list[str]) -> list[int]:
        """Cofnięcie importu kopiującego: usuwa wpisy usuniętych kopii i rejestr importu."""
        c = self.conn()
        touched = set()
        with self._write_lock, c:
            for t in targets:
                row = c.execute("SELECT material_id FROM files WHERE path_key=?", (path_key(t),)).fetchone()
                if row:
                    touched.add(row[0])
                    c.execute("DELETE FROM files WHERE path_key=?", (path_key(t),))
                c.execute("DELETE FROM imported WHERE target_path=?", (t,))
            for mid in touched:
                left = c.execute("SELECT COUNT(*) FROM files WHERE material_id=?", (mid,)).fetchone()[0]
                if left == 0:
                    c.execute("DELETE FROM materials WHERE id=?", (mid,))
                else:
                    self._refresh_material_paths(c, mid)
        return sorted(touched)

    # ── Kolejka skanowania (wznowienie) ──
    def queue_reset(self, roots: list[str]) -> None:
        with self._write_lock, self.conn() as c:
            c.execute("DELETE FROM scan_queue")
            c.executemany("INSERT OR IGNORE INTO scan_queue (path_key, path, root) VALUES (?,?,?)",
                          [(path_key(r), r, r) for r in roots])

    def queue_add(self, items: list[tuple[str, str]]) -> None:
        if not items:
            return
        with self._write_lock, self.conn() as c:
            c.executemany("INSERT OR IGNORE INTO scan_queue (path_key, path, root) VALUES (?,?,?)",
                          [(path_key(p), p, root) for p, root in items])

    def queue_next(self, n: int = 50) -> list[sqlite3.Row]:
        return self.conn().execute("SELECT path, root FROM scan_queue WHERE done=0 LIMIT ?", (n,)).fetchall()

    def queue_done(self, path: str) -> None:
        with self._write_lock, self.conn() as c:
            c.execute("UPDATE scan_queue SET done=1 WHERE path_key=?", (path_key(path),))

    def queue_pending(self) -> int:
        return self.conn().execute("SELECT COUNT(*) FROM scan_queue WHERE done=0").fetchone()[0]

    def queue_total(self) -> int:
        return self.conn().execute("SELECT COUNT(*) FROM scan_queue").fetchone()[0]

    def add_skip(self, run: int, path: str, reason: str) -> None:
        with self._write_lock, self.conn() as c:
            c.execute("INSERT INTO scan_skips (run, path, reason) VALUES (?,?,?)", (run, path, reason[:300]))

    def skip_summary(self, run: int) -> list[tuple[str, int]]:
        return [(r[0], r[1]) for r in self.conn().execute(
            "SELECT reason, COUNT(*) FROM scan_skips WHERE run=? GROUP BY reason ORDER BY 2 DESC", (run,))]

    def skips(self, run: int, limit: int = 500) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in self.conn().execute(
            "SELECT path, reason FROM scan_skips WHERE run=? LIMIT ?", (run, limit))]
