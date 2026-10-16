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


class CompletionFixture(unittest.TestCase):
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

    def chat_events(self, mode: str = "interactive") -> list[dict]:
        return [
            self.start,
            {
                "type": "user.message", "timestamp": "2026-10-16T09:00:00Z", "id": "request-1",
                "data": {"content": "Complete a small change", "agentMode": mode},
            },
            {
                "type": "assistant.message", "timestamp": "2026-10-16T09:01:00Z", "id": "reply-1",
                "data": {"content": "The requested change is ready.", "phase": "final_answer", "toolRequests": []},
            },
        ]


class CompletionTests(CompletionFixture):
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

    def test_chat_final_response_is_opt_in(self) -> None:
        self.write_events(*self.chat_events())
        self.assertIsNone(notifier.get_completion_candidate(self.transcript))
        candidate = notifier.get_completion_candidate(self.transcript, notify_chat=True)
        self.assertEqual(candidate["eventId"], "reply-1")
        self.assertEqual(candidate["mode"], "interactive")
        self.assertEqual(candidate["startedAt"], "2026-10-16T09:00:00Z")

    def test_chat_requires_explicit_final_phase_without_tools(self) -> None:
        for phase, tools in (("commentary", []), (None, []), ("final_answer", [{"name": "test"}])):
            with self.subTest(phase=phase, tools=tools):
                events = self.chat_events()
                events[-1]["data"].update(phase=phase, toolRequests=tools)
                self.write_events(*events)
                self.assertIsNone(notifier.get_completion_candidate(self.transcript, notify_chat=True))

    def test_chat_excludes_plan_unknown_and_autopilot_final_messages(self) -> None:
        for mode in ("plan", "unknown", "autopilot", None):
            with self.subTest(mode=mode):
                self.write_events(*self.chat_events(mode))
                self.assertIsNone(notifier.get_completion_candidate(self.transcript, notify_chat=True))

    def test_chat_excludes_child_responses(self) -> None:
        events = self.chat_events()
        events[-1]["agentId"] = "child"
        self.write_events(*events)
        self.assertIsNone(notifier.get_completion_candidate(self.transcript, notify_chat=True))

    def test_root_idle_prompt_routing_task_id_does_not_make_it_a_subagent(self) -> None:
        events = self.chat_events()
        events[1]["data"].update(parentAgentTaskId="root-routing-task", delivery="idle")
        self.write_events(*events)
        self.assertEqual(
            notifier.get_completion_candidate(self.transcript, notify_chat=True)["eventId"],
            "reply-1",
        )

    def test_new_work_error_and_resume_invalidate_old_chat_final(self) -> None:
        for kind, data in (
            ("user.message", {"content": "Next request"}),
            ("session.resume", {}),
            ("session.error", {}),
            ("abort", {}),
            ("assistant.turn_start", {}),
            ("session.mode_changed", {"newMode": "plan"}),
            ("assistant.message", {"content": "Continuing", "phase": "commentary"}),
        ):
            with self.subTest(kind=kind):
                self.write_events(*self.chat_events(), {"type": kind, "data": data})
                self.assertIsNone(notifier.get_completion_candidate(self.transcript, notify_chat=True))

    def test_autopilot_can_be_disabled(self) -> None:
        self.write_events(self.start, self.auto, self.complete)
        self.assertIsNone(notifier.get_completion_candidate(self.transcript, notify_autopilot=False))


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.temp.name) / ".env"
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    def test_missing_env_preserves_defaults(self) -> None:
        self.assertEqual(
            notifier.load_notification_policy(self.path),
            {"chat": False, "plan": False, "autopilot": True, "channels": ["kakao"]},
        )

    def test_env_chat_and_boolean_parsing(self) -> None:
        self.path.write_text(
            'NOTIFIER_NOTIFY_CHAT="true" # enabled\nNOTIFIER_NOTIFY_AUTOPILOT=false\n'
            'KAKAO_CLIENT_SECRET=unused-do-not-execute\n', encoding="utf-8",
        )
        self.assertEqual(
            notifier.load_notification_policy(self.path),
            {"chat": True, "plan": False, "autopilot": False, "channels": ["kakao"]},
        )

    def test_email_channel_can_be_selected_alone_or_with_kakao(self) -> None:
        for channels in ("azure_email", "kakao, azure_email"):
            with self.subTest(channels=channels):
                self.path.write_text(f"NOTIFIER_CHANNELS={channels}\n", encoding="utf-8")
                self.assertEqual(
                    notifier.load_notification_policy(self.path)["channels"],
                    [item.strip() for item in channels.split(",")],
                )

    def test_process_environment_overrides_file(self) -> None:
        self.path.write_text("NOTIFIER_NOTIFY_CHAT=false\n", encoding="utf-8")
        os.environ["NOTIFIER_NOTIFY_CHAT"] = "true"
        self.assertTrue(notifier.load_notification_policy(self.path)["chat"])

    def test_invalid_or_unsupported_settings_are_explicit_errors(self) -> None:
        for line in (
            "NOTIFIER_NOTIFY_CHAT=yes", "NOTIFIER_NOTIFY_CHAT", 'NOTIFIER_NOTIFY_CHAT="true',
            "NOTIFIER_NOTIFY_PLAN=true", "NOTIFIER_CHANNELS=email",
            "NOTIFIER_CHANNELS=kakao,unsupported", "NOTIFIER_CHANNELS=kakao,kakao",
            "NOTIFIER_CHANNELS=",
        ):
            with self.subTest(line=line):
                self.path.write_text(line, encoding="utf-8")
                with self.assertRaises(ValueError):
                    notifier.load_notification_policy(self.path)


class HookTests(CompletionFixture):
    def invoke(self, hook: dict) -> None:
        with mock.patch.object(notifier.sys, "stdin", io.StringIO(json.dumps(hook))):
            notifier.notify_completion(pathlib.Path(self.temp.name) / "kakao.json")

    def test_actual_stop_payload_shape_sends_once(self) -> None:
        self.write_events(*self.chat_events())
        hook = {
            "sessionId": "real-session", "transcriptPath": str(self.transcript),
            "stopReason": "end_turn", "stop_hook_active": False, "cwd": self.temp.name,
        }

        def send(config_path: pathlib.Path, text: str, operation: str, receipt: pathlib.Path) -> None:
            notifier.write_receipt(receipt, operationId=operation, provider="kakao", status="Succeeded")

        with mock.patch.object(
            notifier, "load_notification_policy",
            return_value={"chat": True, "autopilot": True, "channels": ["kakao"]},
        ), mock.patch.object(notifier, "load_kakao_config", return_value={}), mock.patch.object(
            notifier, "send_kakao", side_effect=send,
        ) as sender:
            self.invoke(hook)
            self.invoke(hook)
        self.assertEqual(sender.call_count, 1)
        self.assertIn("[Chat] 응답 완료", sender.call_args.args[1])

    def test_invalid_stop_or_subagent_does_not_send(self) -> None:
        self.write_events(*self.chat_events())
        base = {
            "session_id": "session", "transcript_path": str(self.transcript),
            "cwd": self.temp.name, "hook_event_name": "Stop",
        }
        with mock.patch.object(
            notifier, "load_notification_policy",
            return_value={"chat": True, "autopilot": True, "channels": ["kakao"]},
        ), mock.patch.object(notifier, "send_kakao") as sender:
            for fields in (
                {"stop_reason": "aborted"}, {"hook_event_name": "SubagentStop"},
                {"agent_id": "child"}, {"stop_hook_active": True}, {},
            ):
                self.invoke({**base, **fields})
        sender.assert_not_called()

    def test_email_only_completion_sends_once_without_kakao_credentials(self) -> None:
        self.write_events(
            self.start,
            {
                "type": "user.message", "timestamp": "2026-10-16T09:00:00Z",
                "data": {"content": "Complete a small change", "agentMode": "autopilot"},
            },
            self.auto,
            self.complete,
        )
        hook = {
            "session_id": "email-session", "transcript_path": str(self.transcript),
            "hook_event_name": "Stop", "stop_reason": "end_turn", "cwd": self.temp.name,
        }
        email_config = {
            "subscriptionId": "test", "endpoint": "https://example.communication.azure.com",
            "sender": "sender@example.test", "recipient": "recipient@example.test",
        }

        def send(config: dict, subject: str, body: str, operation: str, receipt: pathlib.Path) -> None:
            notifier.write_receipt(receipt, operationId=operation, status="Succeeded")

        with mock.patch.object(
            notifier, "load_notification_policy",
            return_value={"chat": False, "autopilot": True, "channels": ["azure_email"]},
        ), mock.patch.object(
            notifier, "validate_email_config", return_value=email_config,
        ), mock.patch.object(
            notifier, "send_email", side_effect=send,
        ) as sender, mock.patch.object(notifier, "load_kakao_config") as kakao:
            self.invoke(hook)
            self.invoke(hook)
        self.assertEqual(sender.call_count, 1)
        self.assertIn("[Autopilot task complete]", sender.call_args.args[1])
        self.assertIn("Duration: 00:01:05", sender.call_args.args[2])
        kakao.assert_not_called()


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
            mode="autopilot", started_at="2026-10-16T09:00:00Z",
            completed_at="2026-10-16T09:01:05Z",
        )
        self.assertEqual(content["subject"], "[Copilot] [Autopilot task complete] tools | Add completion emails")
        self.assertIn("Duration: 00:01:05", content["body"])
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

    def test_chat_label_fits_kakao_limit_including_emoji(self) -> None:
        text = notifier.get_kakao_content(
            "project" * 20, "😀" * 200, "result" * 100,
            "2026-10-16T09:00:00Z", "2026-10-18T10:01:05Z", mode="interactive",
        )
        self.assertTrue(text.startswith("[Chat] 응답 완료\n"))
        self.assertLessEqual(notifier.utf16_length(text), 200)

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

    def test_hook_installer_runs_python_directly_and_is_repeatable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = pathlib.Path(temporary)
            with mock.patch.dict(os.environ, {"COPILOT_HOME": str(home)}):
                notifier.install_hook()
                notifier.install_hook()
            hook_path = home / "hooks" / "tools-copilot-notifier.json"
            hook = json.loads(hook_path.read_text(encoding="utf-8"))
            entry = hook["hooks"]["Stop"][0]
            self.assertEqual(entry["exec"], str(pathlib.Path(sys.executable).resolve()))
            self.assertEqual(entry["args"], [str(pathlib.Path(notifier.__file__).resolve()), "notify-completion"])
            self.assertNotIn("powershell", entry)


def run_tests() -> None:
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise RuntimeError(f"{result.testsRun - len(result.failures) - len(result.errors)} checks passed; tests failed.")


if __name__ == "__main__":
    run_tests()
