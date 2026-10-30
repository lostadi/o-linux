import subprocess
import unittest
from unittest.mock import patch

from olinux import peripherals


class PeripheralInventoryTests(unittest.TestCase):
    def test_serial_inventory_never_opens_a_port_or_runs_a_command(self):
        with patch.object(peripherals.platform, "system", return_value="Darwin"), \
                patch.object(peripherals.glob, "glob", return_value=["/dev/cu.z", "/dev/cu.a"]), \
                patch.object(peripherals.subprocess, "run") as run:
            result = peripherals.inventory("serial")
        self.assertEqual(result, {"serial": {"paths": ["/dev/cu.a", "/dev/cu.z"], "ports_opened": False}})
        run.assert_not_called()

    def test_usb_query_is_metadata_and_preserves_diagnostic_output(self):
        completed = subprocess.CompletedProcess([], 3, "partial device tree", "query failed")
        with patch.object(peripherals.platform, "system", return_value="Darwin"), \
                patch.object(peripherals.subprocess, "run", return_value=completed) as run:
            result = peripherals.inventory("usb")
        self.assertEqual(result["usb"]["exit_code"], 3)
        self.assertEqual(result["usb"]["stdout"], "partial device tree")
        self.assertEqual(result["usb"]["stderr"], "query failed")
        run.assert_called_once_with(["/usr/sbin/ioreg", "-p", "IOUSB", "-w", "0"],
                                    capture_output=True, text=True, timeout=30)

    def test_one_failed_query_does_not_hide_other_classes(self):
        calls = [subprocess.TimeoutExpired("diskutil", 2),
                 OSError("missing ioreg"), *[subprocess.CompletedProcess([], 0, "present", "") for _ in range(7)]]
        with patch.object(peripherals.platform, "system", return_value="Darwin"), \
                patch.object(peripherals.subprocess, "run", side_effect=calls), \
                patch.object(peripherals.glob, "glob", return_value=[]):
            result = peripherals.inventory(timeout=2)
        self.assertEqual(set(result), set(peripherals.CATEGORIES))
        self.assertIn("timed out", result["disks"]["error"])
        self.assertEqual(result["usb"]["error"], "missing ioreg")
        self.assertEqual(result["printers"]["stdout"], "present")

    def test_unknown_category_and_unsupported_host_fail_before_commands(self):
        with patch.object(peripherals.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                peripherals.inventory("format")
            with patch.object(peripherals.platform, "system", return_value="FreeBSD"):
                with self.assertRaisesRegex(RuntimeError, "macOS"):
                    peripherals.inventory("usb")
        run.assert_not_called()

    def test_linux_inventory_uses_linux_metadata_tools(self):
        completed = subprocess.CompletedProcess([], 0, '{"blockdevices":[]}', "")
        with patch.object(peripherals.platform, "system", return_value="Linux"), \
                patch.object(peripherals.subprocess, "run", return_value=completed) as run:
            result = peripherals.inventory("disks")
        self.assertEqual(result["disks"]["stdout"], '{"blockdevices":[]}')
        self.assertEqual(run.call_args.args[0][0], "lsblk")
        self.assertIn("--json", run.call_args.args[0])

    def test_linux_serial_lists_usb_nodes_without_opening_them(self):
        with patch.object(peripherals.platform, "system", return_value="Linux"), \
                patch.object(peripherals.glob, "glob", side_effect=[
                    ["/dev/ttyUSB0"], ["/dev/ttyACM0"], ["/dev/serial/by-id/fixture"]]), \
                patch.object(peripherals.subprocess, "run") as run:
            result = peripherals.inventory("serial")
        self.assertEqual(len(result["serial"]["paths"]), 3)
        self.assertFalse(result["serial"]["ports_opened"])
        run.assert_not_called()

    def test_missing_optional_linux_tool_is_reported(self):
        with patch.object(peripherals.platform, "system", return_value="Linux"), \
                patch.object(peripherals.subprocess, "run", side_effect=FileNotFoundError("v4l2-ctl not installed")):
            result = peripherals.inventory("cameras")
        self.assertIn("not installed", result["cameras"]["error"])


if __name__ == "__main__":
    unittest.main()
