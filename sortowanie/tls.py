"""Weryfikacja certyfikatów HTTPS w wersji .exe (PyInstaller).

Python w buildzie PyInstaller nie zawsze ma dostęp do pełnego łańcucha certyfikatów
(błąd „SSLCertVerificationError: unable to get issuer certificate” przy api.github.com).

1. ``truststore`` – weryfikacja natywnie przez Windows (magazyn certyfikatów systemu,
   dociąganie brakujących certyfikatów pośrednich, certyfikaty antywirusów skanujących HTTPS).
   ``truststore.inject_into_ssl()`` jest wywoływane na samym początku programu, więc dotyczy
   każdego połączenia (aktualizacje, pobieranie ZIP z przekierowaniem, Overpass).
2. Awaryjnie – pakiet ``certifi`` (``cacert.pem`` dołączony do builda).
3. W ostateczności – domyślny kontekst Pythona.

Weryfikacja certyfikatów NIGDY nie jest wyłączana.
"""

from __future__ import annotations

import ssl

MODE_TRUSTSTORE = "truststore (magazyn certyfikatów systemu)"
MODE_CERTIFI = "certifi (cacert.pem)"
MODE_DEFAULT = "domyślny kontekst Pythona"

_mode: str | None = None
_error: str = ""


def setup() -> str:
    """Konfiguruje weryfikację TLS (raz, przed pierwszym połączeniem). Zwraca opis trybu."""
    global _mode, _error
    if _mode is not None:
        return _mode
    try:
        import truststore
        truststore.inject_into_ssl()
        _mode = MODE_TRUSTSTORE
        return _mode
    except Exception as exc:  # noqa: BLE001 – brak pakietu lub niezgodny system: tryb awaryjny
        _error = f"truststore niedostępny: {type(exc).__name__}: {exc}"
    try:
        import certifi
        certifi.where()
        _mode = MODE_CERTIFI
    except Exception as exc:  # noqa: BLE001
        _error += f"; certifi niedostępny: {type(exc).__name__}: {exc}"
        _mode = MODE_DEFAULT
    return _mode


def mode() -> str:
    return setup()


def problems() -> str:
    """Dlaczego nie użyto trybu podstawowego (do dziennika)."""
    setup()
    return _error


def context() -> ssl.SSLContext:
    """Kontekst SSL z weryfikacją certyfikatów i nazwy hosta (zawsze włączoną)."""
    current = setup()
    if current == MODE_CERTIFI:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    else:
        ctx = ssl.create_default_context()        # po inject_into_ssl() – kontekst truststore
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def urlopen(request, timeout: float):
    """``urllib.request.urlopen`` z kontekstem weryfikującym – także dla przekierowań
    (np. pobieranie wydania z objects.githubusercontent.com)."""
    import urllib.request
    return urllib.request.urlopen(request, timeout=timeout, context=context())
