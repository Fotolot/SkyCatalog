"""Punkt startowy aplikacji (używany też przez PyInstaller)."""

import sys

# Weryfikacja certyfikatów HTTPS przez Windows (truststore) – przed jakimkolwiek połączeniem
from sortowanie import tls

tls.setup()

from sortowanie.ui.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
