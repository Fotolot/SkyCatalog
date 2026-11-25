# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: buduje folder dist\Sortowanie\ z plikiem Sortowanie.exe (bez okna konsoli).
# Tryb folderu (onedir) zamiast jednego pliku: QtWebEngine (mapa) ma ok. 200 MB i w trybie
# jednego pliku byłby rozpakowywany przy każdym uruchomieniu.
# Użycie:  pyinstaller --clean --noconfirm sortowanie.spec

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("sortowanie/web", "sortowanie/web")],      # Leaflet i strona mapy (lokalnie)
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
    name="Sortowanie",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    # icon="assets\\ikona.ico",
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Sortowanie")
