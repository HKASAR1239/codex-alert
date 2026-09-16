"""User-scoped LaunchAgent management for the self-contained macOS app."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import time

LABEL = "com.local.codex-alert"
DEFAULT_PLIST = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")


class ServiceError(RuntimeError):
    """A safe error message; never includes captured process output."""


def run(command, *, timeout=20):
    return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=timeout)


def domain():
    return "gui/" + str(os.getuid())


def registered():
    return run(["/bin/launchctl", "print", domain() + "/" + LABEL]).returncode == 0


def stop():
    if registered() and run(["/bin/launchctl", "bootout", domain() + "/" + LABEL]).returncode:
        raise ServiceError("macOS could not stop Codex Alert. Close other installers and retry.")


def start(plist, home):
    if registered():
        return
    requested_at = time.time()
    if run(["/bin/launchctl", "bootstrap", domain(), str(plist)]).returncode:
        raise ServiceError("macOS could not start Codex Alert. Open the app directly from Finder and retry.")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            state = json.loads((home / "state.json").read_text())
            if state.get("heartbeat_at", 0) >= requested_at:
                return
        except (OSError, ValueError, TypeError):
            pass
        time.sleep(0.25)
    raise ServiceError("Codex Alert did not report readiness. Check its local launch-error.log.")


def configured_codex_home(previous):
    selected = os.environ.get("CODEX_HOME")
    if selected:
        return selected
    if previous:
        try:
            selected = plistlib.loads(previous).get("EnvironmentVariables", {}).get("CODEX_HOME")
            if isinstance(selected, str) and selected:
                return selected
        except (ValueError, TypeError, AttributeError, plistlib.InvalidFileException):
            pass
    return None


def definition(home, engine, codex_home=None):
    environment = {}
    if codex_home:
        environment["CODEX_HOME"] = str(Path(codex_home).expanduser().resolve())
    return {"Label": LABEL, "ProgramArguments": [str(engine), "--home", str(home), "watch"],
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
            "LimitLoadToSessionType": "Aqua", "ProcessType": "Background",
            "WorkingDirectory": str(home), "EnvironmentVariables": environment,
            "StandardOutPath": "/dev/null", "StandardErrorPath": str(home / "launch-error.log"),
            "Umask": 0o077}


def atomic_write(path, data, mode=0o600):
    descriptor, name = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        staged.chmod(mode)
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


@contextmanager
def install_lock(home):
    with (home / "service.lock").open("a") as handle:
        os.chmod(handle.name, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ServiceError("Another Codex Alert setup is running. Try again when it finishes.") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def install(home, plist, bundle):
    resources = bundle / "Contents/Resources"
    engine = resources / "engine/codex-alert-engine"
    flash = resources / "bin/codex-flash"
    if not bundle.is_dir() or not all(path.is_file() and os.access(path, os.X_OK)
                                     for path in (engine, flash)):
        raise ServiceError("The app is incomplete. Download and unzip the complete Codex Alert app again.")
    previous = plist.read_bytes() if plist.exists() else None
    was_running = registered()
    target = home / "bin/codex-flash"
    target.parent.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".service-install-", dir=home) as temporary:
        stage = Path(temporary)
        shutil.copyfile(flash, stage / "new-flash")
        (stage / "new-flash").chmod(0o700)
        # Stage all fallible copies before interrupting a working service.
        if target.exists():
            shutil.copyfile(target, stage / "old-flash")
            (stage / "old-flash").chmod(target.stat().st_mode & 0o777)
        new_plist = plistlib.dumps(definition(home, engine, configured_codex_home(previous)))
        stop()
        try:
            (stage / "new-flash").replace(target)
            plist.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(plist, new_plist)
            start(plist, home)
        except BaseException:
            try:
                stop()
            except Exception:
                recovery = home / ("recovery-" + str(time.time_ns()))
                recovery.mkdir(mode=0o700)
                if (stage / "old-flash").exists():
                    (stage / "old-flash").rename(recovery / "codex-flash")
                if previous is not None:
                    atomic_write(recovery / "agent.plist", previous)
                raise ServiceError("The replacement could not be stopped. Previous files are preserved in the local recovery folder; stop Codex Alert before retrying.") from None
            if (stage / "old-flash").exists():
                (stage / "old-flash").replace(target)
            else:
                target.unlink(missing_ok=True)
            if previous is None:
                plist.unlink(missing_ok=True)
            else:
                atomic_write(plist, previous)
                if was_running:
                    run(["/bin/launchctl", "bootstrap", domain(), str(plist)])
            raise


def perform(home: Path, action: str, bundle: Path | None = None, *, plist: Path | None = None):
    """Install starts immediately. Start/stop retain private settings and state."""
    home = Path(home).expanduser().resolve()
    plist = Path(plist) if plist is not None else DEFAULT_PLIST
    if action not in ("install", "start", "stop"):
        raise ServiceError("Unknown service action.")
    if action == "install" and bundle is None:
        raise ServiceError("Select the installed Codex Alert app before starting setup.")
    home.mkdir(parents=True, mode=0o700, exist_ok=True)
    home.chmod(0o700)
    with install_lock(home):
        if action == "install":
            install(home, plist, Path(bundle).expanduser().resolve())
        elif action == "stop":
            stop()
        else:
            if not plist.is_file():
                raise ServiceError("Set up Codex Alert before starting the background service.")
            try:
                saved = plistlib.loads(plist.read_bytes())
                if saved.get("Label") != LABEL:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError, plistlib.InvalidFileException):
                raise ServiceError("The saved service configuration is invalid. Run app setup again.") from None
            start(plist, home)
    return {"service": "stopped" if action == "stop" else "active"}
