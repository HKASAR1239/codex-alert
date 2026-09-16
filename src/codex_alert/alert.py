"""Local Codex completion alerts. Python standard library only."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from .watcher import Watcher
from .titles import TitleResolver, sanitize_title


DEFAULT_HOME = Path.home() / "Library/Application Support/CodexAlert"
DEFAULT_CONFIG = {
    "min_seconds": 120,
    "poll_seconds": 3,
    "flash": True,
    "include_task_name": True,
    "phone": {"provider": "none"},
}
NTFY_SERVER = "https://ntfy.sh"
TOPIC_PATTERN = re.compile(r"codex-[a-f0-9]{32}")
TEST_MESSAGE = "Codex Alert test: phone notifications are ready."


class AlertError(RuntimeError):
    """An error with a fixed, safe-to-display message (never remote text)."""


def default_sessions() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser() / "sessions"


def save_json(path: Path, value: dict) -> None:
    """Atomically replace a file, preserving owner-only access (0600)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path: Path, default: dict) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def clean_phone(phone: dict) -> dict:
    """Validate and discard unknown fields, including obsolete credentials."""
    if not isinstance(phone, dict):
        raise AlertError("Invalid phone configuration.")
    provider = phone.get("provider", "none")
    if provider == "none":
        return {"provider": "none"}
    if provider != "ntfy":
        raise AlertError("Unsupported phone provider. Run configure-phone to use ntfy.")
    topic = phone.get("topic")
    if not isinstance(topic, str) or not TOPIC_PATTERN.fullmatch(topic):
        raise AlertError("Invalid ntfy topic. Run configure-phone to set up notifications.")
    return {"provider": "ntfy", "topic": topic}


def config_for(home: Path, *, reset_invalid_phone: bool = False) -> dict:
    raw = read_json(home / "config.json", {})
    if not isinstance(raw, dict):
        raise AlertError("Invalid configuration: expected a JSON object.")
    config = {}
    for field, low, high in (("min_seconds", 0, 86400), ("poll_seconds", 1, 60)):
        value = raw.get(field, DEFAULT_CONFIG[field])
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not low <= value <= high):
            raise AlertError("Invalid configuration field: " + field)
        config[field] = value
    config["flash"] = raw.get("flash", True)
    if not isinstance(config["flash"], bool):
        raise AlertError("Invalid configuration field: flash")
    config["include_task_name"] = raw.get("include_task_name", True)
    if not isinstance(config["include_task_name"], bool):
        raise AlertError("Invalid configuration field: include_task_name")
    try:
        config["phone"] = clean_phone(raw.get("phone", {"provider": "none"}))
    except AlertError:
        if not reset_invalid_phone:
            raise
        # Setup and phone-off can repair an obsolete provider or damaged topic.
        config["phone"] = {"provider": "none"}
    return config


def message_for(seconds: float, task_name: str | None = None) -> str:
    minutes, remainder = divmod(max(0, int(seconds)), 60)
    task_name = sanitize_title(task_name)
    if task_name:
        return f"{task_name}\nCompleted in {minutes} min {remainder:02d} s."
    return f"Codex task complete — {minutes} min {remainder:02d} s. Your Mac is ready."


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_phone(phone: dict, message: str) -> bool:
    """Send an alert; never propagate a URL, body or error chain."""
    phone = clean_phone(phone)
    if phone["provider"] == "none":
        return False
    topic = phone["topic"]
    request = urllib.request.Request(
        NTFY_SERVER + "/" + topic,
        data=message.encode("utf-8"),
        method="POST",
        headers={"Title": "Codex task complete", "Tags": "white_check_mark",
                 "Content-Type": "text/plain; charset=utf-8"},
    )
    failure = None
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            body = response.read(65537)
            if len(body) > 65536:
                failure = "Notification service response was too large."
            else:
                result = json.loads(body)
                if (not isinstance(result, dict) or result.get("event") != "message"
                        or not isinstance(result.get("id"), str) or not result["id"]
                        or result.get("error") or result.get("topic") != topic
                        or result.get("message") != message):
                    failure = "Notification service did not confirm acceptance."
    except urllib.error.HTTPError as error:
        # Only a numeric HTTP code is allowed into the user-facing message.
        code = error.code if isinstance(error.code, int) else "error"
        failure = f"Notification service returned HTTP {code}."
    except (urllib.error.URLError, TimeoutError, OSError):
        failure = "Notification service is unavailable or timed out."
    except Exception:
        failure = "Notification service returned an invalid response."
    # Raise outside the exception handler: no secret-bearing __context__ remains.
    if failure:
        raise AlertError(failure)
    return True


def destination_id(phone: dict) -> str | None:
    """Bind an outbox item to its recipient without retaining their topic."""
    phone = clean_phone(phone)
    if phone["provider"] == "none":
        return None
    return hashlib.sha256((NTFY_SERVER + "/" + phone["topic"]).encode()).hexdigest()


def enqueue(state: dict, events: list[dict], config: dict) -> None:
    destination = destination_id(config["phone"])
    pending = state.setdefault("pending", [])
    for event in events:
        pending.append(dict(event, phone_destination=destination,
                            phone_done=destination is None))


def flash(home: Path) -> None:
    subprocess.run([str(home / "bin/codex-flash")], stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=10, check=True)


def drain(home: Path, state: dict, config: dict, titles: TitleResolver | None = None) -> None:
    pending = state.setdefault("pending", [])
    destination = destination_id(config["phone"])
    for item in list(pending):
        if not item.get("local_done"):
            # At most once: a crash after this save may skip the visual alert.
            item["local_done"] = True
            save_json(home / "state.json", state)
            if config["flash"]:
                try:
                    flash(home)
                except (OSError, subprocess.SubprocessError):
                    logging.error("Flash unavailable; phone delivery will still be attempted.")
        if not item.get("phone_done"):
            # Old unbound queues, disabled phones and changed destinations are
            # discarded rather than delivering an old task to a new recipient.
            if destination is None or item.get("phone_destination") != destination:
                item["phone_done"] = True
                logging.info("Discarded an alert for a previous phone configuration.")
            elif time.time() >= item.get("retry_at", 0):
                try:
                    task_name = None
                    if config.get("include_task_name", True) and titles is not None:
                        try:
                            task_name = titles.resolve(item.get("thread_id"))
                        except Exception:
                            # Missing or changed Codex metadata never blocks delivery.
                            pass
                    send_phone(config["phone"], message_for(item["seconds"], task_name))
                    item["phone_done"] = True
                    logging.info("Notification accepted; task duration %.1f s.", item["seconds"])
                except Exception:
                    item["attempts"] = item.get("attempts", 0) + 1
                    logging.warning("Phone delivery unconfirmed, attempt %s.", item["attempts"])
                    item["retry_at"] = time.time() + min(3600, 30 * 2 ** min(item["attempts"], 7))
                    if item["attempts"] >= 6:
                        item["phone_done"] = True
                        logging.error("Stopped retrying this phone alert after six attempts.")
        if item.get("local_done") and item.get("phone_done"):
            pending.remove(item)
        save_json(home / "state.json", state)


def watch(home: Path, sessions: Path) -> None:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (home / "watcher.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlertError("The watcher is already running.") from None
        handler = RotatingFileHandler(home / "alerts.log", maxBytes=256_000, backupCount=2)
        logging.basicConfig(handlers=[handler], level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        state = read_json(home / "state.json", {})
        state.setdefault("activated_at", time.time())
        monitor = Watcher(sessions, state.setdefault("watcher", {}), state["activated_at"])
        titles = TitleResolver(sessions.parent)
        save_json(home / "state.json", state)
        logging.info("Watcher started.")
        while True:
            try:
                config = config_for(home)
                monitor.min_seconds = config["min_seconds"]
                enqueue(state, monitor.poll(), config)
                state["heartbeat_at"] = time.time()
                save_json(home / "state.json", state)
                drain(home, state, config, titles)
                time.sleep(config["poll_seconds"])
            except KeyboardInterrupt:
                break
            except Exception:
                # Never log exception text, config, requests or session records.
                logging.error("Watcher paused after a local error; retrying in 10 seconds.")
                time.sleep(10)


def configure_phone(home: Path) -> None:
    # A redirected terminal would otherwise leak the topic into tool transcripts
    # or files. There is deliberately no piped-input or fallback-echo mode.
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise AlertError("Run install.command configure-phone in a normal Terminal to set up ntfy privately.")
    config = config_for(home, reset_invalid_phone=True)
    phone = config["phone"]
    if phone["provider"] != "ntfy":
        phone = {"provider": "ntfy", "topic": "codex-" + secrets.token_hex(16)}
    print("Install ntfy on your phone: https://ntfy.sh")
    print("Allow notifications, then subscribe with these details:")
    print("Server: " + NTFY_SERVER + "\nTopic: " + phone["topic"])
    print("Keep this random topic private: anyone who knows it can read and send alerts.")
    print("ntfy receives the conversation name and duration; the name may appear on your lock screen."
          if config["include_task_name"] else
          "ntfy receives a generic completion message and the task duration.")
    input("Once subscribed, press Return to send a test notification: ")
    send_phone(phone, TEST_MESSAGE)
    config["phone"] = phone
    save_json(home / "config.json", config)
    print("Test accepted by ntfy. Check that it arrived on your phone.")
    print("Configuration saved; the watcher will use it automatically.")


def watcher_running(home: Path) -> bool:
    try:
        with (home / "watcher.lock").open("r") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except FileNotFoundError:
        pass
    return False


def status_for(home: Path, sessions: Path) -> dict:
    config = config_for(home)
    state = read_json(home / "state.json", {})
    age = time.time() - state.get("heartbeat_at", 0)
    return {
        "watcher": "active" if watcher_running(home) and 0 <= age < 60 else "inactive or waiting",
        "min_seconds": config["min_seconds"],
        "flash": config["flash"],
        "include_task_name": config["include_task_name"],
        "phone": config["phone"]["provider"],
        "pending_alerts": len(state.get("pending", [])),
        "sessions_available": sessions.is_dir(),
    }


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Codex completion alerts for Mac and phone")
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME, help="Private runtime directory")
    parser.add_argument("--sessions", type=Path, default=default_sessions(), help="Codex sessions directory")
    parser.add_argument("--json", action="store_true", help="Print status as JSON")
    parser.add_argument("command", choices=["watch", "status", "test-flash", "test-phone",
                                           "configure-phone", "phone-off"])
    args = parser.parse_args(argv)
    home, sessions = args.home.expanduser().resolve(), args.sessions.expanduser().resolve()
    try:
        if args.command == "watch":
            watch(home, sessions)
        elif args.command == "status":
            status = status_for(home, sessions)
            if args.json:
                print(json.dumps(status, indent=2))
            else:
                print("Watcher: " + status["watcher"])
                print(f"Threshold: more than {status['min_seconds']:g} seconds")
                print("Mac flash: " + ("on" if status["flash"] else "off"))
                print("Phone: " + status["phone"])
                print("Task names: " + ("on" if status["include_task_name"] else "off"))
                print("Pending alerts: " + str(status["pending_alerts"]))
                if not status["sessions_available"]:
                    print("No Codex sessions directory yet; run a Codex task first.")
        elif args.command == "test-flash":
            flash(home)
            print("Flash test finished.")
        elif args.command == "test-phone":
            if not send_phone(config_for(home)["phone"], TEST_MESSAGE):
                raise AlertError("Phone is not configured. Run install.command configure-phone first.")
            print("Test accepted by ntfy. Check your phone for delivery.")
        elif args.command == "phone-off":
            config = config_for(home, reset_invalid_phone=True)
            config["phone"] = {"provider": "none"}
            save_json(home / "config.json", config)
            print("Phone notifications disabled.")
        else:
            configure_phone(home)
    except AlertError as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception:
        # Exception text can contain personal paths or service response secrets.
        print("Operation unavailable. Check the local configuration and try again.", file=sys.stderr)
        return 1
    return 0
