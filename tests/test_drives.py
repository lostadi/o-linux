"""Drive workflow behavior against fake metadata and command boundaries."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from olinux import drives


PARTITION_UUID = "11111111-2222-3333-4444-555555555555"
FILESYSTEM_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def disk_fixture(number=9, *, uuid=PARTITION_UUID, partition_bytes=1000, whole_bytes=2000, internal=False):
    whole = f"disk{number}"
    partition = whole + "s3"
    return {
        whole: {"DeviceIdentifier": whole, "DeviceNode": "/dev/" + whole,
                "WholeDisk": True, "Internal": internal, "VirtualOrPhysical": "Physical",
                "TotalSize": whole_bytes, "MediaName": "Fixture", "BusProtocol": "USB"},
        partition: {"DeviceIdentifier": partition, "DeviceNode": "/dev/" + partition,
                    "WholeDisk": False, "Internal": internal, "ParentWholeDisk": whole,
                    "DiskUUID": uuid, "VolumeUUID": FILESYSTEM_UUID, "TotalSize": partition_bytes},
    }


class DriveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "state"
        self.mountpoint = "/Volumes/Fixture-ReadOnly"
        self.metadata = disk_fixture()
        self.info = patch.object(drives, "disk_info", side_effect=self.disk_info).start()
        self.addCleanup(patch.stopall)
        patch.object(drives, "platform").start().system.return_value = "Darwin"
        patch.object(drives, "state_dir", return_value=self.state).start()
        patch.object(drives, "load_config", return_value={"provider": "multipass", "host_home": str(Path(self.temporary.name)), "host_user": "fixture"}).start()
        patch.object(drives, "command", return_value="/tools/anylinuxfs").start()

    def disk_info(self, value):
        return copy.deepcopy(self.metadata[str(value).removeprefix("/dev/")])

    def selection(self):
        return drives.selection_for("/dev/disk9s3", self.mountpoint)

    def identifiers(self):
        return ["/dev/" + name for name, info in self.metadata.items() if not info["WholeDisk"]]

    def record(self, mount=None):
        record = {"version": 1, "selection": self.selection(), "mount": mount}
        drives.save_record(record)
        return record

    def evidence(self):
        guest_mount = "/mnt/" + Path(self.mountpoint).name
        return {
            "status": f"disk9s3 on {self.mountpoint} (ext4, ro, noload, mounted by fixture) VM[cpus: 1, ram: 512 MiB]",
            "host": f"disk9s3.local:{guest_mount} on {self.mountpoint} (nfs, read-only, noowners)\n",
            ("cat", "/proc/mounts"): f"/dev/vda {guest_mount} ext4 ro,relatime,norecovery 0 0\n",
            ("cat", "/sys/class/block/vda/ro"): "1",
            ("cat", "/etc/exports"): f'"{guest_mount}" *(ro,no_subtree_check,all_squash)\n',
            ("blkid", "-p", "-o", "export", "/dev/vda"): f"UUID={FILESYSTEM_UUID}\nTYPE=ext4\n",
        }

    @contextlib.contextmanager
    def verified_environment(self, evidence=None):
        evidence = evidence or self.evidence()
        def external(argv, **kwargs):
            if argv == ["/sbin/mount"]:
                return SimpleNamespace(returncode=0, stdout=evidence["host"], stderr="")
            if argv == ["/tools/anylinuxfs", "--version"]:
                return SimpleNamespace(returncode=0, stdout="anylinuxfs 0.20.1\n", stderr="")
            raise AssertionError("Unexpected operation: " + repr(argv))
        with patch.object(drives, "run", side_effect=external) as run, \
                patch.object(drives, "any_status", return_value=evidence["status"]), \
                patch.object(drives, "guest_text", side_effect=lambda app, point, argv: evidence[tuple(argv)]), \
                patch.object(drives.os.path, "ismount", return_value=True), \
                patch.object(drives, "mount_id", return_value=77), \
                patch.object(drives, "signature", return_value=FILESYSTEM_UUID), \
                patch.object(drives, "require_sudo"), \
                patch.object(drives, "locate", return_value="/dev/disk9s3"):
            yield run

    def test_selection_retains_stable_identity_instead_of_disk_number(self):
        selected = self.selection()
        self.assertEqual(selected["partition_uuid"].lower(), PARTITION_UUID)
        self.assertEqual(selected["partition_bytes"], 1000)
        self.assertEqual(selected["whole_bytes"], 2000)
        self.assertEqual(selected["filesystem"], "ext4")
        self.assertEqual(selected["mountpoint"], self.mountpoint)

    def test_locate_reidentifies_uuid_after_bsd_node_changes(self):
        selected = self.selection()
        self.metadata = disk_fixture(12)
        with patch.object(drives, "inventory", return_value=self.identifiers()):
            self.assertEqual(drives.locate(selected), "/dev/disk12s3")

    def test_missing_or_duplicate_uuid_is_refused(self):
        selected = self.selection()
        for metadata in (disk_fixture(12, uuid="66666666-7777-8888-9999-000000000000"),
                         {**disk_fixture(12), **disk_fixture(13)}):
            with self.subTest(identifiers=list(metadata)):
                self.metadata = metadata
                with patch.object(drives, "inventory", return_value=self.identifiers()):
                    with self.assertRaises((RuntimeError, ValueError)):
                        drives.locate(selected)

    def test_changed_partition_or_whole_size_is_refused(self):
        selected = self.selection()
        for metadata in (disk_fixture(12, partition_bytes=999), disk_fixture(12, whole_bytes=2001)):
            with self.subTest(metadata=metadata):
                self.metadata = metadata
                with patch.object(drives, "inventory", return_value=self.identifiers()):
                    with self.assertRaises((RuntimeError, ValueError)):
                        drives.locate(selected)

    def test_internal_drive_is_refused_at_selection_and_reidentification(self):
        selected = self.selection()
        self.metadata = disk_fixture(internal=True)
        with self.assertRaises((RuntimeError, ValueError)):
            self.selection()
        self.metadata = disk_fixture(12, internal=True)
        with patch.object(drives, "inventory", return_value=self.identifiers()):
            with self.assertRaises((RuntimeError, ValueError)):
                drives.locate(selected)

    def test_select_only_records_metadata_without_privileged_or_mount_operations(self):
        with patch.object(drives, "run", side_effect=AssertionError("unexpected external command")), \
                patch.object(drives, "require_sudo", side_effect=AssertionError("selection does not need sudo")), \
                patch.object(drives, "any_status", side_effect=AssertionError("selection does not inspect guest state")):
            result = drives.dispatch(["select", "/dev/disk9s3", "--mountpoint", self.mountpoint, "--filesystem", "ext4"])
        self.assertIsInstance(result, dict)
        self.assertFalse(result["mount_performed"])
        self.assertNotIn("mounted", result)
        record = drives.load_record()
        self.assertEqual(record["selection"]["partition_uuid"].lower(), PARTITION_UUID)
        self.assertIsNone(record["mount"])

    def test_verification_requires_every_read_only_and_identity_layer(self):
        evidence = self.evidence()
        selected = self.selection()
        with self.verified_environment(evidence):
            receipt = drives.verify_mount("/tools/anylinuxfs", selected, "/dev/disk9s3")
        self.assertTrue(receipt["read_only"])
        self.assertEqual(receipt["filesystem_uuid"], FILESYSTEM_UUID)
        self.assertEqual(receipt["host_mount_id"], 77)
        changes = [
            ("status", evidence["status"].replace("ro, noload", "rw")),
            ("host", evidence["host"].replace("read-only", "read-write")),
            ("host", evidence["host"].replace("disk9s3.local", "different.local")),
            (("cat", "/proc/mounts"), evidence[("cat", "/proc/mounts")].replace("ro,relatime,norecovery", "rw,relatime,norecovery")),
            (("cat", "/proc/mounts"), evidence[("cat", "/proc/mounts")].replace("ro,relatime,norecovery", "ro,relatime")),
            (("cat", "/sys/class/block/vda/ro"), "0"),
            (("cat", "/etc/exports"), evidence[("cat", "/etc/exports")].replace("*(ro,", "*(rw,")),
            (("blkid", "-p", "-o", "export", "/dev/vda"), "UUID=66666666-7777-8888-9999-000000000000\nTYPE=ext4\n"),
        ]
        for key, value in changes:
            with self.subTest(layer=key, value=value):
                changed = dict(evidence)
                changed[key] = value
                with self.verified_environment(changed), self.assertRaises(RuntimeError):
                    drives.verify_mount("/tools/anylinuxfs", selected, "/dev/disk9s3")

    def test_open_reuses_verified_mount_without_remounting(self):
        self.record()
        with self.verified_environment() as run:
            result = drives.dispatch(["open"])
        self.assertTrue(result["read_only"])
        self.assertEqual(drives.load_record()["mount"]["host_mount_id"], 77)
        self.assertFalse(any("unmount" in call.args[0] for call in run.call_args_list))

    def test_conflicting_active_mount_is_not_stopped_or_replaced(self):
        original = self.record()
        evidence = self.evidence()
        evidence["status"] = evidence["status"].replace("disk9s3 on", "disk8s3 on")
        with self.verified_environment(evidence) as run, self.assertRaises(RuntimeError):
            drives.dispatch(["open"])
        self.assertEqual(drives.load_record(), original)
        run.assert_not_called()

    def test_missing_cached_authorization_has_bounded_error(self):
        self.record()
        with patch.object(drives, "locate", return_value="/dev/disk9s3"), \
                patch.object(drives, "any_status", return_value=self.evidence()["status"]), \
                patch.object(drives, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="password required")) as run, \
                contextlib.redirect_stderr(io.StringIO()) as errors, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(drives.main(["open"]), 1)
        self.assertIn("sudo -v", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())
        self.assertEqual(run.call_args.args[0], ["/usr/bin/sudo", "-n", "/usr/bin/true"])

    def test_uncertain_mount_attempt_retains_record_and_never_unmounts(self):
        self.record()
        observed = []
        def external(argv, **kwargs):
            observed.append(argv)
            if "/tools/anylinuxfs" in argv:
                record = drives.load_record()
                self.assertEqual(record["mount_attempt"]["device"], "/dev/disk9s3")
                self.assertEqual(record["selection"]["filesystem_uuid"], FILESYSTEM_UUID)
                raise RuntimeError("mount completion unknown")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(drives, "locate", return_value="/dev/disk9s3"), \
                patch.object(drives, "any_status", return_value=""), \
                patch.object(drives, "require_sudo"), \
                patch.object(drives, "signature", return_value=FILESYSTEM_UUID), \
                patch.object(drives.os.path, "ismount", return_value=False), \
                patch.object(drives, "run", side_effect=external), \
                self.assertRaisesRegex(RuntimeError, "completion unknown"):
            drives.dispatch(["open"])
        command = next(argv for argv in observed if "/tools/anylinuxfs" in argv)
        self.assertEqual(command[:5], ["/usr/bin/sudo", "-n", "/tools/anylinuxfs", "disk9s3", self.mountpoint])
        self.assertEqual(command[command.index("-o") + 1], "ro,noload")
        self.assertEqual(command[command.index("-n", 3) + 1], "ro")
        self.assertEqual(command[command.index("-w") + 1], "false")
        self.assertFalse(any("unmount" in argv for argv in observed))
        self.assertIn("mount_attempt", drives.load_record())
        with patch.object(drives, "locate", return_value="/dev/disk9s3"), \
                patch.object(drives, "any_status", return_value=""), \
                patch.object(drives, "require_sudo"), \
                patch.object(drives, "run", side_effect=AssertionError("must not repeat an uncertain mount")), \
                self.assertRaisesRegex(RuntimeError, "completion is unknown"):
            drives.dispatch(["open"])
        with self.assertRaisesRegex(RuntimeError, "prior mount is pending"):
            drives.dispatch(["select", "/dev/disk9s3"])
        self.assertIn("mount_attempt", drives.load_record())

    def test_close_refuses_unowned_mount_and_retains_uncertain_completion(self):
        self.record()
        with self.verified_environment() as run, self.assertRaisesRegex(RuntimeError, "ownership receipt"):
            drives.dispatch(["close"])
        self.assertFalse(any("unmount" in call.args[0] for call in run.call_args_list))
        with self.verified_environment():
            receipt = drives.dispatch(["open"])
        original = drives.load_record()
        with self.verified_environment() as run:
            previous = run.side_effect
            def external(argv, **kwargs):
                if "unmount" in argv:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return previous(argv, **kwargs)
            run.side_effect = external
            with self.assertRaisesRegex(RuntimeError, "completion is uncertain"):
                drives.dispatch(["close"])
        self.assertEqual(drives.load_record(), original)
        self.assertEqual(drives.load_record()["mount"]["host_mount_id"], receipt["host_mount_id"])

    def test_stale_guest_identity_is_rejected_without_previously_saved_fs_uuid(self):
        selected = self.selection()
        selected["filesystem_uuid"] = None
        with self.verified_environment(), \
                patch.object(drives, "signature", return_value="66666666-7777-8888-9999-000000000000"), \
                self.assertRaisesRegex(RuntimeError, "filesystem identity"):
            drives.verify_mount("/tools/anylinuxfs", selected, "/dev/disk9s3")

    def test_existing_selected_mount_is_adopted_beside_unrelated_mount(self):
        self.record()
        evidence = self.evidence()
        evidence["status"] += "\ndisk8s2 on /Volumes/Unrelated (ext4, ro, noload)"
        with self.verified_environment(evidence):
            result = drives.dispatch(["open"])
        self.assertEqual(result["mountpoint"], self.mountpoint)
        self.assertEqual(drives.load_record()["mount"]["device"], "/dev/disk9s3")

    def test_close_only_requires_owned_mount_to_disappear(self):
        self.record()
        evidence = self.evidence()
        unrelated = "disk8s2 on /Volumes/Unrelated (ext4, ro, noload)"
        evidence["status"] += "\n" + unrelated
        with self.verified_environment(evidence):
            drives.dispatch(["open"])
        with self.verified_environment(evidence) as run, \
                patch.object(drives, "any_status", side_effect=[evidence["status"], unrelated]), \
                patch.object(drives.os.path, "ismount", side_effect=[True, False]):
            previous = run.side_effect
            def external(argv, **kwargs):
                if "unmount" in argv:
                    self.assertEqual(argv[4], self.mountpoint)
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return previous(argv, **kwargs)
            run.side_effect = external
            result = drives.dispatch(["close"])
        self.assertTrue(result["closed"])
        self.assertIsNone(drives.load_record()["mount"])

    def test_new_mount_can_start_beside_unrelated_mount(self):
        self.record()
        unrelated = "disk8s2 on /Volumes/Unrelated (ext4, ro, noload)"
        evidence = self.evidence()
        receipt = {"read_only": True, "device": "/dev/disk9s3", "mountpoint": self.mountpoint,
                   "filesystem_uuid": FILESYSTEM_UUID, "host_mount_id": 77}
        with patch.object(drives, "locate", return_value="/dev/disk9s3"), \
                patch.object(drives, "any_status", side_effect=[unrelated, unrelated, evidence["status"] + "\n" + unrelated]), \
                patch.object(drives, "require_sudo"), \
                patch.object(drives, "signature", return_value=FILESYSTEM_UUID), \
                patch.object(drives.os.path, "ismount", return_value=False), \
                patch.object(drives, "verify_mount", return_value=receipt) as verify, \
                patch.object(drives, "run", return_value=SimpleNamespace(returncode=0, stdout="anylinuxfs 0.20.1\n", stderr="")) as run:
            result = drives.dispatch(["open"])
        self.assertTrue(result["read_only"])
        self.assertIn(unrelated, verify.call_args.args[3])
        self.assertFalse(any("unmount" in call.args[0] for call in run.call_args_list))
        self.assertNotIn("mount_attempt", drives.load_record())

    def test_unknown_status_and_stderr_are_not_treated_as_free_mount_space(self):
        with self.assertRaisesRegex(RuntimeError, "Unrecognized"):
            drives.parse_status("VM is still starting")
        with patch.object(drives, "run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="instance still running")), \
                self.assertRaisesRegex(RuntimeError, "incomplete mount"):
            drives.any_status("/tools/anylinuxfs")

    def test_host_mount_parses_on_in_destination_and_octal_spaces(self):
        self.mountpoint = "/Volumes/Copy on Backup"
        evidence = self.evidence()
        evidence["host"] = evidence["host"].replace("Copy on Backup", "Copy\\040on\\040Backup")
        evidence[("cat", "/proc/mounts")] = evidence[("cat", "/proc/mounts")].replace("Copy on Backup", "Copy\\040on\\040Backup")
        with self.verified_environment(evidence):
            receipt = drives.verify_mount("/tools/anylinuxfs", self.selection(), "/dev/disk9s3")
        self.assertEqual(receipt["mountpoint"], self.mountpoint)
        self.assertEqual(receipt["host_source"], "disk9s3.local:/mnt/Copy on Backup")


if __name__ == "__main__":
    unittest.main()
