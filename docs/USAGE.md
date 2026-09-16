# Using Codex Alert

Open the menu-bar bell to check monitoring, pause alerts, test delivery or open Settings. Quitting the menu app leaves the background watcher running; **Stop monitoring** stops the watcher and removes its sleep assertion. **Start monitoring** starts it again.

Phone setup shows your random ntfy topic only in the private setup window. Subscribe in the ntfy phone app using `https://ntfy.sh`, allow notifications and press **Send test**. Service acceptance does not prove your phone displayed it; confirm receipt on the phone.

Completion alerts normally require a task longer than 120 seconds. Structured terminal failures alert even for short tasks. A failed individual tool command is not treated as a failed task.

Completions arriving within 10 seconds are grouped, up to five per notification. Set grouping to zero to turn it off. Quiet hours defer alerts until the quiet period ends, using your Mac's local timezone. Pause discards pending and newly completed alerts; resuming does not replay them. Manual test buttons deliberately send immediately.

The menu shows when ntfy last accepted an alert, not a phone delivery receipt. The private outbox retries temporary failures up to six times. Names are resolved at send time and are not saved in the queue or logs.

## Install from source

Requires macOS 13+, Python 3.9+ and Apple Command Line Tools. [Download the source ZIP](https://github.com/HKASAR1239/codex-alert/archive/refs/heads/main.zip), unzip, then double-click `install.command`. Source installation uses the command line and does not install the menu app.

From the source folder:

```sh
bash install.command status
bash install.command test-phone
bash install.command pause --minutes 60
bash install.command resume
bash install.command power-mode --mode always
bash install.command settings --set group_seconds=10
bash install.command settings --set quiet_hours.enabled=true --set quiet_hours.start=22:00 --set quiet_hours.end=08:00
bash install.command settings --set include_task_name=false
bash install.command uninstall
```

Uninstall stops monitoring and removes its login entry; it preserves your private settings and state. To remove everything, quit the menu app, uninstall, then delete `~/Applications/Codex Alert.app` and `~/Library/Application Support/CodexAlert`.

The native app and source installer share the same private settings. Upgrading preserves your ntfy topic, battery preference and existing deduplication state.
