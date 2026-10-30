"""Validate the O/native boundary without invoking O, sudo, or a real drive."""
import contextlib
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from olinux import drive_frontend as frontend


class DriveFrontendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.state = Path(self.temporary.name).resolve() / "state"
        self.document = self.state / "app/integrations/ostadix/read-drive.O"
        self.document.parent.mkdir(parents=True)
        self.document.write_text("fixture declaration\n")
        self.root = Path(self.temporary.name).resolve() / "OSTADIX"
        self.selection = {"partition_uuid": "11111111-2222-3333-4444-555555555555",
                          "filesystem": "ext4", "mountpoint": "/Volumes/Fixture-ReadOnly",
                          "partition_bytes": 1000, "whole_bytes": 2000, "media_name": "Fixture"}
        self.locked = False

        @contextlib.contextmanager
        def lock():
            self.assertFalse(self.locked, "the frontend must not nest the profile lock")
            self.locked = True
            try:
                yield
            finally:
                self.locked = False

        def operate(args):
            self.assertTrue(self.locked, "the profile lock must cover native dispatch")
            self.assertEqual(vars(args), {"action": "open"})
            return {"mounted": True, "read_only": True, "mountpoint": self.selection["mountpoint"]}

        self.stack.enter_context(patch.object(frontend, "state_dir", return_value=self.state))
        self.stack.enter_context(patch.object(frontend.platform, "system", return_value="Darwin"))
        self.stack.enter_context(patch.object(frontend, "load_config", return_value={"provider": "multipass"}))
        self.environment = self.stack.enter_context(patch.object(
            frontend, "environment", side_effect=lambda config: (self.root, lambda name: "/tools/" + name, {})))
        self.stack.enter_context(patch.object(frontend.drives, "drive_lock", side_effect=lock))
        self.stack.enter_context(patch.object(frontend.drives, "load_record", side_effect=lambda: {"selection": copy.deepcopy(self.selection)}))
        self.operate = self.stack.enter_context(patch.object(frontend.drives, "operate", side_effect=operate))
        self.stack.enter_context(patch.object(frontend.drives, "run", side_effect=AssertionError("No real host command is permitted")))

    def declaration(self, argv, **kwargs):
        self.assertTrue(self.locked)
        self.assertEqual(argv, ["/tools/O", "--json", str(self.document), str(self.root / "backends")])
        nonce = kwargs["env"][frontend.NONCE_ENV]
        self.assertRegex(nonce, r"^[0-9a-f]{64}$")
        request = frontend.request_for(self.selection, nonce)
        value = {"t": "map", "v": {key: {"t": "text", "v": {"utf8": item, "encoding": "utf-8"}}
                                      for key, item in request.items()}}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "value": value}), stderr="")

    def receipts(self):
        return [json.loads(path.read_text()) for path in (self.state / "runs").glob("drive-*/receipt.json")]

    def test_open_request_only_reads_saved_metadata(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(frontend.subprocess, "run", side_effect=AssertionError("request must not launch a process")):
            result = frontend.open_request()
        self.assertEqual(result, frontend.request_for(self.selection, None))
        self.assertFalse(self.locked)
        self.operate.assert_not_called()
        self.assertFalse((self.state / "runs").exists())
        with patch.dict(os.environ, {frontend.NONCE_ENV: "bad-nonce"}), self.assertRaisesRegex(RuntimeError, "nonce"):
            frontend.open_request()

    def test_valid_declaration_dispatches_fixed_open_once_under_lock(self):
        with patch.object(frontend.subprocess, "run", side_effect=self.declaration):
            result = frontend.run_open()
        self.assertTrue(result["mounted"])
        self.assertTrue(result["read_only"])
        self.operate.assert_called_once()
        self.assertFalse(self.locked)
        receipt = json.loads(Path(result["operation_receipt"]).read_text())
        self.assertEqual(receipt["state"], "completed")
        self.assertEqual(receipt["request"]["action"], "open-readonly")

    def test_wrong_request_fields_never_reach_native_operation(self):
        mutations = [
            ("schema", "different-schema"), ("action", "open-writable"),
            ("nonce", "0" * 64), ("installation", "/other/state"),
            ("selection_sha256", "f" * 64), ("argv", ["arbitrary-command"]),
        ]
        for name, value in mutations:
            with self.subTest(field=name):
                def response(argv, **kwargs):
                    result = self.declaration(argv, **kwargs)
                    value_document = json.loads(result.stdout)
                    value_document["value"]["v"][name] = {"t": "text", "v": {"utf8": value, "encoding": "utf-8"}}
                    result.stdout = json.dumps(value_document)
                    return result
                with patch.object(frontend.subprocess, "run", side_effect=response), \
                        self.assertRaisesRegex(RuntimeError, "invalid or stale"):
                    frontend.run_open()
                self.operate.assert_not_called()
                self.assertFalse(self.locked)
        self.assertTrue(all(receipt["state"] == "declaration-rejected" for receipt in self.receipts()))

    def test_nonzero_malformed_or_unsuccessful_o_output_is_rejected(self):
        responses = [
            SimpleNamespace(returncode=1, stdout="", stderr="O failed"),
            SimpleNamespace(returncode=0, stdout="not JSON", stderr=""),
            SimpleNamespace(returncode=0, stdout="[]", stderr=""),
            SimpleNamespace(returncode=0, stdout='{"ok": false, "value": {}}', stderr=""),
            SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr=""),
        ]
        for response in responses:
            with self.subTest(output=response.stdout, returncode=response.returncode), \
                    patch.object(frontend.subprocess, "run", return_value=response), self.assertRaises(RuntimeError):
                frontend.run_open()
        self.operate.assert_not_called()

    def test_noncanonical_ovalue_requests_never_reach_native_operation(self):
        def plain(value):
            return {key: item["v"]["utf8"] for key, item in value["v"].items()}
        def field(value, replacement):
            value["v"]["schema"] = replacement
            return value
        mutations = [
            plain,
            lambda value: field(value, {"t": "int", "v": 1}),
            lambda value: field(value, {"t": "map", "v": {}}),
            lambda value: field(value, {"t": "text", "v": {"utf8": frontend.SCHEMA, "encoding": "ascii"}}),
            lambda value: field(value, {"t": "text", "v": {"utf8": frontend.SCHEMA, "encoding": "utf-8", "extra": True}}),
            lambda value: field(value, {"t": "text", "v": {"utf8": 123, "encoding": "utf-8"}}),
            lambda value: {**value, "extra": True},
            lambda value: {"t": "map", "v": []},
            lambda value: {"t": "text", "v": {"utf8": "request", "encoding": "utf-8"}},
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(shape=index):
                def response(argv, **kwargs):
                    result = self.declaration(argv, **kwargs)
                    document = json.loads(result.stdout)
                    document["value"] = mutate(document["value"])
                    result.stdout = json.dumps(document)
                    return result
                with patch.object(frontend.subprocess, "run", side_effect=response), self.assertRaises(RuntimeError):
                    frontend.run_open()
                self.operate.assert_not_called()

    def test_changed_saved_profile_is_rejected_before_dispatch(self):
        def response(argv, **kwargs):
            result = self.declaration(argv, **kwargs)
            self.selection["partition_bytes"] += 1
            return result
        with patch.object(frontend.subprocess, "run", side_effect=response), \
                self.assertRaisesRegex(RuntimeError, "selection changed"):
            frontend.run_open()
        self.operate.assert_not_called()

    def test_changed_source_is_rejected_before_dispatch(self):
        def response(argv, **kwargs):
            result = self.declaration(argv, **kwargs)
            self.document.write_text("changed declaration\n")
            return result
        with patch.object(frontend.subprocess, "run", side_effect=response), \
                self.assertRaisesRegex(RuntimeError, "declaration changed"):
            frontend.run_open()
        self.operate.assert_not_called()

    def test_missing_or_symlinked_declaration_never_runs_o(self):
        self.document.unlink()
        with patch.object(frontend.subprocess, "run") as run, self.assertRaisesRegex(RuntimeError, "missing"):
            frontend.run_open()
        run.assert_not_called()
        target = self.document.with_name("different.O")
        target.write_text("fixture\n")
        self.document.symlink_to(target)
        with patch.object(frontend.subprocess, "run") as run, self.assertRaisesRegex(RuntimeError, "regular file"):
            frontend.run_open()
        run.assert_not_called()
        self.operate.assert_not_called()

    def test_native_failure_is_retained_as_unknown_and_never_retried(self):
        self.operate.side_effect = RuntimeError("uncertain native completion")
        with patch.object(frontend.subprocess, "run", side_effect=self.declaration), \
                self.assertRaisesRegex(RuntimeError, "uncertain native"):
            frontend.run_open()
        self.operate.assert_called_once()
        self.assertEqual(self.receipts()[0]["state"], "native-completion-unknown")
        self.assertFalse(self.locked)

    def test_o_timeout_never_dispatches_native_operation(self):
        with patch.object(frontend.subprocess, "run", side_effect=subprocess.TimeoutExpired("O", 30)), \
                self.assertRaises(subprocess.TimeoutExpired):
            frontend.run_open()
        self.operate.assert_not_called()
        self.assertEqual(self.receipts()[0]["state"], "declaration-rejected")

    def receipt_failure(self, persistent):
        original_replace = os.replace
        failures = []
        def replace(source, destination):
            source, destination = Path(source), Path(destination)
            if destination.name == "receipt.json":
                staged = json.loads(source.read_text())
                if staged["state"] == "completed" or (persistent and failures):
                    durable = destination.read_bytes()
                    self.assertEqual(json.loads(durable)["state"], "native-operation")
                    # A damaged temporary write and failed rename must leave
                    # the previously durable native phase intact.
                    source.write_bytes(b'{"partial receipt')
                    self.assertEqual(destination.read_bytes(), durable)
                    failures.append(staged["state"])
                    raise OSError("receipt storage unavailable")
            return original_replace(source, destination)
        with patch.object(os, "replace", side_effect=replace), \
                patch.object(frontend.subprocess, "run", side_effect=self.declaration), \
                self.assertRaisesRegex(OSError, "receipt storage unavailable"):
            frontend.run_open()
        self.operate.assert_called_once()
        self.assertFalse(self.locked)
        self.assertEqual(failures, ["completed", "native-completion-unknown"] if persistent else ["completed"])
        self.assertFalse(list((self.state / "runs").glob("drive-*/.receipt.json-*")))

    def test_completed_receipt_write_failure_records_native_unknown(self):
        self.receipt_failure(persistent=False)
        self.assertEqual(self.receipts()[0]["state"], "native-completion-unknown")

    def test_persistent_receipt_failure_preserves_interim_native_state_and_error(self):
        self.receipt_failure(persistent=True)
        self.assertEqual(self.receipts()[0]["state"], "native-operation")

    def test_non_macos_host_is_refused_before_environment_or_commands(self):
        with patch.object(frontend.platform, "system", return_value="Linux"), \
                patch.object(frontend.subprocess, "run") as run, self.assertRaisesRegex(RuntimeError, "requires macOS"):
            frontend.run_open()
        self.environment.assert_not_called()
        run.assert_not_called()
        self.operate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
