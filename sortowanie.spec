# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: buduje folder dist\SkyCatalog_1.02\ z plikiem SkyCatalog_1.02.exe (bez okna konsoli).
# Tryb folderu (onedir) zamiast jednego pliku: QtWebEngine (mapa) ma ok. 200 MB i w trybie
# jednego pliku byłby rozpakowywany przy każdym uruchomieniu.
# Użycie:  pyinstaller --clean --noconfirm sortowanie.spec

import importlib.util, pathlib
_spec = importlib.util.spec_from_file_location("ver", pathlib.Path(SPECPATH) / "sortowanie" / "__init__.py")
_ver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ver)
NAME = f"{_ver.APP_NAME}_{_ver.__version__}"          # np. SkyCatalog_1.02

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("sortowanie/web", "sortowanie/web"),        # Leaflet i strona mapy (lokalnie)
           ("sortowanie/assets", "sortowanie/assets")],  # ikona okna
    hiddenimports=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
                   "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets"],
    excludes=["tkinter", "PIL", "pydoc"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon="assets/SkyCatalog.ico",
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)
