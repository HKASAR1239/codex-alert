---
name: codex-alert
description: Set up, check, configure or remove Codex Alert, the macOS companion that flashes screen edges and sends ntfy phone notifications when long local Codex turns finish. Use for this companion's setup and troubleshooting.
---

# Codex Alert

The plugin root is two directories above this file. Prefer the self-contained menu app from GitHub Releases for ordinary users; `install.command` remains the source-install entrypoint; read [README.md](../../README.md) for requirements and behavior. Installing this skill alone does not install or start the background service.

## Setup and operation

- Run `bash install.command --check` from the plugin root to check prerequisites without modifying the installation. The companion requires macOS 13+, Python 3.9+ and Xcode Command Line Tools.
- For authorized installation, run `bash install.command` or direct the user to double-click it. It installs a per-user background service and guides phone setup. `--local-only` installs Mac alerts without phone delivery; `--no-flash` disables the screen effect. `--min-seconds N` changes the duration threshold.
- Phone setup is interactive and displays a secret topic. Have the user complete this step in their own Terminal and ntfy app; do not capture its output, read the clipboard, request the topic in chat or open their configuration file. For later setup, use `bash install.command configure-phone`.
- `bash install.command status` provides a sanitized status check. Use `test-flash` to preview the visual effect and `test-phone` to send a phone test when the user asks to test alerts. Service acceptance does not prove the phone displayed the notification; ask the user to check reception if needed.
- `bash install.command phone-off` disables phone alerts. `bash install.command uninstall` stops alerts and removes login startup when requested, while keeping local settings and state.
- `bash install.command power-mode --mode plugged_in` prevents idle sleep during detected tasks on external power (the default). Use `--mode always` when the user wants this on battery too, or `--mode off` to disable it. The screen may still turn off or lock. No global energy setting is changed.

Keep actions within the user's request. Respect tool and filesystem permissions; if an operation is denied, explain the specific blocked step and give the user its command rather than trying another route to bypass the restriction. Do not change Codex settings or install a marketplace as part of this companion's setup.

The menu app includes Settings, Pause alerts for one hour, Resume, and private phone setup. Source commands and quiet-hours behavior are in [USAGE.md](../../docs/USAGE.md). Source installation needs Python and Apple tools; the packaged app does not.

Optional approval hooks require separate user review and trust in Codex. Do not automatically trust hooks, change their approval decisions or claim the Mac app alone enables them. They report that approval was requested; they cannot establish that a task is still blocked. See [APPROVALS.md](../../docs/APPROVALS.md).

## Troubleshooting and privacy

Use status and preflight checks first. Do not read raw Codex sessions, state or configuration just to diagnose installation. A missing prerequisite should be installed through the user's normal local workflow; the installer does not need root privileges.

Alerts watch local Codex completion events, normally under `~/.codex/sessions` or `CODEX_HOME/sessions`. They exclude interrupted turns and subagents. Successful completions use a strict duration threshold: 120 seconds exactly does not trigger the default alert. Structured terminal failures bypass this duration threshold. Cloud tasks are outside the scope. The Mac must be awake for the watcher to run, and connected for timely phone delivery. Idle-sleep protection ends after completion or 2 hours without session activity; activity resumes it. It does not keep tasks running through closed-lid sleep, manual sleep or shutdown. A Codex event format change may require an update.

The default ntfy topic is a random shared secret, not an authenticated or end-to-end encrypted channel. Notifications include the Codex conversation name and duration by default; the name may appear on the phone's lock screen. A generic message is used when no name is available or the local `include_task_name` setting is false. Never use prompt or response content as a fallback name. Keep screenshots and bug reports free of topics, credentials and session content. See [PRIVACY.md](../../PRIVACY.md).
