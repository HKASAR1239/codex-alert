"""Delivery regression tests: all network requests and visual output are mocked."""

import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest import mock
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_alert import alert


# These are synthetic fixtures; no real user's topic belongs in the repository.
TOPIC = "codex-" + "a" * 32
PHONE = {"provider": "ntfy", "topic": TOPIC}
SECRET = "remote-response-secret"


class PhoneDeliveryTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(alert.urllib.request, "build_opener")
        self.build_opener = patcher.start()
        self.addCleanup(patcher.stop)
        self.response = mock.MagicMock()
        self.response.__enter__.return_value = self.response
        self.build_opener.return_value.open.return_value = self.response

    def reply(self, value):
        self.response.read.return_value = json.dumps(value).encode("utf-8")

    def test_ntfy_posts_utf8_to_fixed_https_endpoint(self):
        message = "Codex complete — café & tea + 2 min."
        self.reply({"event": "message", "id": "accepted", "topic": TOPIC, "message": message})
        self.assertTrue(alert.send_phone(PHONE, message))
        call = self.build_opener.return_value.open.call_args
        request = call.args[0]
        self.assertEqual(request.full_url, "https://ntfy.sh/" + TOPIC)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, message.encode("utf-8"))
        self.assertEqual(call.kwargs["timeout"], 15)

    def test_named_task_unicode_and_duration_reach_ntfy_body(self):
        message = alert.message_for(125.9, "Réviser café — 日本語 👩‍💻")
        expected = "Réviser café — 日本語 👩‍💻\nCompleted in 2 min 05 s."
        self.assertEqual(message, expected)
        self.reply({"event": "message", "id": "accepted", "topic": TOPIC, "message": expected})
        self.assertTrue(alert.send_phone(PHONE, message))
        request = self.build_opener.return_value.open.call_args.args[0]
        self.assertEqual(request.data, expected.encode("utf-8"))
        # Unicode task names belong in the UTF-8 body, not HTTP headers.
        self.assertEqual(request.get_header("Title"), "Codex task complete")

    def test_disabled_phone_makes_no_request(self):
        self.assertFalse(alert.send_phone({"provider": "none"}, "message"))
        self.build_opener.assert_not_called()

    def test_redirects_are_not_followed(self):
        self.assertIsNone(alert.NoRedirect().redirect_request(
            mock.Mock(), None, 302, "Found", {}, "https://other.example/" + TOPIC))

    def test_http_error_redacts_exception_traceback_and_chain(self):
        self.build_opener.return_value.open.side_effect = urllib.error.HTTPError(
            "https://ntfy.sh/" + TOPIC, 403, SECRET, {}, io.BytesIO(SECRET.encode()))
        try:
            alert.send_phone(PHONE, "message")
        except alert.AlertError as error:
            self.assertIn("HTTP 403", str(error))
            rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            self.assertNotIn(SECRET, rendered)
            self.assertNotIn(TOPIC, rendered)
            self.assertIsNone(error.__context__)
            self.assertIsNone(error.__cause__)
        else:
            self.fail("HTTP failure was incorrectly accepted")

    def test_untrusted_errors_are_redacted(self):
        for error in (urllib.error.URLError(TOPIC), RuntimeError(TOPIC), ValueError(TOPIC)):
            with self.subTest(error=type(error).__name__):
                self.build_opener.return_value.open.side_effect = error
                with self.assertRaises(alert.AlertError) as result:
                    alert.send_phone(PHONE, "message")
                self.assertNotIn(TOPIC, str(result.exception))
                self.assertIsNone(result.exception.__context__)

    def test_invalid_and_mismatched_responses_are_not_accepted(self):
        for result in (
            [], None, {"error": SECRET}, {"event": "open", "id": "no-message"},
            {"event": "message"},
            {"event": "message", "id": "bad", "topic": TOPIC, "message": "wrong"},
            {"event": "message", "id": "bad", "topic": "wrong", "message": "message"},
            {"event": "message", "id": [], "topic": TOPIC, "message": "message"},
        ):
            with self.subTest(result=result):
                self.reply(result)
                with self.assertRaises(alert.AlertError):
                    alert.send_phone(PHONE, "message")

    def test_malformed_json_and_oversized_responses_are_redacted(self):
        for body in (b"{not-json-" + SECRET.encode(), b"x" * 65537):
            with self.subTest(length=len(body)):
                self.response.read.return_value = body
                with self.assertRaises(alert.AlertError) as result:
                    alert.send_phone(PHONE, "message")
                self.assertNotIn(SECRET, str(result.exception))
                self.assertIsNone(result.exception.__context__)

    def test_invalid_topics_and_providers_cannot_change_destination(self):
        for phone in (
            {"provider": "unknown", "topic": TOPIC},
            {"provider": "ntfy", "topic": "https://example.com"},
            {"provider": "ntfy", "topic": TOPIC + "\n"},
            {"provider": "ntfy", "topic": "codex-short"},
            {"provider": "ntfy", "topic": None}, [],
        ):
            with self.subTest(phone=phone), self.assertRaises(alert.AlertError):
                alert.send_phone(phone, "message")
        self.build_opener.assert_not_called()


class LocalRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex-alert-tests-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.config = {"min_seconds": 120, "poll_seconds": 3, "flash": True, "phone": PHONE}
        # Block real UI and network even when an assertion fails in a test.
        self.flash_patcher = mock.patch.object(alert, "flash")
        self.flash = self.flash_patcher.start()
        self.addCleanup(self.flash_patcher.stop)
        self.phone_patcher = mock.patch.object(alert, "send_phone", return_value=True)
        self.phone = self.phone_patcher.start()
        self.addCleanup(self.phone_patcher.stop)

    def queued(self, **fields):
        state = {}
        alert.enqueue(state, [dict(seconds=125, turn_id="example-turn", **fields)], self.config)
        return state

    def test_queue_binds_destination_without_storing_topic(self):
        state = self.queued()
        self.assertEqual(state["pending"][0]["phone_destination"], alert.destination_id(PHONE))
        self.assertNotIn(TOPIC, json.dumps(state))

    def test_drain_resolves_matching_threads_without_persisting_or_logging_names(self):
        metadata = self.home / "synthetic-codex"
        metadata.mkdir()
        names = {"thread-one": "Synthetic café review 👩‍💻", "thread-two": "Synthetic 日本語 report"}
        (metadata / "session_index.jsonl").write_text("".join(
            json.dumps({"id": thread_id, "thread_name": name}) + "\n"
            for thread_id, name in names.items()), encoding="utf-8")
        resolver = alert.TitleResolver(metadata)
        state = {}
        alert.enqueue(state, [
            {"thread_id": "thread-two", "turn_id": "turn-two", "seconds": 181},
            {"thread_id": "thread-one", "turn_id": "turn-one", "seconds": 125},
        ], self.config)
        snapshots = [json.dumps(state, ensure_ascii=False)]
        original_save = alert.save_json

        def capture_save(path, value):
            snapshots.append(json.dumps(value, ensure_ascii=False))
            original_save(path, value)

        with mock.patch.object(alert, "save_json", side_effect=capture_save), self.assertLogs(level="INFO") as logs:
            alert.drain(self.home, state, self.config, resolver)
        self.assertEqual(self.phone.call_args_list, [
            mock.call(PHONE, "Synthetic 日本語 report\nCompleted in 3 min 01 s."),
            mock.call(PHONE, "Synthetic café review 👩‍💻\nCompleted in 2 min 05 s."),
        ])
        self.assertEqual(state["pending"], [])
        snapshots.append((self.home / "state.json").read_text(encoding="utf-8"))
        for name in names.values():
            self.assertNotIn(name, "\n".join(snapshots))
            self.assertNotIn(name, "\n".join(logs.output))

    def test_drain_uses_generic_message_when_name_is_missing_or_lookup_fails(self):
        for lookup in (None, RuntimeError("synthetic private task name")):
            with self.subTest(lookup=type(lookup).__name__):
                resolver = mock.Mock(spec=alert.TitleResolver)
                if isinstance(lookup, Exception):
                    resolver.resolve.side_effect = lookup
                else:
                    resolver.resolve.return_value = lookup
                self.phone.reset_mock()
                state = self.queued(thread_id="missing-thread")
                with self.assertLogs(level="INFO") as logs:
                    alert.drain(self.home, state, self.config, resolver)
                resolver.resolve.assert_called_once_with("missing-thread")
                self.phone.assert_called_once_with(
                    PHONE, "Codex task complete — 2 min 05 s. Your Mac is ready.")
                self.assertEqual(state["pending"], [])
                self.assertNotIn("synthetic private task name", "\n".join(logs.output))

    def test_missing_metadata_files_fall_back_without_creating_a_database(self):
        metadata = self.home / "absent-codex"
        state = self.queued(thread_id="unknown-thread")
        alert.drain(self.home, state, self.config, alert.TitleResolver(metadata))
        self.phone.assert_called_once_with(
            PHONE, "Codex task complete — 2 min 05 s. Your Mac is ready.")
        self.assertFalse(metadata.exists())
        self.assertEqual(state["pending"], [])

    def test_disabling_task_names_skips_metadata_lookup(self):
        resolver = mock.Mock(spec=alert.TitleResolver)
        resolver.resolve.side_effect = AssertionError("Task names disabled: do not inspect metadata")
        state = self.queued(thread_id="example-thread")
        alert.drain(self.home, state, dict(self.config, include_task_name=False), resolver)
        resolver.resolve.assert_not_called()
        self.phone.assert_called_once_with(
            PHONE, "Codex task complete — 2 min 05 s. Your Mac is ready.")

    def test_retry_refreshes_renamed_task_without_saving_names_or_logging_errors(self):
        metadata = self.home / "synthetic-codex"
        metadata.mkdir()
        index = metadata / "session_index.jsonl"
        old_name = "Synthetic private draft α"
        new_name = "Synthetic renamed review β"
        index.write_text(json.dumps({"id": "example-thread", "thread_name": old_name}) + "\n",
                         encoding="utf-8")
        resolver = alert.TitleResolver(metadata)
        state = self.queued(thread_id="example-thread")
        self.phone.side_effect = RuntimeError(old_name)
        with mock.patch.object(alert.time, "time", return_value=100), self.assertLogs(level="WARNING") as logs:
            alert.drain(self.home, state, self.config, resolver)
        self.phone.assert_called_once_with(PHONE, old_name + "\nCompleted in 2 min 05 s.")
        persisted = (self.home / "state.json").read_text(encoding="utf-8")
        self.assertNotIn(old_name, persisted)
        self.assertNotIn(old_name, "\n".join(logs.output))
        restarted = json.loads(persisted)
        self.assertEqual(restarted["pending"][0]["attempts"], 1)
        with index.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"id": "example-thread", "thread_name": new_name}) + "\n")
        self.phone.reset_mock(side_effect=True)
        with mock.patch.object(alert.time, "time", return_value=161), self.assertLogs(level="INFO") as retry_logs:
            alert.drain(self.home, restarted, self.config, resolver)
        self.phone.assert_called_once_with(PHONE, new_name + "\nCompleted in 2 min 05 s.")
        self.assertEqual(restarted["pending"], [])
        for name in (old_name, new_name):
            self.assertNotIn(name, (self.home / "state.json").read_text(encoding="utf-8"))
            self.assertNotIn(name, "\n".join(retry_logs.output))

    def test_task_name_lookup_is_deferred_until_phone_retry_is_due(self):
        resolver = mock.Mock(spec=alert.TitleResolver)
        state = self.queued(thread_id="example-thread", local_done=True, retry_at=160)
        with mock.patch.object(alert.time, "time", return_value=159):
            alert.drain(self.home, state, self.config, resolver)
        resolver.resolve.assert_not_called()
        self.phone.assert_not_called()

    def test_phone_is_attempted_and_saved_when_flash_fails(self):
        state = self.queued()
        self.flash.side_effect = subprocess.CalledProcessError(1, "helper")
        with self.assertLogs(level="ERROR"):
            alert.drain(self.home, state, self.config)
        self.flash.assert_called_once_with(self.home)
        self.phone.assert_called_once_with(PHONE, alert.message_for(125))
        self.assertEqual(state["pending"], [])
        self.assertEqual(alert.read_json(self.home / "state.json", {}), state)

    def test_failed_phone_retry_survives_restart_without_repeated_flash(self):
        state = self.queued()
        self.phone.side_effect = RuntimeError(SECRET)
        with mock.patch.object(alert.time, "time", return_value=100), self.assertLogs(level="WARNING") as logs:
            alert.drain(self.home, state, self.config)
        self.flash.assert_called_once()
        self.assertNotIn(SECRET, "\n".join(logs.output))
        restarted = alert.read_json(self.home / "state.json", {})
        item = restarted["pending"][0]
        self.assertTrue(item["local_done"])
        self.assertEqual(item["attempts"], 1)
        self.assertGreater(item["retry_at"], 100)
        self.flash.reset_mock()
        self.phone.reset_mock(side_effect=True)
        with mock.patch.object(alert.time, "time", return_value=item["retry_at"] + 1):
            alert.drain(self.home, restarted, self.config)
        self.flash.assert_not_called()
        self.phone.assert_called_once()
        self.assertEqual(restarted["pending"], [])

    def test_retry_wait_and_six_attempt_limit(self):
        state = self.queued(local_done=True, attempts=5, retry_at=160)
        with mock.patch.object(alert.time, "time", return_value=159):
            alert.drain(self.home, state, self.config)
        self.flash.assert_not_called()
        self.phone.assert_not_called()
        self.phone.side_effect = RuntimeError(SECRET)
        with mock.patch.object(alert.time, "time", return_value=160), self.assertLogs(level="WARNING"):
            alert.drain(self.home, state, self.config)
        self.phone.assert_called_once()
        self.assertEqual(state["pending"], [])

    def test_changed_destination_drops_pending_retry(self):
        state = self.queued(local_done=True, attempts=1, retry_at=999)
        changed = dict(self.config, phone={"provider": "ntfy", "topic": "codex-" + "b" * 32})
        alert.drain(self.home, state, changed)
        self.phone.assert_not_called()
        self.assertEqual(state["pending"], [])

    def test_old_unbound_queue_is_never_sent_to_current_phone(self):
        state = {"pending": [{"seconds": 125, "local_done": True}]}
        alert.drain(self.home, state, self.config)
        self.phone.assert_not_called()
        self.assertEqual(state["pending"], [])

    def test_disabling_phone_drops_pending_retry(self):
        state = self.queued(local_done=True)
        alert.drain(self.home, state, dict(self.config, phone={"provider": "none"}))
        self.phone.assert_not_called()
        self.assertEqual(state["pending"], [])

    def test_enabling_phone_does_not_replay_local_only_alert(self):
        state = {}
        alert.enqueue(state, [{"seconds": 125}], dict(self.config, phone={"provider": "none"}))
        alert.drain(self.home, state, self.config)
        self.flash.assert_called_once()
        self.phone.assert_not_called()
        self.assertEqual(state["pending"], [])

    def test_config_save_is_private_atomic_and_removes_old_fields(self):
        path = self.home / "config.json"
        alert.save_json(path, dict(self.config, secret=SECRET,
                                 phone=dict(PHONE, obsolete_credential=SECRET)))
        clean = alert.config_for(self.home)
        alert.save_json(path, clean)
        self.assertNotIn(SECRET, path.read_text())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(list(self.home.iterdir()), [path])

    def test_config_validates_ranges_types_and_nonfinite_numbers(self):
        for field, value in (
            ("min_seconds", True), ("min_seconds", -1), ("min_seconds", float("nan")),
            ("min_seconds", float("inf")), ("poll_seconds", 0), ("poll_seconds", 61),
            ("flash", "yes"), ("phone", []), ("include_task_name", "yes"),
            ("include_task_name", None),
        ):
            with self.subTest(field=field, value=value):
                alert.save_json(self.home / "config.json", {field: value})
                with self.assertRaises(alert.AlertError):
                    alert.config_for(self.home)

    def test_task_name_setting_defaults_on_and_preserves_explicit_opt_out(self):
        self.assertTrue(alert.config_for(self.home)["include_task_name"])
        alert.save_json(self.home / "config.json", dict(self.config, include_task_name=False))
        self.assertFalse(alert.config_for(self.home)["include_task_name"])

    def test_setup_refuses_redirected_input_or_output_without_exposing_topic(self):
        alert.save_json(self.home / "config.json", self.config)
        for terminal_in, terminal_out in ((False, True), (True, False), (False, False)):
            with self.subTest(stdin=terminal_in, stdout=terminal_out), \
                 mock.patch.object(alert.sys.stdin, "isatty", return_value=terminal_in), \
                 mock.patch.object(alert.sys, "stdout") as output, \
                 mock.patch("builtins.input") as input_fn:
                output.isatty.return_value = terminal_out
                with self.assertRaises(alert.AlertError):
                    alert.configure_phone(self.home)
                output.write.assert_not_called()
                input_fn.assert_not_called()
        self.phone.assert_not_called()

    def configure_interactively(self):
        with mock.patch.object(alert.sys.stdin, "isatty", return_value=True), \
             mock.patch.object(alert.sys, "stdout") as output, \
             mock.patch("builtins.input", return_value=""):
            output.isatty.return_value = True
            alert.configure_phone(self.home)

    def test_setup_reuses_existing_topic(self):
        alert.save_json(self.home / "config.json", self.config)
        with mock.patch.object(alert.secrets, "token_hex") as generate:
            self.configure_interactively()
        generate.assert_not_called()
        self.phone.assert_called_once_with(PHONE, alert.TEST_MESSAGE)
        self.assertEqual(alert.config_for(self.home)["phone"], PHONE)

    def test_new_setup_generates_128_bit_topic(self):
        with mock.patch.object(alert.secrets, "token_hex", return_value="a" * 32) as generate:
            self.configure_interactively()
        generate.assert_called_once_with(16)
        self.assertEqual(alert.config_for(self.home)["phone"], PHONE)

    def test_failed_setup_never_saves_unconfirmed_phone(self):
        self.phone.side_effect = alert.AlertError("Test unavailable.")
        with self.assertRaises(alert.AlertError):
            self.configure_interactively()
        self.assertFalse((self.home / "config.json").exists())

    def test_setup_repairs_obsolete_provider_without_retaining_credentials(self):
        alert.save_json(self.home / "config.json", {
            "phone": {"provider": "obsolete", "credential": SECRET}})
        with mock.patch.object(alert.secrets, "token_hex", return_value="a" * 32):
            self.configure_interactively()
        self.assertEqual(alert.config_for(self.home)["phone"], PHONE)
        self.assertNotIn(SECRET, (self.home / "config.json").read_text())

    def test_phone_off_repairs_invalid_topic_and_removes_it(self):
        alert.save_json(self.home / "config.json", {"phone": {"provider": "ntfy", "topic": SECRET}})
        with mock.patch.object(alert.sys, "stdout", new_callable=io.StringIO):
            result = alert.main(["--home", str(self.home), "phone-off"])
        self.assertEqual(result, 0)
        self.assertEqual(alert.config_for(self.home)["phone"], {"provider": "none"})
        self.assertNotIn(SECRET, (self.home / "config.json").read_text())

    def test_cli_never_prints_untrusted_runtime_error(self):
        self.flash.side_effect = RuntimeError(SECRET)
        with mock.patch.object(alert.sys, "stderr", new_callable=io.StringIO) as output:
            result = alert.main(["--home", str(self.home), "test-flash"])
        self.assertEqual(result, 1)
        self.assertNotIn(SECRET, output.getvalue())

    def test_status_is_redacted_and_checks_lock_not_just_heartbeat(self):
        alert.save_json(self.home / "config.json", self.config)
        alert.save_json(self.home / "state.json", {"heartbeat_at": 100})
        with mock.patch.object(alert.time, "time", return_value=110), \
             mock.patch.object(alert, "watcher_running", return_value=False), \
             mock.patch.object(alert.sys, "stdout", new_callable=io.StringIO) as output:
            result = alert.main(["--home", str(self.home), "status", "--json"])
        self.assertEqual(result, 0)
        status = json.loads(output.getvalue())
        self.assertEqual(status["watcher"], "inactive or waiting")
        self.assertEqual(status["phone"], "ntfy")
        self.assertNotIn(TOPIC, output.getvalue())

    def test_custom_codex_home_is_resolved(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home / "custom")}):
            self.assertEqual(alert.default_sessions(), self.home / "custom/sessions")


if __name__ == "__main__":
    unittest.main()
