"""Diagnostyka: rzeczywiste zapytania Overpass dla podanych współrzędnych.

Użycie (z folderu projektu):
    py narzedzia\\sprawdz_lokalizacje.py 50.027082,22.003366 50.040451,21.999130
    py narzedzia\\sprawdz_lokalizacje.py --surowe 50.027082,22.003366     # pełny JSON odpowiedzi
    py narzedzia\\sprawdz_lokalizacje.py --endpoint https://moj-serwer/api/interpreter 50.0,22.0

Wypisuje: zapytania, skrót odpowiedzi serwera, klasyfikację obszarów OSM, wynik
rozpoznania miejscowości i proponowane foldery dla kategorii Hobby (bez nazwy miejsca
i z przykładową nazwą z opcji --nazwa). Program nie wyszukuje pobliskich obiektów.
Używa osobnego, tymczasowego cache, aby pokazać prawdziwe odpowiedzi serwera.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sortowanie import osm  # noqa: E402
from sortowanie.config import Settings  # noqa: E402
from sortowanie.geo import DecisionStore, LocationService, OsmCache, Place  # noqa: E402
from sortowanie.media import KIND_PHOTO, KIND_VIDEO, MediaGroup  # noqa: E402
from sortowanie.overpass import OverpassClient  # noqa: E402
from sortowanie.planner import Assignment, CATEGORY_HOBBY, relative_dir  # noqa: E402


class LoggingClient(OverpassClient):
    def __init__(self, *a, raw: bool = False, **k):
        super().__init__(*a, **k)
        self.raw = raw
        self.on_status = lambda t: print(f"    [{t}]")

    def query(self, ql: str, description: str = "") -> dict:
        print(f"\n  → zapytanie ({description}) do {self.endpoint}:\n    {ql}")
        data = super().query(ql, description)
        els = data.get("elements", [])
        print(f"  ← odpowiedź: {len(els)} elementów; osm3s.timestamp_osm_base="
              f"{data.get('osm3s', {}).get('timestamp_osm_base')}")
        print(f"    licencja: {data.get('osm3s', {}).get('copyright')}")
        if self.raw:
            print(json.dumps(data, ensure_ascii=False, indent=1)[:200000])
        else:
            for el in els[:60]:
                tags = el.get("tags") or {}
                keys = ("name", "place", "boundary", "admin_level", "border_type")
                short = {k: tags[k] for k in keys if k in tags}
                geom = ""
                if el.get("geometry"):
                    geom = f", geometria: {len(el['geometry'])} pkt"
                elif el.get("members"):
                    geom = f", członków: {len(el['members'])}"
                print(f"     {el['type']}/{el['id']} {short}{geom}")
            if len(els) > 60:
                print(f"     … i {len(els) - 60} więcej")
        return data


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("punkty", nargs="+", help="lat,lon")
    ap.add_argument("--endpoint", help="adres Overpass API (domyślnie z ustawień)")
    ap.add_argument("--nazwa", default="Pomnik", help="przykładowa nazwa miejsca do podglądu folderów")
    ap.add_argument("--surowe", action="store_true", help="wypisz pełny JSON odpowiedzi")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="sortowanie_diag_"))
    settings = Settings(tmp / "ustawienia.json")
    if args.endpoint:
        settings.set("overpass_url", args.endpoint)
    client = LoggingClient(lambda: settings.get("overpass_url"), raw=args.surowe)
    service = LocationService(settings, OsmCache(tmp / "cache.json"), DecisionStore(tmp / "d.json"), client)

    print(f"Dane: {osm.ATTRIBUTION} – {osm.ATTRIBUTION_URL}")
    points = []
    for i, text in enumerate(args.punkty):
        lat, lon = (float(x) for x in text.split(","))
        points.append((f"p{i}", lat, lon))

    places: dict[str, Place] = {}
    for key, lat, lon in points:
        print(f"\n════ Punkt {lat}, {lon} – miejscowość")
        result, source, error = service.town(lat, lon)
        print("\n  Obszary OSM obejmujące punkt:")
        for a in result.areas:
            print(f"    - {a.name!r}: {a.kind} ({a.reason})")
        if not result.areas:
            print("    (brak lub z cache)")
        print("  Najbliższe węzły place:")
        for n in result.nodes[:6]:
            print(f"    - {n.name!r} place={n.place} {n.distance_m:.0f} m")
        if result.town:
            print(f"  ⇒ MIEJSCOWOŚĆ: {result.town!r}  [{result.reason}; źródło: {source}]")
        elif result.candidates:
            print(f"  ⇒ NIEJEDNOZNACZNE – użytkownik wybiera z: {result.candidates}  [{result.reason}]")
        else:
            print(f"  ⇒ NIE ROZPOZNANO ({result.reason}) {error}")
        places[key] = Place(town=result.town, town_candidates=result.candidates,
                            town_reason=result.reason, town_source=source, error=error)

    print("\n════ Proponowane foldery (Hobby)")
    plain = Assignment(CATEGORY_HOBBY)
    named = Assignment(CATEGORY_HOBBY, name=args.nazwa)
    for key, lat, lon in points:
        p = places[key]
        if p.ambiguous:
            print(f"  {lat}, {lon}: najpierw wybór miejscowości z {p.town_candidates}")
            continue
        for kind in (KIND_PHOTO, KIND_VIDEO):
            g = MediaGroup(key=key, name=key, files=[], kind=kind, gps=(lat, lon))
            rel = relative_dir(g, plain, p)
            if rel is None:
                print(f"  {lat}, {lon} [{g.media_folder}]: brak danych – miejscowość do wpisania ręcznie")
                continue
            print(f"  {lat}, {lon} [{g.media_folder}]: {rel.as_posix()}/"
                  f"   (z nazwą miejsca: {relative_dir(g, named, p).as_posix()}/)")
    print(f"\nZapytań: {client.requests_made}, pobrano: {client.bytes_received / 1024:.0f} KB")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
