"""Aktualizacja wersji przenośnej: wersje, odpowiedź API, rozpakowanie, skrypt (.cmd w Windows)."""
import io
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
import threading
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest import mock

from sortowanie import updater


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, headers=None):
        super().__init__(data)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


RELEASE = {"tag_name": "v1.1.15", "body": "## Zmiany\n* Import z karty", "html_url": "https://x/r",
           "draft": False, "prerelease": False,
           "assets": [{"name": "SkyCatalog-portable.zip", "browser_download_url": "https://x/z.zip", "size": 3}]}


class VersionTests(unittest.TestCase):
    def test_compare(self):
        self.assertTrue(updater.is_newer("v1.1.15", "1.1.9"))
        self.assertTrue(updater.is_newer("v1.2.0", "1.1.99"))
        self.assertFalse(updater.is_newer("v1.1.9", "1.1.15"))
        self.assertFalse(updater.is_newer("v1.1.15", "1.1.15"))
        self.assertFalse(updater.is_newer("nie-wersja", "1.0.0"))
        self.assertEqual(updater.parse_version("v2"), (2, 0, 0))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"LOCALAPPDATA": self._tmp.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def log(self):
        return updater.log_path().read_text(encoding="utf-8")

    def test_parse_latest(self):
        opener = lambda req, timeout: FakeResponse(json.dumps(RELEASE).encode())  # noqa: E731
        r = updater.check_latest(opener=opener)
        self.assertTrue(r.ok)
        self.assertEqual((r.release.version, r.release.asset_url, r.release.asset_size), ("1.1.15", "https://x/z.zip", 3))
        self.assertIn("Import z karty", r.release.notes)
        self.assertIn("OK", self.log())

    def test_request_headers_are_valid_http(self):
        """Nagłówki muszą dać się zakodować (latin-1) – polskie litery blokowały każde sprawdzenie."""
        seen = {}

        def opener(req, timeout):
            seen.update(req.header_items())
            for value in req.header_items():
                value[1].encode("latin-1")
            return FakeResponse(json.dumps(RELEASE).encode())
        self.assertTrue(updater.check_latest(opener=opener).ok)
        self.assertTrue(seen["User-agent"].startswith("SkyCatalog/"))
        updater._user_agent().encode("ascii")

    def test_network_error_is_described_and_logged(self):
        def opener(req, timeout):
            raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
        r = updater.check_latest(opener=opener)
        self.assertEqual(r.status, updater.STATUS_ERROR)
        self.assertTrue(r.error.startswith("SSLCertVerificationError:"), r.error)
        log = self.log()
        for text in ("URLError.reason:", "Traceback:", updater.API_URL, "Wersja programu:", "Typ wyjątku:"):
            self.assertIn(text, log)

    def test_http_error_shows_code_and_body(self):
        body = json.dumps({"message": "API rate limit exceeded"}).encode()

        def opener(req, timeout):
            raise urllib.error.HTTPError(updater.API_URL, 403, "Forbidden", {}, io.BytesIO(body))
        r = updater.check_latest(opener=opener)
        self.assertEqual(r.error, "HTTP 403: API rate limit exceeded")
        self.assertIn("Kod HTTP: 403", self.log())
        self.assertIn("rate limit", self.log())

    def test_any_exception_is_caught(self):
        def opener(req, timeout):
            raise RuntimeError("coś nieoczekiwanego")
        r = updater.check_latest(opener=opener)
        self.assertEqual(r.error, "RuntimeError: coś nieoczekiwanego")
        self.assertEqual(updater.check_latest(opener=lambda r, timeout: FakeResponse(b"<html>")).error[:15],
                         "JSONDecodeError")

    def test_invalid_release_has_own_message(self):
        def answer(data):
            return updater.check_latest(opener=lambda r, timeout: FakeResponse(json.dumps(data).encode()))
        r = answer({"message": "Not Found"})
        self.assertEqual(r.status, updater.STATUS_INVALID)
        self.assertIn("brak pola tag_name", r.error)
        self.assertIn("Not Found", r.error)
        r = answer({**RELEASE, "assets": []})
        self.assertEqual(r.status, updater.STATUS_INVALID)
        self.assertIn("SkyCatalog-portable.zip", r.error)
        self.assertEqual(r.release.tag, "v1.1.15")             # do linku „Strona wydań”
        self.assertIn("nieprawidłowy numer wersji", answer({**RELEASE, "tag_name": "najnowsza"}).error)
        self.assertIn("NIEPOPRAWNE", self.log())

    def test_log_is_rotated(self):
        updater.log_path().parent.mkdir(parents=True)
        updater.log_path().write_text("x" * (updater.LOG_MAX_BYTES + 10), encoding="utf-8")
        updater.check_latest(opener=lambda r, timeout: FakeResponse(json.dumps(RELEASE).encode()))
        self.assertTrue(updater.log_path().with_name("aktualizacja.log.1").exists())
        self.assertLess(updater.log_path().stat().st_size, 2000)

    def test_prerelease_and_missing_asset(self):
        self.assertIsNone(updater.parse_release({**RELEASE, "prerelease": True}))
        r = updater.parse_release({**RELEASE, "assets": []})
        self.assertIsNone(r.asset_url)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make_zip(self, entries: dict) -> Path:
        p = self.dir / "a.zip"
        with zipfile.ZipFile(p, "w") as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        return p

    def test_extract_finds_exe_folder(self):
        z = self.make_zip({"SkyCatalog/SkyCatalog.exe": b"exe", "SkyCatalog/_internal/x.dll": b"dll"})
        new = updater.extract(z, self.dir / "out")
        self.assertEqual(new.name, "SkyCatalog")
        self.assertTrue((new / "_internal" / "x.dll").exists())

    def test_zip_slip_rejected(self):
        z = self.make_zip({"../../zly.txt": b"x", "SkyCatalog/SkyCatalog.exe": b"e"})
        with self.assertRaises(updater.UpdateError):
            updater.extract(z, self.dir / "out")
        self.assertFalse((self.dir.parent / "zly.txt").exists())

    def test_download_size_check(self):
        rel = updater.parse_release(RELEASE)
        ok = updater.download(rel, self.dir / "d", opener=lambda r, timeout: FakeResponse(b"abc"))
        self.assertEqual(ok.read_bytes(), b"abc")
        with self.assertRaises(updater.UpdateError):
            updater.download(rel, self.dir / "d2", opener=lambda r, timeout: FakeResponse(b"abcdef"))
        # niepełny plik nie zostaje pod docelową nazwą ani jako .part
        self.assertEqual(list((self.dir / "d2").iterdir()), [])

    def test_download_checks_sha256_from_release(self):
        import hashlib
        good = hashlib.sha256(b"abc").hexdigest()
        rel = updater.parse_release({**RELEASE, "assets": [{**RELEASE["assets"][0], "digest": "sha256:" + good}]})
        self.assertEqual(rel.sha256, good)
        updater.download(rel, self.dir / "d", opener=lambda r, timeout: FakeResponse(b"abc"))
        rel.sha256 = "0" * 64
        with self.assertRaises(updater.UpdateError) as ctx:
            updater.download(rel, self.dir / "d2", opener=lambda r, timeout: FakeResponse(b"abc"))
        self.assertIn("SHA-256", str(ctx.exception))
        self.assertEqual(list((self.dir / "d2").iterdir()), [])

    def test_download_can_be_cancelled(self):
        rel = updater.parse_release(RELEASE)
        with self.assertRaises(updater.UpdateError):
            updater.download(rel, self.dir / "d", opener=lambda r, timeout: FakeResponse(b"abc"),
                             cancelled=lambda: True)
        self.assertEqual(list((self.dir / "d").iterdir()), [])

    def test_corrupted_zip_rejected(self):
        z = self.make_zip({"SkyCatalog/SkyCatalog.exe": b"e" * 1000})
        data = bytearray(z.read_bytes())
        data[40:60] = b"X" * 20                      # uszkodzona treść pliku (zły CRC)
        z.write_bytes(bytes(data))
        with self.assertRaises(updater.UpdateError):
            updater.extract(z, self.dir / "out")

    def test_verify_package(self):
        new = self.dir / "SkyCatalog"
        (new / "_internal").mkdir(parents=True)
        with self.assertRaises(updater.UpdateError):
            updater.verify_package(new, "1.5.0")       # brak .exe
        (new / "SkyCatalog.exe").write_bytes(b"MZ" + b"0" * 200_000)
        with self.assertRaises(updater.UpdateError):
            updater.verify_package(new, "1.5.0")       # puste _internal
        (new / "_internal" / "VERSION").write_text("1.4.0\n", encoding="utf-8")
        with self.assertRaises(updater.UpdateError):
            updater.verify_package(new, "1.5.0")       # paczka innej wersji
        (new / "_internal" / "VERSION").write_text("1.5.0\n", encoding="utf-8")
        updater.verify_package(new, "1.5.0")

    def test_helper_is_ascii_and_overwrites_same_size_files(self):
        text = updater.helper_source().read_text(encoding="ascii")
        self.assertIn('"/IS", "/IT"', text)
        for word in ("Wait-Process", "ProductVersion", "-Verb RunAs", "1223", "explorer.exe", "_backup",
                     "confirm_path", "CreateShortcut"):
            self.assertIn(word, text)

    def test_helper_command_quotes_paths(self):
        cmd = updater.helper_command(Path("C:/Temp/O'Brien & syn/aktualizator.ps1"), Path("C:/Temp/p.json"))
        self.assertEqual(cmd[1:4], ["-NoProfile", "-NonInteractive", "-WindowStyle"])
        self.assertIn("'C:/Temp/O''Brien & syn/aktualizator.ps1'", cmd[-1])
        self.assertTrue(cmd[-1].endswith("'main'"))

    def test_helper_params(self):
        prep = updater.PreparedUpdate(updater.parse_release(RELEASE), self.dir, self.dir / "a.zip", self.dir / "nowa")
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.dir / "local")}):
            script, params = updater.write_helper_files(prep, Path("C:/Programy/SkyCatalog"), [10, 11])
            data = json.loads(params.read_text(encoding="utf-8"))
        self.assertTrue(script.exists())
        self.assertEqual(data["pids"], [10, 11])
        self.assertEqual(data["target_version"], "1.1.15")
        self.assertIn("ustawienia.json", data["protected_files"])
        self.assertIn("_backup", data["protected_dirs"])
        self.assertTrue(data["log_path"].endswith("aktualizacja.log"))


class PendingTests(unittest.TestCase):
    """Po ponownym uruchomieniu: potwierdzenie wersji i wynik aktualizacji."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.dir / "local")})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def pending(self, target):
        data = {"target": target, "current": "1.0.0", "result": str(self.dir / "wynik.json"),
                "confirm": str(self.dir / "potwierdzenie.json")}
        updater.pending_path().parent.mkdir(parents=True, exist_ok=True)
        updater.pending_path().write_text(json.dumps(data), encoding="utf-8")

    def test_no_pending(self):
        self.assertIsNone(updater.confirm_started())

    def test_new_version_confirms_and_reports_success(self):
        self.pending(updater.__version__)
        p = updater.confirm_started()
        confirm = json.loads((self.dir / "potwierdzenie.json").read_text(encoding="utf-8"))
        self.assertEqual((confirm["version"], confirm["pid"]), (updater.__version__, os.getpid()))
        (self.dir / "wynik.json").write_text(json.dumps({"status": "ok", "skroty_nieaktualne": ["a.lnk -> C:/stary"]}),
                                             encoding="utf-8")
        out = updater.finish_pending(p, wait_s=1)
        self.assertTrue(out.ok)
        self.assertEqual(out.stale_shortcuts, ["a.lnk -> C:/stary"])
        self.assertFalse(updater.pending_path().exists())
        self.assertIn("Program uruchomiony po aktualizacji", updater.log_path().read_text(encoding="utf-8"))

    def test_old_version_after_failure_reports_reason(self):
        self.pending("99.0.0")
        p = updater.confirm_started()
        (self.dir / "wynik.json").write_text(json.dumps({"status": "uac_anulowano", "komunikat": "UAC"}),
                                             encoding="utf-8")
        out = updater.finish_pending(p, wait_s=1)
        self.assertFalse(out.ok)
        self.assertEqual(out.status, "uac_anulowano")


@unittest.skipUnless(sys.platform == "win32", "proces aktualizujący (PowerShell) – tylko Windows")
class WindowsHelperTests(unittest.TestCase):
    """Uruchamia prawdziwy proces aktualizujący (aktualizator.ps1) na folderach testowych.
    Program udaje kopia whoami.exe; potwierdzenie startu „nowej wersji” zapisuje wątek testu."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.app = self.root / "Moja aplikacja & O'Brien"        # spacja, & i apostrof w ścieżce
        self.work = self.root / "praca"
        self.new = self.work / "nowa_wersja" / "SkyCatalog"
        exe_src = Path(os.environ["SystemRoot"]) / "System32" / "whoami.exe"
        for d, ver in ((self.app, "v1"), (self.new, "v2")):
            (d / "_internal").mkdir(parents=True)
            (d / "SkyCatalog.exe").write_bytes(exe_src.read_bytes())
            (d / "_internal" / "lib.dll").write_text(ver, encoding="utf-8")
        # ten sam rozmiar i data, inna treść – robocopy bez /IS /IT pominąłby plik
        for d in (self.app, self.new):
            os.utime(d / "_internal" / "lib.dll", (1_700_000_000, 1_700_000_000))
        (self.app / "ustawienia.json").write_text("dane użytkownika", encoding="utf-8")
        (self.new / "ustawienia.json").write_text("NIE KOPIOWAĆ", encoding="utf-8")
        (self.app / "katalog.sqlite").write_text("baza", encoding="utf-8")
        self.version = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", f"(Get-Item -LiteralPath '{exe_src}').VersionInfo.ProductVersion"],
            capture_output=True, text=True).stdout.strip()
        self._env = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "local")})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def run_helper(self, target=None, confirm=True, pids=(), wait=5, confirm_s=8):
        rel = updater.Release(version=target or self.version, tag="v", notes="", asset_url=None, asset_size=0,
                              page_url="")
        prep = updater.PreparedUpdate(rel, self.work, self.work / "x.zip", self.new)
        script, params = updater.write_helper_files(prep, self.app, list(pids))
        data = json.loads(params.read_text(encoding="utf-8"))
        data.update(wait_seconds=wait, confirm_seconds=confirm_s)
        params.write_text(json.dumps(data), encoding="utf-8")
        stop = threading.Event()

        def confirm_when_started():                 # udaje nową wersję potwierdzającą start
            log = updater.log_path()
            while not stop.is_set():
                if log.exists() and "Uruchomiono nowa wersje" in log.read_text(encoding="utf-8", errors="replace"):
                    Path(data["confirm_path"]).write_text(json.dumps(
                        {"version": data["target_version"], "pid": 1, "exe": "x"}), encoding="utf-8")
                    return
                time.sleep(0.2)
        if confirm:
            threading.Thread(target=confirm_when_started, daemon=True).start()
        res = subprocess.run(updater.helper_command(script, params), capture_output=True, timeout=240)
        stop.set()
        result = json.loads(Path(data["result_path"]).read_text(encoding="utf-8-sig"))
        return res.returncode, result, updater.log_path().read_text(encoding="utf-8", errors="replace")

    def lib(self):
        return (self.app / "_internal" / "lib.dll").read_text()

    def test_successful_update_keeps_user_data_and_backup(self):
        code, result, log = self.run_helper()
        self.assertEqual((code, result["status"]), (0, "ok"), log)
        self.assertEqual(self.lib(), "v2", log)
        self.assertEqual((self.app / "ustawienia.json").read_text(encoding="utf-8"), "dane użytkownika")
        self.assertEqual((self.app / "katalog.sqlite").read_text(), "baza")
        self.assertEqual((self.app / "_backup" / "_internal" / "lib.dll").read_text(), "v1")
        self.assertTrue((self.app / "_backup" / "PRZYWROC_POPRZEDNIA_WERSJE.cmd").exists())
        self.assertFalse((self.app / "_backup" / "ustawienia.json").exists())
        for stage in ("Start aktualizacji", "Program zamkniety", "Wersja w metadanych", "Uruchomiono nowa wersje",
                      "Potwierdzenie: uruchomiony program raportuje wersje", "zakonczona powodzeniem"):
            self.assertIn(stage, log)

    def test_waits_for_running_process(self):
        blocker = subprocess.Popen(["ping", "-n", "4", "127.0.0.1"], stdout=subprocess.DEVNULL)
        code, result, log = self.run_helper(pids=[blocker.pid], wait=30)
        blocker.wait()
        self.assertEqual(result["status"], "ok", log)
        self.assertEqual(self.lib(), "v2")

    def test_locked_by_another_process_changes_nothing(self):
        ping = Path(os.environ["SystemRoot"]) / "System32" / "PING.EXE"
        copy = self.app / "_internal" / "pomocnik.exe"
        copy.write_bytes(ping.read_bytes())
        blocker = subprocess.Popen([str(copy), "-n", "30", "127.0.0.1"], stdout=subprocess.DEVNULL)
        try:
            code, result, log = self.run_helper(wait=3)
        finally:
            blocker.kill()
            blocker.wait()
        self.assertEqual(result["status"], "pliki_zablokowane", log)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.lib(), "v1")
        self.assertFalse((self.app / "_backup").exists())

    def test_copy_error_restores_previous_version(self):
        (self.app / "konflikt").write_text("plik", encoding="utf-8")
        (self.new / "konflikt").mkdir()                 # w nowej wersji folder o tej nazwie
        (self.new / "konflikt" / "x.txt").write_text("x", encoding="utf-8")
        code, result, log = self.run_helper()
        self.assertEqual(result["status"], "blad_instalacji", log)
        self.assertEqual(self.lib(), "v1", log)
        self.assertIn("Uruchamianie poprzedniej wersji", log)

    def test_wrong_version_in_package_is_rolled_back(self):
        code, result, log = self.run_helper(target="99.9.9")
        self.assertEqual(result["status"], "zla_wersja", log)
        self.assertEqual(self.lib(), "v1", log)

    def test_new_version_not_starting_is_rolled_back(self):
        code, result, log = self.run_helper(confirm=False, confirm_s=6)   # whoami kończy się bez potwierdzenia
        self.assertEqual(result["status"], "nowa_wersja_nie_dziala", log)
        self.assertEqual(self.lib(), "v1", log)
        self.assertIn("Uruchamianie poprzedniej wersji", log)


if __name__ == "__main__":
    unittest.main()
