"""SkyCatalog – archiwizacja i mapa materiałów z lotów (zakładki Sortowanie i Mapa materiałów)."""

from pathlib import Path

APP_NAME = "SkyCatalog"
EXE_NAME = "SkyCatalog.exe"
GITHUB_REPO = "skipperpvp-sudo/Sortowanie"
RELEASE_ASSET = "SkyCatalog-portable.zip"


def _version() -> str:
    """Wersja MAJOR.MINOR.BUILD.

    W wydaniach CI zapisuje ją do ``sortowanie/_build.py`` (numer budowania z GitHub Actions).
    Przy uruchomieniu ze źródeł: MAJOR.MINOR z pliku VERSION i numer budowania 0.
    """
    try:
        from ._build import VERSION  # type: ignore[import-not-found]
        return VERSION
    except ImportError:
        pass
    try:
        base = (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        base = "0.0"
    return f"{base}.0"


__version__ = _version()
