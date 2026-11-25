"""Leaflet w QWebEngineView z bezpiecznym mostem QWebChannel.

* Zasoby strony (map.html, map.js, Leaflet, qwebchannel.js) serwuje własny schemat
  ``fotolot://`` – wyłącznie pliki z białej listy z pakietu aplikacji. Brak lokalnego
  serwera HTTP i brak dostępu strony do plików na dysku (``file://`` wyłączone).
* Strona nie może nawigować ani otwierać okien; odnośniki z oznaczenia źródła danych
  otwierają się w przeglądarce systemowej.
* Kafelki pobiera silnik Chromium z własnym profilem: trwały cache HTTP (zgodny
  z nagłówkami, zapytania warunkowe) i User-Agent identyfikujący aplikację.
* Komunikacja: tylko JSON przez sygnał ``toJs`` i slot ``fromJs``.
"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QCoreApplication, QFile, QIODevice, QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile, QWebEngineSettings,
                                     QWebEngineUrlRequestJob, QWebEngineUrlScheme,
                                     QWebEngineUrlSchemeHandler)
from PySide6.QtWebEngineWidgets import QWebEngineView

from ..config import local_data_dir
from ..tiles import USER_AGENT

SCHEME = b"fotolot"
HOST = "mapa"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
ALLOWED = {
    "/map.html": "map.html", "/map.js": "map.js", "/map.css": "map.css",
    "/leaflet/leaflet.js": "leaflet/leaflet.js", "/leaflet/leaflet.css": "leaflet/leaflet.css",
}
ALLOWED_IMAGES = {"layers.png", "layers-2x.png", "marker-icon.png", "marker-icon-2x.png", "marker-shadow.png"}


def register_scheme() -> None:
    """Musi zostać wywołane PRZED utworzeniem QApplication."""
    scheme = QWebEngineUrlScheme(SCHEME)
    scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
    scheme.setFlags(QWebEngineUrlScheme.Flag.SecureScheme)
    QWebEngineUrlScheme.registerScheme(scheme)


def resolve_asset(path: str) -> bytes | None:
    """Treść zasobu z białej listy albo None (każda inna ścieżka jest odrzucana)."""
    if path == "/qwebchannel.js":
        f = QFile(":/qtwebchannel/qwebchannel.js")
        if f.open(QIODevice.OpenModeFlag.ReadOnly):
            return bytes(f.readAll())
        return None
    rel = ALLOWED.get(path)
    if rel is None and path.startswith("/leaflet/images/") and path.rsplit("/", 1)[-1] in ALLOWED_IMAGES:
        rel = "leaflet/images/" + path.rsplit("/", 1)[-1]
    if rel is None:
        return None
    try:
        return (WEB_DIR / rel).read_bytes()
    except OSError:
        return None


class AssetHandler(QWebEngineUrlSchemeHandler):
    def requestStarted(self, job: QWebEngineUrlRequestJob) -> None:  # noqa: N802 (API Qt)
        url = job.requestUrl()
        if url.host() != HOST:
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        data = resolve_asset(url.path())
        if data is None:
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        mime = mimetypes.guess_type(url.path())[0] or "application/octet-stream"
        if url.path().endswith(".js"):
            mime = "text/javascript"
        buf = QBuffer(parent=job)
        buf.setData(QByteArray(data))
        buf.open(QIODevice.OpenModeFlag.ReadOnly)
        job.reply(mime.encode("ascii"), buf)


_profile: QWebEngineProfile | None = None
_handler: AssetHandler | None = None


def map_profile(parent: QObject | None = None) -> QWebEngineProfile:
    """Wspólny profil mapy: trwały cache HTTP kafelków i User-Agent aplikacji."""
    global _profile, _handler
    if _profile is None:
        storage = local_data_dir() / "przegladarka"
        # rodzicem jest aplikacja – profil musi żyć dłużej niż strony mapy
        _profile = QWebEngineProfile("Sortowanie-mapa", QCoreApplication.instance())
        _profile.setPersistentStoragePath(str(storage / "dane"))
        _profile.setCachePath(str(storage / "cache"))
        _profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
        _profile.setHttpCacheMaximumSize(512 * 1024 * 1024)
        _profile.setHttpUserAgent(USER_AGENT)
        _handler = AssetHandler(_profile)
        _profile.installUrlSchemeHandler(SCHEME, _handler)
    return _profile


class Bridge(QObject):
    toJs = Signal(str)
    message = Signal(dict)

    @Slot(str)
    def fromJs(self, text: str) -> None:  # noqa: N802 (nazwa widoczna w JS)
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return
        if isinstance(data, dict) and isinstance(data.get("type"), str):
            self.message.emit(data)


class MapPage(QWebEnginePage):
    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:  # noqa: N802
        if url.scheme() == SCHEME.decode() and url.host() == HOST:
            return True
        if url.scheme() == "https" and nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            QDesktopServices.openUrl(url)          # oznaczenie źródła, „Zgłoś błąd mapy”
        return False

    def createWindow(self, _type):  # noqa: N802 – nowe okna zablokowane
        return None

    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            print(f"[mapa] {source}:{line}: {message}")


class MapWidget(QWebEngineView):
    """Widok mapy. Wiadomości z JS: sygnał ``message`` (dict). Do JS: ``send()``."""

    message = Signal(dict)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        profile = map_profile(self.window())
        page = MapPage(profile, self)
        s = page.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        s.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, False)
        self.bridge = Bridge(self)
        self.bridge.message.connect(self._on_message)
        channel = QWebChannel(page)
        channel.registerObject("bridge", self.bridge)
        page.setWebChannel(channel)
        self.setPage(page)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self._ready = False
        self._queue: list[str] = []
        self.load(QUrl(f"{SCHEME.decode()}://{HOST}/map.html"))

    @property
    def ready(self) -> bool:
        return self._ready

    def _on_message(self, data: dict) -> None:
        if data.get("type") == "ready":
            self._ready = True
        self.message.emit(data)

    def send(self, type_: str, **payload) -> None:
        text = json.dumps({"type": type_, **payload}, separators=(",", ":"), ensure_ascii=False)
        if self._ready or type_ == "init":
            self.bridge.toJs.emit(text)
        else:
            self._queue.append(text)

    def flush(self) -> None:
        for text in self._queue:
            self.bridge.toJs.emit(text)
        self._queue.clear()
