"""Ustala wersję wydania i zapisuje ją do aplikacji oraz metadanych pliku .exe.

Użycie (CI):  python narzedzia/wersja.py --z-tagow
Wersja = MAJOR.MINOR z pliku VERSION + kolejny numer poprawki: o 1 większy od najwyższego
istniejącego tagu vMAJOR.MINOR.N (pierwsze wydanie nowej wersji MAJOR.MINOR ma numer 1),
np. VERSION = 1.2 → 1.2.1, 1.2.2, 1.2.3…
Pełny numer w pliku VERSION (MAJOR.MINOR.PATCH, np. 1.3.0) jest używany dokładnie – tak ustawia
się wersję wybraną przez użytkownika (SemVer, patrz CLAUDE.md).
Lokalnie:     python narzedzia/wersja.py --build 0   (wersja testowa MAJOR.MINOR.0)
Tworzy:
  sortowanie/_build.py         – stała VERSION w aplikacji (plik w .gitignore),
  build/version_info.txt       – zasób wersji Windows dla PyInstallera (Właściwości → Szczegóły).
Wypisuje wersję na standardowe wyjście.
"""

import argparse
import re
import subprocess
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


def next_patch(major: int, minor: int, tags: list[str]) -> int:
    """Kolejny numer poprawki dla MAJOR.MINOR na podstawie istniejących tagów vX.Y.Z."""
    pattern = re.compile(rf"^v{major}\.{minor}\.(\d+)$")
    used = [int(m.group(1)) for t in tags if (m := pattern.match(t.strip()))]
    return max(used, default=0) + 1


def git_tags() -> list[str]:
    out = subprocess.run(["git", "tag", "--list", "v*"], cwd=ROOT, capture_output=True, text=True, check=True)
    return out.stdout.split()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", type=int, default=0, help="numer poprawki podany ręcznie (lokalnie: 0)")
    ap.add_argument("--z-tagow", action="store_true", help="kolejny numer poprawki z tagów git (CI)")
    args = ap.parse_args()
    base = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if re.fullmatch(r"\d+\.\d+\.\d+", base):
        major, minor, patch = (int(x) for x in base.split("."))     # wersja wybrana ręcznie
    elif re.fullmatch(r"\d+\.\d+", base):
        major, minor = (int(x) for x in base.split("."))
        patch = next_patch(major, minor, git_tags()) if args.z_tagow else args.build
    else:
        raise SystemExit(f"Plik VERSION musi mieć postać MAJOR.MINOR lub MAJOR.MINOR.PATCH, a ma: {base!r}")
    version = f"{major}.{minor}.{patch}"
    (ROOT / "sortowanie" / "_build.py").write_text(
        f'"""Wygenerowano przez narzedzia/wersja.py – nie edytuj."""\nVERSION = "{version}"\n', encoding="utf-8")
    (ROOT / "build").mkdir(exist_ok=True)
    (ROOT / "build" / "version_info.txt").write_text(
        VERSION_INFO.format(a=major, b=minor, c=patch, v=version), encoding="utf-8")
    print(version)


if __name__ == "__main__":
    main()
