# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: buduje przenośny folder dist\SkyCatalog\ z plikiem SkyCatalog.exe (bez okna konsoli).
# Nazwa jest stała (bez numeru wersji) – aktualizacja podmienia pliki w tym samym folderze.
# Tryb folderu (onedir): QtWebEngine (mapa) jest duży i w trybie jednego pliku byłby rozpakowywany
# przy każdym uruchomieniu.
# Wersja: python narzedzia/wersja.py --build N  (tworzy build/version_info.txt i sortowanie/_build.py)
# Użycie:  pyinstaller --clean --noconfirm sortowanie.spec

import pathlib
import re

from PyInstaller.utils.hooks import collect_data_files

ROOT = pathlib.Path(SPECPATH)
NAME = "SkyCatalog"
VERSION_FILE = ROOT / "build" / "version_info.txt"

# ── Części Qt, których program nie używa (mniejszy rozmiar) ──
# Program korzysta z: Widgets, WebEngine (mapa; wymaga Quick/Qml jako bibliotek), WebChannel,
# Multimedia (odtwarzacz), Network, PrintSupport, Positioning. Moduły QML (folder qml), Quick 3D,
# kontrolki Quick, wykresy, PDF itp. nie są potrzebne. Tłumaczenia: tylko polski i angielski.
EXCLUDE = [re.compile(p, re.I) for p in (
    r"Qt6?Quick3D", r"QuickControls2", r"QuickDialogs2", r"QuickTemplates2", r"QuickLayouts",
    r"QuickParticles", r"QuickShapes", r"QuickTimeline", r"QuickVectorImage", r"QuickEffects",
    r"Qt6?Graphs", r"Qt6?Labs", r"ShaderTools", r"Qt6?Charts", r"DataVisualization", r"VirtualKeyboard",
    r"Qt6?Pdf", r"Qt6?Location", r"Qt6?3D", r"Scxml", r"StateMachine", r"RemoteObjects", r"Qt6?Sensors",
    r"SerialPort", r"SerialBus", r"TextToSpeech", r"Bluetooth", r"Qt6?Nfc", r"Designer", r"Qt6?Help",
    r"UiTools", r"Qt6?Test", r"Qt6?Sql", r"SpatialAudio", r"HttpServer", r"WebSockets", r"Qt6?Svg",
    r"QtOpenGL\.(pyd|abi3)", r"QtQuick\.(pyd|abi3)", r"QtQml\.(pyd|abi3)",
    r"(^|/)qml/",                                            # moduły QML (aplikacja ich nie ładuje)
    r"qtwebengine_devtools_resources",                        # narzędzia deweloperskie przeglądarki
    r"qtwebengine_locales/(?!(pl|en-US)\.pak$)[^/]+$",        # tylko polski i angielski
    r"translations/(?![^/]*_(pl|en)\.qm$)[^/]+\.qm$",
)]


def keep(entry) -> bool:
    dest = str(entry[0]).replace("\\", "/")
    return not any(p.search(dest) for p in EXCLUDE)


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("sortowanie/web", "sortowanie/web"),        # Leaflet i strona mapy (lokalnie)
           ("sortowanie/assets", "sortowanie/assets"),  # ikona okna
           ("VERSION", ".")]
          + collect_data_files("certifi"),               # cacert.pem – awaryjna weryfikacja HTTPS
    hiddenimports=["truststore", "certifi",              # weryfikacja certyfikatów HTTPS (tls.py)
                   "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
                   "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets"],
    excludes=["tkinter", "PIL", "pydoc", "PySide6.QtOpenGL", "PySide6.QtQuick3D", "PySide6.QtCharts",
              "PySide6.QtPdf", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner"],
    noarchive=False,
)
before = len(a.binaries) + len(a.datas)
a.binaries = [e for e in a.binaries if keep(e)]
a.datas = [e for e in a.datas if keep(e)]
print(f"[SkyCatalog] pominieto {before - len(a.binaries) - len(a.datas)} nieuzywanych plikow Qt")  # ASCII: konsola Windows (cp1252)
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
    version=str(VERSION_FILE) if VERSION_FILE.exists() else None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)
