# Codex Alert

A small Mac menu-bar app that sends [ntfy](https://ntfy.sh/) phone alerts when your Codex tasks finish. See the **task name and duration**, get a gentle screen flash, and let tasks run with your screen off.

![Illustrated Codex Alert preview](docs/assets/preview.png)
*Illustrated preview.*

## Setup

1. [Download the app](https://github.com/HKASAR1239/codex-alert/releases/latest) for **Apple silicon** or **Intel**, then unzip it.
2. Open **Codex Alert.app** and choose **Set up Codex Alert**. No Python or developer tools needed.
3. Choose **Connect phone**, install ntfy on [iPhone](https://apps.apple.com/app/ntfy/id1625396347) or [Android](https://docs.ntfy.sh/subscribe/phone/), subscribe to the private topic shown, and send a test.

The app installs into your user Applications folder. Builds are currently **not notarized**; macOS may require [Open Anyway in Privacy & Security](https://support.apple.com/en-us/102445). Check the release for the minimum macOS version.

## In the menu

- See monitoring status, active task count and the last accepted notification.
- Pause alerts for an hour; change the duration threshold, flash and title privacy.
- Group completions and set quiet hours. Terminal failures get a distinct alert.
- Keep tasks awake on power or battery while the screen turns off. Keep the lid open and Internet connected; protection expires after 2 hours without session activity.

## Approval alerts

Optional [Codex hooks](docs/APPROVALS.md) notify you when approval was requested. They require a separate review and trust step in Codex, and never approve anything automatically.

Keep your ntfy topic private: anyone who knows it can read and send alerts. Task names can appear on your lock screen.

[Source install & commands](docs/USAGE.md) · [Build the app](docs/BUILDING.md) · [Privacy](PRIVACY.md) · [MIT](LICENSE)
