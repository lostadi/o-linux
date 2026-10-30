"""Controlling-terminal transport checks using local disposable commands only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from olinux import drives


class DrivePtyTests(unittest.TestCase):
    def test_controlling_tty_and_terminal_formatting(self):
        source = ("import os; assert all(os.isatty(fd) for fd in (0,1,2)); "
                  "fd=os.open('/dev/tty',os.O_RDWR); os.close(fd); "
                  "os.write(1,b'\\x1b]0;metadata\\x07\\x1b[32mfirst\\x1b[0m\\r\\nsecond\\n')")
        self.assertEqual(drives.pty_text([sys.executable, "-c", source]), "first\nsecond")

    def test_arguments_are_literal_without_a_shell(self):
        values = ["mount with spaces", "$(not-a-command)", "semi;colon", "'quote'"]
        result = drives.pty_text([sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *values])
        self.assertEqual(json.loads(result), values)

    def test_nonzero_status_is_not_accepted_as_metadata(self):
        with self.assertRaisesRegex(RuntimeError, r"failed \(7\): fixture failure"):
            drives.pty_text([sys.executable, "-c", "import sys; print('fixture failure'); sys.exit(7)"])

    def test_timeout_reaps_only_the_owned_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "pid"
            source = "import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
            start = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                drives.pty_text([sys.executable, "-c", source, str(marker)], timeout=0.5)
            self.assertLess(time.monotonic() - start, 3)
            pid = int(marker.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_output_limit_stops_an_unbounded_command(self):
        with self.assertRaisesRegex(RuntimeError, "size limit"):
            drives.pty_text([sys.executable, "-c", "import os; os.write(1,b'x'*65536)"], max_output=1024)

    def test_guest_transport_preserves_mount_and_command_arguments(self):
        with patch.object(drives, "run", return_value=SimpleNamespace(stdout="1\n")) as elevated, \
                patch.object(drives, "pty_text", side_effect=AssertionError("PTY before elevation")):
            result = drives.guest_text("/tools/anylinuxfs", "/Volumes/Copy on Backup",
                                       ["cat", "/sys/class/block/vda/ro"])
        self.assertEqual(result, "1")
        elevated.assert_called_once_with(["/usr/bin/sudo", "-n", sys.executable, "-I", "-S", "-c",
                                          drives.GUEST_PTY_HELPER, str(Path(drives.__file__).resolve().parent.parent),
                                          "/tools/anylinuxfs", "vm", "exec", "/Volumes/Copy on Backup",
                                          "--", "/bin/cat", "/sys/class/block/vda/ro"], timeout=35)

    def test_filesystem_probe_uses_absolute_guest_sbin_path(self):
        arguments = ["blkid", "-p", "-o", "export", "/dev/vda"]
        with patch.object(drives, "run", return_value=SimpleNamespace(stdout="TYPE=ext4\n")) as transported:
            drives.guest_text("/tools/anylinuxfs", "/Volumes/Fixture", arguments)
        self.assertEqual(transported.call_args.args[0][-5:], ["/sbin/blkid", "-p", "-o", "export", "/dev/vda"])
        self.assertEqual(arguments[0], "blkid")

    def test_isolated_helper_imports_known_package_and_creates_tty_after_entry(self):
        source = "import os,sys; assert os.isatty(0); fd=os.open('/dev/tty',os.O_RDWR); os.close(fd); print(sys.argv[1])"
        result = subprocess.run([sys.executable, "-I", "-S", "-c", drives.GUEST_PTY_HELPER,
                                 str(Path(drives.__file__).resolve().parent.parent),
                                 sys.executable, "-c", source, "literal;$(data)"],
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "literal;$(data)\n")

    def test_cleanup_never_signals_a_reaped_leaders_process_group(self):
        with patch("pty.fork", return_value=(12345, 73)), \
                patch.object(os, "waitpid", return_value=(12345, 0)), \
                patch("select.select", return_value=([73], [], [])), \
                patch.object(os, "read", return_value=b"oversized"), \
                patch.object(os, "close"), patch.object(os, "killpg") as killed:
            with self.assertRaisesRegex(RuntimeError, "size limit"):
                drives.pty_text(["never-executed"], max_output=1)
        killed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
