import base64
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from olinux import cli, ostadix, providers


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(provider="multipass", host_home="/Users/person", guest_home="/home/ubuntu",
                           guest_user="ubuntu", ssh="/usr/bin/ssh", share_volumes=False)

    def test_unshared_volume_falls_back_but_shared_home_keeps_cwd(self):
        self.assertEqual(providers.effective_cwd(self.config, "linux", "/Volumes/secret"), "/home/ubuntu")
        self.assertEqual(providers.effective_cwd(self.config, "linux", "/Users/person/project"), "/Users/person/project")
        self.assertEqual(providers.effective_cwd(self.config, "linux", "/Users/person-other"), "/home/ubuntu")
        self.config["share_volumes"] = True
        self.assertEqual(providers.effective_cwd(self.config, "linux", "/Volumes/a"), "/Volumes/a")

    def test_remote_arguments_remain_literal(self):
        import shlex
        args = ["printf", "%s", "a b", "$(touch should-not-exist)", "'", "\n", ""]
        result = providers.linux_argv(self.config, args, cwd="/Users/person/a b", address="192.0.2.1")
        self.assertIn("-T", result)
        self.assertEqual(shlex.split(result[-1]), ["cd", "/Users/person/a b", "&&", "exec", *args])

    def test_partial_terminal_does_not_merge_piped_streams(self):
        with patch.object(cli, "load_config", return_value=self.config), patch.object(cli, "ensure_running", return_value="192.0.2.1"), \
             patch.object(cli, "linux_argv", return_value=["ssh"]) as argv, patch.object(cli.os, "execvp"), \
             patch.object(cli.sys.stdin, "isatty", return_value=True), patch.object(cli.sys.stdout, "isatty", return_value=False), \
             patch.object(cli.sys.stderr, "isatty", return_value=True):
            cli.main(["exec", "true"])
        self.assertFalse(argv.call_args.kwargs["tty"])

    def test_native_execution_preserves_exit_and_streams(self):
        import sys
        result = providers.execute({"provider": "native"}, "linux", [sys.executable, "-c", "import sys; print('out'); print('err',file=sys.stderr); sys.exit(7)"])
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, "out\n", "err\n"))


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.config = dict(provider="native", host_home=str(self.state), guest_home=str(self.state))
        self.calls = []
        self.timeout = False
        self.wrong_digest = False

    def fake_run(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == "o":
            import hashlib
            digest = hashlib.sha256(Path(argv[2]).read_bytes()).hexdigest()
            return subprocess.CompletedProcess(argv, 0, json.dumps({"execution_intent": {
                "source_sha256": "0" * 64 if self.wrong_digest else digest,
                "execution_intent_sha256": "1" * 64}}), "")
        if "--check" in argv:
            return subprocess.CompletedProcess(argv, 0, "ok", "")
        source = Path(argv[-2])
        record = dict(exit_code=124 if self.timeout else 7, stdout="", stderr="", timed_out=self.timeout)
        if self.timeout: record["completion"] = "unknown: remote effects may continue after client timeout"
        (source.parent / "result.json").write_text(json.dumps(record))
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    def run_job(self, arguments):
        with patch.object(ostadix, "state_dir", return_value=self.state), \
             patch.object(ostadix, "load_config", return_value=self.config), \
             patch.object(ostadix, "environment", return_value=(self.state, lambda name: name, {})), \
             patch.object(ostadix.subprocess, "run", side_effect=self.fake_run), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return ostadix.job(arguments)

    def receipt(self):
        return json.loads(next((self.state / "runs").glob("*/receipt.json")).read_text())

    def test_plan_has_no_dispatch(self):
        self.assertEqual(self.run_job(["--plan", "--", "touch", "absent"]), 0)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.receipt()["state"], "planned")
        self.assertFalse(list(self.state.glob("runs/*/result.json")))

    def test_different_source_intent_is_rejected_before_dispatch(self):
        self.wrong_digest = True
        with self.assertRaisesRegex(RuntimeError, "not dispatched"):
            self.run_job(["--", "true"])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.receipt()["state"], "intent-invalid")

    def test_native_admission_gets_both_digests_and_exit_status(self):
        self.assertEqual(self.run_job(["--", "false"]), 7)
        argv = self.calls[-1]
        self.assertEqual(argv[argv.index("--require-source-sha256") + 1], self.receipt()["source_sha256"])
        self.assertEqual(argv[argv.index("--require-execution-intent-sha256") + 1], "1" * 64)

    def test_timeout_is_not_completed_even_if_o_returns_zero(self):
        self.timeout = True
        self.assertEqual(self.run_job(["--timeout", "1", "--", "sleep", "10"]), 124)
        self.assertEqual(self.receipt()["state"], "completion-unknown")
        self.assertIn("remote effects may continue", self.receipt()["completion"])

    def test_payload_captures_actual_fallback_cwd_and_timeout(self):
        config = dict(provider="multipass", host_home="/Users/person", guest_home="/home/ubuntu", share_volumes=False)
        payload = dict(target="linux", argv=["sleep", "1"], cwd="/Volumes/a", timeout=0.01,
                       result_path=str(self.state / "result.json"))
        with patch.object(ostadix, "load_config", return_value=config), \
             patch.object(ostadix, "execute", side_effect=subprocess.TimeoutExpired("ssh", 0.01, output=b"partial")):
            result = ostadix.execute_payload(base64.b64encode(json.dumps(payload).encode()).decode())
        self.assertEqual(result["cwd"], "/home/ubuntu")
        self.assertEqual(result["requested_cwd"], "/Volumes/a")
        self.assertEqual(result["stdout"], "partial")
        self.assertTrue(result["timed_out"])


class EnvironmentTests(unittest.TestCase):
    def test_darwin_o_alias_cannot_shadow_root_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backends").mkdir()
            binary = root / "target/release/o-cli"
            binary.parent.mkdir(parents=True)
            binary.write_text("fixture")
            binary.chmod(0o700)
            with patch.dict(os.environ, {"O_LANG_ROOT": directory}), \
                 patch.object(ostadix.platform, "system", return_value="Darwin"), \
                 patch.object(ostadix.shutil, "which", side_effect=lambda name: "/wrong/evaluator" if name == "o" else None):
                _, find, _ = ostadix.environment({})
                self.assertEqual(find("o"), str(binary.resolve()))

    def test_explicit_missing_root_cannot_silently_use_another_checkout(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"O_LANG_ROOT": directory}):
            with self.assertRaises(RuntimeError):
                ostadix.environment({"ostadix_root": "/some/other/checkout"})


if __name__ == "__main__":
    unittest.main()
