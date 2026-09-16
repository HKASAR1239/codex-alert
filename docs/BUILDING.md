# Build the macOS app

End users download an app ZIP; no Python, Terminal or Apple development tools are needed to run it. The source installer remains available for contributors.

For a local build, use macOS, Python 3.12 and Apple's Command Line Tools:

```sh
python3 -m venv work/build-venv
work/build-venv/bin/python -m pip install -r scripts/requirements-build.txt
work/build-venv/bin/python scripts/build_app.py
```

The app, ZIP, SHA-256 checksum and build report appear in `work/dist/`. The build compiles the AppKit menu bar interface and screen flash, and embeds Python with [PyInstaller](https://pyinstaller.org/en/stable/usage.html). The engine includes [certifi](https://github.com/certifi/python-certifi)'s CA bundle, keeping ntfy HTTPS verification independent of the build machine. No user settings or Codex data are copied.

Builds are native to the current CPU. GitHub Actions builds and tests Apple Silicon (`macos-15`) and Intel (`macos-15-intel`) separately; these labels follow the [official runner list](https://github.com/actions/runner-images). Tag builds attach both verified ZIPs to a release, creating a draft if needed. Versions come from `src/codex_alert/__init__.py`; build dependencies are pinned.

The Swift target is macOS 13. The build scans bundled Mach-O binaries and records the actual minimum macOS version in `Info.plist` and the JSON report. A build interpreter targeting newer macOS can raise this minimum. Smoke tests prove that the frozen CLI, TLS roots, SQLite, app model and isolated status command work with only `/usr/bin:/bin` on `PATH`. They never install a service or send a notification.

Default builds are **ad-hoc signed, not Apple notarized**. macOS may require approval in System Settings → Privacy & Security after opening a downloaded app. Do not disable Gatekeeper. Maintainers with their own Developer ID can explicitly sign and notarize:

```sh
work/build-venv/bin/python scripts/build_app.py \
  --identity 'Developer ID Application: YOUR NAME (TEAMID)' \
  --notarize-profile 'YOUR_NOTARYTOOL_PROFILE'
```

Signing uses only the explicitly supplied identity; the default never selects a certificate from the keychain. Notarization credentials are managed by Apple's `notarytool`, never stored in this repository. The app bundle contains the project and embedded-runtime license notices.
