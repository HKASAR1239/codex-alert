#!/usr/bin/env python3
"""User-scoped macOS setup. No sudo, package downloads or Codex config edits."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from codex_alert import __version__
from codex_alert.alert import config_for, main as alert_main, save_json

LABEL = 'com.local.codex-alert'
DEFAULT_HOME = Path.home() / 'Library/Application Support/CodexAlert'
DEFAULT_PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')


def run(command, *, timeout=15):
    return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=timeout)


def definition(home, python=None, codex_home=None):
    environment = {'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': str(home / 'app')}
    if codex_home:
        environment['CODEX_HOME'] = str(Path(codex_home).expanduser().resolve())
    return {'Label': LABEL,
            'ProgramArguments': [python or sys.executable, '-m', 'codex_alert',
                                 '--home', str(home), 'watch'],
            'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 30,
            'LimitLoadToSessionType': 'Aqua', 'ProcessType': 'Background',
            'WorkingDirectory': str(home), 'EnvironmentVariables': environment,
            'StandardOutPath': '/dev/null',
            'StandardErrorPath': str(home / 'launch-error.log'), 'Umask': 0o077}


def configured_codex_home(previous_plist):
    """Finder does not inherit a custom CODEX_HOME used for the first install."""
    selected = os.environ.get('CODEX_HOME')
    if selected:
        return selected
    if previous_plist:
        try:
            previous = plistlib.loads(previous_plist)
            selected = previous.get('EnvironmentVariables', {}).get('CODEX_HOME')
            if isinstance(selected, str) and selected:
                return selected
        except (ValueError, TypeError, plistlib.InvalidFileException):
            pass
    return None


def domain():
    return f'gui/{os.getuid()}'


def registered():
    return run(['/bin/launchctl', 'print', domain() + '/' + LABEL]).returncode == 0


def stop():
    if registered():
        result = run(['/bin/launchctl', 'bootout', domain() + '/' + LABEL])
        if result.returncode:
            raise RuntimeError('Could not stop the existing service. Close other installers and try again.')


def start(plist, home):
    requested_at = time.time()
    result = run(['/bin/launchctl', 'bootstrap', domain(), str(plist)])
    if result.returncode:
        raise RuntimeError('macOS could not start the service. Run install.command from Finder or a normal Terminal, outside the Codex sandbox. No sudo is needed.')
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            state = json.loads((home / 'state.json').read_text())
            if state.get('heartbeat_at', 0) >= requested_at:
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.25)
    raise RuntimeError('The service was registered but did not report readiness. Check the local launch-error.log; do not share your config.json.')


def preflight():
    if platform.system() != 'Darwin':
        raise RuntimeError('Codex Alert currently supports macOS only.')
    version = platform.mac_ver()[0]
    if not version or int(version.split('.')[0]) < 13:
        raise RuntimeError('macOS 13 or newer is required.')
    if sys.version_info < (3, 9):
        raise RuntimeError('Python 3.9 or newer is required.')
    result = run(['/usr/bin/xcrun', '--find', 'swiftc'])
    if result.returncode:
        raise RuntimeError('Apple Command Line Tools are required. Run xcode-select --install, finish the installation, then reopen install.command.')
    if not (ROOT / 'native/codex-flash.swift').is_file():
        raise RuntimeError('Download and unzip the whole project before running install.command.')
    print(f'Codex Alert {__version__} • macOS {version} • Python {platform.python_version()}')
    print('Prerequisites OK. The flash will be compiled locally; no packages are downloaded.')


def install(home, plist, args):
    preflight()
    # Compile and stage before touching an existing running installation.
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix='.codex-alert-build-', dir=home) as temporary:
        stage = Path(temporary)
        (stage / 'app').mkdir()
        shutil.copytree(ROOT / 'src/codex_alert', stage / 'app/codex_alert',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (stage / 'bin').mkdir()
        result = run(['/usr/bin/xcrun', 'swiftc', '-O', '-framework', 'AppKit',
                      '-module-cache-path', str(stage / 'swift-cache'),
                      str(ROOT / 'native/codex-flash.swift'),
                      '-o', str(stage / 'bin/codex-flash')], timeout=180)
        if result.returncode:
            raise RuntimeError('Swift compilation failed. Update Apple Command Line Tools and try again. Your existing service was not changed.')
        config_path = home / 'config.json'
        config = config_for(home)
        if args.min_seconds is not None:
            config['min_seconds'] = args.min_seconds
        if args.no_flash:
            config['flash'] = False
        if args.local_only:
            config['phone'] = {'provider': 'none'}
        old_config = config_path.read_bytes() if config_path.exists() else None
        old_plist = plist.read_bytes() if plist.exists() else None
        was_running = registered()
        stop()
        replaced = []
        try:
            for name in ('app', 'bin'):
                destination = home / name
                backup = stage / ('old-' + name)
                if destination.exists():
                    destination.rename(backup)
                replaced.append(name)
                (stage / name).rename(destination)
            save_json(config_path, config)
            plist.parent.mkdir(parents=True, exist_ok=True)
            with plist.open('wb') as handle:
                plistlib.dump(definition(home, codex_home=configured_codex_home(old_plist)), handle)
            plist.chmod(0o600)
            start(plist, home)
        except BaseException:
            # Restore previous files and registration after an unsuccessful update.
            try:
                stop()
            except Exception:
                # Never let TemporaryDirectory destroy the last usable version
                # when macOS refuses to stop a partially started replacement.
                recovery = home / ('recovery-' + str(time.time_ns()))
                recovery.mkdir(mode=0o700)
                for name in ('app', 'bin'):
                    backup = stage / ('old-' + name)
                    if backup.exists():
                        backup.rename(recovery / name)
                for name, original in (('agent.plist', old_plist), ('config.json', old_config)):
                    if original is not None:
                        (recovery / name).write_bytes(original)
                raise RuntimeError('Could not stop the replacement service. Previous files were preserved in the local CodexAlert recovery folder; run uninstall before retrying.') from None
            for name in reversed(replaced):
                destination = home / name
                if destination.exists():
                    shutil.rmtree(destination)
                backup = stage / ('old-' + name)
                if backup.exists():
                    backup.rename(destination)
            for path, original in ((plist, old_plist), (config_path, old_config)):
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(original)
            if was_running and old_plist is not None:
                run(['/bin/launchctl', 'bootstrap', domain(), str(plist)])
            raise
    print('Background service is running and will start at login.')


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Install or manage Codex Alert for the current macOS user.')
    parser.add_argument('command', nargs='?', default='setup',
                        choices=['setup', 'status', 'test-flash', 'test-phone',
                                 'configure-phone', 'phone-off', 'power-mode', 'uninstall'])
    parser.add_argument('--mode', choices=['off', 'plugged_in', 'always'],
                        help='Use with power-mode to select idle-sleep prevention.')
    parser.add_argument('--local-only', action='store_true', help='Enable Mac alerts without phone notifications.')
    parser.add_argument('--no-flash', action='store_true', help='Disable the Mac screen-edge pulses.')
    parser.add_argument('--min-seconds', type=float, help='Notify only after turns strictly longer than this duration.')
    parser.add_argument('--check', action='store_true', help='Check prerequisites without writing files or starting services.')
    args = parser.parse_args(argv)
    if (args.command == 'power-mode') != (args.mode is not None):
        parser.error('Use power-mode with --mode off, plugged_in or always.')
    try:
        if args.min_seconds is not None and not 0 <= args.min_seconds <= 86400:
            raise ValueError('--min-seconds must be between 0 and 86400.')
        if args.check:
            preflight()
            return 0
        home, plist = DEFAULT_HOME, DEFAULT_PLIST
        if args.command == 'setup':
            if not args.local_only and not (sys.stdin.isatty() and sys.stdout.isatty()):
                raise RuntimeError('Open install.command in Finder or a normal Terminal for private phone setup. For a noninteractive Mac-only install use --local-only.')
            install(home, plist, args)
            if not args.local_only:
                print('\nNext: subscribe on your phone and send a test notification.')
                code = alert_main(['--home', str(home), 'configure-phone'])
                if code:
                    print('Mac monitoring is installed. Phone setup is incomplete; rerun install.command configure-phone.')
                return code
        elif args.command == 'uninstall':
            stop()
            plist.unlink(missing_ok=True)
            print('Alerts stopped and login startup removed. Local settings are kept in ~/Library/Application Support/CodexAlert.')
            print('To erase saved settings and state, delete that folder manually after uninstalling.')
        else:
            extra = ['--mode', args.mode] if args.command == 'power-mode' else []
            return alert_main(['--home', str(home), *extra, args.command])
        return 0
    except KeyboardInterrupt:
        print('\nSetup cancelled.', file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        # Do not expose subprocess output or arbitrary exceptions containing config data.
        if isinstance(error, (RuntimeError, ValueError)):
            print(str(error), file=sys.stderr)
        else:
            print('Setup could not finish (' + type(error).__name__ + ').', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
