# Privacy

Codex Alert runs on your Mac. The project author receives no telemetry, analytics or notification data.

## Local data

The watcher reads your local Codex session JSONL files to identify turn starts, completions and interruptions. Although those files contain conversations, Codex Alert retains only event metadata: session file paths, read positions, identifiers, timestamps and durations. It does not save conversation text or change Codex's configuration or session files.

Application files, notification settings, state and operational logs live in `~/Library/Application Support/CodexAlert`. The install directory is private to your user; configuration and state files use owner-only permissions. This is filesystem protection, not encryption or protection from other software running as your user. A per-user launch agent starts the watcher at login.

Logs contain operational status and delivery attempts, not session text or the ntfy topic. Diagnostic `status` output does not display the topic. Do not publish your configuration, state, session files or unreviewed logs in an issue.

## Phone notifications

Phone setup creates a random topic with 128 bits of randomness on `https://ntfy.sh`. No phone number, account or API key is requested. The topic is displayed in your own interactive terminal so you can subscribe on your phone; do not paste it into chats, screenshots or issue reports.

An alert sends only a generic completion message and duration, such as “Codex task complete — 3 min 12 s. Your Mac is ready.” Tests send a fixed test message. No prompt, code, filename, task title or Codex identifier is included.

The topic is an unlisted address, **not an authenticated private channel**. Anyone with its name can read and publish messages. HTTPS protects transport; messages are not end-to-end encrypted. ntfy receives the topic, message and connection metadata such as your IP address. Delivery also uses the phone platform's push infrastructure and may expose content on the lock screen according to your settings. See [ntfy's topic model](https://docs.ntfy.sh/publish/#picking-a-topic) and [privacy policy](https://docs.ntfy.sh/privacy/).

Run `bash install.command phone-off` to stop sending phone alerts. `--local-only` installs without phone alerts; `uninstall` stops alerts and removes login startup while keeping local settings and state. The uninstaller explains how to remove the remaining local files if wanted. You can also remove the subscription from your phone. These actions do not erase messages already delivered to ntfy or your phone.
