import os
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from codex_alert import service


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "runtime"
        self.home.mkdir()
        self.plist = self.root / "LaunchAgents" / "agent.plist"
        self.bundle = self.root / "Codex Alert.app"
        for relative in ("engine/codex-alert-engine", "bin/codex-flash"):
            path = self.bundle / "Contents/Resources" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test-binary")
            path.chmod(0o755)

    def test_definition_uses_only_frozen_engine(self):
        engine = self.bundle / "Contents/Resources/engine/codex-alert-engine"
        result = service.definition(self.home, engine)
        self.assertEqual(result["ProgramArguments"], [str(engine), "--home", str(self.home), "watch"])
        self.assertEqual(result["EnvironmentVariables"], {})
        self.assertEqual(result["Umask"], 0o077)

    def test_finder_upgrade_keeps_custom_codex_home(self):
        previous = plistlib.dumps({"EnvironmentVariables": {"CODEX_HOME": "/custom/codex"}})
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(service.configured_codex_home(previous), "/custom/codex")
        with patch.dict(os.environ, {"CODEX_HOME": "/new/codex"}):
            self.assertEqual(service.configured_codex_home(previous), "/new/codex")

    def test_install_preserves_private_config_and_state(self):
        for name in ("config.json", "state.json"):
            (self.home / name).write_bytes(b"private-existing-data")
        with patch.object(service, "registered", return_value=False), \
                patch.object(service, "stop"), patch.object(service, "start") as start:
            result = service.perform(self.home, "install", self.bundle, plist=self.plist)
        self.assertEqual(result, {"service": "active"})
        start.assert_called_once_with(self.plist, self.home.resolve())
        for name in ("config.json", "state.json"):
            self.assertEqual((self.home / name).read_bytes(), b"private-existing-data")
        self.assertEqual((self.home / "bin/codex-flash").read_bytes(), b"test-binary")
        self.assertEqual(self.plist.stat().st_mode & 0o777, 0o600)

    def test_incomplete_bundle_never_stops_existing_service(self):
        (self.bundle / "Contents/Resources/engine/codex-alert-engine").unlink()
        with patch.object(service, "stop") as stop:
            with self.assertRaisesRegex(service.ServiceError, "incomplete"):
                service.perform(self.home, "install", self.bundle, plist=self.plist)
        stop.assert_not_called()

    def test_failed_install_restores_previous_flash_and_plist(self):
        target = self.home / "bin/codex-flash"
        target.parent.mkdir()
        target.write_bytes(b"old-flash")
        target.chmod(0o700)
        self.plist.parent.mkdir()
        self.plist.write_bytes(b"old-plist")
        with patch.object(service, "registered", return_value=True), \
                patch.object(service, "stop"), \
                patch.object(service, "start", side_effect=service.ServiceError("cannot start")), \
                patch.object(service, "run") as run:
            with self.assertRaisesRegex(service.ServiceError, "cannot start"):
                service.perform(self.home, "install", self.bundle, plist=self.plist)
        self.assertEqual(self.plist.read_bytes(), b"old-plist")
        self.assertEqual(target.read_bytes(), b"old-flash")
        self.assertEqual(run.call_args.args[0][1], "bootstrap")

    def test_failed_first_install_removes_partial_files(self):
        with patch.object(service, "registered", return_value=False), \
                patch.object(service, "stop"), \
                patch.object(service, "start", side_effect=service.ServiceError("cannot start")):
            with self.assertRaises(service.ServiceError):
                service.perform(self.home, "install", self.bundle, plist=self.plist)
        self.assertFalse(self.plist.exists())
        self.assertFalse((self.home / "bin/codex-flash").exists())

    def test_unstoppable_replacement_preserves_recovery(self):
        target = self.home / "bin/codex-flash"
        target.parent.mkdir()
        target.write_bytes(b"old-flash")
        self.plist.parent.mkdir()
        self.plist.write_bytes(b"old-plist")
        with patch.object(service, "registered", return_value=True), \
                patch.object(service, "stop", side_effect=[None, service.ServiceError("blocked")]), \
                patch.object(service, "start", side_effect=service.ServiceError("failed")):
            with self.assertRaisesRegex(service.ServiceError, "preserved"):
                service.perform(self.home, "install", self.bundle, plist=self.plist)
        recovery = next(self.home.glob("recovery-*"))
        self.assertEqual((recovery / "agent.plist").read_bytes(), b"old-plist")
        self.assertEqual((recovery / "codex-flash").read_bytes(), b"old-flash")

    def test_stop_retains_registration_file_and_private_settings(self):
        self.plist.parent.mkdir()
        self.plist.write_bytes(b"existing")
        with patch.object(service, "stop") as stop:
            result = service.perform(self.home, "stop", plist=self.plist)
        self.assertEqual(result, {"service": "stopped"})
        stop.assert_called_once()
        self.assertEqual(self.plist.read_bytes(), b"existing")

    def test_start_rejects_missing_or_wrong_service_before_launchctl(self):
        with patch.object(service, "start") as start:
            with self.assertRaises(service.ServiceError):
                service.perform(self.home, "start", plist=self.plist)
            self.plist.parent.mkdir()
            self.plist.write_bytes(plistlib.dumps({"Label": "unrelated"}))
            with self.assertRaises(service.ServiceError):
                service.perform(self.home, "start", plist=self.plist)
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
