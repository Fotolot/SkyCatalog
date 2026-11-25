"""Klient Overpass API zgodny z zasadami publicznej instancji.

Zasady (https://dev.overpass-api.de/overpass-doc/en/preface/commons.html):
- publiczna instancja: orientacyjnie do ~10 000 zapytań i ~1 GB danych dziennie,
- HTTP 429 – brak wolnego slotu (rate limit); ``/api/status`` podaje, kiedy slot się zwolni,
- HTTP 504 – serwer nie ma zasobów na zapytanie o zadeklarowanym [timeout]/[maxsize];
  mniejsze deklaracje zwiększają szansę przyjęcia,
- przy większym użyciu należy uruchomić własną instancję – endpoint jest konfigurowalny
  (ustawienia aplikacji lub zmienna środowiskowa ``SORTOWANIE_OVERPASS_URL``).

Zapytania wykonywane są sekwencyjnie (jedno naraz) z minimalnym odstępem.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from . import __version__

DEFAULT_ENDPOINT = "https://overpass-api.de/api/interpreter"
ENV_ENDPOINT = "SORTOWANIE_OVERPASS_URL"

Opener = Callable[[urllib.request.Request, float], tuple[int, bytes]]
StatusCallback = Callable[[str], None]


class OverpassUnavailable(Exception):
    """Serwer nieosiągalny (brak internetu, DNS, zerwane połączenie)."""


class OverpassError(Exception):
    """Serwer odpowiedział błędem, którego nie da się naprawić ponowieniem."""


def _default_opener(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if exc.fp else b""


_SLOT_IN = re.compile(r"in\s+(-?\d+)\s+seconds", re.I)
_SLOTS_FREE = re.compile(r"(\d+)\s+slots?\s+available\s+now", re.I)


def parse_status_wait(text: str) -> float | None:
    """Liczba sekund do zwolnienia slotu na podstawie odpowiedzi ``/api/status``.

    0 – slot dostępny teraz, None – nie udało się odczytać.
    """
    m = _SLOTS_FREE.search(text)
    if m and int(m.group(1)) > 0:
        return 0.0
    waits = [int(x) for x in _SLOT_IN.findall(text)]
    if waits:
        return float(max(0, min(waits)))
    return None


def status_url(endpoint: str) -> str | None:
    """``…/api/interpreter`` → ``…/api/status`` (własne serwery mogą go nie mieć)."""
    if endpoint.rstrip("/").endswith("/interpreter"):
        return endpoint.rstrip("/")[: -len("interpreter")] + "status"
    return None


class OverpassClient:
    MIN_INTERVAL = 1.0          # s między kolejnymi zapytaniami
    MAX_ATTEMPTS = 5
    BACKOFF_504 = (15, 30, 60, 120)
    BACKOFF_429 = (10, 20, 40, 60)
    HTTP_TIMEOUT = 90           # s – dłuższy niż [timeout] zapytania + 15 s kolejki serwera

    def __init__(self, endpoint_getter: Callable[[], str], contact_getter: Callable[[], str] | None = None,
                 opener: Opener | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self._endpoint_getter = endpoint_getter
        self._contact_getter = contact_getter or (lambda: "")
        self.opener = opener or _default_opener
        self.sleep = sleep
        self._lock = threading.Lock()
        self._last = 0.0
        self.requests_made = 0
        self.bytes_received = 0
        self.on_status: StatusCallback | None = None

    @property
    def endpoint(self) -> str:
        env = os.environ.get(ENV_ENDPOINT, "").strip()
        return env or (self._endpoint_getter() or DEFAULT_ENDPOINT).strip()

    def user_agent(self) -> str:
        ua = f"SkyCatalog/{__version__} (aplikacja desktopowa do archiwizacji nagran)"
        contact = (self._contact_getter() or "").strip()
        return f"{ua} {contact}" if contact else ua

    def _notify(self, text: str) -> None:
        if self.on_status:
            try:
                self.on_status(text)
            except Exception:
                pass

    def _throttle(self) -> None:
        wait = self._last + self.MIN_INTERVAL - time.monotonic()
        if wait > 0:
            self.sleep(wait)
        self._last = time.monotonic()

    def _status_wait(self) -> float | None:
        url = status_url(self.endpoint)
        if not url:
            return None
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent()})
        try:
            code, body = self.opener(req, 20)
        except (urllib.error.URLError, OSError):
            return None
        if code != 200:
            return None
        return parse_status_wait(body.decode("utf-8", "replace"))

    def query(self, ql: str, description: str = "") -> dict:
        """Wysyła zapytanie Overpass QL (POST). Ponawia przy 429/504 po odpowiedniej przerwie."""
        with self._lock:  # jedno zapytanie naraz
            last_error = ""
            for attempt in range(1, self.MAX_ATTEMPTS + 1):
                self._throttle()
                req = urllib.request.Request(
                    self.endpoint, data=urllib.parse.urlencode({"data": ql}).encode("utf-8"),
                    headers={"User-Agent": self.user_agent(), "Accept": "application/json",
                             "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
                self._notify(f"Overpass: {description or 'zapytanie'}…")
                try:
                    code, body = self.opener(req, self.HTTP_TIMEOUT)
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    raise OverpassUnavailable(str(getattr(exc, "reason", exc))) from exc
                self.requests_made += 1
                self.bytes_received += len(body)

                if code == 200:
                    try:
                        data = json.loads(body.decode("utf-8"))
                    except ValueError as exc:
                        raise OverpassError("Nieprawidłowa odpowiedź JSON z Overpass") from exc
                    remark = str(data.get("remark") or "")
                    if "runtime error" in remark.lower() or "out of memory" in remark.lower():
                        # Przekroczony limit czasu/pamięci po stronie serwera – jak 504
                        last_error = remark
                        wait = self.BACKOFF_504[min(attempt - 1, len(self.BACKOFF_504) - 1)]
                        self._notify(f"Overpass: serwer przerwał zapytanie ({remark[:80]}) – ponowienie za {wait} s")
                        self.sleep(wait)
                        continue
                    return data
                if code == 429:
                    wait = self._status_wait()
                    if wait is None:
                        wait = self.BACKOFF_429[min(attempt - 1, len(self.BACKOFF_429) - 1)]
                    wait = max(wait + 1, 2)
                    last_error = "HTTP 429 (limit zapytań)"
                    self._notify(f"Overpass: limit zapytań (429) – ponowienie za {wait:.0f} s")
                    self.sleep(wait)
                    continue
                if code in (502, 503, 504):
                    wait = self.BACKOFF_504[min(attempt - 1, len(self.BACKOFF_504) - 1)]
                    last_error = f"HTTP {code} (serwer przeciążony)"
                    self._notify(f"Overpass: serwer przeciążony ({code}) – ponowienie za {wait} s")
                    self.sleep(wait)
                    continue
                text = body.decode("utf-8", "replace")
                detail = re.sub(r"<[^>]+>", " ", text)
                detail = re.sub(r"\s+", " ", detail).strip()[:300]
                raise OverpassError(f"HTTP {code}: {detail}")
            raise OverpassError(f"Overpass nie odpowiedział po {self.MAX_ATTEMPTS} próbach: {last_error}")
