# Codex Alert

**Leave Codex working. Know when it's done.** A small open-source macOS companion, with an optional Codex skill.

[Français](README.fr.md) · [Download ZIP](https://github.com/HKASAR1239/codex-alert/archive/refs/heads/main.zip) · [MIT license](LICENSE)

![Illustrated preview of turquoise Mac screen edges and a phone completion notification](docs/assets/preview.png)
*Illustrated preview — not an actual screenshot. Notification appearance depends on your phone.*

## What it does

- **Mac:** gently pulses turquoise screen edges three times in about four seconds, without taking keyboard focus.
- **Phone:** sends a notification through [ntfy](https://ntfy.sh/), with the task duration. No phone number or account needed.
- Triggers for completed local Codex turns **longer than 2 minutes**, even while you use another app. Skips interrupted turns and subagents.
- Runs in the background and starts at login. The duration threshold and screen effect are configurable.

## Set up once

Requires **macOS 13+, Python 3.9+ and Xcode Command Line Tools**. If the tools are missing, run `xcode-select --install` in Terminal first. No Python packages to install.

1. [Download the ZIP](https://github.com/HKASAR1239/codex-alert/archive/refs/heads/main.zip) and unzip it.
2. Double-click **`install.command`**. Alternatively, open Terminal in that folder and run `bash install.command`.
3. Install ntfy on [iPhone](https://apps.apple.com/app/ntfy/id1625396347) or [Android](https://docs.ntfy.sh/subscribe/phone/) and allow notifications.
4. In ntfy, subscribe to the random topic shown by the installer, using server **`https://ntfy.sh`**. Press Return on the Mac to send the test.

Keep the topic private: anyone who knows it can read and send alerts. Setup displays it only in your local terminal. See [privacy details](PRIVACY.md).

## Useful commands

Run these from the downloaded folder:

```bash
bash install.command status             # Check the background service
bash install.command test-flash         # Preview the Mac effect
bash install.command test-phone         # Send a test to your phone
bash install.command configure-phone    # Set up or reconnect ntfy
bash install.command phone-off          # Disable phone notifications
bash install.command --local-only       # Install with Mac alerts only
bash install.command --no-flash          # Install with phone alerts only
bash install.command --min-seconds 300   # Change the threshold to 5 minutes
bash install.command uninstall          # Stop alerts and login startup
```

Uninstall keeps local settings. Use `bash install.command --check` to check prerequisites without installing. The included [Codex skill](skills/codex-alert/SKILL.md) can guide setup and status checks; adding the skill alone does not start the service.

## Good to know

The Mac must be awake and online for timely phone alerts. Phone settings and ntfy availability can delay delivery. macOS only; local sessions in `~/.codex/sessions` (or `CODEX_HOME/sessions`), not cloud tasks. Detection uses Codex's local event format, which may change. A completed turn means Codex finished responding, not that its work passed every check.

Only a generic completion message and duration leave the Mac; no prompts, code, paths or task titles are sent. This community project is not affiliated with OpenAI. [Contributions welcome](CONTRIBUTING.md).
