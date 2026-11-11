import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.campaign.sandbox_pilot_cli import main


class SandboxPilotCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.registry = Path(self.temp.name) / "synthetic-volunteers.json"
        self.out = io.StringIO()
        self.err = io.StringIO()

    def tearDown(self):
        self.temp.cleanup()

    def _run(self, argv, *, input_text=""):
        values = iter(input_text.splitlines())
        return main(
            argv,
            input_fn=lambda _prompt: next(values),
            stdout=self.out,
            stderr=self.err,
        )

    def _add_volunteer(self, volunteer_id="ada", name="Synthetic Ada", phone="+1 208 555 0101"):
        return self._run([
            "volunteers", "add", "--registry", str(self.registry),
            "--id", volunteer_id, "--name", name, "--phone", phone,
        ])

    def test_synthetic_volunteer_add_and_list_work(self):
        self.assertEqual(self._add_volunteer(), 0)
        self.assertEqual(self._run(["volunteers", "list", "--registry", str(self.registry)]), 0)
        listing = self.out.getvalue()
        self.assertIn("Synthetic Ada", listing)
        self.assertIn("+12085550101", listing)
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["volunteers"]), 1)

    def test_remove_synthetic_volunteer(self):
        self.assertEqual(self._add_volunteer(), 0)
        status = self._run([
            "volunteers", "remove", "--registry", str(self.registry), "--id", "ada"
        ])
        self.assertEqual(status, 0)
        self.assertEqual(self._run(["volunteers", "list", "--registry", str(self.registry)]), 0)
        self.assertEqual(json.loads(self.registry.read_text(encoding="utf-8"))["volunteers"], [])
        self.assertIn("No synthetic volunteers", self.out.getvalue())

    def test_real_number_is_rejected_without_registry_write(self):
        status = self._add_volunteer(phone="+1 208 555 0201")
        self.assertEqual(status, 2)
        self.assertFalse(self.registry.exists())
        self.assertIn("synthetic", self.err.getvalue().casefold())

    def test_non_synthetic_display_name_is_rejected(self):
        status = self._add_volunteer(name="Ada Lovelace")
        self.assertEqual(status, 2)
        self.assertFalse(self.registry.exists())

    def test_display_name_format_controls_are_rejected(self):
        status = self._add_volunteer(name="Synthetic Ada\u202e")
        self.assertEqual(status, 2)
        self.assertFalse(self.registry.exists())

    def test_duplicate_normalized_number_is_rejected(self):
        self.assertEqual(self._add_volunteer(), 0)
        status = self._add_volunteer(volunteer_id="grace", name="Synthetic Grace", phone="+12085550101")
        self.assertEqual(status, 2)
        data = json.loads(self.registry.read_text(encoding="utf-8"))
        self.assertEqual(len(data["volunteers"]), 1)

    def test_dry_run_requires_two_operators_and_per_message_review(self):
        self.assertEqual(self._add_volunteer(), 0)
        self.out.seek(0)
        self.out.truncate(0)
        message = "Synthetic sandbox notice for Ada."
        status = self._run([
            "dry-run", "--registry", str(self.registry), "--volunteer-id", "ada",
            "--campaign-id", "fixture-campaign", "--message", message,
            "--approver-one", "operator-a", "--approver-two", "operator-b",
            "--reviewer", "operator-a",
        ], input_text="CONFIRM ONE TEXT\n")

        self.assertEqual(status, 0, self.err.getvalue())
        output = self.out.getvalue()
        self.assertIn("OFFLINE MOCK DRY RUN", output)
        self.assertIn("Synthetic Ada", output)
        self.assertIn("+12085550101", output)
        self.assertIn(message, output)
        self.assertIn("two-person kill switch: enabled", output)
        self.assertIn("mock submissions: 1", output)
        self.assertIn("ACCEPTED_BY_PROVIDER", output)
        self.assertIn("not human consent", output.casefold())

    def test_dry_run_wrong_confirmation_never_submits_mock_request(self):
        self.assertEqual(self._add_volunteer(), 0)
        self.out.seek(0)
        self.out.truncate(0)
        status = self._run([
            "dry-run", "--registry", str(self.registry), "--volunteer-id", "ada",
            "--campaign-id", "fixture-campaign", "--message", "Do not submit.",
            "--approver-one", "operator-a", "--approver-two", "operator-b",
            "--reviewer", "operator-a",
        ], input_text="NO\n")
        self.assertEqual(status, 2)
        self.assertIn("mock submissions: 0", self.out.getvalue())
        self.assertIn("confirmation", self.err.getvalue().casefold())

    def test_same_person_cannot_satisfy_both_kill_switch_approvals(self):
        self.assertEqual(self._add_volunteer(), 0)
        self.out.seek(0)
        self.out.truncate(0)
        status = self._run([
            "dry-run", "--registry", str(self.registry), "--volunteer-id", "ada",
            "--campaign-id", "fixture-campaign", "--message", "No self-approval.",
            "--approver-one", "operator-a", "--approver-two", "operator-a",
            "--reviewer", "operator-a",
        ], input_text="CONFIRM ONE TEXT\n")
        self.assertEqual(status, 2)
        self.assertIn("mock submissions: 0", self.out.getvalue())
        self.assertIn("distinct", self.err.getvalue().casefold())

    def test_registry_tampering_or_unknown_version_fails_closed(self):
        self.registry.write_text(json.dumps({"version": 9, "volunteers": []}), encoding="utf-8")
        status = self._run(["volunteers", "list", "--registry", str(self.registry)])
        self.assertEqual(status, 2)
        self.assertIn("version", self.err.getvalue().casefold())

    def test_duplicate_json_keys_fail_closed(self):
        self.registry.write_text(
            '{"version": 1, "version": 1, "volunteers": []}', encoding="utf-8"
        )
        status = self._run(["volunteers", "list", "--registry", str(self.registry)])
        self.assertEqual(status, 2)
        self.assertIn("duplicate", self.err.getvalue().casefold())

    def test_unc_registry_path_is_rejected_before_file_access(self):
        unc_path = r"\\server\share\volunteers.json"
        status = self._run(["volunteers", "list", "--registry", unc_path])
        self.assertEqual(status, 2)
        self.assertIn("local", self.err.getvalue().casefold())

    def test_mapped_network_drive_is_rejected_before_registry_access(self):
        with (
            patch("src.campaign.sandbox_pilot_cli.ctypes.windll.kernel32.GetDriveTypeW", return_value=4),
            patch("pathlib.Path.exists", side_effect=AssertionError("registry was accessed")),
        ):
            status = self._run([
                "volunteers", "list", "--registry", r"Z:\\volunteers.json"
            ])
        self.assertEqual(status, 2)
        self.assertIn("local", self.err.getvalue().casefold())

    def test_reparse_registry_path_is_rejected_before_file_access(self):
        reparse_point = self.registry.parent / "junction"
        redirected_registry = reparse_point / "volunteers.json"
        original_lstat = Path.lstat

        def fake_lstat(path):
            if path == reparse_point:
                return SimpleNamespace(st_file_attributes=0x400, st_mode=0)
            return original_lstat(path)

        with (
            patch("pathlib.Path.lstat", new=fake_lstat),
            patch("pathlib.Path.exists", side_effect=AssertionError("registry was accessed")),
        ):
            status = self._run(["volunteers", "list", "--registry", str(redirected_registry)])
        self.assertEqual(status, 2)
        self.assertIn("reparse", self.err.getvalue().casefold())

    def test_nested_duplicate_json_keys_fail_closed(self):
        self.registry.write_text(
            '{"version":1,"volunteers":[{"id":"ada","id":"grace",'
            '"name":"Synthetic Ada","phone":"+1 208 555 0101"}]}',
            encoding="utf-8",
        )
        status = self._run(["volunteers", "list", "--registry", str(self.registry)])
        self.assertEqual(status, 2)
        self.assertIn("duplicate", self.err.getvalue().casefold())

    def test_dry_run_refuses_noninteractive_stdin(self):
        self.assertEqual(self._add_volunteer(), 0)
        self.out.seek(0)
        self.out.truncate(0)
        argv = [
            "dry-run", "--registry", str(self.registry), "--volunteer-id", "ada",
            "--campaign-id", "fixture-campaign", "--message", "Must be reviewed interactively.",
            "--approver-one", "operator-a", "--approver-two", "operator-b",
            "--reviewer", "operator-a",
        ]
        with patch("sys.stdin", io.StringIO("CONFIRM ONE TEXT\n")):
            status = main(argv, stdout=self.out, stderr=self.err)
        self.assertEqual(status, 2)
        self.assertIn("interactive", self.err.getvalue().casefold())
        self.assertNotIn("mock submissions: 1", self.out.getvalue())

    def test_post_dispatch_write_error_reports_mock_attempt_outcome(self):
        self.assertEqual(self._add_volunteer(), 0)

        class FailingAttemptWriter(io.StringIO):
            def write(self, value):
                if value.startswith("attempt state:"):
                    raise OSError("simulated stdout write failure")
                return super().write(value)

        status = main([
            "dry-run", "--registry", str(self.registry), "--volunteer-id", "ada",
            "--campaign-id", "fixture-campaign", "--message", "Write failure handling.",
            "--approver-one", "operator-a", "--approver-two", "operator-b",
            "--reviewer", "operator-a",
        ], input_fn=lambda _prompt: "CONFIRM ONE TEXT", stdout=FailingAttemptWriter(), stderr=self.err)
        self.assertEqual(status, 3)
        self.assertIn("mock submission already occurred", self.err.getvalue().casefold())
        self.assertIn("ACCEPTED_BY_PROVIDER", self.err.getvalue())

    def test_post_dispatch_flush_error_reports_mock_attempt_outcome(self):
        self.assertEqual(self._add_volunteer(), 0)

        class FailingFlushWriter(io.StringIO):
            post_dispatch_output = False

            def write(self, value):
                result = super().write(value)
                if value.startswith("attempt state:"):
                    self.post_dispatch_output = True
                return result

            def flush(self):
                if self.post_dispatch_output:
                    raise OSError("simulated buffered stdout failure")
                return super().flush()

        status = main([
            "dry-run", "--registry", str(self.registry), "--volunteer-id", "ada",
            "--campaign-id", "fixture-campaign", "--message", "Output failure handling.",
            "--approver-one", "operator-a", "--approver-two", "operator-b",
            "--reviewer", "operator-a",
        ], input_fn=lambda _prompt: "CONFIRM ONE TEXT", stdout=FailingFlushWriter(), stderr=self.err)
        self.assertEqual(status, 3)
        self.assertIn("mock submission already occurred", self.err.getvalue().casefold())
        self.assertIn("ACCEPTED_BY_PROVIDER", self.err.getvalue())


if __name__ == "__main__":
    unittest.main()
