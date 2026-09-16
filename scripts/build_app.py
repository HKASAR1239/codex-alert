#!/usr/bin/env python3
"""Build and verify an offline, self-contained macOS menu bar app."""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import sysconfig
import tempfile

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "Codex Alert.app"
MIN_MACOS = "13.0"
MACHO_MAGICS = {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
                b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


def package_version():
    text = (ROOT / "src/codex_alert/__init__.py").read_text()
    return re.search(r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$', text, re.M).group(1)


def bundle_info(version):
    return {"CFBundleName": "Codex Alert", "CFBundleDisplayName": "Codex Alert",
            "CFBundleIdentifier": "org.codexalert.app", "CFBundleExecutable": "CodexAlert",
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": version,
            "CFBundleVersion": version, "CFBundleIconFile": "AppIcon",
            "LSMinimumSystemVersion": MIN_MACOS, "LSUIElement": True,
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "Codex Alert contributors. MIT license."}


def run(command, *, env=None, capture=False):
    return subprocess.run([str(value) for value in command], check=True, env=env,
                          text=True, capture_output=capture)


def is_macho(path):
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as handle:
        return handle.read(4) in MACHO_MAGICS


def private_test_env(home):
    # Prove the runtime works without external Python, package paths or config.
    return {"HOME": str(home), "PATH": "/usr/bin:/bin", "TMPDIR": str(home),
            "CODEX_HOME": str(home / "codex"), "LANG": "en_US.UTF-8"}


def verify_bundle(app):
    engine = app / "Contents/Resources/engine/codex-alert-engine"
    gui = app / "Contents/MacOS/CodexAlert"
    flash = app / "Contents/Resources/bin/codex-flash"
    for path in (engine, gui, flash):
        if not is_macho(path) or not os.access(path, os.X_OK):
            raise RuntimeError("Missing native executable: " + str(path.relative_to(app)))
    if not (engine.parent / "_internal").is_dir():
        raise RuntimeError("The embedded Python runtime is missing.")
    for path in app.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(app.resolve()):
            raise RuntimeError("A bundled link points outside the app.")
        if is_macho(path):
            dependencies = run(["/usr/bin/otool", "-L", path], capture=True).stdout
            for line in dependencies.splitlines()[1:]:
                library = line.strip().split(" (", 1)[0]
                if library.startswith("/") and not library.startswith(("/usr/lib/", "/System/Library/")):
                    raise RuntimeError("A bundled binary depends on a library outside macOS or the app.")
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", app])
    with tempfile.TemporaryDirectory(prefix="codex-alert-smoke-") as temporary:
        home = Path(temporary)
        env = private_test_env(home)
        test = json.loads(run([engine, "--bundle-self-test"], env=env, capture=True).stdout)
        if not test.get("frozen") or test.get("version") != package_version():
            raise RuntimeError("The embedded engine version or runtime is incorrect.")
        state = json.loads(run([engine, "--home", home / "runtime", "--sessions",
                                home / "sessions", "--json", "status"],
                               env=env, capture=True).stdout)
        if state["phone"] != "none" or state["watcher"] == "active":
            raise RuntimeError("The smoke test did not use isolated settings.")
        run([gui, "--self-test"], env=env)


def copy_licenses(destination, analysis=None):
    destination.mkdir()
    shutil.copyfile(ROOT / "LICENSE", destination / "Codex-Alert-MIT.txt")
    for name in ("pyinstaller", "certifi"):
        distribution = importlib.metadata.distribution(name)
        found = False
        for item in distribution.files or []:
            if item.name.lower().startswith(("license", "copying")):
                source = Path(distribution.locate_file(item))
                if source.is_file():
                    shutil.copyfile(source, destination / (name + "-" + item.name))
                    found = True
        if not found:
            raise RuntimeError("Missing bundled dependency license: " + name)
    # CPython installers and Homebrew keep this beside the runtime or prefix.
    prefixes = [Path(sys.base_prefix), Path(sysconfig.get_path("stdlib")),
                Path(sys.executable).resolve()]
    candidates = []
    for prefix in prefixes:
        for parent in [prefix, *list(prefix.parents)[:5]]:
            candidates.extend(parent / name for name in ("LICENSE", "LICENSE.txt"))
    python_license = next((path for path in candidates if path.is_file()
                           and "PYTHON" in path.read_text(errors="replace")[:10000].upper()), None)
    if python_license is None:
        raise RuntimeError("CPython license was not found beside the build interpreter.")
    shutil.copyfile(python_license, destination / "CPython.txt")
    if analysis is not None:
        # Homebrew and python.org include library license files near each
        # original binary. Use the freezer's own collected-binary manifest.
        sources = set()
        def visit(value):
            if isinstance(value, (tuple, list)):
                if len(value) == 3 and value[2] in ("BINARY", "EXTENSION"):
                    sources.add(Path(value[1]).resolve())
                else:
                    for child in value:
                        visit(child)
        visit(ast.literal_eval(analysis.read_text()))
        copied = set()
        for source in sources:
            for parent in list(source.parents)[:6]:
                if parent == Path.home() or parent == Path("/"):
                    break
                notices = [path for path in parent.glob("*") if path.is_file()
                           and path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE"))]
                for notice in notices:
                    digest = hashlib.sha256(notice.read_bytes()).hexdigest()
                    if digest not in copied:
                        copied.add(digest)
                        shutil.copyfile(notice, destination / (parent.name + "-" + notice.name))
                if notices:
                    break
    (destination / "README.txt").write_text(
        "Codex Alert bundles CPython, its standard library and linked libraries, "
        "a PyInstaller bootloader and certifi's unmodified Mozilla CA bundle.\n"
        "Sources: https://github.com/python/cpython ; https://github.com/pyinstaller/pyinstaller ; "
        "https://github.com/certifi/python-certifi\n"
        "The PyInstaller bootloader exception permits this distribution under MIT.\n")


def generate_icon(stage, resources, env, architecture):
    generator = stage / "icon-generator"
    run(["/usr/bin/xcrun", "swiftc", "-O", "-target", architecture + "-apple-macosx" + MIN_MACOS,
         "-framework", "AppKit", "-module-cache-path", stage / "swift-cache",
         ROOT / "native/app-icon.swift", "-o", generator], env=env)
    original = stage / "icon.png"
    run([generator, original], env=env)
    iconset = stage / "AppIcon.iconset"
    iconset.mkdir()
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            name = f"icon_{size}x{size}" + ("@2x" if scale == 2 else "") + ".png"
            run(["/usr/bin/sips", "-z", str(size * scale), str(size * scale), original,
                 "--out", iconset / name], capture=True)
    # ICNS is a typed container of these PNG representations. Writing the
    # container directly also works in headless builders where iconutil fails.
    kinds = {(16, 1): b"icp4", (16, 2): b"ic11", (32, 1): b"icp5", (32, 2): b"ic12",
             (128, 1): b"ic07", (128, 2): b"ic13", (256, 1): b"ic08", (256, 2): b"ic14",
             (512, 1): b"ic09", (512, 2): b"ic10"}
    chunks = []
    for (size, scale), kind in kinds.items():
        name = f"icon_{size}x{size}" + ("@2x" if scale == 2 else "") + ".png"
        png = (iconset / name).read_bytes()
        chunks.append(struct.pack(">4sI", kind, len(png) + 8) + png)
    content = b"".join(chunks)
    icon = resources / "AppIcon.icns"
    icon.write_bytes(struct.pack(">4sI", b"icns", len(content) + 8) + content)
    run(["/usr/bin/sips", "-g", "format", icon], capture=True)


def deployment_floor(app):
    """Report the actual binary floor; do not claim older support from a plist."""
    maximum = tuple(int(part) for part in MIN_MACOS.split(".")) + (0,)
    for path in app.rglob("*"):
        if is_macho(path):
            headers = run(["/usr/bin/otool", "-l", path], capture=True).stdout
            values = []
            for block in re.findall(r"\bcmd LC_(?:BUILD_VERSION|VERSION_MIN_MACOSX)\n(.*?)(?=\nLoad command|\Z)",
                                    headers, re.S):
                values.extend(re.findall(r"\b(?:minos|version) (\d+\.\d+(?:\.\d+)?)\s*\n", block))
            for value in values:
                parts = tuple(int(part) for part in value.split("."))
                maximum = max(maximum, parts + (0,) * (3 - len(parts)))
    return ".".join(map(str, maximum[:2]))


def build(output, *, identity="-", notarize_profile=None):
    if sys.platform != "darwin":
        raise RuntimeError("Build the app on macOS; use CI for the other architecture.")
    if notarize_profile and identity == "-":
        raise RuntimeError("Notarization requires an explicit Developer ID signing identity.")
    architecture = platform.machine()
    if architecture not in ("arm64", "x86_64"):
        raise RuntimeError("Unsupported build architecture.")
    version = package_version()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".app-build-", dir=output) as temporary:
        stage = Path(temporary)
        env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(stage / "pyinstaller-cache"),
                   MACOSX_DEPLOYMENT_TARGET=MIN_MACOS)
        run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
             "--name", "codex-alert-engine", "--contents-directory", "_internal",
             "--target-architecture", architecture, "--codesign-identity", identity,
             "--paths", ROOT / "src", "--distpath", stage / "frozen",
             "--workpath", stage / "freeze-work", "--specpath", stage,
             ROOT / "scripts/engine_entry.py"], env=env)
        app = stage / APP_NAME
        contents = app / "Contents"
        resources = contents / "Resources"
        (contents / "MacOS").mkdir(parents=True)
        (resources / "bin").mkdir(parents=True)
        shutil.copytree(stage / "frozen/codex-alert-engine", resources / "engine", symlinks=True)
        for source, executable in (("CodexAlertApp.swift", contents / "MacOS/CodexAlert"),
                                   ("codex-flash.swift", resources / "bin/codex-flash")):
            run(["/usr/bin/xcrun", "swiftc", "-O", "-target",
                 architecture + "-apple-macosx" + MIN_MACOS, "-framework", "AppKit",
                 "-module-cache-path", stage / "swift-cache", ROOT / "native" / source,
                 "-o", executable], env=env)
        generate_icon(stage, resources, env, architecture)
        copy_licenses(resources / "Licenses", stage / "freeze-work/codex-alert-engine/Analysis-00.toc")
        info = bundle_info(version)
        info["LSMinimumSystemVersion"] = deployment_floor(app)
        (contents / "Info.plist").write_bytes(plistlib.dumps(info))
        signing = ["/usr/bin/codesign", "--force", "--sign", identity]
        signing += ["--timestamp", "--options", "runtime"] if identity != "-" else ["--timestamp=none"]
        for binary in sorted((path for path in app.rglob("*") if is_macho(path)),
                             key=lambda path: len(path.parts), reverse=True):
            run(signing + [binary], capture=True)
        run(signing + [app], capture=True)
        verify_bundle(app)
        filename = f"codex-alert-{version}-macos-{architecture}.zip"
        archive = stage / filename
        run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, archive])
        if notarize_profile:
            run(["/usr/bin/xcrun", "notarytool", "submit", archive,
                 "--keychain-profile", notarize_profile, "--wait"])
            run(["/usr/bin/xcrun", "stapler", "staple", app])
            archive.unlink()
            run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, archive])
        destination = output / APP_NAME
        if destination.exists():
            shutil.rmtree(destination)
        app.rename(destination)
        shutil.copyfile(archive, output / filename)
        digest = hashlib.sha256((output / filename).read_bytes()).hexdigest()
        (output / (filename + ".sha256")).write_text(digest + "  " + filename + "\n")
        report = {"version": version, "architecture": architecture,
                  "minimum_macos": info["LSMinimumSystemVersion"],
                  "signing": "ad-hoc" if identity == "-" else "Developer ID",
                  "notarized": bool(notarize_profile), "sha256": digest,
                  "python": platform.python_version(),
                  "pyinstaller": importlib.metadata.version("pyinstaller")}
        (output / (filename + ".json")).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print("Built " + str(output / filename))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "work/dist")
    parser.add_argument("--verify", type=Path, help="Verify an existing .app without rebuilding")
    parser.add_argument("--identity", default="-", help="Explicit Developer ID, or - for ad-hoc")
    parser.add_argument("--notarize-profile", help="Explicit notarytool keychain profile")
    args = parser.parse_args()
    if args.verify:
        verify_bundle(args.verify.resolve())
    else:
        build(args.output.resolve(), identity=args.identity, notarize_profile=args.notarize_profile)


if __name__ == "__main__":
    main()
