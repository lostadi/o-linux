"""Offline mode changes retain ownership and never read disconnected media."""
import contextlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from olinux import drives


class DisconnectedDriveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.state = Path(self.temp.name)
        self.live = {"mounted": True, "id": 77}
        self.selection = {"partition_uuid": "11111111-2222-3333-4444-555555555555",
                          "filesystem_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                          "filesystem": "ext4", "partition_bytes": 1000, "whole_bytes": 2000,
                          "media_name": "Fixture", "mountpoint": "/Volumes/Fixture"}
        self.owned = {**self.selection, "device": "/dev/disk9s3", "mounted": True,
                      "read_only": True, "host_mount_id": 77,
                      "host_source": "disk9s3.local:/mnt/Fixture"}
        def mock(name, **kwargs):
            return self.stack.enter_context(patch.object(drives, name, **kwargs))
        mock("state_dir", return_value=self.state)
        self.stack.enter_context(patch.object(drives.platform, "system", return_value="Darwin"))
        mock("load_config", return_value={})
        mock("command", return_value="/tools/anylinuxfs")
        self.locate = mock("locate", side_effect=RuntimeError("Saved partition is not connected"))
        mock("require_sudo")
        mock("previous_device_detached", return_value="inactive")
        mock("any_status", side_effect=lambda app: "disk9s3 on /Volumes/Fixture (ext4, ro, noload)" if self.live["mounted"] else "")
        mock("host_mount", return_value=self.owned["host_source"])
        mock("mount_id", side_effect=lambda path: self.live["id"])
        self.stack.enter_context(patch.object(drives.os.path, "ismount", side_effect=lambda path: self.live["mounted"]))
        self.signature = mock("signature", side_effect=AssertionError("No media reads while disconnected"))
        def unmount(argv, **kwargs):
            self.assertIn("stale_unmount_attempt", drives.load_record())
            self.live["mounted"] = False
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        self.run = mock("run", side_effect=unmount)
        drives.save_record({"version": 1, "selection": self.selection, "mount": self.owned})

    def test_owned_disconnected_close_then_offline_mode_change(self):
        result = drives.dispatch(["close"])
        self.assertEqual((result["closed"], result["connected"]), (True, False))
        self.run.assert_called_once_with(["/usr/bin/sudo", "-n", "/tools/anylinuxfs", "unmount", "/Volumes/Fixture", "--wait-for-vm=20"], timeout=40)
        self.assertIsNone(drives.load_record()["mount"])
        self.assertNotIn("stale_unmount_attempt", drives.load_record())
        changed = drives.dispatch(["mode", "rw", "--mountpoint", "/Volumes/Fixture-Writable"])
        self.assertEqual(changed["access_mode"], "rw")
        self.assertEqual(changed["selection"]["mountpoint"], "/Volumes/Fixture-Writable")
        self.assertEqual(drives.load_record()["selection"]["partition_uuid"], self.selection["partition_uuid"])
        status = drives.dispatch(["status"])
        self.assertFalse(status["connected"])
        self.assertFalse(status["mounted"])
        self.assertEqual(status["access_mode"], "rw")
        self.signature.assert_not_called()

    def test_uncertain_close_retains_intent_and_never_repeats_teardown(self):
        self.run.side_effect = RuntimeError("response lost")
        with self.assertRaisesRegex(RuntimeError, "completion is unknown"):
            drives.dispatch(["close"])
        self.assertIn("stale_unmount_attempt", drives.load_record())
        with self.assertRaisesRegex(RuntimeError, "no teardown repeated"):
            drives.dispatch(["close"])
        self.run.assert_called_once()
        self.live["mounted"] = False
        self.assertTrue(drives.dispatch(["close"])["closed"])
        self.run.assert_called_once()

    def test_changed_mount_identity_prevents_disconnected_close(self):
        self.live["id"] = 88
        with self.assertRaisesRegex(RuntimeError, "ownership receipt"):
            drives.dispatch(["close"])
        self.run.assert_not_called()

    def test_identity_rechecked_after_intent_is_saved(self):
        save = drives.save_record
        def changed(record):
            save(record)
            self.live["id"] = 88
        with patch.object(drives, "save_record", side_effect=changed), self.assertRaisesRegex(RuntimeError, "ownership receipt"):
            drives.dispatch(["close"])
        self.assertIn("stale_unmount_attempt", drives.load_record())
        self.run.assert_not_called()

    def test_mode_change_refuses_owned_or_active_disconnected_mount(self):
        with self.assertRaisesRegex(RuntimeError, "Close the saved mount"):
            drives.dispatch(["mode", "rw"])
        self.assertEqual(drives.access_mode(drives.load_record()["selection"]), "ro")
        self.run.assert_not_called()

    def test_only_missing_partition_error_allows_offline_close(self):
        self.locate.side_effect = RuntimeError("Duplicate partition UUID")
        with self.assertRaisesRegex(RuntimeError, "Duplicate"):
            drives.dispatch(["close"])
        self.run.assert_not_called()

    def test_retained_physical_export_blocks_open_before_device_access(self):
        directory = self.state / "storage"
        directory.mkdir()
        state = directory / "state.json"
        for contents in (json.dumps({"source_mode": "physical", "access_mode": "rw"}), "not JSON", "[]"):
            state.write_text(contents)
            self.locate.reset_mock()
            with self.assertRaisesRegex(RuntimeError, "(export|storage state)"):
                drives.dispatch(["open"])
            self.locate.assert_not_called()
        state.write_text(json.dumps({"source_mode": "image"}))
        drives.require_no_physical_export()
