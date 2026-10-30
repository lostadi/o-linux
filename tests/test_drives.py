"""Drive workflow behavior against fake metadata and command boundaries."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import plistlib
import stat
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


def inactive_media_fixture():
    """ioreg emits overlapping roots and intermediate non-IOMedia children."""
    roots = []
    for number, base_id, state, minor in ((9, 100, 1, 22), (12, 200, 30, 23)):
        partition = {"BSD Major": 1, "BSD Minor": minor, "BSD Name": f"disk{number}s3",
                     "IOObjectClass": "IOMedia", "IORegistryEntryID": base_id + 3,
                     "IOServiceState": state, "Size": 1000, "UUID": PARTITION_UUID.upper(), "Whole": False,
                     "IORegistryEntryChildren": [{"IOObjectClass": "IOMediaBSDClient",
                                                  "IORegistryEntryID": base_id + 4, "IOServiceState": state}]}
        whole = {"BSD Major": 1, "BSD Minor": 20, "BSD Name": f"disk{number}",
                 "IOObjectClass": "IOMedia", "IORegistryEntryID": base_id,
                 "IOServiceState": state, "Size": 2000, "Whole": True,
                 "IORegistryEntryChildren": [{"IOObjectClass": "IOGUIDPartitionScheme",
                                              "IORegistryEntryID": base_id + 1, "IOServiceState": state,
                                              "IORegistryEntryChildren": [partition]}]}
        roots.extend([whole, copy.deepcopy(partition)])
    return roots


def change_media(document, name, key, value):
    for node in document:
        if node.get("BSD Name") == name:
            node[key] = value
        change_media(node.get("IORegistryEntryChildren", []), name, key, value)


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

    def selection(self, mode="ro"):
        return drives.selection_for("/dev/disk9s3", self.mountpoint, mode)

    def identifiers(self):
        return ["/dev/" + name for name, info in self.metadata.items() if not info["WholeDisk"]]

    def record(self, mount=None, mode="ro"):
        record = {"version": 1, "selection": self.selection(mode), "mount": mount}
        drives.save_record(record)
        return record

    def evidence(self, mode="ro"):
        guest_mount = "/mnt/" + Path(self.mountpoint).name
        result = {
            "status": f"disk9s3 on {self.mountpoint} (ext4, ro, noload, mounted by fixture) VM[cpus: 1, ram: 512 MiB]",
            "host": f"disk9s3.local:{guest_mount} on {self.mountpoint} (nfs, read-only, noowners)\n",
            ("cat", "/proc/mounts"): f"/dev/vda {guest_mount} ext4 ro,relatime,norecovery 0 0\n",
            ("cat", "/sys/class/block/vda/ro"): "1",
            ("cat", "/etc/exports"): f'"{guest_mount}" *(ro,no_subtree_check,all_squash)\n',
            ("blkid", "-p", "-o", "export", "/dev/vda"): f"UUID={FILESYSTEM_UUID}\nTYPE=ext4\n",
        }
        if mode == "rw":
            result["status"] = result["status"].replace("ro, noload", "rw")
            result["host"] = result["host"].replace("read-only, ", "")
            result[("cat", "/proc/mounts")] = result[("cat", "/proc/mounts")].replace("ro,relatime,norecovery", "rw,relatime")
            result[("cat", "/sys/class/block/vda/ro")] = "0"
            result[("cat", "/etc/exports")] = result[("cat", "/etc/exports")].replace("(ro,", "(rw,")
        return result

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

    def test_new_selections_default_to_rw_with_explicit_read_only_available(self):
        for options, expected in (([], "rw"), (["--read-write"], "rw"), (["--read-only"], "ro")):
            with self.subTest(options=options), patch.object(drives, "run") as external:
                result = drives.dispatch(["select", "/dev/disk9s3", *options])
            self.assertEqual(result["selection"]["access_mode"], expected)
            self.assertEqual(result["selection"]["mountpoint"], "/Volumes/Linux-Drive")
            external.assert_not_called()

    def test_legacy_mode_is_read_only_without_rewriting_and_invalid_mode_is_rejected(self):
        original = self.record()
        original["selection"].pop("access_mode")
        drives.save_record(original)
        before = (self.state / "drives.json").read_bytes()
        self.assertEqual(drives.access_mode(drives.load_record()["selection"]), "ro")
        self.assertEqual((self.state / "drives.json").read_bytes(), before)
        for invalid in ("", None, True, "read-write", [], {}):
            with self.subTest(mode=invalid):
                original["selection"]["access_mode"] = invalid
                drives.save_record(original)
                with self.assertRaisesRegex(RuntimeError, "access_mode"):
                    drives.load_record()

    def test_mode_changes_only_unmounted_metadata_and_preserves_identity(self):
        original = self.record()
        with patch.object(drives, "inventory", side_effect=self.identifiers), \
                patch.object(drives, "any_status", return_value=""), \
                patch.object(drives.os.path, "ismount", return_value=False), \
                patch.object(drives, "require_sudo") as sudo, \
                patch.object(drives, "run") as external:
            for mode in ("rw", "ro"):
                result = drives.dispatch(["mode", mode])
                self.assertEqual(result["access_mode"], mode)
                self.assertFalse(result["mount_performed"])
                self.assertEqual(result["selection"], dict(original["selection"], access_mode=mode))
        sudo.assert_not_called()
        external.assert_not_called()

    def test_mode_change_refuses_saved_live_or_pending_mount_without_state_change(self):
        cases = ("receipt", "active", "host", "partition", "mount_attempt", "stale_unmount_attempt")
        for case in cases:
            with self.subTest(case=case):
                original = self.record({"mounted": True} if case == "receipt" else None)
                if case in ("mount_attempt", "stale_unmount_attempt"):
                    original[case] = {"operation_id": "fixture"}
                    drives.save_record(original)
                self.metadata["disk9s3"]["MountPoint"] = "/Volumes/Other" if case == "partition" else ""
                with patch.object(drives, "inventory", side_effect=self.identifiers), \
                        patch.object(drives, "any_status", return_value=self.evidence()["status"] if case == "active" else ""), \
                        patch.object(drives.os.path, "ismount", return_value=case == "host"), \
                        patch.object(drives, "run") as external, \
                        self.assertRaisesRegex(RuntimeError, "Close the saved mount"):
                    drives.dispatch(["mode", "rw"])
                self.assertEqual(drives.load_record(), original)
                external.assert_not_called()

    def test_mode_arguments_cannot_change_access_during_open(self):
        with contextlib.redirect_stderr(io.StringIO()), patch.object(drives, "operate") as operate:
            for arguments in (["mode"], ["mode", "unknown"], ["open", "--read-write"],
                              ["mode", "rw", "--read-only"], ["select", "/dev/disk9s3", "--read-only", "--read-write"]):
                with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                    drives.dispatch(arguments)
        operate.assert_not_called()

    def test_reselection_cannot_discard_saved_mount_receipt(self):
        original = self.record({"mounted": True})
        with self.assertRaisesRegex(RuntimeError, "Close the saved mount"):
            drives.dispatch(["select", "/dev/disk9s3", "--read-write"])
        self.assertEqual(drives.load_record(), original)

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

    def test_rw_verification_requires_all_layers_and_journal_replay_enabled(self):
        evidence = self.evidence("rw")
        selected = self.selection("rw")
        with self.verified_environment(evidence):
            receipt = drives.verify_mount("/tools/anylinuxfs", selected, "/dev/disk9s3")
        self.assertFalse(receipt["read_only"])
        self.assertEqual(receipt["access_mode"], "rw")
        changes = [
            ("status", evidence["status"].replace("ext4, rw", "ext4, ro")),
            ("status", evidence["status"].replace("ext4, rw", "ext4, rw, noload")),
            ("status", evidence["status"].replace("ext4, rw", "ext4, rw, norecovery")),
            ("host", evidence["host"].replace("nfs, noowners", "nfs, read-only, noowners")),
            ("host", evidence["host"].replace("nfs, noowners", "apfs, noowners")),
            (("cat", "/proc/mounts"), evidence[("cat", "/proc/mounts")].replace("rw,relatime", "ro,relatime")),
            (("cat", "/proc/mounts"), evidence[("cat", "/proc/mounts")].replace("rw,relatime", "rw,relatime,noload")),
            (("cat", "/proc/mounts"), evidence[("cat", "/proc/mounts")].replace("rw,relatime", "rw,relatime,norecovery")),
            (("cat", "/sys/class/block/vda/ro"), "1"),
            (("cat", "/etc/exports"), evidence[("cat", "/etc/exports")].replace("(rw,", "(ro,")),
            (("cat", "/etc/exports"), evidence[("cat", "/etc/exports")].replace("(rw,", "(rw,ro,")),
        ]
        for key, value in changes:
            with self.subTest(layer=key, value=value):
                changed = dict(evidence)
                changed[key] = value
                with self.verified_environment(changed), self.assertRaises(RuntimeError):
                    drives.verify_mount("/tools/anylinuxfs", selected, "/dev/disk9s3")

    def test_rw_open_reuses_verified_mount_and_close_requires_same_mode_receipt(self):
        self.record(mode="rw")
        evidence = self.evidence("rw")
        with self.verified_environment(evidence) as run:
            result = drives.dispatch(["open"])
        self.assertFalse(result["read_only"])
        self.assertFalse(any("unmount" in call.args[0] for call in run.call_args_list))
        original = drives.load_record()
        wrong = copy.deepcopy(original)
        wrong["mount"]["access_mode"] = "ro"
        wrong["mount"]["read_only"] = True
        drives.save_record(wrong)
        with self.verified_environment(evidence) as run, self.assertRaisesRegex(RuntimeError, "ownership receipt"):
            drives.dispatch(["close"])
        self.assertFalse(any("unmount" in call.args[0] for call in run.call_args_list))
        drives.save_record(original)
        with self.verified_environment(evidence) as run, \
                patch.object(drives, "any_status", side_effect=[evidence["status"], ""]), \
                patch.object(drives.os.path, "ismount", side_effect=[True, False]):
            previous = run.side_effect
            run.side_effect = lambda argv, **kwargs: (SimpleNamespace(returncode=0, stdout="", stderr="")
                                                      if "unmount" in argv else previous(argv, **kwargs))
            result = drives.dispatch(["close"])
        self.assertTrue(result["closed"])
        self.assertIsNone(drives.load_record()["mount"])

    def test_legacy_owned_read_only_mount_can_close_before_mode_change(self):
        self.record()
        with self.verified_environment():
            drives.dispatch(["open"])
        legacy = drives.load_record()
        legacy["selection"].pop("access_mode")
        legacy["mount"].pop("access_mode")
        drives.save_record(legacy)
        with self.verified_environment() as run, \
                patch.object(drives, "any_status", side_effect=[self.evidence()["status"], ""]), \
                patch.object(drives.os.path, "ismount", side_effect=[True, False]):
            previous = run.side_effect
            run.side_effect = lambda argv, **kwargs: (SimpleNamespace(returncode=0, stdout="", stderr="")
                                                      if "unmount" in argv else previous(argv, **kwargs))
            self.assertTrue(drives.dispatch(["close"])["closed"])
        self.assertEqual(drives.access_mode(drives.load_record()["selection"]), "ro")

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

    def test_interactive_authorization_runs_in_caller_terminal_and_rechecks(self):
        for stream in (drives.sys.stdin, drives.sys.stdout, drives.sys.stderr):
            patch.object(stream, "isatty", return_value=True).start()
        with patch.object(drives, "run", side_effect=[SimpleNamespace(returncode=1),
                                                     SimpleNamespace(returncode=0)]) as probe, \
                patch.object(drives.subprocess, "call", return_value=0) as authenticate:
            drives.require_sudo()
        authenticate.assert_called_once_with(["/usr/bin/sudo", "-v"])
        self.assertEqual(probe.call_count, 2)

    def test_authorization_never_prompts_captured_streams_and_does_not_accept_failed_auth(self):
        for terminals, authenticate_code, final_code in (((True, False, True), 0, 0),
                                                         ((True, True, True), 1, 0),
                                                         ((True, True, True), 0, 1)):
            with self.subTest(terminals=terminals, authenticate_code=authenticate_code, final_code=final_code), \
                    contextlib.ExitStack() as stack:
                for stream, terminal in zip((drives.sys.stdin, drives.sys.stdout, drives.sys.stderr), terminals):
                    stack.enter_context(patch.object(stream, "isatty", return_value=terminal))
                stack.enter_context(patch.object(drives, "run", side_effect=[SimpleNamespace(returncode=1),
                                                                            SimpleNamespace(returncode=final_code)]))
                authenticate = stack.enter_context(patch.object(drives.subprocess, "call", return_value=authenticate_code))
                with self.assertRaisesRegex(RuntimeError, "macOS authorization required"):
                    drives.require_sudo()
                self.assertEqual(authenticate.call_count, int(all(terminals)))

    def test_cached_authorization_never_prompts(self):
        with patch.object(drives, "run", return_value=SimpleNamespace(returncode=0)), \
                patch.object(drives.subprocess, "call") as authenticate:
            drives.require_sudo()
        authenticate.assert_not_called()

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

    @contextlib.contextmanager
    def stale_environment(self, mode="ro"):
        evidence = self.evidence(mode)
        owned = {"mounted": True, "read_only": mode == "ro", "access_mode": mode, "device": "/dev/disk9s3",
                 "mountpoint": self.mountpoint, "partition_uuid": PARTITION_UUID,
                 "filesystem_uuid": FILESYSTEM_UUID, "filesystem": "ext4",
                 "host_source": "disk9s3.local:/mnt/" + Path(self.mountpoint).name,
                 "host_mount_id": 77}
        self.record(owned, mode)
        self.metadata = disk_fixture(12)
        state = {"phase": "stale", "failure": None, "host_mount_id": 77,
                 "present_nodes": set(), "inaccessible_nodes": set(), "events": [],
                 "inactive_nodes": False, "registry": inactive_media_fixture(),
                 "active_disks": ["disk12", "disk12s3"],
                 "unrelated": "disk8s2 on /Volumes/Unrelated (ext4, ro, noload)",
                 "evidence": evidence}
        original_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            if str(path) in ("/dev/disk9s3", "/dev/rdisk9s3"):
                if str(path) in state["inaccessible_nodes"]:
                    raise PermissionError(str(path))
                if state["inactive_nodes"]:
                    mode = stat.S_IFCHR if str(path) == "/dev/rdisk9s3" else stat.S_IFBLK
                    return SimpleNamespace(st_mode=mode | 0o640, st_rdev=os.makedev(1, 22))
                if str(path) not in state["present_nodes"]:
                    raise FileNotFoundError(str(path))
                return SimpleNamespace(st_mode=0)
            return original_lstat(path, *args, **kwargs)

        def status(app):
            row = evidence["status"] if state["phase"] == "stale" else (
                evidence["status"].replace("disk9s3", "disk12s3") if state["phase"] == "fresh" else "")
            return "\n".join(filter(None, (row, state["unrelated"])))

        def external(argv, **kwargs):
            state["events"].append(argv)
            if argv == ["/usr/sbin/diskutil", "list", "-plist"]:
                return SimpleNamespace(returncode=0, stdout=plistlib.dumps({"AllDisks": state["active_disks"]}).decode(), stderr="")
            if argv == ["/usr/sbin/ioreg", "-r", "-c", "IOMedia", "-a"]:
                return SimpleNamespace(returncode=0, stdout=plistlib.dumps(state["registry"]).decode(), stderr="")
            if argv == ["/sbin/mount"]:
                text = evidence["host"]
                if state["phase"] == "fresh":
                    text = text.replace("disk9s3", "disk12s3")
                return SimpleNamespace(returncode=0, stdout=text, stderr="")
            if "unmount" in argv:
                self.assertEqual(argv, ["/usr/bin/sudo", "-n", "/tools/anylinuxfs", "unmount",
                                        self.mountpoint, "--wait-for-vm=20"])
                saved = drives.load_record()
                self.assertEqual(saved["stale_unmount_attempt"]["host_mount_id"], 77)
                self.assertEqual(saved["mount"]["device"], "/dev/disk9s3")
                self.assertTrue(any(item == ["signature", "/dev/disk12s3"] for item in state["events"]))
                if state["failure"] == "busy":
                    raise RuntimeError("unmount busy")
                if state["failure"] != "still-mounted":
                    state["phase"] = "absent"
                if state["failure"] == "lost-response":
                    raise RuntimeError("unmount response lost")
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[:4] == ["/usr/bin/sudo", "-n", "/tools/anylinuxfs", "disk12s3"]:
                saved = drives.load_record()
                self.assertIsNone(saved["mount"])
                self.assertNotIn("stale_unmount_attempt", saved)
                self.assertEqual(saved["mount_attempt"]["device"], "/dev/disk12s3")
                self.assertEqual(state["phase"], "absent")
                state["phase"] = "fresh"
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv[:4] == ["/usr/bin/sudo", "-n", "/bin/mkdir", "-p"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if argv == ["/tools/anylinuxfs", "--version"]:
                return SimpleNamespace(returncode=0, stdout="anylinuxfs 0.20.1\n", stderr="")
            raise AssertionError("Unexpected operation: " + repr(argv))

        def signature(device, expected):
            self.assertEqual((device, expected), ("/dev/disk12s3", FILESYSTEM_UUID))
            state["events"].append(["signature", device])
            return FILESYSTEM_UUID

        with patch.object(drives, "inventory", side_effect=self.identifiers), \
                patch.object(drives.os, "lstat", side_effect=lstat), \
                patch.object(drives, "any_status", side_effect=status) as statuses, \
                patch.object(drives, "run", side_effect=external), \
                patch.object(drives.os.path, "ismount", side_effect=lambda path: state["phase"] != "absent"), \
                patch.object(drives, "mount_id", side_effect=lambda path: state["host_mount_id"]), \
                patch.object(drives, "require_sudo") as sudo, \
                patch.object(drives, "signature", side_effect=signature) as physical, \
                patch.object(drives, "guest_text", side_effect=lambda app, point, argv: evidence[tuple(argv)]):
            state.update(statuses=statuses, sudo=sudo, signature=physical)
            yield state

    def test_stale_status_reports_renumbered_drive_without_probes_or_mutation(self):
        with self.stale_environment() as state:
            before = (self.state / "drives.json").read_bytes()
            result = drives.dispatch(["status"])
            self.assertTrue(result["stale"])
            self.assertFalse(result["usable"])
            self.assertEqual(result["current_device"], "/dev/disk12s3")
            self.assertEqual(result["previous_device"], "/dev/disk9s3")
            self.assertEqual(result["old_device_state"], "absent")
            self.assertIn("o-linux drive open", result["reopen_hint"])
            self.assertEqual((self.state / "drives.json").read_bytes(), before)
            state["signature"].assert_not_called()
            state["sudo"].assert_not_called()
            self.assertEqual(state["events"], [["/sbin/mount"]])

    def test_stale_open_detaches_only_exact_owned_instance_before_reopening(self):
        with self.stale_environment() as state:
            result = drives.dispatch(["open"])
            record = drives.load_record()
        self.assertTrue(result["read_only"])
        self.assertEqual(result["device"], "/dev/disk12s3")
        self.assertEqual(record["mount"]["device"], "/dev/disk12s3")
        self.assertNotIn("stale_unmount_attempt", record)
        self.assertNotIn("mount_attempt", record)
        self.assertEqual(sum("unmount" in item for item in state["events"]), 1)
        self.assertFalse(any("--force" in item for item in state["events"]))

    def test_stale_rw_mount_reconnects_with_rw_options_and_mode_bound_receipts(self):
        with self.stale_environment("rw") as state:
            status = drives.dispatch(["status"])
            self.assertEqual(status["access_mode"], "rw")
            self.assertFalse(status["read_only"])
            result = drives.dispatch(["open"])
            record = drives.load_record()
        self.assertEqual(result["access_mode"], "rw")
        self.assertFalse(result["read_only"])
        self.assertEqual(record["mount"]["access_mode"], "rw")
        self.assertEqual(sum("unmount" in item for item in state["events"]), 1)
        mount = next(item for item in state["events"] if item[:4] == ["/usr/bin/sudo", "-n", "/tools/anylinuxfs", "disk12s3"])
        self.assertEqual(mount[mount.index("-o") + 1], "rw")
        self.assertEqual(mount[mount.index("-n", 3) + 1], "rw")
        self.assertEqual(mount[mount.index("-w") + 1], "false")
        self.assertNotIn("ro,noload", mount)

    def test_rw_stale_retry_does_not_accept_a_pending_read_only_intent(self):
        with self.stale_environment("rw") as state:
            state["failure"] = "lost-response"
            with self.assertRaisesRegex(RuntimeError, "completion is unknown"):
                drives.dispatch(["open"])
            record = drives.load_record()
            self.assertEqual(record["stale_unmount_attempt"]["access_mode"], "rw")
            record["stale_unmount_attempt"]["access_mode"] = "ro"
            drives.save_record(record)
            with self.assertRaisesRegex(RuntimeError, "intent does not match"):
                drives.dispatch(["open"])
            self.assertEqual(sum("unmount" in item for item in state["events"]), 1)
            self.assertEqual(drives.load_record(), record)

    def test_rw_mount_uncertainty_retains_mode_and_never_retries_dispatch(self):
        self.record(mode="rw")
        observed = []
        def external(argv, **kwargs):
            observed.append(argv)
            if "/tools/anylinuxfs" in argv:
                self.assertEqual(drives.load_record()["mount_attempt"]["access_mode"], "rw")
                raise RuntimeError("mount result lost")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(drives, "locate", return_value="/dev/disk9s3"), \
                patch.object(drives, "any_status", return_value=""), \
                patch.object(drives, "require_sudo"), \
                patch.object(drives, "signature", return_value=FILESYSTEM_UUID), \
                patch.object(drives.os.path, "ismount", return_value=False), \
                patch.object(drives, "run", side_effect=external):
            with self.assertRaisesRegex(RuntimeError, "completion is uncertain"):
                drives.dispatch(["open"])
            with self.assertRaisesRegex(RuntimeError, "completion is unknown"):
                drives.dispatch(["open"])
        self.assertEqual(sum("/tools/anylinuxfs" in argv for argv in observed), 1)
        self.assertFalse(any("unmount" in argv for argv in observed))
        self.assertEqual(drives.load_record()["mount_attempt"]["access_mode"], "rw")

    def test_stale_receipt_mismatches_never_trigger_teardown(self):
        changes = [("partition_uuid", "66666666-7777-8888-9999-000000000000"),
                   ("filesystem_uuid", "66666666-7777-8888-9999-000000000000"),
                   ("host_source", "other.local:/mnt/Fixture-ReadOnly"), ("host_mount_id", 78),
                   ("mountpoint", "/Volumes/Other"), ("read_only", False), ("filesystem", "ext3")]
        for key, value in changes:
            with self.subTest(key=key), self.stale_environment() as state:
                record = drives.load_record()
                record["mount"][key] = value
                drives.save_record(record)
                with self.assertRaises(RuntimeError):
                    drives.dispatch(["open"])
                self.assertEqual(drives.load_record(), record)
                self.assertFalse(any("unmount" in item for item in state["events"]))
            self.metadata = disk_fixture()

    def test_existing_or_inaccessible_previous_nodes_prevent_teardown(self):
        for key, node in (("present_nodes", "/dev/disk9s3"), ("present_nodes", "/dev/rdisk9s3"),
                          ("inaccessible_nodes", "/dev/disk9s3")):
            with self.subTest(key=key, node=node), self.stale_environment() as state:
                state[key].add(node)
                with self.assertRaisesRegex(RuntimeError, "drive node"):
                    drives.dispatch(["open"])
                self.assertFalse(any("unmount" in item for item in state["events"]))
                self.assertNotIn("stale_unmount_attempt", drives.load_record())
            self.metadata = disk_fixture()

    def test_stale_mount_flags_and_host_source_must_match(self):
        for field, replacement in (("status", self.evidence()["status"].replace("ro, noload", "rw")),
                                   ("host", self.evidence()["host"].replace("disk9s3.local", "other.local"))):
            with self.subTest(field=field), self.stale_environment() as state:
                state["evidence"][field] = replacement
                with self.assertRaises(RuntimeError):
                    drives.dispatch(["open"])
                self.assertFalse(any("unmount" in item for item in state["events"]))
            self.metadata = disk_fixture()

    def test_current_physical_signature_mismatch_leaves_stale_mount_untouched(self):
        with self.stale_environment() as state:
            state["signature"].side_effect = RuntimeError("Filesystem signature UUID changed")
            with self.assertRaisesRegex(RuntimeError, "signature UUID changed"):
                drives.dispatch(["open"])
            self.assertFalse(any("unmount" in item for item in state["events"]))
            self.assertNotIn("stale_unmount_attempt", drives.load_record())

    def test_unknown_teardown_is_recorded_and_never_repeated_while_mount_remains(self):
        for failure in ("busy", "still-mounted"):
            with self.subTest(failure=failure), self.stale_environment() as state:
                state["failure"] = failure
                with self.assertRaisesRegex(RuntimeError, "completion is unknown"):
                    drives.dispatch(["open"])
                saved = drives.load_record()
                self.assertIn("stale_unmount_attempt", saved)
                self.assertEqual(saved["mount"]["device"], "/dev/disk9s3")
                with self.assertRaisesRegex(RuntimeError, "without repeating teardown"):
                    drives.dispatch(["open"])
                self.assertEqual(drives.load_record(), saved)
                self.assertEqual(sum("unmount" in item for item in state["events"]), 1)
                result = drives.dispatch(["status"])
                self.assertTrue(result["reconciliation_pending"])
            self.metadata = disk_fixture()

    def test_unknown_teardown_resumes_only_after_confirmed_disappearance(self):
        with self.stale_environment() as state:
            state["failure"] = "lost-response"
            with self.assertRaisesRegex(RuntimeError, "completion is unknown"):
                drives.dispatch(["open"])
            self.assertIn("stale_unmount_attempt", drives.load_record())
            result = drives.dispatch(["status"])
            self.assertFalse(result["mounted"])
            self.assertTrue(result["reconciliation_pending"])
            self.assertIn("stale_unmount_attempt", drives.load_record())
            result = drives.dispatch(["open"])
            self.assertTrue(result["read_only"])
            self.assertEqual(sum("unmount" in item for item in state["events"]), 1)
            self.assertNotIn("stale_unmount_attempt", drives.load_record())

    def test_failed_intent_write_prevents_teardown(self):
        with self.stale_environment() as state, \
                patch.object(drives, "save_record", side_effect=OSError("disk full")), \
                self.assertRaisesRegex(OSError, "disk full"):
            drives.dispatch(["open"])
        self.assertFalse(any("unmount" in item for item in state["events"]))
        self.assertNotIn("stale_unmount_attempt", drives.load_record())

    def test_ownership_changed_during_intent_write_prevents_teardown(self):
        with self.stale_environment() as state:
            save = drives.save_record
            def change_mount(record):
                save(record)
                state["host_mount_id"] = 99
            with patch.object(drives, "save_record", side_effect=change_mount), \
                    self.assertRaisesRegex(RuntimeError, "no teardown dispatched"):
                drives.dispatch(["open"])
            self.assertFalse(any("unmount" in item for item in state["events"]))
            self.assertIn("stale_unmount_attempt", drives.load_record())

    def test_pending_recovery_refreshes_status_after_physical_preflight(self):
        with self.stale_environment() as state:
            state["failure"] = "lost-response"
            with self.assertRaisesRegex(RuntimeError, "completion is unknown"):
                drives.dispatch(["open"])
            physical = state["signature"].side_effect
            def reappear(device, expected):
                result = physical(device, expected)
                state["phase"] = "stale"
                return result
            state["signature"].side_effect = reappear
            with self.assertRaisesRegex(RuntimeError, "without repeating teardown"):
                drives.dispatch(["open"])
            self.assertEqual(sum("unmount" in item for item in state["events"]), 1)
            self.assertIn("stale_unmount_attempt", drives.load_record())

    def test_inactive_retained_nodes_report_stale_and_reconcile_without_old_device_reads(self):
        with self.stale_environment() as state:
            state["inactive_nodes"] = True
            before = (self.state / "drives.json").read_bytes()
            result = drives.dispatch(["status"])
            self.assertEqual(result["old_device_state"], "inactive")
            self.assertTrue(result["stale"])
            self.assertFalse(result["usable"])
            self.assertEqual((self.state / "drives.json").read_bytes(), before)
            state["signature"].assert_not_called()
            state["sudo"].assert_not_called()
            result = drives.dispatch(["open"])
            self.assertTrue(result["read_only"])
            self.assertEqual(result["device"], "/dev/disk12s3")
            self.assertEqual(sum("unmount" in item for item in state["events"]), 1)
            self.assertFalse(any("/dev/disk9s3" in item or "/dev/rdisk9s3" in item for item in state["events"]))

    def test_inactive_media_requires_exact_state_identity_type_and_device_numbers(self):
        mutations = [("disk9s3", "IOServiceState", 30, "inactive state"),
                     ("disk9", "IOServiceState", 30, "inactive state"),
                     ("disk9s3", "IOServiceState", 3, "inactive state"),
                     ("disk9s3", "IOServiceState", 5, "inactive state"),
                     ("disk9s3", "IOServiceState", True, "inactive state"),
                     ("disk9s3", "UUID", "66666666-7777-8888-9999-000000000000", "identity"),
                     ("disk9s3", "Size", 999, "identity"), ("disk9", "Size", 1999, "identity"),
                     ("disk9s3", "Whole", True, "identity"), ("disk9", "Whole", False, "identity"),
                     ("disk9s3", "IOObjectClass", "Other", "inactive state"),
                     ("disk9s3", "BSD Major", 2, "device numbers"),
                     ("disk9s3", "BSD Minor", 23, "device numbers"),
                     ("disk9s3", "BSD Major", True, "device numbers")]
        for name, key, value, error in mutations:
            with self.subTest(name=name, key=key, value=value), self.stale_environment() as state:
                state["inactive_nodes"] = True
                change_media(state["registry"], name, key, value)
                with self.assertRaisesRegex(RuntimeError, error):
                    drives.dispatch(["open"])
                self.assertFalse(any("unmount" in item for item in state["events"]))
                self.assertNotIn("stale_unmount_attempt", drives.load_record())
            self.metadata = disk_fixture()

    def test_retained_node_pair_rejects_symlinks_nondevices_missing_and_reused_rdev(self):
        fixtures = [(stat.S_IFLNK, stat.S_IFCHR, 22, 22), (stat.S_IFREG, stat.S_IFCHR, 22, 22),
                    (stat.S_IFCHR, stat.S_IFBLK, 22, 22), (stat.S_IFBLK, stat.S_IFCHR, 22, 23),
                    (stat.S_IFBLK, None, 22, 22)]
        selected = self.selection()
        for block_type, raw_type, block_minor, raw_minor in fixtures:
            with self.subTest(fixture=(block_type, raw_type, block_minor, raw_minor)):
                def node(path):
                    mode, minor = (raw_type, raw_minor) if str(path).startswith("/dev/r") else (block_type, block_minor)
                    if mode is None:
                        raise FileNotFoundError(str(path))
                    return SimpleNamespace(st_mode=mode, st_rdev=os.makedev(1, minor))
                with patch.object(drives.os, "lstat", side_effect=node), \
                        patch.object(drives, "run") as external, \
                        self.assertRaisesRegex(RuntimeError, "matching block/raw pair"):
                    drives.previous_device_detached("/dev/disk9s3", selected)
                external.assert_not_called()

    def test_inactive_parent_and_partition_must_be_absent_from_active_inventory(self):
        for active_name in ("disk9", "disk9s3"):
            with self.subTest(active_name=active_name), self.stale_environment() as state:
                state["inactive_nodes"] = True
                state["active_disks"].append(active_name)
                with self.assertRaisesRegex(RuntimeError, "active disk inventory"):
                    drives.dispatch(["open"])
                self.assertFalse(any("unmount" in item for item in state["events"]))
            self.metadata = disk_fixture()

    def test_inactive_registry_rejects_ambiguous_entries_conflicting_copies_and_ancestry(self):
        for fault in ("duplicate-name", "conflicting-copy", "missing-parent", "wrong-parent", "multiple-parents", "reused-number"):
            with self.subTest(fault=fault), self.stale_environment() as state:
                state["inactive_nodes"] = True
                registry = state["registry"]
                if fault == "duplicate-name":
                    duplicate = copy.deepcopy(registry[1])
                    duplicate["IORegistryEntryID"] = 999
                    registry.append(duplicate)
                elif fault == "conflicting-copy":
                    registry[1]["IOServiceState"] = 30
                elif fault == "missing-parent":
                    registry[0]["IORegistryEntryChildren"] = []
                elif fault == "wrong-parent":
                    registry[2]["IORegistryEntryChildren"].append(registry[0]["IORegistryEntryChildren"].pop())
                elif fault == "multiple-parents":
                    registry[2]["IORegistryEntryChildren"].append(copy.deepcopy(registry[0]["IORegistryEntryChildren"][0]))
                else:
                    change_media(registry, "disk12s3", "BSD Minor", 22)
                with self.assertRaises(RuntimeError):
                    drives.dispatch(["open"])
                self.assertFalse(any("unmount" in item for item in state["events"]))
            self.metadata = disk_fixture()

    def test_inactive_state_is_rechecked_after_durable_intent(self):
        with self.stale_environment() as state:
            state["inactive_nodes"] = True
            save = drives.save_record
            def reactivate(record):
                save(record)
                change_media(state["registry"], "disk9s3", "IOServiceState", 30)
            with patch.object(drives, "save_record", side_effect=reactivate), \
                    self.assertRaisesRegex(RuntimeError, "no teardown dispatched"):
                drives.dispatch(["open"])
            self.assertFalse(any("unmount" in item for item in state["events"]))
            self.assertIn("stale_unmount_attempt", drives.load_record())


if __name__ == "__main__":
    unittest.main()
