from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from olinux import systems


class LinuxSystemTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve() / "home with spaces"
        self.root = self.home / "OSTADIX"
        self.harness = self.root / "scripts/foreign_kernel_lab.py"
        self.harness.parent.mkdir(parents=True)
        self.harness.write_text("# fixture\n")
        self.state = self.home / ".local/share/o-linux"
        self.config = {"provider": "multipass", "host_home": str(self.home), "share_volumes": False}

    def invoke(self, arguments):
        with patch.object(systems, "load_config", return_value=self.config), \
                patch.object(systems, "environment", return_value=(self.root, None, {})), \
                patch.object(systems, "state_dir", return_value=self.state), \
                patch.object(systems, "job", return_value=7) as run:
            result = systems.run_linux(arguments)
        return result, run.call_args.args[0]

    def test_default_list_uses_host_cache_and_linux_job(self):
        result, argv = self.invoke([])
        self.assertEqual(result, 7)
        self.assertEqual(argv, ["--target", "linux", "--", "python3", str(self.harness),
                                "--guest-dir", str(self.home / ".local/share/ostadix/guests"), "list"])

    def test_run_preserves_literal_arguments_and_defaults_output(self):
        result, argv = self.invoke(["run", "name; $(touch absent)", "--firmware", "uefi=/guest/fw with spaces.fd"])
        self.assertEqual(result, 7)
        self.assertIn("name; $(touch absent)", argv)
        self.assertIn("uefi=/guest/fw with spaces.fd", argv)
        self.assertEqual(argv[-2:], ["--output-dir", str(self.state / "foreign-system-runs")])

    def test_explicit_guest_cache_output_and_manifest_are_unchanged(self):
        arguments = ["--manifest", "/guest/custom manifest.json", "--guest-dir=/guest/custom cache", "run-all", "--output-dir", "/guest/results"]
        _, argv = self.invoke(arguments)
        self.assertEqual(argv[5:], arguments)

    def test_manifest_value_is_not_mistaken_for_run_command(self):
        _, argv = self.invoke(["--manifest", "run", "list"])
        self.assertNotIn("--output-dir", argv)

    def test_unshared_checkout_or_default_output_is_refused(self):
        self.config["host_home"] = str(self.home / "different")
        with self.assertRaisesRegex(RuntimeError, "checkout must be inside"):
            self.invoke(["list"])
        self.config["host_home"] = str(self.home)
        self.state = Path(self.temporary.name).resolve() / "unshared state"
        with self.assertRaisesRegex(RuntimeError, "output is outside"):
            self.invoke(["run", "guest"])
        _, argv = self.invoke(["run", "guest", "--output-dir=/tmp/guest-results"])
        self.assertIn("--output-dir=/tmp/guest-results", argv)

    def test_native_provider_accepts_local_checkout_outside_home(self):
        self.config.update(provider="native", host_home="/different/home")
        _, argv = self.invoke(["run-all"])
        self.assertEqual(argv[-2:], ["--output-dir", str(self.state / "foreign-system-runs")])

    def test_missing_harness_is_refused_before_job_dispatch(self):
        self.harness.unlink()
        with self.assertRaisesRegex(RuntimeError, "no foreign-system harness"):
            self.invoke(["list"])

    def test_guest_native_root_is_not_checked_on_host_and_flag_is_consumed(self):
        self.harness.unlink()
        self.config["host_home"] = str(self.home / "different")
        _, argv = self.invoke(["--linux-root", "/home/ubuntu/native checkout", "list"])
        self.assertEqual(argv[4], "/home/ubuntu/native checkout/scripts/foreign_kernel_lab.py")
        self.assertNotIn("--linux-root", argv)
        self.assertEqual(argv[-1], "list")

    def test_persistent_guest_root_and_explicit_override(self):
        self.config["linux_ostadix_root"] = "/home/ubuntu/persistent"
        _, argv = self.invoke(["list"])
        self.assertEqual(argv[4], "/home/ubuntu/persistent/scripts/foreign_kernel_lab.py")
        _, argv = self.invoke(["--linux-root=/home/ubuntu/override", "verify", "--guest", "literal; name"])
        self.assertEqual(argv[4], "/home/ubuntu/override/scripts/foreign_kernel_lab.py")
        self.assertEqual(argv[-3:], ["verify", "--guest", "literal; name"])
        self.assertFalse(any(value.startswith("--linux-root") for value in argv))

    def test_guest_root_requires_an_absolute_normalized_path(self):
        for value in ("", "relative", "~/OSTADIX", "/home/../root", "/home/./repo", "/home/repo/", "//home/repo", "/home/bad\nname"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.invoke(["--linux-root=" + value, "list"])
        with self.assertRaisesRegex(ValueError, "only once"):
            self.invoke(["--linux-root=/one", "--linux-root=/two", "list"])
        with self.assertRaisesRegex(ValueError, "requires"):
            self.invoke(["--linux-root"])

    def test_native_provider_checks_explicit_root_locally(self):
        self.config["provider"] = "native"
        with self.assertRaisesRegex(RuntimeError, "no foreign-system harness"):
            self.invoke(["--linux-root=/definitely/absent/ostadix", "list"])


if __name__ == "__main__":
    unittest.main()
