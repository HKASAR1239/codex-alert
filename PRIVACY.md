# Privacy

Codex Alert runs on your Mac. The project author receives no telemetry, analytics or notification data.

## Local data

The watcher reads your local Codex session JSONL files to identify turn starts, completions and interruptions. Although those files contain conversations, Codex Alert retains only event metadata: session file paths, read positions, identifiers, timestamps and durations. It also reads conversation names from Codex metadata in read-only mode. Names are resolved at delivery time and are not stored in the alert queue or logs. It does not save conversation text or change Codex's configuration or session files.

Application files, notification settings, state and operational logs live in `~/Library/Application Support/CodexAlert`. The install directory is private to your user; configuration and state files use owner-only permissions. This is filesystem protection, not encryption or protection from other software running as your user. A per-user launch agent starts the watcher at login.

Logs contain operational status and delivery attempts, not session text or the ntfy topic. Diagnostic `status` output does not display the topic. Do not publish your configuration, state, session files or unreviewed logs in an issue.

The menu app reads this local status and your settings. Its setup window can reveal the topic; copying it to the clipboard requires an explicit button press. Do not include that window in public screenshots. The self-contained app bundles its runtime and does not download code during installation.

Optional reviewed and trusted Codex approval hooks store only opaque IDs, timestamps and a one-way fingerprint locally. Tool arguments are processed transiently to match request and tool-completion events; commands, arguments, approval decisions and questions are not written to disk or sent to ntfy. Approval metadata expires after 10 minutes. See [approval behavior and limitations](docs/APPROVALS.md).

## Screen-off operation

During detected active tasks, the companion uses macOS `caffeinate -i` to prevent idle system sleep while allowing the screen to turn off or lock. It does not change your global energy settings. The default `plugged_in` mode only applies on external power; `bash install.command power-mode --mode always` also allows battery use, which consumes additional energy. Use `--mode off` to disable it.

Protection is released after the task's completion notification is attempted, on interruption, or after 2 hours without session activity. Activity resumes protection. This inactivity limit avoids keeping the Mac awake indefinitely after an abandoned task, but a command that produces no Codex session activity for over 2 hours can lose protection. Assertions have a short renewable timeout and are tied to the watcher process, so they also expire if it stops or hangs. Closing the lid, choosing Sleep, powering off or losing network access is not handled by this mode.

## Phone notifications

Phone setup creates a random topic with 128 bits of randomness on `https://ntfy.sh`. No phone number, account or API key is requested. The topic is displayed in your own interactive terminal so you can subscribe on your phone; do not paste it into chats, screenshots or issue reports.

By default, an alert sends the conversation name and duration, such as “Update landing page — Completed in 3 min 12 s.” This is the conversation name shown in Codex, not a separate name for each message. Names may themselves contain sensitive information and may appear on your phone's lock screen. No prompt or response body, code, file contents or Codex identifier is added. Tests send a fixed test message. If a name cannot be found, the alert uses a generic message; it never substitutes the first user prompt.

To keep alerts generic, set `"include_task_name": false` in your local `config.json`. The watcher picks up this setting automatically. Keep that file local because it also contains your ntfy topic.

Grouped alerts include up to five names and durations. Terminal failure and optional approval-request alerts include a name and a fixed status message, never the underlying error or requested command. Quiet hours defer alerts until the period ends; pause discards alerts rather than replaying them later. Manual tests bypass quiet hours and pause. The last-notification indicator means ntfy accepted the message, not that the phone displayed it.

The topic is an unlisted address, **not an authenticated private channel**. Anyone with its name can read and publish messages. HTTPS protects transport; messages are not end-to-end encrypted. ntfy receives the topic, message and connection metadata such as your IP address. Delivery also uses the phone platform's push infrastructure and may expose content on the lock screen according to your settings. See [ntfy's topic model](https://docs.ntfy.sh/publish/#picking-a-topic) and [privacy policy](https://docs.ntfy.sh/privacy/).

Run `bash install.command phone-off` to stop sending phone alerts. `--local-only` installs without phone alerts; `uninstall` stops alerts and removes login startup while keeping local settings and state. The uninstaller explains how to remove the remaining local files if wanted. You can also remove the subscription from your phone. These actions do not erase messages already delivered to ntfy or your phone.
