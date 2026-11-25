# -*- mode: python ; coding: utf-8 -*-
# Konfiguracja PyInstaller: buduje jeden plik dist\Sortowanie.exe bez okna konsoli.
# Użycie:  pyinstaller --clean --noconfirm sortowanie.spec

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    excludes=["pydoc", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Sortowanie",
    debug=False,
    strip=False,
    upx=False,
    console=False,  # aplikacja okienkowa – bez czarnego okna konsoli
    # icon="assets\\ikona.ico",  # odkomentuj po dodaniu własnej ikony
)
