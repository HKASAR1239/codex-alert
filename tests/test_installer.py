"""Installer checks use temporary files and fully mocked launchctl/Swift calls."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock


PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("codex_alert_manage", PROJECT / "scripts/manage.py")
manage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manage)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex-alert-installer-tests-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "Project with spaces"
        package = self.source / "src/codex_alert"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("# replacement package\n")
        (self.source / "native").mkdir()
        (self.source / "native/codex-flash.swift").write_text("// test compiler input\n")
        self.home = self.base / "Runtime with spaces"
        self.plist = self.base / "Launch Agents" / "codex-alert.plist"
        self.args = types.SimpleNamespace(min_seconds=None, no_flash=False, local_only=False)
        self.events = []
        self.config = {
            "min_seconds": 180, "poll_seconds": 7, "flash": False,
            "group_seconds": 10, "paused_until": 0, "discard_before": 0,
            "quiet_hours": {"enabled": False, "start": "22:00", "end": "08:00"},
            "phone": {"provider": "ntfy", "topic": "codex-" + "c" * 32},
        }
        patches = {
            "ROOT": self.source,
            "DEFAULT_HOME": self.home,
            "DEFAULT_PLIST": self.plist,
            "preflight": mock.Mock(),
            "run": mock.Mock(side_effect=self.fake_run),
            "registered": mock.Mock(return_value=False),
            "stop": mock.Mock(side_effect=lambda: self.events.append("stop")),
            "start": mock.Mock(side_effect=lambda *_: self.events.append("start")),
        }
        for name, value in patches.items():
            patcher = mock.patch.object(manage, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Avoid using the caller's CODEX_HOME in any fixture.
        environment = mock.patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.output = io.StringIO()
        output_patch = contextlib.redirect_stdout(self.output)
        output_patch.__enter__()
        self.addCleanup(output_patch.__exit__, None, None, None)

    def fake_run(self, command, *, timeout=15):
        if command[:2] == ["/usr/bin/xcrun", "swiftc"]:
            self.events.append("compile")
            Path(command[command.index("-o") + 1]).write_text("compiled helper\n")
        elif command[:2] == ["/bin/launchctl", "bootstrap"]:
            self.events.append("restore-start")
        else:
            self.fail("Unexpected subprocess request in mocked installer: " + repr(command))
        return types.SimpleNamespace(returncode=0)

    def existing_installation(self, codex_home=None):
        (self.home / "app").mkdir(parents=True)
        (self.home / "app/old-app-marker").write_text("previous app\n")
        (self.home / "bin").mkdir()
        (self.home / "bin/codex-flash").write_text("previous helper\n")
        manage.save_json(self.home / "config.json", self.config)
        self.plist.parent.mkdir()
        self.plist.write_bytes(plistlib.dumps(manage.definition(self.home, codex_home=codex_home)))
        manage.registered.return_value = True
        return (self.home / "config.json").read_bytes(), self.plist.read_bytes()

    def test_definition_preserves_space_containing_paths_as_arguments(self):
        definition = manage.definition(self.home, python="/Python path/python3",
                                       codex_home=self.base / "Custom Codex home")
        self.assertEqual(definition["ProgramArguments"], ["/Python path/python3", "-m",
                         "codex_alert", "--home", str(self.home), "watch"])
        self.assertEqual(definition["EnvironmentVariables"]["PYTHONPATH"], str(self.home / "app"))
        self.assertEqual(definition["EnvironmentVariables"]["CODEX_HOME"],
                         str(self.base / "Custom Codex home"))
        self.assertEqual(definition["Umask"], 0o077)

    def test_upgrade_compiles_before_stop_and_preserves_settings(self):
        self.existing_installation()
        manage.install(self.home, self.plist, self.args)
        self.assertEqual(self.events, ["compile", "stop", "start"])
        self.assertEqual(json.loads((self.home / "config.json").read_text()),
                         dict(self.config, include_task_name=True, keep_awake="plugged_in"))
        self.assertEqual((self.home / "bin/codex-flash").read_text(), "compiled helper\n")
        self.assertTrue((self.home / "app/codex_alert/__init__.py").exists())
        self.assertFalse((self.home / "app/old-app-marker").exists())
        self.assertNotIn(self.config["phone"]["topic"], self.output.getvalue())

    def test_upgrade_preserves_disabled_task_names(self):
        self.config["include_task_name"] = False
        self.existing_installation()
        manage.install(self.home, self.plist, self.args)
        self.assertEqual(json.loads((self.home / "config.json").read_text()),
                         dict(self.config, keep_awake="plugged_in"))

    def test_upgrade_preserves_battery_power_mode(self):
        self.config.update(include_task_name=True, keep_awake="always")
        self.existing_installation()
        manage.install(self.home, self.plist, self.args)
        self.assertEqual(json.loads((self.home / "config.json").read_text()), self.config)

    def test_power_mode_command_forwards_mode_without_reinstalling(self):
        with mock.patch.object(manage, "alert_main", return_value=0) as alert_main:
            self.assertEqual(manage.main(["power-mode", "--mode", "always"]), 0)
        alert_main.assert_called_once_with(["--home", str(self.home), "--mode", "always", "power-mode"])
        manage.stop.assert_not_called()
        manage.start.assert_not_called()

    def test_failed_compile_does_not_stop_or_change_existing_installation(self):
        old_config, old_plist = self.existing_installation()
        manage.run.side_effect = lambda *a, **kw: types.SimpleNamespace(returncode=1)
        with self.assertRaisesRegex(RuntimeError, "compilation failed"):
            manage.install(self.home, self.plist, self.args)
        manage.stop.assert_not_called()
        manage.start.assert_not_called()
        self.assertEqual((self.home / "config.json").read_bytes(), old_config)
        self.assertEqual(self.plist.read_bytes(), old_plist)
        self.assertEqual((self.home / "app/old-app-marker").read_text(), "previous app\n")
        self.assertEqual((self.home / "bin/codex-flash").read_text(), "previous helper\n")

    def test_failed_bootstrap_restores_previous_files_and_registration(self):
        old_config, old_plist = self.existing_installation()
        self.args.min_seconds = 300
        manage.start.side_effect = RuntimeError("bootstrap failed")
        with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
            manage.install(self.home, self.plist, self.args)
        self.assertEqual((self.home / "config.json").read_bytes(), old_config)
        self.assertEqual(self.plist.read_bytes(), old_plist)
        self.assertEqual((self.home / "app/old-app-marker").read_text(), "previous app\n")
        self.assertEqual((self.home / "bin/codex-flash").read_text(), "previous helper\n")
        self.assertFalse((self.home / "app/codex_alert").exists())
        self.assertEqual(manage.stop.call_count, 2)
        self.assertEqual(self.events[-1], "restore-start")

    def test_failed_first_install_removes_new_files_without_starting_old_service(self):
        manage.start.side_effect = RuntimeError("bootstrap failed")
        with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
            manage.install(self.home, self.plist, self.args)
        self.assertFalse((self.home / "app").exists())
        self.assertFalse((self.home / "bin").exists())
        self.assertFalse((self.home / "config.json").exists())
        self.assertFalse(self.plist.exists())
        self.assertNotIn("restore-start", self.events)

    def test_rollback_stop_failure_keeps_previous_files_recoverable(self):
        old_config, old_plist = self.existing_installation()
        self.args.min_seconds = 300
        manage.start.side_effect = RuntimeError("readiness failed")
        manage.stop.side_effect = [None, RuntimeError("cannot stop replacement")]
        with self.assertRaisesRegex(RuntimeError, "[Rr]ecover"):
            manage.install(self.home, self.plist, self.args)
        # When a partially started service cannot be stopped, preserve backups
        # without swapping its active files. Their exact recovery path may vary.
        contents = [path.read_bytes() for path in self.home.rglob("*") if path.is_file()]
        for previous in (b"previous app\n", b"previous helper\n", old_config, old_plist):
            self.assertIn(previous, contents)

    def test_upgrade_retains_custom_codex_home_without_environment_override(self):
        custom_home = self.base / "Previous Codex home"
        self.existing_installation(codex_home=custom_home)
        manage.install(self.home, self.plist, self.args)
        definition = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(definition["EnvironmentVariables"]["CODEX_HOME"], str(custom_home))

    def test_explicit_codex_home_environment_overrides_previous_setting(self):
        self.existing_installation(codex_home=self.base / "Old home")
        new_home = self.base / "New Codex home"
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(new_home)}):
            manage.install(self.home, self.plist, self.args)
        definition = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(definition["EnvironmentVariables"]["CODEX_HOME"], str(new_home))

    def test_uninstall_stops_registration_but_keeps_local_settings(self):
        old_config, _ = self.existing_installation()
        self.assertEqual(manage.main(["uninstall"]), 0)
        manage.stop.assert_called_once()
        manage.start.assert_not_called()
        manage.run.assert_not_called()
        self.assertFalse(self.plist.exists())
        self.assertEqual((self.home / "config.json").read_bytes(), old_config)
        self.assertTrue((self.home / "app/old-app-marker").exists())


class ShellLauncherTests(unittest.TestCase):
    def test_launcher_passes_project_path_and_arguments_without_shell_splitting(self):
        # Run only a copied launcher with stub uname/python executables. The stub
        # interpreter records arguments; manage.py and launchctl never execute.
        with tempfile.TemporaryDirectory(prefix="codex-alert-shell-tests-") as temporary:
            base = Path(temporary)
            project = base / "A project with spaces"
            project.mkdir()
            launcher = project / "install.command"
            shutil.copyfile(PROJECT / "install.command", launcher)
            fake_bin = base / "fake bin"
            fake_bin.mkdir()
            (fake_bin / "uname").write_text("#!/bin/sh\nprintf '%s\\n' Darwin\n")
            interpreter = fake_bin / "python3"
            interpreter.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = '-c' ]; then\n"
                "  case \"$2\" in *'print(sys.executable)'*) printf '%s\\n' \"$TEST_INTERPRETER\";; esac\n"
                "  exit 0\n"
                "fi\n"
                "printf '%s\\0' \"$@\" > \"$TEST_ARGUMENTS\"\n"
            )
            (fake_bin / "uname").chmod(0o700)
            interpreter.chmod(0o700)
            arguments_file = base / "arguments.bin"
            environment = {
                "PATH": str(fake_bin) + ":/usr/bin:/bin",
                "CODEX_ALERT_NO_PAUSE": "1",
                "TEST_INTERPRETER": str(interpreter),
                "TEST_ARGUMENTS": str(arguments_file),
            }
            result = subprocess.run(["/bin/bash", str(launcher), "setup", "--min-seconds", "180"],
                                    env=environment, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(arguments_file.read_bytes().split(b"\0")[:-1], [
                str(project / "scripts/manage.py").encode(), b"setup", b"--min-seconds", b"180"])


if __name__ == "__main__":
    unittest.main()
