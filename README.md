# Codex Alert

Get an [ntfy](https://ntfy.sh/) notification on your phone and a gentle Mac screen flash when a local Codex task finishes after **more than 2 minutes**. Works in the background and starts at login.

![Illustrated preview of turquoise Mac screen edges and a phone completion notification](docs/assets/preview.png)
*Illustrated preview.*

## Setup

Requires **macOS 13+, Python 3.9+ and Xcode Command Line Tools** (`xcode-select --install`).

1. [Download the ZIP](https://github.com/HKASAR1239/codex-alert/archive/refs/heads/main.zip) and unzip it.
2. Install ntfy on [iPhone](https://apps.apple.com/app/ntfy/id1625396347) or [Android](https://docs.ntfy.sh/subscribe/phone/) and allow notifications.
3. Double-click **`install.command`** on your Mac.
4. In ntfy, subscribe to the topic shown by the installer on **`https://ntfy.sh`**. Press Return on your Mac to send a test.

Keep your Mac awake and online. Keep the topic private: anyone who knows it can read and send alerts. Only a generic message and task duration are sent.

**Uninstall:** run `bash install.command uninstall` from the downloaded folder.

[MIT license](LICENSE) · [Privacy](PRIVACY.md)
