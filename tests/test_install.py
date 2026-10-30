import contextlib
import io
import json
import os
from pathlib import Path
import pathlib
import pwd
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from olinux import install

KEY = "ssh-ed25519 YWJj"


class InstallTests(unittest.TestCase):
    def fake_install(self, root, *, existing=False, adopt=False, fault=None, initial_mounts=None, argv=None):
        home, state = root / "home", root / "state"
        home.mkdir(exist_ok=True)
        calls, mounts = [], dict(initial_mounts or {})

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            output = ""
            if argv[0] == "ssh-keygen":
                if "-f" in argv and "-q" in argv:
                    Path(argv[argv.index("-f") + 1]).write_text("test-only-key-placeholder")
                else:
                    output = KEY + "\n"
            elif argv[1:4] == ["list", "--format", "json"]:
                output = json.dumps({"list": [{"name": "o-linux", "state": "Running"}] if existing else []})
            elif argv[1] == "info":
                output = json.dumps({"info": {"o-linux": {"mounts": mounts}}})
            elif argv[1] == "mount":
                mounts[argv[3].split(":", 1)[1]] = {"source_path": argv[2]}
            elif argv[1] == "exec":
                command = argv[4:]
                if command[:2] == ["cloud-init", "status"]:
                    output = "status: done\n"
                elif command[:5] == ["ip", "-j", "-4", "route", "show"]:
                    output = json.dumps([{"gateway": "192.0.2.1", "dev": "eth0"}])
                elif command[:5] == ["ip", "-j", "-4", "address", "show"]:
                    output = json.dumps([{"addr_info": [{"local": "192.0.2.2", "prefixlen": 24}]}])
                elif command[0] == "cat":
                    output = KEY + "\n"
                elif command[:2] == ["python3", "-"]:
                    output = "/usr/bin/python3\n/usr/sbin/nbd-client\n/usr/bin/ddrescue\n"
                elif command[0] == "/usr/local/bin/mac":
                    output = "Darwin\n"
                elif command[:3] == ["sudo", "-n", "python3"] and fault == "guest-provision":
                    raise RuntimeError("injected mid-provision failure")
            return SimpleNamespace(returncode=0, stdout=output, stderr="")

        original_add_host_key = install.add_host_key
        def add_key(*args, **kwargs):
            if fault == "host-authorization":
                raise RuntimeError("injected post-config failure")
            return original_add_host_key(*args, **kwargs)

        with patch.object(install, "state_dir", return_value=state), \
                patch.object(install.Path, "home", return_value=home), \
                patch.object(install.platform, "system", return_value="Darwin"), \
                patch.object(install.os, "geteuid", return_value=501), \
                patch.object(install.getpass, "getuser", return_value="example"), \
                patch.object(install.shutil, "which", side_effect=lambda name: "/tools/" + name), \
                patch.object(install, "read_host_public_key", return_value=KEY), \
                patch.object(install.socket, "create_connection"), \
                patch.object(install, "run", side_effect=fake_run), \
                patch.object(install, "add_host_key", side_effect=add_key), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = install.main((["--adopt-existing"] if adopt else []) + (argv or []))
        return result, state, home, calls

    def test_fresh_setup_wires_guest_and_host_without_external_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            result, state, home, calls = self.fake_install(Path(folder))
            self.assertEqual(result, 0)
            config = json.loads((state / "config.json").read_text())
            self.assertEqual(config["host_home"], str(home.resolve()))
            self.assertEqual(config["host_subnet"], "192.0.2.0/24")
            self.assertTrue(any(command[1] == "launch" for command, _ in calls if command[0].endswith("multipass")))
            self.assertIn('restrict,pty,from="127.0.0.1,::1,192.0.2.0/24"', (home / ".ssh/authorized_keys").read_text())
            self.assertTrue((home / ".local/bin/linux").is_file())
            self.assertTrue((home / ".local/bin/o-linux").is_file())
            self.assertEqual(json.loads((state / "install-transaction.json").read_text())["status"], "complete")

    def test_conflicting_mount_is_rejected_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            result, state, home, calls = self.fake_install(Path(folder), existing=True, adopt=True,
                                                         initial_mounts={"/Volumes": {"source_path": "/different"}})
            self.assertEqual(result, 1)
            self.assertFalse(state.exists())
            self.assertFalse((home / ".ssh").exists())
            self.assertFalse(any(argv[1] in ("start", "exec", "mount") for argv, _ in calls))

    def test_failures_record_recovery_and_rerun_keeps_credentials(self):
        for phase in ("host-authorization", "guest-provision"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                result, state, home, _ = self.fake_install(root, fault=phase)
                self.assertEqual(result, 1)
                self.assertTrue((state / "config.json").is_file())
                transaction = json.loads((state / "install-transaction.json").read_text())
                self.assertEqual(transaction["status"], "incomplete")
                self.assertEqual(transaction["phase"], phase)
                self.assertIn("--adopt-existing", transaction["retry_argv"])
                self.assertEqual(transaction["retry_environment"]["O_LINUX_STATE"], str(state))
                key = (state / "host_id_ed25519").read_bytes()
                self.assertFalse((home / ".local/bin/linux").exists())
                result, _, _, calls = self.fake_install(root, existing=True, adopt=True)
                self.assertEqual(result, 0)
                self.assertEqual((state / "host_id_ed25519").read_bytes(), key)
                self.assertFalse(any(argv[0] == "ssh-keygen" and "-q" in argv for argv, _ in calls))
                self.assertEqual(json.loads((state / "install-transaction.json").read_text())["status"], "complete")

    def test_existing_vm_is_not_adopted_implicitly(self):
        with tempfile.TemporaryDirectory() as folder:
            result, state, _, calls = self.fake_install(Path(folder), existing=True)
            self.assertEqual(result, 1)
            self.assertFalse(state.exists())
            self.assertFalse(any(command[1] in ("launch", "exec", "mount") for command, _ in calls if command[0].endswith("multipass")))

    def test_explicit_adoption_never_replaces_guest_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            result, state, _, calls = self.fake_install(Path(folder), existing=True, adopt=True)
            self.assertEqual(result, 0)
            self.assertTrue((state / "config.json").is_file())
            self.assertFalse(any(command[1] in ("launch", "delete", "purge") for command, _ in calls if command[0].endswith("multipass")))

    def test_rerun_preserves_integration_resources_and_share_choice(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result, state, _, _ = self.fake_install(root, argv=["--no-volumes", "--cpus", "6", "--memory", "8G", "--disk", "32G"])
            self.assertEqual(result, 0)
            path = state / "config.json"
            config = json.loads(path.read_text())
            config.update(ostadix_root="/opt/custom-ostadix", ssh="/custom/ssh", nbd_host_port=12000, nbd_guest_port=12001)
            path.write_text(json.dumps(config))
            result, _, _, calls = self.fake_install(root, existing=True, adopt=True)
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(path.read_text()), config)
            self.assertFalse(any(argv[1:3] == ["mount", "/Volumes"] for argv, _ in calls))

    def test_authorized_keys_preserves_unrelated_bytes(self):
        other = b'# comment\r\nssh-ed25519 ZGVm other key\r\n'
        original = other + b'restrict ssh-ed25519 YWJj old\n'
        entry = 'restrict,pty ssh-ed25519 YWJj new'
        updated = install.authorized_key_bytes(original, KEY, entry)
        self.assertEqual(updated, other + entry.encode() + b'\n')
        self.assertEqual(install.authorized_key_bytes(updated, KEY, entry), updated)

    def test_host_key_change_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "known"
            install.pin_host_key(path, "o-linux-guest", KEY)
            before = path.read_bytes()
            with self.assertRaises(RuntimeError):
                install.pin_host_key(path, "o-linux-guest", "ssh-ed25519 ZGVm")
            self.assertEqual(path.read_bytes(), before)

    def test_foreign_launcher_requires_explicit_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            launcher = base / "bin/linux"
            launcher.parent.mkdir()
            launcher.write_text("existing command\n")
            state = base / "state"
            with self.assertRaises(RuntimeError):
                install.install_launcher(launcher, "new command\n", state)
            self.assertEqual(launcher.read_text(), "existing command\n")
            install.install_launcher(launcher, "new command\n", state, replace=True)
            self.assertEqual(launcher.read_text(), "new command\n")
            self.assertEqual(next((state / "backups").iterdir()).read_text(), "existing command\n")

    def test_installed_launcher_uses_bounded_cli_error_reporting(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / "state"
            state.mkdir()
            install.install_app(Path(__file__).resolve().parents[1], state)
            launcher = root / "linux"
            install.atomic_write(launcher, install.launcher_text(state), 0o755)
            environment = os.environ.copy()
            environment["O_LINUX_STATE"] = str(state)
            result = subprocess.run([sys.executable, str(launcher), "status"], capture_output=True,
                                    text=True, env=environment)
            self.assertEqual(result.returncode, 1)
            self.assertTrue(result.stderr.startswith("o-linux: "))
            self.assertNotIn("Traceback", result.stderr)

    def test_cloud_init_uses_configuration_without_shell_interpolation(self):
        config = {"host_home": "/Users/test space", "guest_home": "/home/ubuntu", "guest_user": "ubuntu"}
        document = json.loads(install.cloud_config(config).split("\n", 1)[1])
        self.assertIn(["ln", "-sfn", "/Users/test space", "/host/home"], document["runcmd"])
        self.assertFalse(document["package_upgrade"])
        self.assertTrue({"qemu-system-arm", "qemu-system-x86", "qemu-efi-aarch64", "ovmf"}.issubset(document["packages"]))

    def test_plan_has_no_mutations_or_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "absent"
            with patch.object(install, "state_dir", return_value=state), \
                    patch.object(install.platform, "system", return_value="Darwin"), \
                    patch.object(install, "run", side_effect=AssertionError("unexpected command")), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(install.main(["--plan"]), 0)
            self.assertEqual(json.loads(output.getvalue())["state"], str(state))
            self.assertFalse(state.exists())

    def test_private_sources_are_not_copied_into_installed_app(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo, state = root / "repo", root / "state"
            (repo / "src/olinux").mkdir(parents=True)
            state.mkdir()
            (repo / "src/olinux/__init__.py").write_text("")
            (repo / "src/olinux/private.key").write_text("not source")
            (repo / "host_id_ed25519").write_text("not source")
            install.install_app(repo, state)
            files = [p.name for p in (state / "app").rglob("*") if p.is_file()]
            self.assertEqual(files, ["__init__.py"])

    def test_only_explicit_drive_integration_is_copied(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo, state = root / "repo", root / "state"
            (repo / "src/olinux").mkdir(parents=True)
            (repo / "src/olinux/__init__.py").write_text("")
            integrations = repo / "integrations/ostadix"
            integrations.mkdir(parents=True)
            source = integrations / "read-drive.O"
            source.write_text('python^(\n__oval_result__ = "fixture"\n)_python\n')
            (integrations / "private-experiment.O").write_text("private fixture")
            state.mkdir()
            install.install_app(repo, state)
            target = state / "app/integrations/ostadix/read-drive.O"
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertFalse((target.parent / "private-experiment.O").exists())
            source.unlink()
            source.symlink_to(integrations / "private-experiment.O")
            with self.assertRaisesRegex(RuntimeError, "non-regular integration"):
                install.install_app(repo, state)

    def test_guest_provision_source_compiles_and_contains_no_inline_key_command(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            source = state / "app/src/olinux/guest_mac.py"
            source.parent.mkdir(parents=True)
            source.write_text("print('mac')\n")
            (state / "host_id_ed25519").write_text("test-only-placeholder")
            config = {"host_home": "/Users/example", "host_user": "example", "host_address": "192.0.2.1",
                      "guest_user": "ubuntu", "guest_home": "/home/ubuntu", "share_volumes": True}
            with patch.object(install, "guest_python") as call:
                install.provision_guest(config, state, KEY, KEY)
            program = call.call_args.args[1]
            compile(program, "guest-provision", "exec")
            self.assertTrue(call.call_args.kwargs["privileged"])

    def test_guest_provision_exec_preserves_keys_modes_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / "state"
            module = state / "app/src/olinux/guest_mac.py"
            module.parent.mkdir(parents=True)
            module.write_text("print('mac')\n")
            (state / "host_id_ed25519").write_text("test-only-placeholder")
            config = {"host_home": "/Users/example", "host_user": "example", "host_address": "192.0.2.1",
                      "guest_user": "ubuntu", "guest_home": "/home/ubuntu", "share_volumes": True}
            with patch.object(install, "guest_python") as call:
                install.provision_guest(config, state, KEY, KEY)
            program = call.call_args.args[1]
            fixture = root / "guest"
            fixture.mkdir()
            # Python 3.11's abstract Path constructor consults pathlib.Path;
            # keep the concrete class while that public name is mocked below.
            native_path = type(root)
            def guest_path(value):
                value = native_path(value)
                return fixture / str(value).lstrip("/") if value.is_absolute() else value
            auth = fixture / "home/ubuntu/.ssh/authorized_keys"
            auth.parent.mkdir(parents=True)
            original = b"# keep this\r\nssh-ed25519 ZGVm unrelated\r\n"
            auth.write_bytes(original)
            account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
            original_replace = os.replace
            def execute(fail=False):
                def replace(source, destination):
                    if fail and str(destination).endswith("/etc/o-linux/host.json"):
                        raise OSError("injected guest write failure")
                    original_replace(source, destination)
                with patch.object(pathlib, "Path", side_effect=guest_path), \
                        patch.object(pwd, "getpwnam", return_value=account), \
                        patch.object(os, "chown"), patch.object(os, "replace", side_effect=replace), \
                        contextlib.redirect_stdout(io.StringIO()):
                    exec(compile(program, "guest-provision", "exec"), {})
            with self.assertRaisesRegex(OSError, "guest write failure"):
                execute(fail=True)
            self.assertEqual(auth.read_bytes(), original + KEY.encode() + b"\n")
            execute()
            self.assertEqual(auth.read_bytes(), original + KEY.encode() + b"\n")
            self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
            self.assertEqual(auth.parent.stat().st_mode & 0o777, 0o700)
            identity = auth.parent / "o_linux_host_ed25519"
            self.assertEqual(identity.stat().st_mode & 0o777, 0o600)
            self.assertEqual((fixture / "usr/local/bin/mac").stat().st_mode & 0o777, 0o755)
            execute()
            self.assertEqual(auth.read_bytes(), original + KEY.encode() + b"\n")
            identity.unlink()
            identity.symlink_to(root / "outside")
            auth.write_bytes(original)
            with self.assertRaisesRegex(RuntimeError, "non-regular destination"):
                execute()
            self.assertEqual(auth.read_bytes(), original)
            self.assertFalse((root / "outside").exists())


if __name__ == "__main__":
    unittest.main()
