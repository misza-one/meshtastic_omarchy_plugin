"""Catalog validation and isolated native localization regressions.

Run: ./bin/python -m unittest discover -s tests -p 'test_i18n.py' -v
Native live-binding coverage requires installed Omarchy and Quickshell; portable
catalog and notification tests need only Python's standard library.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CATALOGS = ROOT / "plugin/i18n"
COMMONS = Path("/usr/share/omarchy/shell/Commons")
SPEC = importlib.util.spec_from_file_location("i18n_bridge", ROOT / "plugin/bridge.py")
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate catalog key: {key}")
        result[key] = value
    return result


class CatalogTests(unittest.TestCase):
    def test_catalogs_are_complete_and_have_matching_placeholders(self):
        catalogs = {code: json.loads((CATALOGS / f"{code}.json").read_text(encoding="utf-8"),
                                     object_pairs_hook=unique_object)
                    for code in ("en", "pl", "de")}
        for code, catalog in catalogs.items():
            with self.subTest(language=code):
                self.assertEqual(set(catalog), set(catalogs["en"]))
                for key, value in catalog.items():
                    source = catalogs["en"][key]
                    self.assertEqual(isinstance(value, dict), isinstance(source, dict), key)
                    forms = value.values() if isinstance(value, dict) else [value]
                    original = source["other"] if isinstance(source, dict) else source
                    if isinstance(value, dict):
                        required = {"one", "few", "many", "other"} if code == "pl" else {"one", "other"}
                        self.assertEqual(set(value), required, key)
                    for text in forms:
                        self.assertIsInstance(text, str, key)
                        self.assertTrue(text.strip(), key)
                        self.assertEqual(sorted(re.findall(r"%[1-9][0-9]*", text)),
                                         sorted(re.findall(r"%[1-9][0-9]*", original)), key)
                        self.assertNotRegex(text, r"[\x00-\x08\x0b\x0c\x0e-\x1f]", key)


class NotificationLanguageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.language = self.home / "chosen-language"
        patch = mock.patch.dict(os.environ, {"HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / "config"), "OMARCHY_LANGUAGE_FILE": str(self.language)}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def action(self):
        return next(arg for arg in bridge.notification_args("sender", "channel", "text")
                    if arg.startswith("--action="))

    def test_live_language_and_regional_base_and_english_fallback(self):
        for code, label in [("en", "Open Meshtastic"), ("pl", "Otwórz Meshtastic"),
                            ("de", "Meshtastic öffnen"), ("de-AT", "Meshtastic öffnen"),
                            ("pl-PL", "Otwórz Meshtastic"), ("fr", "Open Meshtastic"),
                            ("ar", "Open Meshtastic"), ("de", "Meshtastic öffnen")]:
            with self.subTest(language=code):
                self.language.write_text(f" {code}\n", encoding="utf-8")
                self.assertEqual(self.action(), f"--action=default={label}")

    def test_native_language_file_precedence_and_missing_defaults(self):
        native = self.home / "config/omarchy/language"
        native.parent.mkdir(parents=True)
        native.write_text("pl\n", encoding="utf-8")
        self.assertEqual(self.action(), "--action=default=Open Meshtastic")
        self.language.write_text("de\n", encoding="utf-8")
        self.assertEqual(self.action(), "--action=default=Meshtastic öffnen")
        del os.environ["OMARCHY_LANGUAGE_FILE"]
        self.assertEqual(self.action(), "--action=default=Otwórz Meshtastic")
        del os.environ["XDG_CONFIG_HOME"]
        native = self.home / ".config/omarchy/language"
        native.parent.mkdir(parents=True)
        native.write_text("de\n", encoding="utf-8")
        self.assertEqual(self.action(), "--action=default=Meshtastic öffnen")

    def test_invalid_language_cannot_select_arbitrary_paths_or_commands(self):
        for raw in (b"../../de", b"de/../../pl", b"DE", b"de_AT", b"de-123", b"de\npl",
                    b"$(touch /tmp/not-executed)", b"", b"\xff"):
            with self.subTest(raw=raw):
                self.language.write_bytes(raw)
                self.assertEqual(self.action(), "--action=default=Open Meshtastic")

    def test_missing_or_malformed_catalog_falls_through_each_layer(self):
        self.language.write_text("de-AT", encoding="utf-8")
        original = Path.read_text
        for broken in ("{", "[]", "null", '{"notifications.open":""}', '{"notifications.open":7}'):
            def read(path, *args, **kwargs):
                return broken if path.name == "de-AT.json" else original(path, *args, **kwargs)
            with self.subTest(catalog=broken), mock.patch.object(Path, "read_text", read):
                self.assertEqual(self.action(), "--action=default=Meshtastic öffnen")
        def missing(path, *args, **kwargs):
            if path.parent.name == "i18n":
                raise OSError("catalog unavailable")
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "read_text", missing):
            self.assertEqual(self.action(), "--action=default=Open Meshtastic")


@unittest.skipUnless(shutil.which("quickshell") and (COMMONS / "Translations.qml").is_file(),
                     "Native Omarchy/Quickshell is not installed")
class NativeLanguageTests(unittest.TestCase):
    def test_native_plural_fallback_numbers_and_live_consumer_bindings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native = root / "Commons"
            native.mkdir()
            # Exercise the installed native implementation, not a reimplementation.
            for name in ("I18n.qml", "Translations.qml", "Plural.js", "Numbers.js"):
                shutil.copyfile(COMMONS / name, native / name)
            (native / "qmldir").write_text("singleton I18n 1.0 I18n.qml\nTranslations 1.0 Translations.qml\n")
            shutil.copyfile(ROOT / "tests/i18n.qml", root / "test.qml")
            shutil.copytree(CATALOGS, root / "i18n")
            language = root / "language"
            language.write_text("en\n")
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", XDG_RUNTIME_DIR=str(runtime),
                XDG_CONFIG_HOME=str(root / "config"), XDG_CACHE_HOME=str(root / "cache"), HOME=str(root),
                OMARCHY_LANGUAGE_FILE=str(language), OMARCHY_PATH="/usr/share/omarchy")
            process = subprocess.Popen(["quickshell", "--no-color", "--path", str(root / "test.qml")],
                env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            output = b""
            pending = b""
            deadline = time.monotonic() + 20
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while selector.get_map() and time.monotonic() < deadline:
                        for key, _ in selector.select(timeout=0.2):
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            output += chunk
                            pending += chunk
                            while b"\n" in pending:
                                line, pending = pending.split(b"\n", 1)
                                request = re.search(rb"LANGUAGE_REQUEST:([a-zA-Z-]+)", line)
                                if request:
                                    language.write_text(request[1].decode() + "\n", encoding="utf-8")
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
                self.assertEqual(process.returncode, 0, output.decode(errors="replace"))
                self.assertIn(b"Native localization regressions passed", output)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()


if __name__ == "__main__":
    unittest.main()
