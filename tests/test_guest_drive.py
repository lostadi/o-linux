"""Guest routing tests never connect to a host or touch a real drive."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from olinux import cli, drive_frontend, drives, guest_drive, guest_mac


class GuestDriveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "host.json"
        self.config = {"host_home": "/Users/host with spaces", "host_user": "host",
                       "host_address": "192.0.2.1", "host_alias": "o-linux-mac",
                       "identity": str(self.directory / "identity"),
                       "known_hosts": str(self.directory / "known_hosts"),
                       "share_volumes": True}
        self.path.write_text(json.dumps(self.config))
        self.path.chmod(0o600)
        for name in ("identity", "known_hosts"):
            Path(self.config[name]).touch(mode=0o600)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"O_LINUX_HOST_CONFIG": str(self.path)}))
        self.stack.enter_context(patch.object(cli, "load_config", return_value={"provider": "native"}))
        self.stack.enter_context(patch.object(cli.platform, "system", return_value="Linux"))
        for stream in (cli.sys.stdin, cli.sys.stdout, cli.sys.stderr):
            self.stack.enter_context(patch.object(stream, "isatty", return_value=False))
        self.local_open = self.stack.enter_context(patch.object(drive_frontend, "run_open"))
        self.local_drive = self.stack.enter_context(patch.object(drives, "main"))

    def test_each_action_uses_absolute_host_command_and_preserves_arguments(self):
        actions = [[], ["open"], ["status"], ["close"], ["mode", "rw"], ["mode", "ro"],
                   ["select", "/dev/disk9s3", "--mountpoint", "/Volumes/a b $(literal) 'quoted'", "--read-write"],
                   ["select", "/dev/disk9s3", "--read-only"]]
        with patch.object(guest_mac, "main", return_value=23) as bridge, \
                patch.object(cli.shutil, "which", side_effect=AssertionError("PATH must not select the bridge")):
            for arguments in actions:
                with self.subTest(arguments=arguments):
                    self.assertEqual(cli.main(["drive", *arguments]), 23)
                    bridge.assert_called_with(["--", "/Users/host with spaces/.local/bin/o-linux", "_host-drive", *arguments])
        self.local_open.assert_not_called()
        self.local_drive.assert_not_called()

    def test_bridge_serializes_status_without_mount_or_tty(self):
        with patch.object(guest_mac.os, "execv") as execute, \
                patch.object(guest_mac.os, "getcwd", return_value="/Users/host with spaces/project"):
            cli.main(["drive", "status"])
        executable, arguments = execute.call_args.args
        self.assertEqual(executable, "/usr/bin/ssh")
        self.assertIn("-T", arguments)
        self.assertNotIn("-tt", arguments)
        self.assertIn("StrictHostKeyChecking=yes", arguments)
        request = json.loads(shlex.split(arguments[-1])[0])
        self.assertEqual(request, {"version": 1,
                                  "argv": ["/Users/host with spaces/.local/bin/o-linux", "_host-drive", "status"],
                                  "cwd": "/Users/host with spaces/project"})
        self.local_open.assert_not_called()
        self.local_drive.assert_not_called()

    def test_interactive_bridge_requests_tty_only_when_every_stream_is_a_terminal(self):
        for output_is_tty in (True, False):
            with self.subTest(stdout_tty=output_is_tty), \
                    patch.object(cli.sys.stdin, "isatty", return_value=True), \
                    patch.object(cli.sys.stdout, "isatty", return_value=output_is_tty), \
                    patch.object(cli.sys.stderr, "isatty", return_value=True), \
                    patch.object(guest_mac, "main", return_value=0) as bridge:
                cli.main(["drive", "open"])
                self.assertEqual(bridge.call_args.args[0][0], "--tty" if output_is_tty else "--")

    def test_missing_bridge_does_not_use_a_mac_executable_from_path(self):
        self.path.unlink()
        with patch.object(guest_mac, "main") as bridge, \
                patch.object(cli.shutil, "which", return_value="/untrusted/mac") as lookup, \
                self.assertRaisesRegex(RuntimeError, "no Mac bridge is provisioned"):
            cli.main(["drive", "status"])
        bridge.assert_not_called()
        lookup.assert_not_called()
        self.local_open.assert_not_called()

    def test_malformed_or_non_mac_configuration_never_dispatches(self):
        invalid = ["not JSON", "[]", "{}", json.dumps(dict(self.config, host_home="relative")),
                   json.dumps(dict(self.config, host_home="/Users/../host")),
                   json.dumps(dict(self.config, host_home=7)),
                   json.dumps(dict(self.config, host_alias="other-host")),
                   json.dumps(dict(self.config, host_platform="Linux")),
                   json.dumps(dict(self.config, identity=None))]
        with patch.object(guest_mac, "main") as bridge:
            for content in invalid:
                with self.subTest(content=content):
                    self.path.write_text(content)
                    with self.assertRaisesRegex(RuntimeError, "Invalid Mac drive bridge configuration"):
                        cli.main(["drive", "status"])
        bridge.assert_not_called()

    def test_symlink_and_writable_configuration_never_dispatch(self):
        target = self.directory / "target.json"
        self.path.rename(target)
        self.path.symlink_to(target)
        with patch.object(guest_mac, "main") as bridge:
            with self.assertRaisesRegex(RuntimeError, "regular host configuration"):
                cli.main(["drive", "status"])
            self.path.unlink()
            target.rename(self.path)
            self.path.chmod(0o666)
            with self.assertRaisesRegex(RuntimeError, "group/world writable"):
                cli.main(["drive", "status"])
        bridge.assert_not_called()

    def test_native_macos_keeps_existing_local_routes(self):
        with patch.object(cli.platform, "system", return_value="Darwin"), \
                patch.object(guest_drive, "forward") as forward, \
                contextlib.redirect_stdout(io.StringIO()):
            self.local_open.return_value = {"mounted": True}
            self.local_drive.return_value = 9
            self.assertEqual(cli.main(["drive", "open"]), 0)
            self.assertEqual(cli.main(["drive", "status"]), 9)
        forward.assert_not_called()
        self.local_open.assert_called_once_with()
        self.local_drive.assert_called_once_with(["status"])

    def test_host_entry_rejects_non_macos_before_config_or_forwarding(self):
        for system in ("Linux", "FreeBSD", "Windows"):
            with self.subTest(system=system), \
                    patch.object(cli.platform, "system", return_value=system), \
                    patch.object(cli, "load_config") as config, \
                    patch.object(guest_drive, "forward") as forward, \
                    self.assertRaisesRegex(RuntimeError, "refusing to forward again"):
                cli.main(["_host-drive", "status"])
            config.assert_not_called()
            forward.assert_not_called()
        self.local_open.assert_not_called()
        self.local_drive.assert_not_called()

    def test_host_entry_normalizes_to_existing_macos_routes(self):
        with patch.object(cli.platform, "system", return_value="Darwin"), \
                patch.object(guest_drive, "forward") as forward, \
                contextlib.redirect_stdout(io.StringIO()):
            self.local_open.return_value = {"mounted": True}
            self.local_drive.return_value = 9
            for arguments in ([], ["open"]):
                self.assertEqual(cli.main(["_host-drive", *arguments]), 0)
            for arguments in (["status"], ["close"], ["mode", "rw"], ["mode", "ro"],
                              ["select", "/dev/disk9s3", "--mountpoint", "/Volumes/a b", "--read-write"],
                              ["select", "/dev/disk9s3", "--read-only"]):
                self.assertEqual(cli.main(["_host-drive", *arguments]), 9)
                self.local_drive.assert_called_with(arguments)
        forward.assert_not_called()
        self.assertEqual(self.local_open.call_count, 2)


if __name__ == "__main__":
    unittest.main()
