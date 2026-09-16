# Codex Alert

Get an [ntfy](https://ntfy.sh/) notification with the **task name and duration**, plus a gentle Mac screen flash, when a local Codex task finishes after **more than 2 minutes**. Works in the background and starts at login.

![Illustrated preview of turquoise Mac screen edges and a phone completion notification](docs/assets/preview.png)
*Illustrated preview.*

## Setup

Requires **macOS 13+, Python 3.9+ and Xcode Command Line Tools** (`xcode-select --install`).

1. [Download the ZIP](https://github.com/HKASAR1239/codex-alert/archive/refs/heads/main.zip) and unzip it.
2. Install ntfy on [iPhone](https://apps.apple.com/app/ntfy/id1625396347) or [Android](https://docs.ntfy.sh/subscribe/phone/) and allow notifications.
3. Double-click **`install.command`** on your Mac.
4. In ntfy, subscribe to the topic shown by the installer on **`https://ntfy.sh`**. Press Return on your Mac to send a test.

The screen can turn off or lock: Codex Alert prevents idle sleep during detected tasks while plugged in. For battery use too: `bash install.command power-mode --mode always` (uses more battery). Disable with `--mode off`. Keep the Mac online and its lid open; manual sleep and shutdown suspend local tasks. Protection ends after completion or 2 hours without session activity.

Keep the topic private: anyone who knows it can read and send alerts. The conversation name and duration are sent to ntfy and may appear on your lock screen.

**Uninstall:** run `bash install.command uninstall` from the downloaded folder.

[MIT license](LICENSE) · [Privacy](PRIVACY.md)
