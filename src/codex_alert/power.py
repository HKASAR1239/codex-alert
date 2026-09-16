"""Short macOS idle-sleep assertions that leave display sleep unchanged."""

from __future__ import annotations

import os
import subprocess
import sys
import time


LEASE_SECONDS = 90
RENEW_SECONDS = 45
POWER_CHECK_SECONDS = 15
RETRY_SECONDS = 10
PROCESS_TIMEOUT = 0.25


class KeepAwake:
    """Keep active work running, using only child processes owned by this object.

    Each assertion expires after 90 seconds unless renewed by a healthy watcher,
    and also ends when the watcher exits. ``-i`` prevents only idle system sleep:
    it neither lights the display nor overrides deliberate sleep or lid closure.
    An unavailable power source or native command leaves normal sleep enabled.
    """

    def __init__(self):
        self._process = None
        self._renew_at = 0.0
        self._retry_at = 0.0
        self._power_check_at = 0.0
        self._on_ac = False
        self._mode = "off"

    @property
    def active(self) -> bool:
        """Whether our current assertion process is still running."""
        try:
            return self._process is not None and self._process.poll() is None
        except Exception:
            return False

    def update(self, active: bool, mode: str = "plugged_in") -> None:
        """Refresh the assertion during work; all native failures are best effort."""
        if not active or mode not in ("plugged_in", "always") or sys.platform != "darwin":
            self.close()
            return

        now = time.monotonic()
        if mode != self._mode:
            # Switching back from battery mode must check the current source.
            self._power_check_at = 0.0
            self._on_ac = False
            self._retry_at = 0.0
            self._mode = mode

        if mode == "plugged_in":
            if now >= self._power_check_at:
                self._power_check_at = now + POWER_CHECK_SECONDS
                self._on_ac = self._ac_power()
            if not self._on_ac:
                self._release()
                return

        if self._process is not None:
            try:
                running = self._process.poll() is None
            except Exception:
                self._release()
                self._retry_at = now + RETRY_SECONDS
                return
            if running:
                if now < self._renew_at:
                    return
            else:
                # An unexpectedly exiting native tool must not create a tight
                # spawn loop, even if update() is called very frequently.
                self._process = None
                self._renew_at = 0.0
                self._retry_at = max(self._retry_at, now + RETRY_SECONDS)

        if now < self._retry_at:
            return
        self._retry_at = now + RETRY_SECONDS
        try:
            replacement = subprocess.Popen(
                ["/usr/bin/caffeinate", "-i", "-t", str(LEASE_SECONDS),
                 "-w", str(os.getpid())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True,
            )
        except Exception:
            return

        previous, self._process = self._process, replacement
        self._renew_at = now + RENEW_SECONDS
        if previous is not None:
            # Acquire the replacement before releasing the previous assertion.
            self._stop(previous)

    def close(self) -> None:
        """Release our assertion promptly and reset cached state."""
        self._release()
        self._mode = "off"
        self._on_ac = False
        self._power_check_at = 0.0
        self._retry_at = 0.0

    def _release(self) -> None:
        process, self._process = self._process, None
        self._renew_at = 0.0
        if process is not None:
            self._stop(process)

    @staticmethod
    def _ac_power() -> bool:
        try:
            result = subprocess.run(
                ["/usr/bin/pmset", "-g", "batt"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, timeout=2, check=False,
                env=dict(os.environ, LC_ALL="C"),
            )
            lines = result.stdout.splitlines()
            return (result.returncode == 0 and bool(lines)
                    and lines[0].strip() == "Now drawing from 'AC Power'")
        except Exception:
            return False

    @staticmethod
    def _stop(process) -> None:
        # Never signal by a discovered PID or process name: these handles belong
        # exclusively to children started above. Even a failed cleanup has the
        # native timeout and parent lifetime as independent release mechanisms.
        try:
            if process.poll() is not None:
                return
        except Exception:
            pass
        try:
            process.terminate()
        except Exception:
            pass
        try:
            process.wait(timeout=PROCESS_TIMEOUT)
            return
        except Exception:
            pass
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=PROCESS_TIMEOUT)
        except Exception:
            pass
