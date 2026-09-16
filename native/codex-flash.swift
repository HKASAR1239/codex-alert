import AppKit

// A quiet, click-through screen edge signal. The application never activates,
// changes the display brightness, or receives keyboard/mouse input.
final class AlertPanel: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

final class BorderView: NSView {
    override var isOpaque: Bool { false }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.clear.setFill()
        dirtyRect.fill()

        let edge = NSBezierPath(
            roundedRect: bounds.insetBy(dx: 11, dy: 11),
            xRadius: 18,
            yRadius: 18
        )
        let color = NSColor(calibratedRed: 0.12, green: 0.90, blue: 0.78, alpha: 1)
        let glow = NSShadow()
        glow.shadowColor = color.withAlphaComponent(0.65)
        glow.shadowBlurRadius = 14
        glow.shadowOffset = .zero

        NSGraphicsContext.saveGraphicsState()
        glow.set()
        color.setStroke()
        edge.lineWidth = 7
        edge.stroke()
        NSGraphicsContext.restoreGraphicsState()
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var panels: [NSPanel] = []
    private var timer: Timer?
    private var started = ProcessInfo.processInfo.systemUptime
    private let duration: TimeInterval = 4.2
    private let pulseCount = 3.0

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard !NSScreen.screens.isEmpty else {
            fputs("codex-flash: no active display found\n", stderr)
            NSApplication.shared.terminate(nil)
            return
        }

        for screen in NSScreen.screens {
            let panel = AlertPanel(
                contentRect: screen.frame,
                styleMask: [.borderless, .nonactivatingPanel],
                backing: .buffered,
                defer: false,
                screen: screen
            )
            panel.backgroundColor = .clear
            panel.isOpaque = false
            panel.hasShadow = false
            panel.ignoresMouseEvents = true
            panel.hidesOnDeactivate = false
            panel.isReleasedWhenClosed = false
            panel.level = .screenSaver
            panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary,
                                        .stationary, .ignoresCycle]
            panel.animationBehavior = .none
            panel.alphaValue = 0
            panel.contentView = BorderView(frame: NSRect(origin: .zero, size: screen.frame.size))
            panel.setFrame(screen.frame, display: false)
            panel.orderFrontRegardless()
            panels.append(panel)
        }

        started = ProcessInfo.processInfo.systemUptime
        timer = Timer.scheduledTimer(withTimeInterval: 1.0 / 30.0, repeats: true) {
            [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
    }

    private func tick() {
        let elapsed = ProcessInfo.processInfo.systemUptime - started
        guard elapsed < duration else {
            timer?.invalidate()
            timer = nil
            for panel in panels { panel.orderOut(nil) }
            NSApplication.shared.terminate(nil)
            return
        }

        // One smooth rise/fall per 1.4 seconds; no abrupt white flashes.
        let phase = elapsed / duration * pulseCount
        let opacity = 0.62 * pow(sin(.pi * phase), 2)
        for panel in panels { panel.alphaValue = opacity }
    }
}

MainActor.assumeIsolated {
    let application = NSApplication.shared
    let delegate = AppDelegate()
    application.setActivationPolicy(.prohibited)
    application.delegate = delegate
    withExtendedLifetime(delegate) { application.run() }
}
