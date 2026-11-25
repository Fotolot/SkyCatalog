"""Tworzy przykładowe, syntetyczne materiały do pokazania aplikacji (bez prywatnych plików).

Użycie:  py narzedzia\\dane_demo.py <folder_docelowy>

Powstaje folder <folder>\\Karta\\DCIM\\100MEDIA ze zdjęciami (z GPS, bez GPS, para JPG/DNG,
nakładające się punkty) i filmami (SRT z luką GPS, format Avata 2, film bez SRT).
Pliki wideo nie zawierają obrazu – służą tylko do pokazania metadanych i tras.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers import make_jpeg, make_mp4  # noqa: E402
from tests.test_srt import avata2, build, mini4  # noqa: E402


def walk(i, lat0, lon0):
    return lat0 + i * 0.000012, lon0 + i * 0.000018


def main(target: Path) -> None:
    d = target / "Karta" / "DCIM" / "100MEDIA"
    d.mkdir(parents=True, exist_ok=True)
    make_jpeg(d / "DJI_0101.JPG", 50.027082, 22.003366, "2026:11:11 10:00:00")
    make_jpeg(d / "DJI_0102.JPG", 50.027090, 22.003370, "2026:11:11 10:01:00")       # nakłada się na 0101
    make_jpeg(d / "DJI_0103.JPG", 50.040451, 21.999130, "2026:11:12 12:00:00")
    make_jpeg(d / "DJI_0103.DNG", 50.040451, 21.999130, "2026:11:12 12:00:00")       # para JPG/DNG
    make_jpeg(d / "IMG_0200.JPG", None, None, "2026:10:01 09:00:00")                 # bez GPS
    make_mp4(d / "DJI_0201.MP4")
    (d / "DJI_0201.SRT").write_text(build(lambda i, t: mini4(*walk(i, 50.0262, 22.0010), i=i)
                                          if not 300 <= i < 500 else mini4(0, 0, i), 900), encoding="utf-8")
    make_mp4(d / "DJI_0202.MP4")
    (d / "DJI_0202.SRT").write_text(build(lambda i, t: avata2(*walk(-i, 50.0410, 22.0010)), 600), encoding="utf-8")
    make_mp4(d / "DJI_0203.MP4")                                                       # bez SRT
    print(f"Utworzono przykładowe materiały w {d}")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "dane_demo"))
