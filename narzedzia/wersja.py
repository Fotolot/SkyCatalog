"""Ustala wersję wydania i zapisuje ją do aplikacji oraz metadanych pliku .exe.

Użycie (CI):  python narzedzia/wersja.py --build <github.run_number>
Wersja = MAJOR.MINOR z pliku VERSION + numer budowania, np. 1.1.15.
Tworzy:
  sortowanie/_build.py         – stała VERSION w aplikacji (plik w .gitignore),
  build/version_info.txt       – zasób wersji Windows dla PyInstallera (Właściwości → Szczegóły).
Wypisuje wersję na standardowe wyjście.
"""

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

VERSION_INFO = """# Wygenerowano przez narzedzia/wersja.py
VSVersionInfo(
  ffi=FixedFileInfo(filevers=({a}, {b}, {c}, 0), prodvers=({a}, {b}, {c}, 0), mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041504B0', [
      StringStruct('CompanyName', 'SkyCatalog'),
      StringStruct('FileDescription', 'SkyCatalog – archiwizacja i mapa materiałów z lotów'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('InternalName', 'SkyCatalog'),
      StringStruct('LegalCopyright', 'SkyCatalog'),
      StringStruct('OriginalFilename', 'SkyCatalog.exe'),
      StringStruct('ProductName', 'SkyCatalog'),
      StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [0x0415, 1200])])
  ]
)
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", type=int, default=0, help="numer budowania (github.run_number)")
    args = ap.parse_args()
    base = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+", base):
        raise SystemExit(f"Plik VERSION musi mieć postać MAJOR.MINOR, a ma: {base!r}")
    major, minor = (int(x) for x in base.split("."))
    version = f"{major}.{minor}.{args.build}"
    (ROOT / "sortowanie" / "_build.py").write_text(
        f'"""Wygenerowano przez narzedzia/wersja.py – nie edytuj."""\nVERSION = "{version}"\n', encoding="utf-8")
    (ROOT / "build").mkdir(exist_ok=True)
    (ROOT / "build" / "version_info.txt").write_text(
        VERSION_INFO.format(a=major, b=minor, c=args.build, v=version), encoding="utf-8")
    print(version)


if __name__ == "__main__":
    main()
