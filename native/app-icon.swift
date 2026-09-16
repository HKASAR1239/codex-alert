import AppKit

// Build-time vector artwork; no downloaded assets or GUI interaction.
let output = URL(fileURLWithPath: CommandLine.arguments[1])
let image = NSImage(size: NSSize(width: 1024, height: 1024))
image.lockFocus()
NSColor(calibratedRed: 0.055, green: 0.075, blue: 0.09, alpha: 1).setFill()
NSBezierPath(roundedRect: NSRect(x: 40, y: 40, width: 944, height: 944),
             xRadius: 205, yRadius: 205).fill()
let border = NSBezierPath(roundedRect: NSRect(x: 85, y: 85, width: 854, height: 854),
                          xRadius: 166, yRadius: 166)
NSColor(calibratedRed: 0.12, green: 0.90, blue: 0.78, alpha: 1).setStroke()
border.lineWidth = 24
border.stroke()
let bell = NSImage(systemSymbolName: "bell.badge.fill", accessibilityDescription: nil)!
let configured = bell.withSymbolConfiguration(.init(paletteColors: [
    NSColor(calibratedRed: 0.12, green: 0.90, blue: 0.78, alpha: 1), .white
]))!
configured.draw(in: NSRect(x: 250, y: 230, width: 524, height: 564))
image.unlockFocus()
let bitmap = NSBitmapImageRep(data: image.tiffRepresentation!)!
try bitmap.representation(using: .png, properties: [:])!.write(to: output)
