"""Packaging lifecycle tests: no real systemd, desktop, dependencies or radio."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import venv

SOURCE = Path(__file__).resolve().parents[1]
NATIVE_VALIDATOR = shutil.which("omarchy-plugin-validate")


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "checkout"
        for name in ("bin", "packaging"):
            shutil.copytree(SOURCE / name, self.root / name)
        (self.root / "plugin").mkdir()
        shutil.copy2(SOURCE / "manifest.json", self.root / "manifest.json")
        shutil.copy2(SOURCE / "plugin/BarWidget.qml", self.root / "plugin/BarWidget.qml")
        self.config = self.base / "config"
        self.target = self.config / "omarchy/plugins/miszarchy.meshtastic"
        self.unit = self.config / "systemd/user/omarchy-meshtastic.service"
        tools = self.base / "tools"
        tools.mkdir()
        self.log = self.base / "calls"
        for name in ("uv", "omarchy", "systemctl", "notify-send"):
            tool = tools / name
            tool.write_text('#!/bin/bash\nprintf "%s %s\\n" "${0##*/}" "$*" >> "$CALLS"\n')
            tool.chmod(0o755)
        self.runtime = self.base / "data/omarchy-meshtastic/venv"
        venv.EnvBuilder(with_pip=False, symlinks=True).create(self.runtime)
        self.env = dict(os.environ, HOME=str(self.base), XDG_CONFIG_HOME=str(self.config),
                        PATH=f"{tools}:{os.environ['PATH']}", CALLS=str(self.log))
        self.envfile = self.config / "omarchy/meshtastic.env"
        self.envfile.parent.mkdir(parents=True)
        self.envfile.write_text("keep-environment\n")
        self.layout = self.config / "omarchy/shell.json"
        self.layout.write_text('{"keep":"layout"}\n')
        self.history = self.base / "data/omarchy-meshtastic/history.sqlite3"
        self.history.parent.mkdir(parents=True, exist_ok=True)
        self.history.write_bytes(b"keep-history")
        self.env["XDG_DATA_HOME"] = str(self.base / "data")

    def run_script(self, name):
        return subprocess.run(["bash", str(self.root / "bin" / name)], env=self.env,
                              capture_output=True, text=True)

    def assert_preserved(self):
        self.assertEqual(self.envfile.read_text(), "keep-environment\n")
        self.assertEqual(self.layout.read_text(), '{"keep":"layout"}\n')
        self.assertEqual(self.history.read_bytes(), b"keep-history")

    def test_development_install_update_remove(self):
        for _ in range(2):
            result = self.run_script("install")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.resolve(), self.root)
        result = self.run_script("uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.unit.exists())
        self.assertTrue(self.target.is_symlink())
        self.assertIn("systemctl --user disable --now omarchy-meshtastic.service", self.log.read_text())
        self.assert_preserved()

    def test_native_installed_root(self):
        self.target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(self.root, self.target)
        self.root = self.target
        result = self.run_script("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.target.is_symlink())
        self.assert_preserved()

    @unittest.skipUnless(NATIVE_VALIDATOR, "Native Omarchy validator unavailable")
    def test_setup_keeps_runtime_outside_native_validated_plugin(self):
        result = self.run_script("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([str(self.root / "bin/python"), "-c", "import sys; print(sys.prefix)"],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.runtime))
        self.assertTrue((self.runtime / "bin/python").is_symlink())
        result = subprocess.run(["bash", NATIVE_VALIDATOR, str(self.root)],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_preserved()

    def test_legacy_symlink_and_unit_migration(self):
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.symlink_to(self.root / "plugin", target_is_directory=True)
        old_runtime = self.root / ".venv"
        venv.EnvBuilder(with_pip=False, symlinks=True).create(old_runtime)
        (old_runtime / "keep.txt").write_text("preserve old environment")
        legacy_venv = self.root / "plugin/.venv"
        legacy_venv.symlink_to(self.root / ".venv", target_is_directory=True)
        self.unit.parent.mkdir(parents=True)
        template = (self.root / "packaging/omarchy-meshtastic.service").read_text()
        self.unit.write_text(template.split("\n", 1)[1]
                             .replace("/bin/python", "/.venv/bin/python")
                             .replace("/plugin/bridge.py", "/bridge.py"))
        result = self.run_script("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.target.resolve(), self.root)
        self.assertFalse(legacy_venv.is_symlink())
        self.assertFalse(old_runtime.exists())
        self.assertEqual((self.base / "data/omarchy-meshtastic/legacy-project-venv/keep.txt").read_text(),
                         "preserve old environment")
        self.assert_preserved()

    def test_unrelated_target_refused_without_commands(self):
        self.target.mkdir(parents=True)
        for name in ("install", "uninstall"):
            result = self.run_script(name)
            self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())
        self.assert_preserved()

    def test_custom_unit_and_real_legacy_environment_refused(self):
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text("[Service]\nExecStart=/unrelated\n")
        for name in ("install", "uninstall"):
            self.assertNotEqual(self.run_script(name).returncode, 0)
        self.assertEqual(self.unit.read_text(), "[Service]\nExecStart=/unrelated\n")
        self.unit.unlink()
        (self.root / "plugin/.venv").mkdir()
        self.assertNotEqual(self.run_script("install").returncode, 0)
        self.assertFalse(self.log.exists())
        self.assert_preserved()
