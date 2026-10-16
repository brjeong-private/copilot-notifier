"""Offline tests for the notifier CLI."""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

import notifier


class CompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.transcript = pathlib.Path(self.temp.name) / "events.jsonl"
        self.start = {"type": "session.start", "data": {}}
        self.auto = {"type": "session.mode_changed", "data": {"newMode": "autopilot"}}
        self.complete = {
            "type": "session.task_complete",
            "timestamp": "2026-10-16T09:01:05Z",
            "id": "done-1",
            "data": {"summary": "Implemented the requested change.", "success": True, "outcome": "completed"},
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_events(self, *events: dict) -> None:
        self.transcript.write_text(
            "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
        )

    def test_accepts_root_autopilot_completion(self) -> None:
        self.write_events(self.start, self.auto, self.complete)
        self.assertEqual(notifier.get_completion_candidate(self.transcript)["eventId"], "done-1")

    def test_rejects_other_modes_and_incomplete_outcomes(self) -> None:
        for mode in ("interactive", "plan", "unknown"):
            with self.subTest(mode=mode):
                self.write_events(
                    self.start, {"type": "session.mode_changed", "data": {"newMode": mode}},
                    self.complete,
                )
                self.assertIsNone(notifier.get_completion_candidate(self.transcript))
        for outcome in ("continue", "blocked"):
            with self.subTest(outcome=outcome):
                rejected = {**self.complete, "data": {**self.complete["data"], "outcome": outcome, "success": False}}
                self.write_events(self.start, self.auto, rejected)
                self.assertIsNone(notifier.get_completion_candidate(self.transcript))

    def test_tracks_latest_human_request_and_excludes_continuations(self) -> None:
        request = {
            "type": "user.message", "timestamp": "2026-10-16T09:00:00Z",
            "data": {"content": "Add completion emails\nLonger task details", "agentMode": "autopilot"},
        }
        continuation = {
            "type": "user.message", "timestamp": "2026-10-16T09:00:30Z",
            "data": {"content": "Continue", "isAutopilotContinuation": True},
        }
        self.write_events(self.start, request, continuation, self.complete)
        candidate = notifier.get_completion_candidate(self.transcript)
        self.assertEqual(candidate["task"], request["data"]["content"])
        self.assertEqual(candidate["startedAt"], request["timestamp"])

    def test_excludes_subagents_and_resumes(self) -> None:
        child = {
            "type": "session.task_complete", "agentId": "child", "id": "child",
            "data": {"summary": "Child finished"},
        }
        self.write_events(self.start, self.auto, child)
        self.assertIsNone(notifier.get_completion_candidate(self.transcript))
        self.write_events(self.start, self.auto, self.complete, {"type": "session.resume", "data": {}})
        self.assertIsNone(notifier.get_completion_candidate(self.transcript))

    def test_unsupported_transcript_fails_closed(self) -> None:
        self.write_events({"role": "assistant", "content": "Done"})
        with self.assertRaisesRegex(RuntimeError, "Unsupported transcript"):
            notifier.get_completion_candidate(self.transcript)


class FormattingTests(unittest.TestCase):
    def test_summary_filters_secrets_code_and_limits_lines(self) -> None:
        self.assertEqual(
            notifier.safe_lines("api_key=example\n```text\nhidden\n```\nFirst.\nSecond.\nThird."),
            ["First.", "Second."],
        )
        self.assertEqual(notifier.utf16_length(notifier.safe_lines("x" * 300, 1)[0]), 240)

    def test_operation_identity_is_stable_and_distinct(self) -> None:
        self.assertEqual(
            notifier.get_operation_id("session:event"),
            notifier.get_operation_id("session:event"),
        )
        self.assertNotEqual(
            notifier.get_operation_id("session:event"),
            notifier.get_operation_id("session:other"),
        )

    def test_email_content_excludes_full_request(self) -> None:
        content = notifier.email_summary(
            "tools", "Add completion emails\nPrivate full prompt",
            "Azure email is ready.\nAutopilot notifications are enabled.\nExtra details.",
        )
        self.assertEqual(content["subject"], "[Copilot] tools | Add completion emails")
        self.assertIn("Result: Azure email is ready. Autopilot notifications are enabled.", content["body"])
        self.assertNotIn("Private full prompt", str(content))

    def test_kakao_message_has_six_fields_and_respects_limit(self) -> None:
        text = notifier.get_kakao_content(
            "tools", "Add Kakao notifications\nPrivate full request",
            "Kakao notifications are ready.\nTiming is included.",
            "2026-10-16T09:00:00Z", "2026-10-16T09:01:05Z",
        )
        self.assertEqual(len(text.splitlines()), 6)
        self.assertIn("소요: 00:01:05", text)
        self.assertNotIn("Private", text)
        long_text = notifier.get_kakao_content(
            "p" * 200, "t" * 300, "r" * 300,
            "2026-10-16T09:00:00Z", "2026-10-18T10:01:05Z",
        )
        self.assertLessEqual(notifier.utf16_length(long_text), 200)
        self.assertIn("49:01:05", long_text)

    def test_kakao_rejects_invalid_or_reversed_timestamps(self) -> None:
        for started, completed in (
            ("", "2026-10-16T09:01:05Z"),
            ("invalid", "2026-10-16T09:01:05Z"),
            ("2026-10-16T09:00:00", "2026-10-16T09:01:05Z"),
            ("2026-10-16T09:02:00Z", "2026-10-16T09:01:05Z"),
        ):
            with self.subTest(started=started, completed=completed):
                with self.assertRaises(ValueError):
                    notifier.get_kakao_content("tools", "Task", "Result", started, completed)

    def test_email_config_rejects_unsafe_endpoint_and_accepts_acs_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "config.json"
            config = {
                "subscriptionId": "test", "endpoint": "", "sender": "sender@example.test",
                "recipient": "recipient@example.test",
            }
            for endpoint in (
                "http://example.communication.azure.com", "https://example.com",
                "https://example.communication.azure.com.attacker.test",
            ):
                config["endpoint"] = endpoint
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                    notifier.validate_email_config(path)
            for endpoint in (
                "https://example.communication.azure.com",
                "https://example.unitedstates.communication.azure.com",
            ):
                config["endpoint"] = endpoint
                path.write_text(json.dumps(config), encoding="utf-8")
                self.assertEqual(notifier.validate_email_config(path)["endpoint"], endpoint)


class ReceiptTests(unittest.TestCase):
    def test_kakao_missing_consent_is_explicit_and_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            receipt = root / "receipt.json"
            error = urllib.error.HTTPError(
                notifier.KAKAO_SEND_URL, 403, "Forbidden", {},
                io.BytesIO(b'{"code":-402,"msg":"insufficient scopes"}'),
            )
            with mock.patch.object(
                notifier, "resolve_kakao_token",
                return_value=({"messageUrl": "https://example.test"}, "test-token"),
            ), mock.patch.object(notifier, "request_json", side_effect=error) as request:
                with self.assertRaisesRegex(RuntimeError, "talk_message consent is missing"):
                    notifier.send_kakao(root / "kakao.json", "hello", "op", receipt)
                with self.assertRaisesRegex(RuntimeError, "prior Kakao send"):
                    notifier.send_kakao(root / "kakao.json", "hello", "op", receipt)
            self.assertEqual(json.loads(receipt.read_text())["status"], "Failed")
            self.assertEqual(request.call_count, 1)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI is only available on Windows.")
    def test_dpapi_credential_storage_round_trip(self) -> None:
        config = {
            "clientId": "unit-client-id", "clientSecret": "unit-client-secret",
            "accessToken": "unit-access-token", "refreshToken": "unit-refresh-token",
            "expiresAt": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat(),
            "messageUrl": "https://example.test/project",
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "kakao.json"
            notifier.save_kakao_config(path, config)
            stored = path.read_text(encoding="utf-8")
            self.assertNotIn("unit-refresh-token", stored)
            self.assertEqual(notifier.load_kakao_config(path), config)

    def test_email_receipt_polls_existing_operation_without_resubmitting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            receipt = pathlib.Path(temporary) / "receipt.json"
            calls: list[str] = []

            def request(url: str, **_: object) -> dict[str, str]:
                calls.append(url)
                return {"status": "Accepted"} if len(calls) == 1 else {"status": "Succeeded"}

            config = {
                "subscriptionId": "test", "endpoint": "https://example.communication.azure.com",
                "sender": "sender@example.test", "recipient": "recipient@example.test",
            }
            with mock.patch.object(notifier, "azure_token", return_value="token"), mock.patch.object(
                notifier, "request_json", side_effect=request
            ), mock.patch.object(notifier.time, "sleep"):
                notifier.send_email(config, "subject", "body", "operation-id", receipt)
                notifier.send_email(config, "subject", "body", "operation-id", receipt)
            self.assertEqual(len(calls), 3)
            self.assertIn("/emails:send?", calls[0])
            self.assertTrue(all("/emails/operations/operation-id?" in url for url in calls[1:]))
            self.assertEqual(json.loads(receipt.read_text())["status"], "Succeeded")

    def test_kakao_receipt_marks_only_confirmed_send_succeeded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            config_path = root / "kakao.json"
            receipt = root / "receipt.json"
            config = {
                "clientId": "id", "clientSecret": "secret", "accessToken": "token",
                "refreshToken": "refresh", "expiresAt": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat(),
                "messageUrl": "https://example.test/project",
            }
            with mock.patch.object(notifier, "load_kakao_config", return_value=config), mock.patch.object(
                notifier, "request_json", return_value={"result_code": 0}
            ) as request:
                notifier.send_kakao(config_path, "hello", "op", receipt)
                notifier.send_kakao(config_path, "hello", "op", receipt)
            self.assertEqual(json.loads(receipt.read_text())["status"], "Succeeded")
            self.assertEqual(request.call_count, 1)

    def test_uncertain_kakao_send_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            config = {
                "clientId": "id", "clientSecret": "secret", "accessToken": "token",
                "refreshToken": "refresh", "expiresAt": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat(),
                "messageUrl": "https://example.test/project",
            }
            receipt = root / "receipt.json"
            with mock.patch.object(notifier, "load_kakao_config", return_value=config), mock.patch.object(
                notifier, "request_json", side_effect=OSError("network")
            ) as request:
                with self.assertRaisesRegex(RuntimeError, "uncertain"):
                    notifier.send_kakao(root / "kakao.json", "hello", "op", receipt)
                with self.assertRaisesRegex(RuntimeError, "prior Kakao send"):
                    notifier.send_kakao(root / "kakao.json", "hello", "op", receipt)
            self.assertEqual(json.loads(receipt.read_text())["status"], "Pending")
            self.assertEqual(request.call_count, 1)

    def test_hook_installer_uses_python_in_the_copilot_windows_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = pathlib.Path(temporary)
            with mock.patch.dict(os.environ, {"COPILOT_HOME": str(home)}):
                notifier.install_hook()
            hook_path = home / "hooks" / "tools-copilot-notifier.json"
            hook = json.loads(hook_path.read_text(encoding="utf-8"))
            command = hook["hooks"]["Stop"][0]["powershell"]
            self.assertTrue(command.startswith("& "))
            self.assertIn(str(pathlib.Path(notifier.__file__).resolve()), command)
            self.assertNotIn("Notify-Completion.ps1", command)


def run_tests() -> None:
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise RuntimeError(f"{result.testsRun - len(result.failures) - len(result.errors)} checks passed; tests failed.")


if __name__ == "__main__":
    run_tests()
