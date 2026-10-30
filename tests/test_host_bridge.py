import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from olinux import guest_mac, host_bridge


class HostBridgeTests(unittest.TestCase):
    def test_literal_arguments_and_cwd(self):
        with tempfile.TemporaryDirectory() as home:
            arguments = ["echo", "spaces here", "$(touch never)", "a'b\"c", "line\nbreak", ""]
            request = shlex.quote(json.dumps({"version": 1, "argv": arguments, "cwd": home}))
            self.assertEqual(host_bridge.parse_request(request, home), (arguments, home))

    def test_rejects_unstructured_shell_and_bad_arguments(self):
        invalid = ["echo hello", "", shlex.quote(json.dumps({"version": 1, "argv": ["echo", 7]})),
                   shlex.quote(json.dumps({"version": 1, "argv": [""]})),
                   shlex.quote(json.dumps({"version": 1, "argv": ["echo"], "env": {}})),
                   shlex.quote(json.dumps({"version": 1, "argv": ["echo\0bad"]}))]
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(ValueError):
                host_bridge.parse_request(request, "/tmp")

    def test_binary_streams_and_exit_status(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(json.dumps({"host_home": folder}))
            program = "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read());sys.stderr.write('stderr-proof');sys.exit(23)"
            request = {"version": 1, "argv": [sys.executable, "-c", program], "cwd": folder}
            environment = dict(os.environ, SSH_ORIGINAL_COMMAND=shlex.quote(json.dumps(request)))
            result = subprocess.run([sys.executable, host_bridge.__file__, "--config", str(config)],
                                    input=b"zero\0one\xff\n", capture_output=True, env=environment)
            self.assertEqual(result.returncode, 23)
            self.assertEqual(result.stdout, b"zero\0one\xff\n")
            self.assertEqual(result.stderr, b"stderr-proof")

    def test_native_environment_uses_configured_home(self):
        with patch.dict(os.environ, {"PYTHONPATH": "untrusted", "SSH_ORIGINAL_COMMAND": "request"}):
            environment = host_bridge.native_environment({"host_home": "/Users/example"}, "/Volumes")
        self.assertEqual(environment["HOME"], "/Users/example")
        self.assertEqual(environment["PWD"], "/Volumes")
        self.assertTrue(environment["PATH"].startswith("/Users/example/.local/bin:"))
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("SSH_ORIGINAL_COMMAND", environment)

    def test_guest_ssh_uses_pinned_identity_and_explicit_pty(self):
        config = {"host_address": "192.0.2.1", "host_user": "example", "identity": "/tmp/key",
                  "known_hosts": "/tmp/known", "host_alias": "o-linux-mac"}
        for tty in (False, True):
            command = guest_mac.build_ssh(["printf", "$(literal)", ""], "/Users/example", config, tty)
            self.assertIn("-tt" if tty else "-T", command)
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertIn("ClearAllForwardings=yes", command)
            request = json.loads(shlex.split(command[-1])[0])
            self.assertEqual(request["argv"], ["printf", "$(literal)", ""])

    def test_guest_option_order_and_shared_paths(self):
        self.assertEqual(guest_mac.parse_arguments(["--cwd", "/Volumes", "--tty", "pwd"]),
                         (["pwd"], "/Volumes", True))
        config = {"host_home": "/Users/example", "share_volumes": True}
        self.assertEqual(guest_mac.shared_cwd("/Users/example/project", config), "/Users/example/project")
        self.assertEqual(guest_mac.shared_cwd("/Users/examples", config), "/Users/example")
        self.assertEqual(guest_mac.shared_cwd("/Volumes/data", config), "/Volumes/data")
        config["share_volumes"] = False
        self.assertEqual(guest_mac.shared_cwd("/Volumes/data", config), "/Users/example")


if __name__ == "__main__":
    unittest.main()
