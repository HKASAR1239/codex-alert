"""Power management tests: no real native commands or settings are touched."""

from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from codex_alert import power


class KeepAwakeTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.processes = []
        self.clock = self.patch(power.time, "monotonic", side_effect=lambda: self.now)
        self.patch(power.sys, "platform", "darwin")
        self.patch(power.os, "getpid", return_value=12345)
        self.run = self.patch(power.subprocess, "run", return_value=mock.Mock(
            returncode=0, stdout="Now drawing from 'AC Power'\n -InternalBattery-0 80%\n"))
        self.popen = self.patch(power.subprocess, "Popen", side_effect=self.new_process)
        self.awake = power.KeepAwake()
        self.addCleanup(self.awake.close)

    def patch(self, target, name, *args, **kwargs):
        patcher = mock.patch.object(target, name, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def new_process(self, *args, **kwargs):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        self.processes.append(process)
        return process

    def test_default_requires_ac_and_asserts_only_idle_sleep(self):
        self.awake.update(True)
        self.assertTrue(self.awake.active)
        self.run.assert_called_once()
        args, kwargs = self.run.call_args
        self.assertEqual(args[0], ["/usr/bin/pmset", "-g", "batt"])
        self.assertEqual(kwargs["timeout"], 2)
        self.assertEqual(kwargs["env"]["LC_ALL"], "C")
        self.popen.assert_called_once_with(
            ["/usr/bin/caffeinate", "-i", "-t", "90", "-w", "12345"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
        )

    def test_always_mode_works_without_reading_power_source(self):
        self.awake.update(True, "always")
        self.assertTrue(self.awake.active)
        self.run.assert_not_called()

    def test_disabled_idle_and_unsupported_platform_do_not_launch_commands(self):
        for active, mode in ((False, "always"), (True, "off"), (True, "unknown")):
            self.awake.update(active, mode)
        with mock.patch.object(power.sys, "platform", "linux"):
            self.awake.update(True, "always")
        self.popen.assert_not_called()
        self.run.assert_not_called()
        self.assertFalse(self.awake.active)

    def test_battery_and_unknown_source_fail_closed(self):
        for output, returncode in (
            ("Now drawing from 'Battery Power'\n", 0),
            ("Now drawing from 'UPS Power'\n", 0),
            ("unexpected AC Power text", 0),
            ("", 0),
            ("Now drawing from 'AC Power'\n", 1),
            (None, 0),
        ):
            with self.subTest(output=output, returncode=returncode):
                self.awake.close()
                self.run.return_value = mock.Mock(stdout=output, returncode=returncode)
                self.awake.update(True)
                self.assertFalse(self.awake.active)
        self.popen.assert_not_called()

    def test_source_check_cadence_and_unplug_release(self):
        self.awake.update(True)
        first = self.processes[0]
        self.run.return_value.stdout = "Now drawing from 'Battery Power'\n"
        self.now += power.POWER_CHECK_SECONDS - 0.1
        self.awake.update(True)
        self.assertTrue(self.awake.active)
        self.run.assert_called_once()
        self.now += 0.1
        self.awake.update(True)
        self.assertFalse(self.awake.active)
        first.terminate.assert_called_once()
        self.assertEqual(self.run.call_count, 2)
        self.now += 1
        self.awake.update(True)
        self.assertEqual(self.run.call_count, 2)

    def test_switching_from_always_to_plugged_in_checks_immediately(self):
        self.awake.update(True, "always")
        self.run.return_value.stdout = "Now drawing from 'Battery Power'\n"
        self.awake.update(True, "plugged_in")
        self.run.assert_called_once()
        self.assertFalse(self.awake.active)
        self.processes[0].terminate.assert_called_once()

    def test_renewal_acquires_replacement_before_releasing_previous(self):
        self.awake.update(True, "always")
        first = self.processes[0]
        first.terminate.side_effect = lambda: self.assertEqual(self.popen.call_count, 2)
        self.now += power.RENEW_SECONDS - 0.1
        self.awake.update(True, "always")
        self.popen.assert_called_once()
        self.now += 0.1
        self.awake.update(True, "always")
        self.assertEqual(self.popen.call_count, 2)
        first.terminate.assert_called_once()
        self.processes[1].terminate.assert_not_called()
        self.assertTrue(self.awake.active)

    def test_no_work_and_close_release_only_owned_child_and_are_idempotent(self):
        self.awake.update(True, "always")
        first = self.processes[0]
        self.awake.update(False, "always")
        self.awake.close()
        self.assertFalse(self.awake.active)
        first.terminate.assert_called_once()
        first.wait.assert_called_once_with(timeout=power.PROCESS_TIMEOUT)
        first.kill.assert_not_called()

    def test_disabling_releases_owned_child(self):
        self.awake.update(True, "always")
        self.awake.update(True, "off")
        self.assertFalse(self.awake.active)
        self.processes[0].terminate.assert_called_once()

    def test_finished_child_is_reaped_without_signals_and_restart_is_throttled(self):
        self.awake.update(True, "always")
        first = self.processes[0]
        first.poll.return_value = 1
        self.assertFalse(self.awake.active)
        self.now += 1
        self.awake.update(True, "always")
        self.now += power.RETRY_SECONDS - 0.1
        self.awake.update(True, "always")
        self.popen.assert_called_once()
        self.now += 0.1
        self.awake.update(True, "always")
        self.assertEqual(self.popen.call_count, 2)
        first.terminate.assert_not_called()
        first.kill.assert_not_called()

    def test_launch_error_is_swallowed_and_retried_with_backoff(self):
        self.popen.side_effect = OSError("native tool unavailable")
        self.awake.update(True, "always")
        self.assertFalse(self.awake.active)
        self.now += power.RETRY_SECONDS - 0.1
        self.awake.update(True, "always")
        self.popen.assert_called_once()
        self.now += 0.1
        self.awake.update(True, "always")
        self.assertEqual(self.popen.call_count, 2)

    def test_failed_renewal_keeps_existing_lease_and_retries(self):
        self.awake.update(True, "always")
        first = self.processes[0]
        self.popen.side_effect = OSError("native tool unavailable")
        self.now += power.RENEW_SECONDS
        self.awake.update(True, "always")
        self.assertTrue(self.awake.active)
        first.terminate.assert_not_called()
        self.now += 1
        self.awake.update(True, "always")
        self.assertEqual(self.popen.call_count, 2)
        self.popen.side_effect = self.new_process
        self.now += power.RETRY_SECONDS
        self.awake.update(True, "always")
        self.assertEqual(self.popen.call_count, 3)
        first.terminate.assert_called_once()

    def test_power_query_error_releases_previous_assertion(self):
        for error in (subprocess.TimeoutExpired("pmset", 2), OSError("no pmset"),
                      RuntimeError("native output failure")):
            with self.subTest(error=type(error).__name__):
                self.run.side_effect = None
                self.awake.update(True)
                first = self.processes[-1]
                self.run.side_effect = error
                self.now += power.POWER_CHECK_SECONDS
                self.awake.update(True)
                self.assertFalse(self.awake.active)
                first.terminate.assert_called_once()
                self.awake.close()

    def test_cleanup_escalates_only_own_child_after_bounded_wait(self):
        self.awake.update(True, "always")
        first = self.processes[0]
        first.wait.side_effect = [subprocess.TimeoutExpired("caffeinate", 0.25), 0]
        self.awake.close()
        first.terminate.assert_called_once()
        first.kill.assert_called_once()
        self.assertEqual(first.wait.call_args_list, [
            mock.call(timeout=power.PROCESS_TIMEOUT),
            mock.call(timeout=power.PROCESS_TIMEOUT),
        ])

    def test_cleanup_and_poll_errors_never_escape(self):
        self.awake.update(True, "always")
        first = self.processes[0]
        for method in (first.poll, first.terminate, first.kill, first.wait):
            method.side_effect = OSError("native process unavailable")
        self.assertFalse(self.awake.active)
        self.awake.update(True, "always")
        self.assertFalse(self.awake.active)
        first.terminate.assert_called_once()
        first.kill.assert_called_once()
        self.awake.close()

    def test_closed_manager_can_start_again_with_fresh_source_check(self):
        self.awake.update(True)
        self.awake.close()
        self.awake.update(True)
        self.assertEqual(self.run.call_count, 2)
        self.assertEqual(self.popen.call_count, 2)
        self.assertTrue(self.awake.active)


if __name__ == "__main__":
    unittest.main()
