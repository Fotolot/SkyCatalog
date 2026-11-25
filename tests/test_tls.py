"""Weryfikacja certyfikatów HTTPS: truststore, awaryjnie certifi, nigdy bez weryfikacji."""
import ssl
import sys
import unittest
from pathlib import Path
from unittest import mock

from sortowanie import overpass, tls, updater

ROOT = Path(__file__).resolve().parent.parent


class TlsTests(unittest.TestCase):
    def setUp(self):
        self._saved = (tls._mode, tls._error)
        tls._mode, tls._error = None, ""

    def tearDown(self):
        tls._mode, tls._error = self._saved

    def test_truststore_is_preferred(self):
        try:
            import truststore  # noqa: F401
        except ImportError:
            self.skipTest("truststore niezainstalowany")
        fake = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"truststore": fake}):
            self.assertEqual(tls.setup(), tls.MODE_TRUSTSTORE)
        fake.inject_into_ssl.assert_called_once()
        self.assertEqual(tls.setup(), tls.MODE_TRUSTSTORE)          # tylko raz
        fake.inject_into_ssl.assert_called_once()

    def test_falls_back_to_certifi(self):
        with mock.patch.dict(sys.modules, {"truststore": None}):     # import się nie uda
            self.assertEqual(tls.setup(), tls.MODE_CERTIFI)
            self.assertIn("truststore niedostępny", tls.problems())
            import certifi
            with mock.patch("ssl.create_default_context", wraps=ssl.create_default_context) as create:
                ctx = tls.context()
            create.assert_called_once_with(cafile=certifi.where())
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_last_resort_still_verifies(self):
        with mock.patch.dict(sys.modules, {"truststore": None, "certifi": None}):
            self.assertEqual(tls.setup(), tls.MODE_DEFAULT)
            ctx = tls.context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_all_connections_use_verified_context(self):
        self.assertIs(updater.check_latest.__defaults__[1], tls.urlopen)
        self.assertIs(updater.download.__defaults__[-1], tls.urlopen)
        with mock.patch("urllib.request.urlopen") as urlopen:
            tls.urlopen("https://example.invalid", timeout=3)
        self.assertIsInstance(urlopen.call_args.kwargs["context"], ssl.SSLContext)
        self.assertIn("tls.urlopen", Path(overpass.__file__).read_text(encoding="utf-8"))

    def test_verification_never_disabled(self):
        for path in (ROOT / "sortowanie").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for bad in ("CERT_NONE", "verify=False", "_create_unverified_context", "check_hostname = False"):
                self.assertNotIn(bad, text, f"{path.name}: {bad}")

    def test_app_sets_up_tls_before_ui(self):
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertLess(main.index("tls.setup()"), main.index("from sortowanie.ui.app import main"))


if __name__ == "__main__":
    unittest.main()
