"""Source-side contracts; actual frozen-runtime tests run in macOS CI."""
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_app", ROOT / "scripts/build_app.py")
build_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_app)


class PackagingTests(unittest.TestCase):
    def test_bundle_info_is_menu_app_and_matches_package_version(self):
        from codex_alert import __version__
        info = build_app.bundle_info(build_app.package_version())
        self.assertEqual(info["CFBundleVersion"], __version__)
        self.assertEqual(info["CFBundleExecutable"], "CodexAlert")
        self.assertTrue(info["LSUIElement"])

    def test_smoke_test_environment_cannot_inherit_private_runtime(self):
        env = build_app.private_test_env(Path("/isolated"))
        self.assertEqual(env["HOME"], "/isolated")
        self.assertEqual(env["CODEX_HOME"], "/isolated/codex")
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("PYTHONHOME", env)

    def test_scripts_are_not_accepted_as_embedded_executables(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / "Codex Alert.app"
            engine = app / "Contents/Resources/engine/codex-alert-engine"
            engine.parent.mkdir(parents=True)
            engine.write_text("#!/usr/bin/env python3\n")
            engine.chmod(0o755)
            with self.assertRaisesRegex(RuntimeError, "Missing native executable"):
                build_app.verify_bundle(app)

    def test_symlink_cannot_substitute_for_embedded_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            real = Path(temporary) / "external"
            real.write_bytes(b"\xcf\xfa\xed\xfe")
            alias = Path(temporary) / "engine"
            alias.symlink_to(real)
            self.assertTrue(build_app.is_macho(real))
            self.assertFalse(build_app.is_macho(alias))

    def test_default_signing_never_selects_a_private_identity(self):
        import inspect
        self.assertEqual(inspect.signature(build_app.build).parameters["identity"].default, "-")

    def test_ad_hoc_notarization_is_rejected_before_build(self):
        with patch.object(build_app.sys, "platform", "darwin"):
            with self.assertRaisesRegex(RuntimeError, "explicit Developer ID"):
                build_app.build(Path("/unused"), notarize_profile="do-not-access")

    def test_minimum_os_ignores_unrelated_library_version_numbers(self):
        headers = """Load command 1
      cmd LC_LOAD_DYLIB
  current version 1900.30.0
Load command 2
      cmd LC_BUILD_VERSION
    minos 14.0
      sdk 15.2
Load command 3
      cmd LC_VERSION_MIN_MACOSX
  version 10.15
      sdk 11.0
"""
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary)
            (app / "binary").write_bytes(b"\xcf\xfa\xed\xfe")
            with patch.object(build_app, "run", return_value=SimpleNamespace(stdout=headers)):
                self.assertEqual(build_app.deployment_floor(app), "14.0")


if __name__ == "__main__":
    unittest.main()
