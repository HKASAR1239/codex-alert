import fcntl
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_alert import attention


class AttentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.state = {"activated_at": 900, "watcher": {"terminal": {}}}

    def payload(self, event="PermissionRequest", **changes):
        payload = {"hook_event_name": event, "session_id": "thread", "turn_id": "turn",
                   "tool_name": "Bash", "tool_input": {"command": "private command $(never-run)",
                                                           "description": "private reason"}}
        payload.update(changes)
        return payload

    def capture(self, event="PermissionRequest", now=1000, **changes):
        data = json.dumps(self.payload(event, **changes)).encode("utf-8")
        return attention.capture(self.home, event, io.BytesIO(data), now=now)

    def poll(self, now=1000):
        return attention.poll(self.home, self.state, now=now)

    def test_approval_alert_contains_only_metadata_and_never_a_decision(self):
        self.assertTrue(self.capture())
        alerts = self.poll()
        self.assertEqual(len(alerts), 1)
        item = alerts[0]
        self.assertEqual(item["kind"], "attention")
        self.assertEqual(item["thread_id"], "thread")
        self.assertEqual(item["turn_id"], "turn")
        self.assertEqual(item["seconds"], 0)
        self.assertEqual(item["completed_at"], 1000)
        self.assertTrue(attention.is_pending(self.home, item, now=1001))
        saved = (self.home / "attention.json").read_text()
        for secret in ("private command", "private reason", "Bash", "never-run", "decision"):
            self.assertNotIn(secret, saved)
            self.assertNotIn(secret, json.dumps(alerts))
            self.assertNotIn(secret, json.dumps(self.state))
        self.assertEqual(stat.S_IMODE((self.home / "attention.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.home / "attention.lock").stat().st_mode), 0o600)

    def test_duplicate_permission_event_deduplicates_across_state_restart(self):
        self.capture()
        first = self.poll()[0]
        self.capture(now=1005)
        self.state = json.loads(json.dumps(self.state))
        self.assertEqual(self.poll(1005), [])
        saved = json.loads((self.home / "attention.json").read_text())
        self.assertEqual(saved["pending"][first["id"]]["created_at"], 1000)

    def test_post_tool_use_resolves_only_matching_tool_arguments_and_turn(self):
        self.capture()
        first = self.poll()[0]
        self.capture(turn_id="other-turn")
        second = self.poll()[0]
        self.capture("PostToolUse", tool_input={"command": "different"})
        self.assertTrue(attention.is_pending(self.home, first, now=1000))
        self.capture("PostToolUse")
        self.assertFalse(attention.is_pending(self.home, first, now=1000))
        self.assertTrue(attention.is_pending(self.home, second, now=1000))

    def test_input_object_key_order_does_not_change_resolution(self):
        self.capture(tool_input={"z": 1, "a": 2})
        item = self.poll()[0]
        self.capture("PostToolUse", tool_input={"a": 2, "z": 1})
        self.assertFalse(attention.is_pending(self.home, item, now=1000))

    def test_stop_and_interrupt_cancel_all_requests_for_exact_turn(self):
        for event in ("Stop", "Interrupt"):
            with self.subTest(event=event):
                self.capture(now=1000)
                self.capture(now=1000, tool_input={"command": "another"})
                self.capture(now=1000, turn_id="other")
                current = attention._load(self.home, 1000)
                self.assertEqual(len(current), 3)
                self.assertTrue(self.capture(event, now=1001))
                self.assertEqual([x["turn_id"] for x in attention._load(self.home, 1001).values()], ["other"])

    def test_completion_state_cancels_unsent_alert_without_stop_hook(self):
        self.capture()
        item = self.poll()[0]
        self.state["watcher"]["terminal"]["thread:turn"] = 1001
        self.assertEqual(self.poll(1001), [])
        self.assertFalse(attention.is_pending(self.home, item, now=1001))

    def test_already_resolved_request_never_enters_outbox(self):
        self.capture()
        self.capture("PostToolUse", now=1001)
        self.assertEqual(self.poll(1001), [])

    def test_expiry_limits_delayed_attention_and_seen_state(self):
        self.capture()
        item = self.poll()[0]
        end = 1000 + attention.EXPIRY_SECONDS
        self.assertTrue(attention.is_pending(self.home, item, now=end))
        self.assertFalse(attention.is_pending(self.home, item, now=end + 1))
        self.assertEqual(self.poll(end + 1), [])
        self.assertEqual(self.state["attention_seen"], {})

    def test_events_before_activation_are_not_replayed(self):
        self.capture(now=800)
        self.assertEqual(self.poll(1000), [])

    def test_future_timestamps_never_become_queued(self):
        self.capture(now=2000)
        self.assertEqual(self.poll(1000), [])
        self.assertEqual(self.poll(float("nan")), [])

    def test_invalid_hook_input_has_no_side_effect(self):
        payloads = [None, [], {}, self.payload(session_id="private title with spaces"),
                    self.payload(turn_id=""), self.payload(hook_event_name="PostToolUse"),
                    self.payload(tool_name=False), self.payload(tool_input=float("nan"))]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertFalse(attention.capture(self.home, "PermissionRequest",
                                                   io.StringIO(json.dumps(payload)), now=1000))
        self.assertFalse(attention.capture(self.home, "Unsupported", io.StringIO("{}"), now=1000))
        self.assertFalse((self.home / "attention.json").exists())

    def test_oversized_or_malformed_input_is_ignored_without_text_written(self):
        for raw in (b"{broken", b"x" * (attention.MAX_INPUT_BYTES + 1)):
            self.assertFalse(attention.capture(self.home, "PermissionRequest", io.BytesIO(raw), now=1000))
        self.assertFalse((self.home / "attention.json").exists())

    def test_busy_sidecar_skips_hook_instead_of_delaying_approval(self):
        with (self.home / "attention.lock").open("w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(self.capture())
        self.assertFalse((self.home / "attention.json").exists())

    def test_filesystem_failure_is_nonfatal_and_leaves_no_temporary_file(self):
        with patch.object(attention.os, "replace", side_effect=OSError("unavailable")):
            self.assertFalse(self.capture())
        self.assertEqual(list(self.home.glob(".attention-*")), [])

    def test_sidecar_is_bounded_and_corruption_does_not_break_watcher(self):
        with patch.object(attention, "MAX_PENDING", 2):
            self.capture(turn_id="first", now=1000)
            self.capture(turn_id="second", now=1001)
            self.capture(turn_id="third", now=1002)
            self.assertEqual({item["turn_id"] for item in self.poll(1002)}, {"second", "third"})
        for content in ("{bad", "[]", '{"pending":false}', '{"pending":{"wrong":{}}}'):
            (self.home / "attention.json").write_text(content)
            self.assertEqual(self.poll(), [])

    def test_missing_optional_hooks_leave_no_files_or_state(self):
        self.assertEqual(self.poll(), [])
        self.assertNotIn("attention_seen", self.state)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_hook_bundle_uses_only_non_deciding_documented_events(self):
        root = Path(__file__).resolve().parents[1]
        hooks = json.loads((root / "hooks/hooks.json").read_text())["hooks"]
        self.assertEqual(set(hooks), attention.EVENTS)
        for event, groups in hooks.items():
            handler = groups[0]["hooks"][0]
            self.assertEqual(handler["type"], "command")
            self.assertIn('"$PLUGIN_ROOT/scripts/codex-alert-hook.sh"', handler["command"])
            self.assertTrue(handler["command"].endswith(" " + event))
            self.assertLessEqual(handler["timeout"], 3)


if __name__ == "__main__":
    unittest.main()
