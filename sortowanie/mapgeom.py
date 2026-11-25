"""Przerzedzanie punktów mapy dla dużych zbiorów (bez Qt – testowalne)."""

from __future__ import annotations

from .tiles import latlon_to_px


def thin_points(points: list[tuple[int, float, float]], cell_px: float = 3.0) -> list[tuple[int, float, float]]:
    """Jeden rysowany punkt na komórkę siatki ekranowej (współrzędne w pikselach).

    Wybór kliknięciem nie korzysta z tej listy – pyta katalog o wszystkie materiały w pobliżu.
    """
    seen = set()
    out = []
    for mid, x, y in points:
        key = (int(x // cell_px), int(y // cell_px))
        if key not in seen:
            seen.add(key)
            out.append((mid, x, y))
    return out


def thin_latlon(rows: list[tuple[int, float, float]], zoom: int, limit: int, cell_px: float = 3.0
                ) -> list[tuple[int, float, float]]:
    """Gdy punktów w widoku jest więcej niż ``limit``: jeden punkt na komórkę ``cell_px`` pikseli."""
    if len(rows) <= limit:
        return rows
    px = [(mid, *latlon_to_px(lat, lon, zoom)) for mid, lat, lon in rows]
    keep = {mid for mid, _x, _y in thin_points(px, cell_px)}
    return [r for r in rows if r[0] in keep]
