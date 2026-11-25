"""Diagnostyka: rzeczywiste zapytania Overpass dla podanych współrzędnych.

Użycie (z folderu projektu):
    py narzedzia\\sprawdz_lokalizacje.py 50.027082,22.003366 50.040451,21.999130
    py narzedzia\\sprawdz_lokalizacje.py --surowe 50.027082,22.003366     # pełny JSON odpowiedzi
    py narzedzia\\sprawdz_lokalizacje.py --endpoint https://moj-serwer/api/interpreter 50.0,22.0

Wypisuje: zapytania, skrót odpowiedzi serwera, klasyfikację obszarów OSM, wynik
rozpoznania miejscowości, kandydatów na obiekt w promieniu i proponowane foldery
dla kategorii Hobby (przed i po zatwierdzeniu obiektu). Używa osobnego, tymczasowego
cache, aby pokazać prawdziwe odpowiedzi serwera.
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
                keys = ("name", "place", "boundary", "admin_level", "border_type", "historic", "tourism",
                        "leisure", "building", "amenity", "man_made", "natural")
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
    ap.add_argument("--promien", type=int, default=250)
    ap.add_argument("--surowe", action="store_true", help="wypisz pełny JSON odpowiedzi")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="sortowanie_diag_"))
    settings = Settings(tmp / "ustawienia.json")
    settings.set("poi_radius_m", args.promien)
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

    print(f"\n════ Obiekty w promieniu {args.promien} m (jedno pobranie kafelków dla wszystkich punktów)")

    def on_result(key, cands, source, error, remembered):
        p = places[key]
        p.objects, p.object_checked = cands, True
        lat, lon = next((la, lo) for k, la, lo in points if k == key)
        print(f"\n  Punkt {lat}, {lon}: {len(cands)} kandydatów [{source}] {error}")
        for c in cands[:10]:
            print(f"    - {c.name!r} ({c.category}, {c.osm_id}) – {c.distance_m:.0f} m od granicy obiektu")

    service.objects(points, on_result)

    print("\n════ Proponowane foldery (Hobby)")
    hobby = Assignment(CATEGORY_HOBBY)
    for key, lat, lon in points:
        p = places[key]
        for kind in (KIND_VIDEO, KIND_PHOTO):
            g = MediaGroup(key=key, name=key, files=[], kind=kind, gps=(lat, lon))
            if p.ambiguous:
                print(f"  {lat}, {lon}: najpierw wybór miejscowości z {p.town_candidates}")
                break
            if p.needs_object_decision:
                prop = p.proposed_object
                pending = relative_dir(g, hobby, p)
                p.object_choice = prop.osm_id
                accepted = relative_dir(g, hobby, p)
                p.object_choice = ""
                rejected = relative_dir(g, hobby, p)
                p.object_choice = None
                print(f"  {lat}, {lon} [{g.media_folder}]: propozycja obiektu {prop.name!r} – do zatwierdzenia"
                      f" (do decyzji folder = {pending})\n      po zatwierdzeniu: {accepted.as_posix()}/"
                      f"\n      po odrzuceniu:    {rejected.as_posix()}/")
            else:
                rel = relative_dir(g, hobby, p)
                print(f"  {lat}, {lon} [{g.media_folder}]: "
                      f"{rel.as_posix() + '/' if rel else 'brak danych – miejscowość do wpisania ręcznie'}")
    print(f"\nZapytań: {client.requests_made}, pobrano: {client.bytes_received / 1024:.0f} KB")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
