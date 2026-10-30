import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from olinux import storage


OPERATION = "a" * 32


def operation_state(mode=None):
    result = {"operation_id": OPERATION, "instance": "test-vm", "status": "pending",
              "export": "o-linux-readonly-" + OPERATION if mode is None else "o-linux-" + mode + "-" + OPERATION}
    if mode is not None:
        result["access_mode"] = mode
    return result


class GuestFixture:
    """Run the real guest helpers against disposable sysfs/proc stand-ins."""
    def __init__(self, root):
        self.root = Path(root)
        self.receipts = self.root / "receipts"
        self.blocks = self.root / "blocks"
        self.processes = self.root / "processes"
        self.device = self.blocks / "nbd7"
        self.device.mkdir(parents=True)
        (self.device / "size").write_text("0")
        (self.device / "dev").write_text("43:224")
        (self.device / "holders").mkdir()
        (self.processes / "self").mkdir(parents=True)
        (self.processes / "self/mountinfo").write_text("")
        (self.processes / "swaps").write_text("Filename\tType\tSize\tUsed\tPriority\n")
        process = self.processes / "321"
        process.mkdir(parents=True)
        (process / "stat").write_text("321 (nbd-client) S " + "0 " * 18 + "777 0")
        self.disconnections = []
        self.attach_error = None
        self.attachment_commands = []
        self.block_read_only = "1\n"
        self.flushes = []
        self.flush_error = None

    def run_command(self, argv, **kwargs):
        if argv[:2] == ["blockdev", "--flushbufs"]:
            self.flushes.append(argv[-1])
            if self.flush_error: raise self.flush_error
        elif argv[0] == "nbd-client" and "-d" in argv:
            if not kwargs.get("capture_output"):
                print("disconnect, sock, done")
            self.disconnections.append(argv[-1])
            (self.device / "pid").unlink()
            (self.device / "size").write_text("0")
        elif argv[0] == "nbd-client":
            self.attachment_commands.append(argv)
            (self.device / "pid").write_text("321")
            (self.device / "size").write_text("2048")
            if self.attach_error:
                raise self.attach_error
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def invoke(self, script, args):
        for original, path in [("/run/o-linux-nbd", self.receipts),
                               ("/sys/class/block", self.blocks), ("/proc", self.processes)]:
            script = script.replace("pathlib.Path(" + repr(original) + ")",
                                    "pathlib.Path(" + repr(str(path)) + ")")
        output, namespace = io.StringIO(), {}
        with patch.object(sys, "argv", ["-", *args]), \
                patch.object(subprocess, "run", side_effect=self.run_command), \
                patch.object(subprocess, "check_output", side_effect=lambda *args, **kwargs: self.block_read_only), \
                contextlib.redirect_stdout(output):
            try:
                exec(script, namespace)
            finally:
                if "lock" in namespace:
                    namespace["lock"].close()
        return SimpleNamespace(stdout=output.getvalue())

    def guest(self, instance, args, **kwargs):
        # Multipass's remote shell drops unquoted empty arguments.
        return self.invoke(args[4], [value for value in args[5:] if value != ""])

    def receipt(self):
        return json.loads((self.receipts / (OPERATION + ".json")).read_text())


def disk_fixture():
    info = {"DeviceIdentifier": "disk999", "DeviceNode": "/dev/disk999",
            "WholeDisk": True, "Internal": False, "VirtualOrPhysical": "Physical",
            "TotalSize": 1000000, "DeviceBlockSize": 512, "BusProtocol": "USB",
            "MediaName": "Fixture disk", "IORegistryEntryName": "Fixture media",
            "DeviceTreePath": "fixture:/external/disk", "MountPoint": ""}
    listing = {"AllDisksAndPartitions": [{"DeviceIdentifier": "disk999",
                "Content": "GUID_partition_scheme", "Partitions": [
                    {"DeviceIdentifier": "disk999s1", "Content": "Linux Filesystem"}]}]}
    child = {"DeviceIdentifier": "disk999s1", "MountPoint": "", "Mounted": False}
    return info, listing, child


class PhysicalSourceTests(unittest.TestCase):
    def inspect(self, info=None, listing=None, child=None, mode=stat.S_IFBLK, host_mounts="", anylinuxfs_status=None):
        default_info, default_listing, default_child = disk_fixture()
        responses = {("info", "-plist", "/dev/disk999"): info or default_info,
                     ("list", "-plist", "/dev/disk999"): listing or default_listing,
                     ("info", "-plist", "/dev/disk999s1"): child or default_child}
        node = SimpleNamespace(st_mode=mode, st_rdev=99, st_ino=42, st_ctime_ns=17)
        with patch.object(storage, "disk_plist", side_effect=lambda *args: responses[args]), \
                patch.object(storage.os, "lstat", return_value=node), \
                patch.object(storage.shutil, "which", return_value="/tools/anylinuxfs" if anylinuxfs_status is not None else None), \
                patch.object(storage, "run", side_effect=lambda argv: SimpleNamespace(
                    stdout=host_mounts if argv == ["/sbin/mount"] else anylinuxfs_status, stderr="", returncode=0)), \
                patch.object(storage.os, "open") as opened:
            result = storage.inspect_device("/dev/disk999", 1000000)
        opened.assert_not_called()
        return result

    def test_valid_external_metadata_yields_stable_identity_without_open(self):
        first = self.inspect()
        self.assertEqual(first, self.inspect())
        self.assertEqual(first["children"], ["disk999", "disk999s1"])
        changed, _, _ = disk_fixture()
        changed["DeviceTreePath"] += "/replacement"
        self.assertNotEqual(first["fingerprint"], self.inspect(info=changed)["fingerprint"])

    def test_internal_virtual_and_partition_sources_are_rejected(self):
        for key, value in [("Internal", True), ("VirtualOrPhysical", "Virtual"), ("WholeDisk", False)]:
            info, _, _ = disk_fixture()
            info[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, "whole, external, physical"):
                self.inspect(info=info)

    def test_wrong_size_and_missing_identity_are_rejected(self):
        for key, value, message in [("TotalSize", 2000000, "Size mismatch"),
                                     ("DeviceTreePath", "", "identity")]:
            info, _, _ = disk_fixture(); info[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, message):
                self.inspect(info=info)

    def test_mounted_child_or_whole_disk_is_rejected(self):
        info, _, child = disk_fixture()
        child["Mounted"] = True
        with self.assertRaisesRegex(RuntimeError, "mounted"):
            self.inspect(child=child)
        info["MountPoint"] = "/Volumes/fixture"
        with self.assertRaisesRegex(RuntimeError, "mounted"):
            self.inspect(info=info)

    def test_anylinuxfs_nfs_mount_or_instance_blocks_raw_disk_despite_empty_diskutil_mountpoint(self):
        for source in ("disk999", "disk999s1"):
            with self.subTest(source=source), self.assertRaisesRegex(RuntimeError, "active anylinuxfs NFS"):
                self.inspect(host_mounts=source + ".local:/mnt/External on /Volumes/External (nfs, noowners)\n")
            with self.subTest(instance=source), self.assertRaisesRegex(RuntimeError, "active anylinuxfs instance"):
                self.inspect(anylinuxfs_status=source + " on /Volumes/External (ext4, rw)\n")
        self.inspect(host_mounts="disk9991s1.local:/mnt/Other on /Volumes/Other (nfs, noowners)\n",
                     anylinuxfs_status="disk9991s1 on /Volumes/Other (ext4, rw)")
        with self.assertRaisesRegex(RuntimeError, "Unrecognized anylinuxfs"):
            self.inspect(anylinuxfs_status="incomplete instance")

    def test_unresolved_compound_storage_and_wrong_node_type_are_rejected(self):
        for content in ("Apple_APFS", "Apple_CoreStorage", "Apple_RAID"):
            _, listing, _ = disk_fixture()
            listing["AllDisksAndPartitions"][0]["Partitions"][0]["Content"] = content
            with self.subTest(content=content), self.assertRaisesRegex(RuntimeError, "Compound"):
                self.inspect(listing=listing)
        for mode in (stat.S_IFREG, stat.S_IFLNK, stat.S_IFCHR):
            with self.subTest(mode=mode), self.assertRaisesRegex(RuntimeError, "block-device"):
                self.inspect(mode=mode)

    def test_partition_alias_and_invalid_size_fail_without_metadata_query(self):
        with patch.object(storage, "disk_plist") as query:
            for path in ("/dev/disk999s1", "/dev/rdisk999", "/tmp/disk999"):
                with self.subTest(path=path), self.assertRaises(RuntimeError):
                    storage.inspect_device(path, 1000000)
            with self.assertRaises(RuntimeError):
                storage.inspect_device("/dev/disk999", 0)
        query.assert_not_called()


class ImageSourceTests(unittest.TestCase):
    def test_read_only_descriptor_pins_original_bytes_after_path_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "fixture.raw"
            payload = b"original image bytes"
            image.write_bytes(payload)
            source, fd = storage.open_image(image)
            try:
                self.assertEqual(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDONLY)
                with self.assertRaises(OSError): os.write(fd, b"changed")
                image.unlink(); image.write_bytes(b"replacement")
                self.assertEqual(os.read(fd, 100), payload)
            finally:
                os.close(fd)

    def test_empty_directory_and_device_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            empty = Path(temporary) / "empty.raw"; empty.touch()
            with self.assertRaisesRegex(RuntimeError, "nonempty"):
                storage.open_image(empty)
            with self.assertRaisesRegex(RuntimeError, "regular"):
                storage.open_image(temporary)
            alias = Path(temporary) / "device-link"; alias.symlink_to("/dev/null")
            with self.assertRaisesRegex(RuntimeError, "rejects devices"):
                storage.open_image(alias)

    def test_export_cannot_open_a_replaced_path_for_writes(self):
        with patch.object(storage, "QEMU_NBD", "/tools/qemu-nbd"), patch.object(storage, "HOST_PORT", 19001):
            argv = storage.server_arguments(73)
        self.assertIn("--read-only", argv)
        self.assertIn("--format=raw", argv)
        self.assertIn("--bind=127.0.0.1", argv)
        self.assertIn("--port=19001", argv)
        self.assertEqual(argv[-1], "/dev/fd/73")
        self.assertNotIn("--snapshot", argv)

    def test_rw_descriptor_updates_pinned_original_without_touching_replacement_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "fixture.raw"
            original = Path(temporary) / "retained.raw"
            image.write_bytes(b"original image bytes")
            source, fd = storage.open_image(image, "rw")
            try:
                self.assertEqual(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDWR)
                image.rename(original)
                image.write_bytes(b"replacement unchanged")
                os.write(fd, b"UPDATED!")
                os.fsync(fd)
                self.assertTrue(original.read_bytes().startswith(b"UPDATED!"))
                self.assertEqual(image.read_bytes(), b"replacement unchanged")
            finally:
                os.close(fd)
        with patch.object(storage, "QEMU_NBD", "/tools/qemu-nbd"):
            arguments = storage.server_arguments(73, "fixture-rw", "rw")
        self.assertNotIn("--read-only", arguments)
        self.assertNotIn("--snapshot", arguments)
        self.assertEqual(arguments[-1], "/dev/fd/73")
        self.assertIn("--shared=1", arguments)

    def test_rw_open_permission_failure_has_no_read_only_fallback_or_source_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "fixture.raw"
            image.write_bytes(b"retained source bytes")
            with patch.object(storage.os, "open", side_effect=PermissionError("fixture denied")) as opened:
                with self.assertRaisesRegex(PermissionError, "fixture denied"):
                    storage.open_image(image, "rw")
            opened.assert_called_once()
            self.assertEqual(opened.call_args.args[1] & os.O_ACCMODE, os.O_RDWR)
            self.assertEqual(image.read_bytes(), b"retained source bytes")

    def test_invalid_mode_fails_before_source_open_or_server_arguments(self):
        for invalid in ("", "readwrite", None, True):
            with self.subTest(mode=invalid), patch.object(storage.os, "open") as opened:
                with self.assertRaisesRegex(RuntimeError, "access_mode"):
                    storage.open_image("/must/not/open", invalid)
                with self.assertRaisesRegex(RuntimeError, "access_mode"):
                    storage.server_arguments(73, mode=invalid)
            opened.assert_not_called()


class LifecycleTests(unittest.TestCase):
    def test_help_needs_no_configuration_or_processes(self):
        with patch.object(storage, "load_config") as load, \
                patch.object(storage.subprocess, "Popen") as opened, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                storage.main(["--help"])
        self.assertEqual(error.exception.code, 0)
        load.assert_not_called(); opened.assert_not_called()

    def test_configuration_uses_private_state_and_custom_tools_user_ports(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"O_LINUX_STATE": temporary}):
            storage.configure({"multipass": "/tools/multipass", "qemu_nbd": "/tools/qemu-nbd",
                               "guest_user": "test-user", "nbd_host_port": 19001, "nbd_guest_port": 19002})
            self.assertEqual(storage.BASE, Path(temporary).resolve() / "storage")
            self.assertEqual(storage.KEY.parent, storage.BASE)
            self.assertEqual(storage.MP, "/tools/multipass")
            self.assertEqual(storage.GUEST_USER, "test-user")
            self.assertEqual((storage.HOST_PORT, storage.GUEST_PORT), (19001, 19002))
            self.assertFalse(storage.BASE.exists())

    def test_invalid_ports_fail_before_starting_a_server(self):
        for port in (0, 80, 65536, True, "10829"):
            with self.subTest(port=port), self.assertRaisesRegex(RuntimeError, "ports"):
                storage.configure({"multipass": "/tools/mp", "qemu_nbd": "/tools/nbd",
                                   "guest_user": "ubuntu", "nbd_host_port": port})

    def test_cleanup_mode_overrides_are_rejected_and_pending_operation_blocks_new_source(self):
        with contextlib.redirect_stderr(io.StringIO()), patch.object(storage, "load_config") as load:
            for arguments in (["--cleanup-pending", "--read-write"], ["--cleanup-pending", "--read-only"],
                              ["fixture.raw", "--read-write", "--read-only"]):
                with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                    storage.main(arguments)
        load.assert_not_called()
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "storage"; base.mkdir()
            state = operation_state("ro")
            path = base / "state.json"; path.write_text(json.dumps(state))
            config = {"multipass": "/tools/mp", "qemu_nbd": "/tools/nbd", "guest_user": "ubuntu",
                      "instance": "test-vm", "storage_access_mode": "rw"}
            with patch.object(storage, "load_config", return_value=config), \
                    patch.object(storage, "state_dir", return_value=Path(temporary)), \
                    patch.object(storage, "open_image") as opened, \
                    patch.object(storage, "inspect_device") as inspected, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                storage.main(["/must/not/open", "--read-write"])
            opened.assert_not_called(); inspected.assert_not_called()
            self.assertEqual(json.loads(path.read_text()), state)

    def test_cli_configuration_and_override_reach_source_opener(self):
        for configured, option, expected in ((None, [], "ro"), ("rw", [], "rw"),
                                             ("rw", ["--read-only"], "ro"), ("ro", ["--read-write"], "rw")):
            with self.subTest(configured=configured, option=option), tempfile.TemporaryDirectory() as temporary:
                config = {"multipass": "/tools/mp", "qemu_nbd": "/tools/nbd", "guest_user": "ubuntu", "instance": "test-vm"}
                if configured is not None: config["storage_access_mode"] = configured
                with patch.object(storage, "load_config", return_value=config), \
                        patch.object(storage, "state_dir", return_value=Path(temporary)), \
                        patch.object(storage, "open_image", side_effect=PermissionError("fixture stop")) as opened, \
                        self.assertRaisesRegex(PermissionError, "fixture stop"):
                    storage.main(["/fixture.raw", *option])
                opened.assert_called_once_with(Path("/fixture.raw"), expected)

    def test_cleanup_disconnects_owned_mapping_and_removes_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"; state.write_text("{}")
            fd = os.open(state, os.O_RDONLY)
            tunnel, server = Mock(), Mock()
            with patch.object(storage, "STATE", state), patch.object(storage, "disconnect_operation") as disconnect, \
                    patch.object(storage, "stop") as stop:
                mapping = dict(operation_state(), device="/dev/nbd7", client_pid="321")
                clean = storage.cleanup("test-vm", mapping, tunnel, server, fd)
            self.assertTrue(clean)
            self.assertFalse(state.exists())
            disconnect.assert_called_once_with("test-vm", mapping)
            self.assertEqual([call.args[0] for call in stop.call_args_list], [tunnel, server])
            with self.assertRaises(OSError): os.fstat(fd)

    def test_failed_guest_cleanup_retains_state_but_stops_host_children(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"; state.write_text('{"device":"/dev/nbd7"}')
            fd = os.open(state, os.O_RDONLY)
            with patch.object(storage, "STATE", state), patch.object(storage, "disconnect_operation", side_effect=RuntimeError("guest offline")), \
                    patch.object(storage, "stop") as stop, contextlib.redirect_stderr(io.StringIO()):
                clean = storage.cleanup("test-vm", {"device": "/dev/nbd7", "client_pid": "321"}, Mock(), Mock(), fd)
            self.assertFalse(clean)
            self.assertTrue(state.exists())
            self.assertEqual(stop.call_count, 2)
            with self.assertRaises(OSError): os.fstat(fd)

    def test_failed_rw_cleanup_retains_server_tunnel_and_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            original = dict(operation_state("rw"), status="attached", device="/dev/nbd7", client_pid="321")
            path.write_text(json.dumps(original))
            fd = os.open(path, os.O_RDONLY)
            with patch.object(storage, "STATE", path), \
                    patch.object(storage, "disconnect_operation", side_effect=RuntimeError("guest filesystem mounted")), \
                    patch.object(storage, "stop") as stop, contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertFalse(storage.cleanup("test-vm", original, Mock(), Mock(), fd))
            stop.assert_not_called()
            self.assertEqual(json.loads(path.read_text()), original)
            self.assertIn("Retaining the read-write NBD server", errors.getvalue())
            with self.assertRaises(OSError): os.fstat(fd)

    def test_physical_preflight_uses_shared_drive_lock_and_releases_it_after_failure(self):
        from olinux import drives
        events = []
        @contextlib.contextmanager
        def guard():
            events.append("locked")
            try: yield
            finally: events.append("unlocked")
        def inspect(*args):
            self.assertEqual(events, ["locked"])
            raise RuntimeError("fixture preflight stop")
        with tempfile.TemporaryDirectory() as temporary:
            config = {"multipass": "/tools/mp", "qemu_nbd": "/tools/nbd", "guest_user": "ubuntu", "instance": "test-vm"}
            with patch.object(storage, "load_config", return_value=config), \
                    patch.object(storage, "state_dir", return_value=Path(temporary)), \
                    patch.object(drives, "drive_lock", side_effect=guard), \
                    patch.object(storage, "inspect_device", side_effect=inspect), \
                    self.assertRaisesRegex(RuntimeError, "fixture preflight stop"):
                storage.main(["--device", "/dev/disk999", "--expected-size", "1000000", "--read-write"])
        self.assertEqual(events, ["locked", "unlocked"])

    def test_process_cleanup_escalates_only_owned_process(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("child", 5), 0]
        storage.stop(process)
        process.terminate.assert_called_once(); process.kill.assert_called_once()


class ReceiptRecoveryTests(unittest.TestCase):
    def test_rw_guest_attachment_and_cleanup_preserve_mode_and_actual_block_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = Path(temporary) / "host"; host.mkdir()
            fixture = GuestFixture(Path(temporary) / "guest")
            fixture.block_read_only = "0\n"
            state = operation_state("rw")
            with patch.object(storage, "BASE", host), patch.object(storage, "STATE", host / "state.json"), \
                    patch.object(storage, "guest", side_effect=fixture.guest):
                result = storage.attach("test-vm", state)
                self.assertFalse(result["read_only"])
                self.assertEqual(result["access_mode"], "rw")
                self.assertNotIn("-R", fixture.attachment_commands[0])
                self.assertEqual(fixture.receipt()["access_mode"], "rw")
                cleaned = storage.disconnect_operation("test-vm", state)
            self.assertTrue(cleaned["clean"])
            self.assertEqual(cleaned["access_mode"], "rw")
            self.assertEqual(fixture.disconnections, ["/dev/nbd7"])
            self.assertEqual(fixture.flushes, ["/dev/nbd7"])

    def test_rw_mounted_device_partition_or_holder_is_not_flushed_or_disconnected(self):
        for use in ("device", "partition", "holder"):
            with self.subTest(use=use), tempfile.TemporaryDirectory() as temporary:
                fixture = GuestFixture(temporary); fixture.block_read_only = "0\n"
                state = operation_state("rw")
                fixture.invoke(storage.CONNECT, ["test-vm", OPERATION, "10819", state["export"], "rw"])
                if use == "holder":
                    (fixture.device / "holders/dm-0").touch()
                else:
                    number = "43:224"
                    if use == "partition":
                        part = fixture.blocks / "nbd7p1"; part.mkdir()
                        (part / "holders").mkdir(); (part / "dev").write_text("259:1")
                        number = "259:1"
                    (fixture.processes / "self/mountinfo").write_text("1 2 " + number + " / /mnt rw - ext4 /dev/nbd7 rw\n")
                before = fixture.receipt()
                with self.assertRaisesRegex(RuntimeError, "mounted|active holders"):
                    fixture.invoke(storage.DISCONNECT, ["test-vm", OPERATION, "-", "-", "rw", state["export"]])
                self.assertEqual(fixture.disconnections, [])
                self.assertEqual(fixture.flushes, [])
                self.assertEqual(fixture.receipt(), before)

    def test_rw_cleanup_never_flushes_changed_owner_and_retains_flush_failure(self):
        for failure in ("owner", "flush"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                fixture = GuestFixture(temporary); fixture.block_read_only = "0\n"
                state = operation_state("rw")
                fixture.invoke(storage.CONNECT, ["test-vm", OPERATION, "10819", state["export"], "rw"])
                if failure == "owner": (fixture.device / "pid").write_text("999")
                else: fixture.flush_error = RuntimeError("flush did not complete")
                with self.assertRaisesRegex(RuntimeError, "ownership changed|flush did not complete"):
                    fixture.invoke(storage.DISCONNECT, ["test-vm", OPERATION, "-", "-", "rw", state["export"]])
                self.assertEqual(fixture.disconnections, [])
                self.assertEqual(fixture.flushes, [] if failure == "owner" else ["/dev/nbd7"])
                self.assertEqual(fixture.receipt()["status"], "attached")

    def test_rw_active_swap_device_or_partition_alias_is_not_flushed_or_disconnected(self):
        for partition in (False, True):
            with self.subTest(partition=partition), tempfile.TemporaryDirectory() as temporary:
                fixture = GuestFixture(temporary); fixture.block_read_only = "0\n"
                state = operation_state("rw")
                fixture.invoke(storage.CONNECT, ["test-vm", OPERATION, "10819", state["export"], "rw"])
                major, minor = 43, 224
                if partition:
                    part = fixture.blocks / "nbd7p1"; part.mkdir()
                    (part / "holders").mkdir(); (part / "dev").write_text("43:225")
                    major, minor = 43, 225
                (fixture.processes / "swaps").write_text("Filename\tType\tSize\tUsed\tPriority\n/dev/swap-alias partition 1024 64 -2\n")
                original_stat = os.stat
                def device_stat(path, *args, **kwargs):
                    if str(path) == "/dev/swap-alias":
                        return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(major, minor))
                    return original_stat(path, *args, **kwargs)
                with patch.object(os, "stat", side_effect=device_stat), self.assertRaisesRegex(RuntimeError, "active swap"):
                    fixture.invoke(storage.DISCONNECT, ["test-vm", OPERATION, "-", "-", "rw", state["export"]])
                self.assertEqual(fixture.disconnections, [])
                self.assertEqual(fixture.flushes, [])
                self.assertEqual(fixture.receipt()["status"], "attached")

    def test_guest_block_mode_mismatch_retains_owned_receipt_for_cleanup(self):
        for mode, reported in (("rw", "1\n"), ("ro", "0\n")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                fixture = GuestFixture(temporary); fixture.block_read_only = reported
                state = operation_state(mode)
                with self.assertRaisesRegex(RuntimeError, "does not match requested"):
                    fixture.invoke(storage.CONNECT, ["test-vm", OPERATION, "10819", state["export"], mode])
                receipt = fixture.receipt()
                self.assertEqual(receipt["status"], "attached")
                self.assertEqual(receipt["client_pid"], "321")
                self.assertNotIn("read_only", receipt)
                fixture.invoke(storage.DISCONNECT, ["test-vm", OPERATION, "-", "-", mode, state["export"]])
                self.assertEqual(fixture.disconnections, ["/dev/nbd7"])

    def test_cleanup_and_host_receipt_reject_mode_or_export_substitution(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = GuestFixture(temporary); fixture.block_read_only = "0\n"
            state = operation_state("rw")
            result = fixture.invoke(storage.CONNECT, ["test-vm", OPERATION, "10819", state["export"], "rw"])
            for mode, export in (("ro", state["export"]), ("rw", "wrong-export")):
                with self.subTest(mode=mode, export=export), self.assertRaisesRegex(RuntimeError, "receipts disagree"):
                    fixture.invoke(storage.DISCONNECT, ["test-vm", OPERATION, "-", "-", mode, export])
            self.assertEqual(fixture.disconnections, [])
            for changed in (dict(state, access_mode="ro"), dict(state, export="wrong-export")):
                with self.assertRaises(RuntimeError):
                    storage.receipt_response(result.stdout, changed)
            self.assertEqual(storage.receipt_response(result.stdout, state)["access_mode"], "rw")

    def test_rw_lost_result_recovers_receipt_without_source_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            host = Path(temporary) / "host"; host.mkdir()
            fixture = GuestFixture(Path(temporary) / "guest"); fixture.block_read_only = "0\n"
            state = operation_state("rw")
            path = host / "state.json"
            def lose_result(instance, args, **kwargs):
                result = fixture.guest(instance, args, **kwargs)
                if args[4] == storage.CONNECT: raise subprocess.TimeoutExpired("lost reply", 45)
                return result
            with patch.object(storage, "BASE", host), patch.object(storage, "STATE", path), \
                    patch.object(storage, "guest", side_effect=lose_result), \
                    patch.object(storage, "open_image") as opened:
                with self.assertRaises(subprocess.TimeoutExpired): storage.attach("test-vm", state)
                self.assertEqual(json.loads(path.read_text())["access_mode"], "rw")
                self.assertTrue(storage.cleanup("test-vm", state, None, None, None))
            opened.assert_not_called()
            self.assertFalse(path.exists())
            self.assertEqual(fixture.disconnections, ["/dev/nbd7"])

    def test_lost_client_result_reconciles_guest_receipt_and_cleans_mapping(self):
        failures = [subprocess.TimeoutExpired("multipass", 45), RuntimeError("client exited"),
                    "not json", KeyboardInterrupt()]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as temporary:
                host = Path(temporary) / "host"; host.mkdir()
                fixture = GuestFixture(Path(temporary) / "guest")
                state = operation_state()
                path = host / "state.json"
                def interrupted_guest(instance, args, **kwargs):
                    self.assertEqual(json.loads(path.read_text())["operation_id"], OPERATION)
                    result = fixture.guest(instance, args, **kwargs)
                    if args[4] == storage.CONNECT:
                        self.assertEqual(json.loads(path.read_text())["status"], "pending")
                        if isinstance(failure, str): return SimpleNamespace(stdout=failure)
                        raise failure
                    return result
                with patch.object(storage, "BASE", host), patch.object(storage, "STATE", path), \
                        patch.object(storage, "guest", side_effect=interrupted_guest):
                    with self.assertRaises((RuntimeError, subprocess.TimeoutExpired, ValueError, KeyboardInterrupt)):
                        storage.attach("test-vm", state)
                    self.assertEqual(fixture.receipt()["client_pid"], "321")
                    self.assertTrue(storage.cleanup("test-vm", state, None, None, None))
                self.assertFalse(path.exists())
                self.assertEqual(fixture.disconnections, ["/dev/nbd7"])
                self.assertEqual(fixture.receipt()["status"], "detached")
                self.assertIn("-L", fixture.attachment_commands[0])

    def test_historical_creator_pid_without_live_process_is_not_claimed(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = GuestFixture(temporary)
            (fixture.processes / "321" / "stat").unlink()
            with patch.object(storage.time, "monotonic", side_effect=[0, 6]):
                with self.assertRaisesRegex(RuntimeError, "verifiable live owner"):
                    fixture.invoke(storage.CONNECT, ["test-vm", OPERATION, "10819", operation_state()["export"]])
            self.assertNotIn("client_pid", fixture.receipt())
            with self.assertRaisesRegex(RuntimeError, "ownership is unknown"):
                fixture.invoke(storage.DISCONNECT, ["test-vm", OPERATION, "-", "-"])
            self.assertEqual(fixture.disconnections, [])

    def test_attach_without_completed_receipt_retains_unknown_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = GuestFixture(Path(temporary) / "guest")
            fixture.attach_error = subprocess.TimeoutExpired("nbd-client", 20)
            host = Path(temporary) / "host"; host.mkdir()
            path = host / "state.json"
            state = operation_state()
            with patch.object(storage, "BASE", host), patch.object(storage, "STATE", path), \
                    patch.object(storage, "guest", side_effect=fixture.guest), \
                    contextlib.redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(subprocess.TimeoutExpired): storage.attach("test-vm", state)
                self.assertFalse(storage.cleanup("test-vm", state, None, None, None))
            self.assertTrue(path.exists())
            self.assertNotIn("client_pid", fixture.receipt())
            self.assertEqual(fixture.disconnections, [])
            self.assertIn("linux image --cleanup-pending", errors.getvalue())

    def test_changed_pid_or_reused_pid_never_disconnects_another_mapping(self):
        for changed_pid in (True, False):
            with self.subTest(changed_pid=changed_pid), tempfile.TemporaryDirectory() as temporary:
                fixture = GuestFixture(temporary)
                fixture.invoke(storage.CONNECT, ["test-vm", OPERATION, "10819", operation_state()["export"]])
                if changed_pid:
                    (fixture.device / "pid").write_text("999")
                else:
                    (fixture.processes / "321" / "stat").write_text("321 (different) S " + "0 " * 18 + "999 0")
                with self.assertRaisesRegex(RuntimeError, "ownership changed"):
                    fixture.invoke(storage.DISCONNECT, ["test-vm", OPERATION, "", ""])
                self.assertEqual(fixture.disconnections, [])

    def test_cancel_before_attach_writes_tombstone_and_prevents_delayed_attach(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = GuestFixture(temporary)
            fixture.invoke(storage.DISCONNECT, ["test-vm", OPERATION, "", ""])
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                fixture.invoke(storage.CONNECT, ["test-vm", OPERATION, "10819", operation_state()["export"]])
            self.assertEqual(fixture.receipt()["status"], "cancelled")
            self.assertFalse((fixture.device / "pid").exists())

    def test_recovery_uses_receipt_without_reopening_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = GuestFixture(Path(temporary) / "guest")
            fixture.invoke(storage.CONNECT, ["test-vm", OPERATION, "10819", operation_state()["export"]])
            state = operation_state()
            state.update(source="/must/not/open", server_pid=987654, tunnel_pid=987655)
            path = Path(temporary) / "state.json"; path.write_text(json.dumps(state))
            with patch.object(storage, "STATE", path), patch.object(storage, "guest", side_effect=fixture.guest), \
                    patch.object(storage.os, "kill", side_effect=ProcessLookupError), \
                    patch.object(storage, "open_image") as opened, \
                    patch.object(storage, "inspect_device") as inspected, contextlib.redirect_stdout(io.StringIO()):
                storage.cleanup_pending()
            opened.assert_not_called(); inspected.assert_not_called()
            self.assertFalse(path.exists())
            self.assertEqual(fixture.disconnections, ["/dev/nbd7"])

    def test_unreachable_guest_and_unidentified_live_host_process_retain_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            state = dict(operation_state(), server_pid=123)
            path.write_text(json.dumps(state))
            with patch.object(storage, "BASE", Path(temporary)), patch.object(storage, "STATE", path):
                with patch.object(storage, "guest", side_effect=RuntimeError("guest offline")):
                    with self.assertRaisesRegex(RuntimeError, "guest offline"): storage.cleanup_pending()
                self.assertEqual(json.loads(path.read_text()), state)
                proof = dict(operation_state(), status="detached", clean=True)
                with patch.object(storage, "disconnect_operation", return_value=proof), \
                        patch.object(storage.os, "kill") as kill:
                    with self.assertRaisesRegex(RuntimeError, "verify their identity"): storage.cleanup_pending()
                kill.assert_called_once_with(123, 0)
                self.assertEqual(json.loads(path.read_text())["status"], "detached")


class AuthorizedKeysTests(unittest.TestCase):
    entry = b'restrict,port-forwarding,command="/bin/false" ssh-ed25519 OWNKEY o-linux'

    def authorize(self, home):
        incoming = SimpleNamespace(buffer=io.BytesIO(self.entry))
        with patch.object(Path, "home", return_value=Path(home)), patch.object(sys, "stdin", incoming):
            exec(storage.AUTHORIZE_KEY, {})

    def test_preserves_unrelated_bytes_and_replaces_only_matching_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / ".ssh"; directory.mkdir()
            path = directory / "authorized_keys"
            unrelated = b'# comment\r\nssh-ed25519 OTHERKEY owner\r\n# no final newline'
            path.write_bytes(b'ssh-ed25519 OWNKEY old comment\n' + unrelated)
            path.chmod(0o640)
            self.authorize(temporary)
            self.assertEqual(path.read_bytes(), unrelated + b'\n' + self.entry + b'\n')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_failed_write_keeps_original_file_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / ".ssh"; directory.mkdir()
            path = directory / "authorized_keys"; original = b'ssh-ed25519 ORIGINAL retained\r\n'
            path.write_bytes(original)
            with patch.object(os, "fsync", side_effect=OSError("fixture write failure")):
                with self.assertRaisesRegex(OSError, "write failure"): self.authorize(temporary)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(directory.iterdir()), [path])

    def test_symlinked_directory_or_key_file_and_nonregular_file_are_rejected(self):
        for target in ("directory", "file", "nonregular"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary) / "home"; home.mkdir()
                real = Path(temporary) / "real"; real.mkdir()
                directory = home / ".ssh"
                if target == "directory": directory.symlink_to(real)
                else:
                    directory.mkdir()
                    if target == "file": (directory / "authorized_keys").symlink_to(real / "missing")
                    else: (directory / "authorized_keys").mkdir()
                with self.assertRaisesRegex(RuntimeError, "symlinked|non-regular"):
                    self.authorize(home)


if __name__ == "__main__":
    unittest.main()
