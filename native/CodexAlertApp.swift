import AppKit
import Darwin
import Foundation

// The UI never receives topics from subprocess output. Private subscription
// details are read only when the user explicitly opens the setup window.
struct AlertStatus {
    var active = false
    var activeTasks = 0
    var minSeconds: Double = 120
    var flash = true
    var includeTaskName = true
    var keepAwake = "plugged_in"
    var keepAwakeActive = false
    var phone = "none"
    var groupSeconds = 10
    var quietEnabled = false
    var quietStart = "22:00"
    var quietEnd = "08:00"
    var pausedUntil: Double = 0
    var lastNotificationAt: Double = 0
    var attentionSupported = false

    init(_ value: [String: Any] = [:]) {
        active = value["watcher"] as? String == "active"
        activeTasks = max(0, value["active_tasks"] as? Int ?? 0)
        minSeconds = value["min_seconds"] as? Double ?? 120
        flash = value["flash"] as? Bool ?? true
        includeTaskName = value["include_task_name"] as? Bool ?? true
        keepAwake = value["keep_awake"] as? String ?? "plugged_in"
        keepAwakeActive = value["keep_awake_active"] as? Bool ?? false
        phone = value["phone"] as? String ?? "none"
        groupSeconds = value["group_seconds"] as? Int ?? 10
        let quiet = value["quiet_hours"] as? [String: Any] ?? [:]
        quietEnabled = quiet["enabled"] as? Bool ?? false
        quietStart = quiet["start"] as? String ?? "22:00"
        quietEnd = quiet["end"] as? String ?? "08:00"
        pausedUntil = value["paused_until"] as? Double ?? 0
        lastNotificationAt = value["last_notification_at"] as? Double ?? 0
        attentionSupported = value["attention_supported"] as? Bool ?? false
    }

    var paused: Bool { pausedUntil > Date().timeIntervalSince1970 }

    enum StartupWindow { case none, settings, setup }
    func startupWindow(arguments: [String]) -> StartupWindow {
        if arguments.contains("--setup") { return .setup }
        return arguments.contains("--installed") ? .settings : .none
    }

    static var preview: AlertStatus {
        AlertStatus(["watcher": "active", "active_tasks": 2, "phone": "ntfy",
                     "keep_awake": "always", "keep_awake_active": true,
                     "last_notification_at": Date().timeIntervalSince1970 - 240])
    }
}

enum AppError: Error {
    case unavailable, malformedResponse, installFailed, rollbackFailed, installationBusy
}

struct EngineResult {
    let status: Int32
    let data: Data
}

final class EngineClient {
    let home: URL
    let bundle: URL
    let preview: Bool
    private let queue = DispatchQueue(label: "org.codexalert.engine", qos: .utility)

    init(home: URL, bundle: URL, preview: Bool) {
        self.home = home
        self.bundle = bundle
        self.preview = preview
    }

    static func engine(in bundle: URL) -> URL {
        bundle.appendingPathComponent("Contents/Resources/engine/codex-alert-engine")
    }

    static func arguments(home: URL, command: [String]) -> [String] {
        ["--home", home.path] + command
    }

    static func execute(_ executable: URL, arguments: [String], timeout: Double = 25) throws -> EngineResult {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.standardInput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        let output = Pipe()
        process.standardOutput = output
        try process.run()
        let cancel = DispatchWorkItem { if process.isRunning { process.terminate() } }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + timeout, execute: cancel)
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        cancel.cancel()
        return EngineResult(status: process.terminationStatus, data: data)
    }

    func run(_ command: [String], completion: @escaping (Result<Data, Error>) -> Void) {
        guard !preview else {
            // Preview actions are deliberately inert: no real service or network.
            completion(.success(Data("{}".utf8)))
            return
        }
        queue.async {
            let result: Result<Data, Error>
            do {
                let response = try Self.execute(Self.engine(in: self.bundle),
                                                arguments: Self.arguments(home: self.home, command: command))
                guard response.status == 0 else { throw AppError.unavailable }
                result = .success(response.data)
            } catch {
                // Never display raw subprocess errors, paths, responses, or secrets.
                result = .failure(AppError.unavailable)
            }
            DispatchQueue.main.async { completion(result) }
        }
    }

    func status(completion: @escaping (Result<AlertStatus, Error>) -> Void) {
        if preview { completion(.success(.preview)); return }
        run(["--json", "status"]) { result in
            completion(result.flatMap { data in
                guard let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                    return .failure(AppError.malformedResponse)
                }
                return .success(AlertStatus(value))
            })
        }
    }

    func privateTopic() -> String? {
        if preview { return "codex-preview-example-not-a-real-subscription" }
        guard let data = try? Data(contentsOf: home.appendingPathComponent("config.json")),
              let config = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let phone = config["phone"] as? [String: Any], phone["provider"] as? String == "ntfy",
              let topic = phone["topic"] as? String,
              topic.range(of: "^[A-Za-z0-9_-]{1,64}$", options: .regularExpression) != nil else { return nil }
        return topic
    }
}

enum AppInstaller {
    static let installedURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Applications/Codex Alert.app")

    static func isInstalled(_ bundle: URL) -> Bool {
        bundle.standardizedFileURL.resolvingSymlinksInPath() == installedURL.standardizedFileURL.resolvingSymlinksInPath()
    }

    static func relocate(source: URL, home: URL) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: home, withIntermediateDirectories: true,
                               attributes: [.posixPermissions: 0o700])
        // This outer lock spans both bundle replacement and the service helper's
        // individual transactions. Keep it distinct from the helper's own lock.
        let lock = open(home.appendingPathComponent("app-install.lock").path,
                        O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0o600)
        guard lock >= 0 else { throw AppError.installFailed }
        defer { close(lock) }
        guard flock(lock, LOCK_EX | LOCK_NB) == 0 else { throw AppError.installationBusy }
        defer { flock(lock, LOCK_UN) }
        let destination = installedURL
        let parent = destination.deletingLastPathComponent()
        try fm.createDirectory(at: parent, withIntermediateDirectories: true)
        let stage = parent.appendingPathComponent(".codex-alert-install-" + UUID().uuidString)
        try fm.createDirectory(at: stage, withIntermediateDirectories: false,
                               attributes: [.posixPermissions: 0o700])
        let prepared = stage.appendingPathComponent("new.app")
        let backup = stage.appendingPathComponent("previous.app")
        var stopped = false
        var movedOld = false
        var movedNew = false
        var keepRecovery = false
        defer { if !keepRecovery { try? fm.removeItem(at: stage) } }

        func service(_ bundle: URL, _ action: String) throws {
            let result = try EngineClient.execute(EngineClient.engine(in: bundle), arguments:
                EngineClient.arguments(home: home, command: ["service", "--action", action, "--bundle", bundle.path]),
                timeout: 45)
            guard result.status == 0 else { throw AppError.installFailed }
        }

        do {
            guard source.pathExtension == "app", fm.isExecutableFile(atPath: EngineClient.engine(in: source).path) else {
                throw AppError.installFailed
            }
            if fm.fileExists(atPath: destination.path), Bundle(url: destination)?.bundleIdentifier != "org.codexalert.app" {
                throw AppError.installFailed
            }
            try fm.copyItem(at: source, to: prepared)
            // Stop only this product's user agent before replacing its executable.
            try service(source, "stop")
            stopped = true
            if fm.fileExists(atPath: destination.path) {
                try fm.moveItem(at: destination, to: backup)
                movedOld = true
            }
            try fm.moveItem(at: prepared, to: destination)
            movedNew = true
            try service(destination, "install")
        } catch {
            if movedNew {
                do { try service(destination, "stop") }
                catch { keepRecovery = true; throw AppError.rollbackFailed }
                try? fm.removeItem(at: destination)
            }
            if movedOld {
                do {
                    try fm.moveItem(at: backup, to: destination)
                    try service(destination, "start")
                } catch { keepRecovery = true; throw AppError.rollbackFailed }
            } else if stopped {
                // A previous script installation may exist without an app bundle.
                // The service helper restores its old registration after failure.
                _ = try? EngineClient.execute(EngineClient.engine(in: source), arguments:
                    EngineClient.arguments(home: home, command: ["service", "--action", "start", "--bundle", source.path]))
            }
            throw AppError.installFailed
        }
    }
}

final class ActionButton: NSButton {
    var invoke: (() -> Void)?
    convenience init(_ title: String, prominent: Bool = false, action: @escaping () -> Void) {
        self.init(title: title, target: nil, action: nil)
        self.target = self
        self.action = #selector(performAction)
        self.invoke = action
        self.bezelStyle = .rounded
        self.controlSize = .large
        if prominent { self.bezelColor = .systemTeal; self.keyEquivalent = "\r" }
    }
    @objc private func performAction() { invoke?() }
}

final class FlippedView: NSView {
    override var isFlipped: Bool { true }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate, NSWindowDelegate {
    let client: EngineClient
    var status = AlertStatus()
    var statusItem: NSStatusItem!
    var menu: NSMenu!
    var timer: Timer?
    var refreshing = false
    var settingsWindow: NSWindow?
    var setupWindow: NSWindow?
    var setupTopic: String?
    var statusMessage: NSTextField?
    var firstStatus = true

    init(client: EngineClient) { self.client = client }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        NSApp.appearance = NSAppearance(named: .darkAqua)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName: "bell.badge", accessibilityDescription: "Codex Alert")
        statusItem.button?.toolTip = "Codex Alert"
        menu = NSMenu()
        menu.delegate = self
        statusItem.menu = menu
        updateMenu()
        if !client.preview && !AppInstaller.isInstalled(client.bundle) {
            showInstall()
        } else if client.preview {
            refresh()
        } else {
            installServiceIfNeeded()
        }
        timer = Timer.scheduledTimer(withTimeInterval: 8, repeats: true) { [weak self] _ in self?.refresh() }
        if client.preview { showSettings() }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func menuWillOpen(_ menu: NSMenu) { refresh() }
    func windowWillClose(_ notification: Notification) {
        guard let closing = notification.object as? NSWindow else { return }
        if closing === setupWindow { setupTopic = nil; setupWindow = nil }
        if closing === settingsWindow { settingsWindow = nil }
    }

    func installServiceIfNeeded() {
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "development"
        let key = "installedServiceVersion"
        if CommandLine.arguments.contains("--installed") {
            UserDefaults.standard.set(version, forKey: key)
        }
        if UserDefaults.standard.string(forKey: key) == version { refresh(); return }
        client.run(["service", "--action", "install", "--bundle", client.bundle.path]) { [weak self] result in
            guard let self = self else { return }
            if case .success = result {
                UserDefaults.standard.set(version, forKey: key)
                self.refresh()
            } else {
                self.notice("Monitoring could not start", "Your preferences are safe. Reopen Codex Alert from Applications and try Start monitoring in the menu.")
                self.refresh()
            }
        }
    }

    func refresh() {
        guard !refreshing else { return }
        if !client.preview && !AppInstaller.isInstalled(client.bundle) { return }
        refreshing = true
        client.status { [weak self] result in
            guard let self = self else { return }
            self.refreshing = false
            switch result {
            case .success(let newStatus):
                self.status = newStatus
                self.updateMenu()
                if self.firstStatus {
                    self.firstStatus = false
                    switch newStatus.startupWindow(arguments: CommandLine.arguments) {
                    case .setup: self.showSetup()
                    case .settings: self.showSettings()
                    case .none: break
                    }
                }
            case .failure:
                self.status.active = false
                self.updateMenu()
            }
        }
    }

    func item(_ title: String, action: Selector? = nil, enabled: Bool = true) -> NSMenuItem {
        let result = NSMenuItem(title: title, action: action, keyEquivalent: "")
        result.target = self
        result.isEnabled = enabled
        return result
    }

    func updateMenu() {
        guard menu != nil else { return }
        menu.removeAllItems()
        menu.autoenablesItems = false
        menu.addItem(item(client.preview ? "Codex Alert · Preview" : "Codex Alert", enabled: false))
        menu.addItem(item(status.active ? "●  Monitoring active" : "○  Monitoring stopped", enabled: false))
        menu.addItem(item("\(status.activeTasks) task\(status.activeTasks == 1 ? "" : "s") in progress", enabled: false))
        menu.addItem(item(status.keepAwakeActive ? "Mac awake for your tasks" : "Normal sleep behavior", enabled: false))
        let last: String
        if status.lastNotificationAt > 0 {
            let formatter = RelativeDateTimeFormatter()
            formatter.unitsStyle = .full
            last = formatter.localizedString(for: Date(timeIntervalSince1970: status.lastNotificationAt), relativeTo: Date())
        } else { last = "none yet" }
        menu.addItem(item("Last notification: " + last, enabled: false))
        menu.addItem(.separator())
        if status.paused {
            menu.addItem(item("Resume notifications", action: #selector(togglePause)))
            menu.addItem(item("Paused until " + Date(timeIntervalSince1970: status.pausedUntil).formatted(date: .omitted, time: .shortened), enabled: false))
        } else { menu.addItem(item("Pause notifications for 1 hour", action: #selector(togglePause))) }
        let tests = NSMenu()
        tests.autoenablesItems = false
        tests.addItem(item("Send a phone notification", action: #selector(testPhone), enabled: status.phone == "ntfy"))
        tests.addItem(item("Preview the screen flash", action: #selector(testFlash)))
        let testItem = item("Test")
        testItem.submenu = tests
        menu.addItem(testItem)
        menu.addItem(item("Settings…", action: #selector(showSettings)))
        menu.addItem(item("Connect phone…", action: #selector(showSetup)))
        if !status.attentionSupported { menu.addItem(item("Enable approval alerts…", action: #selector(openHookHelp))) }
        menu.addItem(.separator())
        menu.addItem(item(status.active ? "Stop monitoring" : "Start monitoring", action: #selector(toggleService)))
        menu.addItem(item("Monitoring continues when the menu app quits", enabled: false))
        menu.addItem(item("Quit menu bar app", action: #selector(quit)))
        statusItem.button?.image = NSImage(systemSymbolName: status.paused ? "bell.slash" : "bell.badge",
                                          accessibilityDescription: "Codex Alert")
        statusItem.button?.toolTip = status.paused ? "Codex Alert — notifications paused" : "Codex Alert"
    }

    @objc func togglePause() {
        operate(status.paused ? ["resume"] : ["pause", "--minutes", "60"])
    }

    @objc func toggleService() {
        operate(["service", "--action", status.active ? "stop" : "start", "--bundle", client.bundle.path])
    }

    @objc func testPhone() {
        operate(["test-phone"], success: "ntfy accepted the test. Check your phone to confirm delivery.")
    }

    @objc func testFlash() { operate(["test-flash"]) }
    @objc func quit() { NSApp.terminate(nil) }
    @objc func openHookHelp() {
        guard !client.preview else { return }
        NSWorkspace.shared.open(URL(string: "https://github.com/HKASAR1239/codex-alert#approval-alerts")!)
    }

    func operate(_ args: [String], success: String? = nil, completion: (() -> Void)? = nil) {
        client.run(args) { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .success:
                self.refresh()
                if let success = success { self.notice(self.client.preview ? "Preview only" : "Codex Alert", self.client.preview ? "No notification was sent in preview mode." : success) }
                completion?()
            case .failure:
                self.notice("Could not complete this action", "Check that Codex Alert is installed and ntfy is connected, then try again.")
            }
        }
    }

    func notice(_ title: String, _ message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }

    func label(_ text: String, size: CGFloat = 13, color: NSColor = .labelColor, bold: Bool = false) -> NSTextField {
        let field = NSTextField(wrappingLabelWithString: text)
        field.font = bold ? .systemFont(ofSize: size, weight: .semibold) : .systemFont(ofSize: size)
        field.textColor = color
        field.maximumNumberOfLines = 0
        field.setContentCompressionResistancePriority(.required, for: .vertical)
        return field
    }

    func window(_ title: String, height: CGFloat) -> (NSWindow, NSStackView) {
        let availableHeight = max(420, (NSScreen.main?.visibleFrame.height ?? 900) - 80)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: min(height, availableHeight)),
                              styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = title
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.backgroundColor = NSColor(calibratedRed: 0.055, green: 0.075, blue: 0.082, alpha: 1)
        window.titlebarAppearsTransparent = true
        window.center()
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 15
        stack.translatesAutoresizingMaskIntoConstraints = false
        if let content = window.contentView {
            let scroll = NSScrollView(frame: content.bounds)
            scroll.autoresizingMask = [.width, .height]
            scroll.hasVerticalScroller = true
            scroll.autohidesScrollers = true
            scroll.drawsBackground = false
            let document = FlippedView(frame: content.bounds)
            document.autoresizingMask = [.width]
            scroll.documentView = document
            content.addSubview(scroll)
            document.addSubview(stack)
            NSLayoutConstraint.activate([
                stack.topAnchor.constraint(equalTo: document.topAnchor, constant: 24),
                stack.leadingAnchor.constraint(equalTo: document.leadingAnchor, constant: 30),
                stack.trailingAnchor.constraint(equalTo: document.trailingAnchor, constant: -30)
            ])
        }
        return (window, stack)
    }

    func add(_ view: NSView, to stack: NSStackView, fullWidth: Bool = true) {
        stack.addArrangedSubview(view)
        if fullWidth { view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true }
    }

    func brand(_ stack: NSStackView, subtitle: String) {
        add(label("◉  CODEX ALERT", size: 12, color: .systemTeal, bold: true), to: stack)
        add(label(subtitle, size: 26, bold: true), to: stack)
    }

    func present(_ window: NSWindow) {
        if let scroll = window.contentView?.subviews.first as? NSScrollView,
           let document = scroll.documentView,
           let stack = document.subviews.first as? NSStackView {
            document.layoutSubtreeIfNeeded()
            document.setFrameSize(NSSize(width: scroll.contentSize.width,
                                         height: max(scroll.contentSize.height, stack.fittingSize.height + 48)))
        }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    func showInstall() {
        let (window, stack) = window("Welcome to Codex Alert", height: 470)
        setupWindow = window
        brand(stack, subtitle: "Leave your desk. Stay in the loop.")
        add(label("A quiet menu bar companion for Codex. Get a screen flash and an ntfy notification when your work is ready.", color: .secondaryLabelColor), to: stack)
        add(label("Installs in your Applications folder and starts monitoring when you sign in. Existing preferences and your private subscription are kept."), to: stack)
        let progress = label("No Terminal, Python installation, or Apple developer tools needed.", size: 12, color: .secondaryLabelColor)
        add(progress, to: stack)
        let button = ActionButton("Set up Codex Alert", prominent: true) {}
        button.invoke = { [weak self, weak button, weak progress] in
            guard let self = self else { return }
            button?.isEnabled = false
            progress?.stringValue = "Installing Codex Alert…"
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    try AppInstaller.relocate(source: self.client.bundle, home: self.client.home)
                    DispatchQueue.main.async {
                        let configuration = NSWorkspace.OpenConfiguration()
                        configuration.arguments = ["--installed", "--home", self.client.home.path]
                        configuration.createsNewApplicationInstance = true
                        for running in NSRunningApplication.runningApplications(withBundleIdentifier: "org.codexalert.app") {
                            if running.processIdentifier != ProcessInfo.processInfo.processIdentifier,
                               let url = running.bundleURL, AppInstaller.isInstalled(url) {
                                running.terminate()
                            }
                        }
                        NSWorkspace.shared.openApplication(at: AppInstaller.installedURL, configuration: configuration) { _, error in
                            DispatchQueue.main.async {
                                if error == nil { NSApp.terminate(nil) }
                                else {
                                    progress?.stringValue = "Installed. Open Codex Alert from your Applications folder to continue."
                                    button?.isEnabled = true
                                }
                            }
                        }
                    }
                } catch {
                    let failedRollback = (error as? AppError).map { if case .rollbackFailed = $0 { return true }; return false } ?? false
                    let installationBusy = (error as? AppError).map { if case .installationBusy = $0 { return true }; return false } ?? false
                    DispatchQueue.main.async {
                        if installationBusy {
                            progress?.stringValue = "Another Codex Alert installation is running. Wait for it to finish, then try again."
                        } else {
                            progress?.stringValue = failedRollback
                                ? "Installation needs attention. Your previous app is kept in a .codex-alert-install recovery folder inside Applications. Monitoring may need restarting."
                                : "Installation could not finish. Your settings are safe. Reopen the app outside Codex and try again."
                        }
                        button?.isEnabled = true
                    }
                }
            }
        }
        add(button, to: stack, fullWidth: false)
        present(window)
    }

    @objc func showSettings() {
        if let existing = settingsWindow, existing.isVisible { present(existing); return }
        let (window, stack) = window("Codex Alert Settings", height: 730)
        settingsWindow = window
        brand(stack, subtitle: "Your focus, your rules.")
        add(label(client.preview ? "Preview · changes do not affect your Mac" : "Changes apply to the background service immediately.", size: 12, color: .secondaryLabelColor), to: stack)
        if status.phone != "ntfy" {
            add(label("Phone notifications are off. Connect ntfy when you want alerts on your phone.", size: 12, color: .secondaryLabelColor), to: stack)
            add(ActionButton("Connect phone…") { [weak self] in self?.showSetup() }, to: stack, fullWidth: false)
        }

        let threshold = NSTextField(string: String(format: "%g", status.minSeconds / 60))
        threshold.alignment = .right
        threshold.widthAnchor.constraint(equalToConstant: 72).isActive = true
        threshold.setAccessibilityLabel("Minimum task duration in minutes")
        let grouping = NSTextField(string: String(status.groupSeconds))
        grouping.alignment = .right
        grouping.widthAnchor.constraint(equalToConstant: 72).isActive = true
        grouping.setAccessibilityLabel("Group completions within seconds")
        let awake = NSPopUpButton(frame: .zero, pullsDown: false)
        awake.addItems(withTitles: ["Off", "While plugged in", "Also on battery"])
        awake.selectItem(at: ["off", "plugged_in", "always"].firstIndex(of: status.keepAwake) ?? 1)
        awake.setAccessibilityLabel("Keep Mac awake during tasks")
        let rows = NSGridView(views: [
            [label("Notify for tasks longer than"), threshold, label("minutes", color: .secondaryLabelColor)],
            [label("Group nearby completions"), grouping, label("seconds", color: .secondaryLabelColor)],
            [label("Keep Mac awake during tasks"), awake, NSView()]
        ])
        rows.rowSpacing = 14
        rows.columnSpacing = 10
        rows.xPlacement = .leading
        rows.yPlacement = .center
        add(rows, to: stack)

        let flash = NSButton(checkboxWithTitle: "Flash the screen when a task completes", target: nil, action: nil)
        flash.state = status.flash ? .on : .off
        let names = NSButton(checkboxWithTitle: "Include the conversation name in phone notifications", target: nil, action: nil)
        names.state = status.includeTaskName ? .on : .off
        add(flash, to: stack)
        add(names, to: stack)
        add(label("Names are sent to ntfy and can appear on your phone’s lock screen. Turn this off for generic alerts.", size: 11, color: .secondaryLabelColor), to: stack)

        let quiet = NSButton(checkboxWithTitle: "Quiet hours", target: nil, action: nil)
        quiet.state = status.quietEnabled ? .on : .off
        add(quiet, to: stack)
        let start = NSTextField(string: status.quietStart)
        let end = NSTextField(string: status.quietEnd)
        for field in [start, end] { field.widthAnchor.constraint(equalToConstant: 66).isActive = true }
        start.setAccessibilityLabel("Quiet hours start, 24-hour time")
        end.setAccessibilityLabel("Quiet hours end, 24-hour time")
        let hours = NSStackView(views: [label("From"), start, label("to"), end, label("local time · HH:MM", size: 11, color: .secondaryLabelColor)])
        hours.orientation = .horizontal
        hours.spacing = 8
        add(hours, to: stack, fullWidth: false)
        add(label("Completions wait until quiet hours end. Pausing notifications leaves sleep protection active. Closing the lid or putting the Mac to sleep manually still interrupts tasks.", size: 11, color: .secondaryLabelColor), to: stack)
        add(label(status.attentionSupported ? "Approval hooks have been observed." : "Approval alerts require trusted Codex hooks. Setup instructions are in the menu.", size: 11, color: .secondaryLabelColor), to: stack)
        let feedback = label("", size: 12, color: .systemTeal)
        add(feedback, to: stack)
        let save = ActionButton("Save settings", prominent: true) {}
        save.invoke = { [weak self, weak save] in
            guard let self = self else { return }
            guard let minutes = Double(threshold.stringValue), minutes.isFinite, minutes >= 0, minutes <= 1440,
                  let seconds = Int(grouping.stringValue), (0...300).contains(seconds),
                  Self.validTime(start.stringValue), Self.validTime(end.stringValue),
                  quiet.state != .on || start.stringValue != end.stringValue else {
                feedback.textColor = .systemOrange
                feedback.stringValue = "Use 0–1440 minutes, 0–300 grouping seconds, and two different times in HH:MM format."
                return
            }
            var args = ["settings"]
            let settings = ["min_seconds=\(minutes * 60)", "group_seconds=\(seconds)",
                            "flash=\(flash.state == .on)", "include_task_name=\(names.state == .on)",
                            "keep_awake=\(["off", "plugged_in", "always"][awake.indexOfSelectedItem])",
                            "quiet_hours.enabled=\(quiet.state == .on)", "quiet_hours.start=\(start.stringValue)",
                            "quiet_hours.end=\(end.stringValue)"]
            for setting in settings { args += ["--set", setting] }
            save?.isEnabled = false
            self.client.run(args) { result in
                save?.isEnabled = true
                switch result {
                case .success:
                    feedback.textColor = .systemTeal
                    feedback.stringValue = self.client.preview ? "Preview only — no settings were changed." : "Settings saved."
                    self.refresh()
                case .failure:
                    feedback.textColor = .systemOrange
                    feedback.stringValue = "Could not save. Check the values and try again."
                }
            }
        }
        add(save, to: stack, fullWidth: false)
        present(window)
    }

    static func validTime(_ value: String) -> Bool {
        value.range(of: "^(?:[01][0-9]|2[0-3]):[0-5][0-9]$", options: .regularExpression) != nil
    }

    @objc func showSetup() {
        if !client.preview && !AppInstaller.isInstalled(client.bundle) { showInstall(); return }
        if let existing = setupWindow, existing.isVisible { present(existing); return }
        let (window, stack) = window("Connect your phone", height: 680)
        setupWindow = window
        brand(stack, subtitle: "One private subscription.")
        add(label("Get Codex alerts on your phone with the free ntfy app.", color: .secondaryLabelColor), to: stack)
        add(label("1. Install ntfy on your iPhone and allow notifications.", bold: true), to: stack)
        add(ActionButton("Open ntfy in the App Store") { [weak self] in
            guard self?.client.preview == false else { return }
            NSWorkspace.shared.open(URL(string: "https://apps.apple.com/app/ntfy/id1625396347")!)
        }, to: stack, fullWidth: false)
        add(label("2. Add a subscription in ntfy. Use ntfy.sh as the server and the topic below.", bold: true), to: stack)

        let topicField = NSTextField(wrappingLabelWithString: "Preparing your private topic…")
        topicField.font = .monospacedSystemFont(ofSize: 12, weight: .medium)
        topicField.textColor = .systemTeal
        topicField.isSelectable = true
        topicField.setAccessibilityLabel("Private ntfy topic")
        add(topicField, to: stack)
        let copy = ActionButton("Copy private topic") { [weak self] in
            guard self?.client.preview == false else { return }
            guard let topic = self?.setupTopic else { return }
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(topic, forType: .string)
        }
        copy.isEnabled = false
        add(copy, to: stack, fullWidth: false)
        add(label("Keep this topic private. Anyone who knows it can read and send messages. Codex Alert stores it only on this Mac; ntfy receives your alerts.", size: 11, color: .secondaryLabelColor), to: stack)
        add(label("3. Send a test, then confirm it arrived on your phone.", bold: true), to: stack)
        let feedback = label("", size: 12, color: .secondaryLabelColor)
        let received = NSButton(checkboxWithTitle: "I received the notification on my phone", target: nil, action: nil)
        received.isEnabled = false
        let test = ActionButton("Send test notification") {}
        test.isEnabled = false
        test.invoke = { [weak self, weak test] in
            guard let self = self else { return }
            test?.isEnabled = false
            feedback.stringValue = "Sending…"
            self.client.run(["test-phone"]) { result in
                test?.isEnabled = true
                switch result {
                case .success:
                    feedback.stringValue = self.client.preview ? "Preview only — nothing was sent." : "Accepted by ntfy. Check your phone to confirm delivery."
                    received.isEnabled = !self.client.preview
                case .failure:
                    feedback.stringValue = "Could not send. Check your connection and try again."
                }
            }
        }
        add(test, to: stack, fullWidth: false)
        add(feedback, to: stack)
        add(received, to: stack)
        add(ActionButton("Done", prominent: true) { [weak self, weak window] in
            if received.state != .on && !((self?.client.preview) ?? false) {
                feedback.stringValue = "Confirm receipt after testing, or close this window and finish later."
            } else {
                self?.setupTopic = nil
                window?.close()
                self?.refresh()
            }
        }, to: stack, fullWidth: false)
        present(window)
        client.run(["setup-phone"]) { [weak self, weak test, weak copy, weak window] result in
            guard let self = self, let window = window, self.setupWindow === window, window.isVisible else { return }
            guard case .success = result, let topic = self.client.privateTopic() else {
                topicField.stringValue = "Could not prepare the subscription. Close this window and try again."
                return
            }
            self.setupTopic = topic
            topicField.stringValue = topic
            copy?.isEnabled = true
            test?.isEnabled = true
        }
    }
}

func selfTest() -> Bool {
    let value = AlertStatus(["watcher": "active", "active_tasks": 3, "min_seconds": 240.0,
                             "phone": "ntfy", "quiet_hours": ["enabled": true, "start": "23:00", "end": "07:00"]])
    guard value.active, value.activeTasks == 3, value.minSeconds == 240,
          value.quietEnabled, value.quietStart == "23:00", !value.paused else { return false }
    guard value.startupWindow(arguments: ["--installed"]) == .settings,
          value.startupWindow(arguments: []) == .none,
          value.startupWindow(arguments: ["--setup"]) == .setup,
          AlertStatus().startupWindow(arguments: []) == .none,
          AlertStatus().startupWindow(arguments: ["--installed"]) == .settings,
          AlertStatus().startupWindow(arguments: ["--setup"]) == .setup else { return false }
    guard AppDelegate.validTime("00:00"), AppDelegate.validTime("23:59"),
          !AppDelegate.validTime("24:00"), !AppDelegate.validTime("7:00"),
          !AppDelegate.validTime("12:00\n--anything") else { return false }
    let home = URL(fileURLWithPath: "/tmp/test path/with $shell characters")
    guard EngineClient.arguments(home: home, command: ["settings", "--set", "flash=true"]) ==
          ["--home", home.path, "settings", "--set", "flash=true"] else { return false }
    let client = EngineClient(home: home, bundle: home, preview: true)
    var called = false
    client.run(["test-phone"]) { if case .success = $0 { called = true } }
    return called && client.privateTopic() == "codex-preview-example-not-a-real-subscription"
}

if CommandLine.arguments.contains("--self-test") {
    let passed = selfTest()
    print(passed ? "Codex Alert UI self-test passed." : "Codex Alert UI self-test failed.")
    exit(passed ? 0 : 1)
}

let preview = CommandLine.arguments.contains("--preview")
let arguments = CommandLine.arguments
var home = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/CodexAlert")
if let index = arguments.firstIndex(of: "--home"), arguments.indices.contains(index + 1) {
    home = URL(fileURLWithPath: arguments[index + 1], isDirectory: true)
}
let application = NSApplication.shared
let delegate = AppDelegate(client: EngineClient(home: home, bundle: Bundle.main.bundleURL, preview: preview))
application.delegate = delegate
application.run()
