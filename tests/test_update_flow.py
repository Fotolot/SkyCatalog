"""Przebieg aktualizacji w oknie programu (Qt, bez sieci): po pobraniu musi pojawić się
„Aktualizacja gotowa”, a „Zainstaluj i uruchom ponownie” – wywołać instalację.

Regresja: QProgressDialog przy zamknięciu wysyła „canceled”; wcześniej zamknięcie okna postępu po
pobraniu było traktowane jak anulowanie i aktualizacja po cichu się zatrzymywała."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QWidget

    from sortowanie import updater
    from sortowanie.ui import update_dialog as ud
except ImportError:  # pragma: no cover
    QApplication = None

RELEASE = None if QApplication is None else updater.Release(
    version="9.9.9", tag="v9.9.9", notes="", asset_url="file:///x.zip", asset_size=3, page_url="")


@unittest.skipIf(QApplication is None, "PySide6 niedostępny")
class UpdateFlowTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.parent = QWidget()
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self._tmp.name})
        self._env.start()
        self.prepared = updater.PreparedUpdate(RELEASE, Path(self._tmp.name), Path("x.zip"), Path("nowa"))

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()
        self.parent.deleteLater()

    def run_flow(self, prepare, ready_answer: int):
        installed, shown = [], []

        def fake_exec(dlg):
            shown.append(type(dlg).__name__)
            return ready_answer
        with mock.patch.object(updater, "is_frozen", return_value=True), \
                mock.patch.object(updater, "prepare_update", side_effect=prepare), \
                mock.patch.object(ud.ReadyDialog, "exec", fake_exec), \
                mock.patch.object(ud, "_show_error", lambda *a: shown.append("błąd")):
            ud.run_update(self.parent, RELEASE, installed.append)
            end = time.monotonic() + 5
            while not shown and time.monotonic() < end:
                self.app.processEvents()
                time.sleep(0.01)
        return installed, shown

    def test_ready_dialog_then_install(self):
        def prepare(release, progress, cancelled):
            progress(3, 3)                       # 100% pobrania
            return self.prepared
        installed, shown = self.run_flow(prepare, 1)
        self.assertEqual(shown, ["ReadyDialog"])
        self.assertEqual(installed, [self.prepared])

    def test_later_does_not_install(self):
        installed, shown = self.run_flow(lambda release, progress, cancelled: self.prepared, 0)
        self.assertEqual(shown, ["ReadyDialog"])
        self.assertEqual(installed, [])

    def test_download_error_shows_reason_and_keeps_running(self):
        def prepare(release, progress, cancelled):
            raise updater.UpdateError("Suma kontrolna SHA-256 nie zgadza się")
        installed, shown = self.run_flow(prepare, 1)
        self.assertEqual(shown, ["błąd"])
        self.assertEqual(installed, [])
        self.assertIn("SHA-256", updater.log_path().read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
