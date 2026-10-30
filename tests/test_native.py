import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from olinux import install, native


class NativeInstallTests(unittest.TestCase):
    def invoke(self, root, argv=None):
        home, state = root / "home", root / "state"
        home.mkdir(exist_ok=True)
        with patch.object(native, "state_dir", return_value=state), \
                patch.object(native.Path, "home", return_value=home), \
                patch.object(native.getpass, "getuser", return_value="example"), \
                patch.object(native.os, "geteuid", return_value=1000), \
                patch.object(native.platform, "system", return_value="Linux"), \
                patch.object(install, "run", side_effect=AssertionError("No external command is needed")), \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            result = native.main(argv or [])
        return result, state, home, output.getvalue(), errors.getvalue()

    def test_native_install_configuration_and_source_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            result, state, home, _, errors = self.invoke(Path(folder))
            self.assertEqual(result, 0, errors)
            config = json.loads((state / "config.json").read_text())
            self.assertEqual(config["provider"], "native")
            self.assertEqual(config["host_home"], str(home.resolve()))
            self.assertEqual(config["guest_home"], config["host_home"])
            self.assertEqual(config["guest_user"], "example")
            self.assertEqual(config["host_user"], "example")
            self.assertNotIn("multipass", config)
            self.assertTrue((state / "app/src/olinux/native.py").is_file())
            self.assertTrue((state / "app/src/olinux/cli.py").is_file())
            self.assertFalse((state / "app/.git").exists())
            self.assertEqual((state / "config.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual((home / ".local/bin/linux").stat().st_mode & 0o777, 0o755)
            self.assertEqual((home / ".local/bin/o-linux").read_text(), install.launcher_text(state))
            self.assertEqual(json.loads((state / "install-transaction.json").read_text())["status"], "complete")

    def test_plan_creates_no_state_or_launchers(self):
        with tempfile.TemporaryDirectory() as folder:
            result, state, home, output, errors = self.invoke(Path(folder), ["--plan"])
            self.assertEqual(result, 0, errors)
            self.assertEqual(json.loads(output)["configuration"]["provider"], "native")
            self.assertFalse(state.exists())
            self.assertFalse((home / ".local").exists())

    def test_unrelated_launcher_requires_explicit_backup_and_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "home/.local/bin/linux"
            path.parent.mkdir(parents=True)
            path.write_text("unrelated command\n")
            result, state, _, _, _ = self.invoke(root)
            self.assertEqual(result, 1)
            self.assertFalse(state.exists())
            self.assertEqual(path.read_text(), "unrelated command\n")
            result, state, _, _, errors = self.invoke(root, ["--replace-launcher"])
            self.assertEqual(result, 0, errors)
            backup = next((state / "backups").glob("linux-*"))
            self.assertEqual(backup.read_text(), "unrelated command\n")

    def test_rerun_preserves_integration_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result, state, _, _, _ = self.invoke(root)
            self.assertEqual(result, 0)
            path = state / "config.json"
            config = json.loads(path.read_text())
            config.update(ostadix_root="/opt/custom-ostadix", ssh="/custom/ssh")
            path.write_text(json.dumps(config))
            result, _, _, _, errors = self.invoke(root)
            self.assertEqual(result, 0, errors)
            self.assertEqual(json.loads(path.read_text()), config)

    def test_provider_or_account_mismatch_is_preserved(self):
        for change in ({"provider": "multipass"}, {"host_user": "someone-else"}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                _, state, _, _, _ = self.invoke(root)
                path = state / "config.json"
                config = json.loads(path.read_text())
                config.update(change)
                original = json.dumps(config)
                path.write_text(original)
                result, _, _, _, errors = self.invoke(root)
                self.assertEqual(result, 1)
                self.assertIn("separate O_LINUX_STATE", errors)
                self.assertEqual(path.read_text(), original)

    def test_main_installer_delegates_to_native_on_linux(self):
        with patch.object(install.platform, "system", return_value="Linux"), \
                patch.object(native, "main", return_value=0) as entry:
            self.assertEqual(install.main(["--plan"]), 0)
            entry.assert_called_once_with(["--plan"])


if __name__ == "__main__":
    unittest.main()
