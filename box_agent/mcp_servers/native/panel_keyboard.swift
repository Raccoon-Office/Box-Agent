// Native file-panel keyboard transport and read-only front verification.
import AppKit
import ApplicationServices

@_silgen_name("_AXUIElementGetWindow")
func getWindowID(_ element: AXUIElement, _ id: UnsafeMutablePointer<UInt32>) -> AXError
@_silgen_name("CGEventSetWindowLocation")
func setWindowLocation(_ event: CGEvent, _ point: CGPoint)

func finish(_ fields: [String: Any]) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
    exit(0)
}
var preparationStarted = false
func refuse(_ code: String) -> Never {
    finish(["effect": preparationStarted ? "unverifiable" : "not_sent", "code": code,
            "keyboard_posted": false])
}
func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
}
func elementAttribute(_ element: AXUIElement, _ name: String) -> AXUIElement? {
    guard let value = attribute(element, name), CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
    return unsafeBitCast(value, to: AXUIElement.self)
}
func isSystemFilePanel(_ element: AXUIElement) -> Bool {
    guard let identifier = attribute(element, "AXIdentifier") as? String else { return false }
    return ["open-panel", "save-panel"].contains(identifier)
}

// Public AXUIElementGetPid reports the host for AppKit's remote proxies.
// Codex's receiver resolution uses actualPID, not that logical host PID.
func actualPID(_ element: AXUIElement) -> pid_t? {
    typealias Getter = @convention(c) (AXUIElement, UnsafeMutablePointer<pid_t>) -> AXError
    guard let symbol = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "_AXUIElementGetActualPid")
    else { return nil }
    var pid: pid_t = 0
    let getter = unsafeBitCast(symbol, to: Getter.self)
    return getter(element, &pid) == .success && pid > 0 ? pid : nil
}

func windowID(_ element: AXUIElement) -> UInt32? {
    var value: UInt32 = 0
    return getWindowID(element, &value) == .success && value != 0 ? value : nil
}

// Explicit foreground fallback for remote AppKit file panels. No input is posted.
if CommandLine.arguments.count == 4 && CommandLine.arguments[1] == "focus-file-panel" {
    guard let pid = Int32(CommandLine.arguments[2]), pid > 0,
          let expected = UInt32(CommandLine.arguments[3]), expected > 0,
          AXIsProcessTrusted() else { refuse("file_panel_invalid_target") }
    let app = AXUIElementCreateApplication(pid)
    var matches:[AXUIElement] = []
    func findPanel(_ element:AXUIElement, _ depth:Int) {
        let identifier = attribute(element,"AXIdentifier") as? String
        if windowID(element) == expected,
           ["open-panel","save-panel","GoToWindow"].contains(identifier ?? "") {
            if !matches.contains(where: { CFEqual($0,element) }) { matches.append(element) }
        }
        if depth < 2 {
            for child in attribute(element,"AXChildren") as? [AXUIElement] ?? [] {
                findPanel(child,depth+1)
            }
        }
    }
    for window in attribute(app,"AXWindows") as? [AXUIElement] ?? [] { findPanel(window,0) }
    guard matches.count == 1, let panel = matches.first,
          let rows = CGWindowListCopyWindowInfo([.optionOnScreenOnly,.excludeDesktopElements],
                                                kCGNullWindowID) as? [[String:Any]],
          rows.filter({ ($0["kCGWindowNumber"] as? UInt32) == expected }).count == 1,
          let row = rows.first(where: { ($0["kCGWindowNumber"] as? UInt32) == expected }),
          (row["kCGWindowOwnerPID"] as? Int32) == pid,
          (row["kCGWindowIsOnscreen"] as? Bool) == true,
          let frame = panelFrame(panel),
          let b = row["kCGWindowBounds"] as? [String:Any],
          let x = b["X"] as? Double, let y = b["Y"] as? Double,
          let w = b["Width"] as? Double, let h = b["Height"] as? Double,
          frame == CGRect(x:x,y:y,width:w,height:h),
          ["AXWindow", "AXSheet"].contains(attribute(panel,"AXRole") as? String ?? ""),
          let running = NSRunningApplication(processIdentifier:pid)
    else { refuse("file_panel_identity_unverified") }
    preparationStarted = true
    if NSWorkspace.shared.frontmostApplication?.processIdentifier != pid {
        guard running.activate(options:[]) else { refuse("file_panel_activation_failed") }
    }
    let raised = AXUIElementPerformAction(panel,kAXRaiseAction as CFString)
    // AXRaise alone restored the exact sheet focus in the live PS reproduction.
    // Do not add a second focus mutation when the first is unverifiable.
    guard raised == .success else { refuse("file_panel_raise_failed") }
    usleep(300000)
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == pid,
          let focused = elementAttribute(app,"AXFocusedWindow"),
          windowID(focused) == expected, CFEqual(focused,panel)
    else { refuse("file_panel_focus_unverified") }
    finish(["effect":"confirmed", "code":"file_panel_focus_verified",
            "pid":pid, "window_id":expected, "frontmost_pid":pid,
            "focused_window_id":expected, "keyboard_posted":false])
}

// Read-only verification; never activate, raise, prepare focus, or post events.
if CommandLine.arguments.count == 4 && CommandLine.arguments[1] == "verify-panel-front" {
    guard let pid = Int32(CommandLine.arguments[2]), pid > 0,
          let expected = UInt32(CommandLine.arguments[3]), expected > 0
    else { refuse("file_panel_invalid_target") }
    guard AXIsProcessTrusted() else { refuse("file_panel_permission_unavailable") }
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == pid
    else { refuse("file_panel_not_frontmost") }
    let app = AXUIElementCreateApplication(pid)
    guard let focused = elementAttribute(app, "AXFocusedWindow"), windowID(focused) == expected,
          let identifier = attribute(focused, "AXIdentifier") as? String,
          ["open-panel", "save-panel", "GoToWindow"].contains(identifier),
          let rows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements],
                                               kCGNullWindowID) as? [[String: Any]]
    else { refuse("file_panel_identity_unverified") }
    func bounds(_ row: [String: Any]) -> CGRect? {
        guard let b = row["kCGWindowBounds"] as? [String: Any],
              let x = b["X"] as? Double, let y = b["Y"] as? Double,
              let w = b["Width"] as? Double, let h = b["Height"] as? Double,
              [x,y,w,h].allSatisfy({ $0.isFinite }), w > 0, h > 0 else { return nil }
        return CGRect(x:x,y:y,width:w,height:h)
    }
    let targets = rows.enumerated().filter { ($0.element["kCGWindowNumber"] as? UInt32) == expected }
    guard targets.count == 1, let target = targets.first,
          (target.element["kCGWindowOwnerPID"] as? Int32) == pid,
          (target.element["kCGWindowIsOnscreen"] as? Bool) == true,
          let frame = bounds(target.element), panelFrame(focused) == frame
    else { refuse("file_panel_window_unverified") }
    if identifier == "GoToWindow" {
        var parent = focused, seen = Set<AXUIElement>()
        while !isSystemFilePanel(parent), let next = elementAttribute(parent,"AXParent") {
            guard seen.count < 128, seen.insert(next).inserted else { refuse("file_panel_parent_unverified") }
            parent = next
        }
        guard isSystemFilePanel(parent) else { refuse("file_panel_parent_unverified") }
    }
    var count:UInt32 = 0
    guard CGGetActiveDisplayList(0,nil,&count) == .success, count > 0 else { refuse("display_unknown") }
    var displays = [CGDirectDisplayID](repeating:0,count:Int(count))
    guard CGGetActiveDisplayList(count,&displays,&count) == .success else { refuse("display_unknown") }
    func display(_ rect:CGRect) -> CGDirectDisplayID? {
        var best:CGDirectDisplayID? = nil; var area:CGFloat = 0
        for id in displays.prefix(Int(count)) {
            let intersection = rect.intersection(CGDisplayBounds(id))
            let size = intersection.isNull ? 0 : intersection.width * intersection.height
            if size > area { best = id; area = size }
        }
        return best
    }
    guard let targetDisplay = display(frame) else { refuse("display_unknown") }
    for row in rows.prefix(target.offset) where (row["kCGWindowOwnerPID"] as? Int32) == pid {
        guard let other = bounds(row), let otherDisplay = display(other) else { refuse("window_order_unknown") }
        if otherDisplay == targetDisplay { refuse("file_panel_obstructed") }
    }
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == pid,
          let latest = elementAttribute(app,"AXFocusedWindow"), CFEqual(latest,focused),
          windowID(latest) == expected else { refuse("file_panel_focus_changed") }
    finish(["effect":"confirmed", "code":"file_panel_front_verified", "pid":pid,
            "window_id":expected, "layer":target.element["kCGWindowLayer"] ?? 0,
            "focused_window_id":expected, "frontmost_pid":pid,
            "frame":[frame.minX,frame.minY,frame.width,frame.height], "panel_identifier":identifier,
            "display_id":targetDisplay, "read_only":true])
}

// Geometry shared by read-only panel verification.
func panelFrame(_ element: AXUIElement) -> CGRect? {
    guard let p = attribute(element, "AXPosition"), let s = attribute(element, "AXSize"),
          CFGetTypeID(p) == AXValueGetTypeID(), CFGetTypeID(s) == AXValueGetTypeID() else { return nil }
    var origin = CGPoint.zero, size = CGSize.zero
    guard AXValueGetValue(unsafeBitCast(p, to: AXValue.self), .cgPoint, &origin),
          AXValueGetValue(unsafeBitCast(s, to: AXValue.self), .cgSize, &size) else { return nil }
    return CGRect(origin: origin, size: size)
}

// Read-only host/sheet geometry proof. Mouse delivery belongs to Driver's
// explicit foreground desktop route; this helper never posts mouse events here.
if CommandLine.arguments.count == 4 && CommandLine.arguments[1] == "panel-geometry" {
    guard let pid = Int32(CommandLine.arguments[2]), pid > 0,
          let expected = UInt32(CommandLine.arguments[3]), AXIsProcessTrusted()
    else { refuse("panel_geometry_invalid_request") }
    let app = AXUIElementCreateApplication(pid)
    guard let sheet = elementAttribute(app, "AXFocusedWindow"), windowID(sheet) == expected,
          attribute(sheet, "AXRole") as? String == "AXSheet",
          attribute(sheet, "AXIdentifier") as? String == "GoToWindow",
          let sf = panelFrame(sheet) else { refuse("panel_geometry_sheet_unresolved") }
    var host = sheet, seen = Set<AXUIElement>()
    var ancestry = [sheet]
    while let parent = elementAttribute(host, "AXParent") {
        if attribute(parent, "AXRole") as? String == "AXApplication" { break }
        guard seen.count < 128, seen.insert(parent).inserted else { refuse("panel_geometry_parent_unresolved") }
        host = parent
        if ["AXWindow", "AXSheet"].contains(attribute(parent, "AXRole") as? String ?? ""),
           let id = windowID(parent), id != windowID(ancestry.last!) {
            ancestry.append(parent)
        }
    }
    guard let panel = ancestry.dropFirst().first(where: isSystemFilePanel),
          let parentID = windowID(panel), parentID != expected, let pf = panelFrame(panel),
          // Compact save panels can be narrower/shorter than their GoTo sheet.
          // AX ancestry and exact CG frames prove ownership, not containment.
          // The capture still uses the parent's own bounds and may be clipped.
          pf.intersects(sf),
          let rows = CGWindowListCopyWindowInfo(.optionAll, kCGNullWindowID) as? [[String:Any]]
    else { refuse("panel_geometry_parent_unresolved") }
    func matches(_ id:UInt32, _ frame:CGRect) -> Bool {
        let found = rows.filter { ($0["kCGWindowNumber"] as? UInt32) == id }
        guard found.count == 1, let row = found.first,
              row["kCGWindowOwnerPID"] as? Int32 == pid,
              let b = row["kCGWindowBounds"] as? [String:Any],
              let x=b["X"] as? Double, let y=b["Y"] as? Double,
              let w=b["Width"] as? Double, let h=b["Height"] as? Double
        else { return false }
        return frame == CGRect(x:x,y:y,width:w,height:h)
    }
    var frames = [[String:Any]]()
    for surface in ancestry {
        guard let id = windowID(surface), let frame = panelFrame(surface), matches(id, frame)
        else { refuse("panel_geometry_ancestor_changed") }
        frames.append(["window_id":id, "frame":[frame.minX,frame.minY,frame.width,frame.height],
                       "role":attribute(surface,"AXRole") as? String ?? ""])
    }
    guard matches(expected,sf), matches(parentID,pf),
          let latest=elementAttribute(app,"AXFocusedWindow"),CFEqual(latest,sheet)
    else { refuse("panel_geometry_changed") }
    finish(["effect":"confirmed", "code":"panel_geometry_verified", "read_only":true,
            "pid":pid, "window_id":expected, "parent_window_id":parentID,
            "sheet_frame":[sf.minX,sf.minY,sf.width,sf.height],
            "parent_frame":[pf.minX,pf.minY,pf.width,pf.height],
            "sheet_contained_in_parent":pf.contains(sf),
            "ancestry":frames,
            "frontmost_pid":NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0])
}

// Resolve lease eligibility through the logical host. The remote panel service
// need not expose AXWindows or AXFocusedWindow of its own.
func hasSystemPanelReceiver(_ pid: pid_t, host hostPID: pid_t, window expected: UInt32) -> Bool {
    let app = AXUIElementCreateApplication(hostPID)
    guard var panel = elementAttribute(app, "AXFocusedWindow") else { return false }
    var ancestors = Set<AXUIElement>()
    var matchedWindow = windowID(panel) == expected
    while !isSystemFilePanel(panel), let parent = elementAttribute(panel, "AXParent") {
        if attribute(parent, "AXRole") as? String == "AXApplication" { break }
        guard ancestors.count < 128, ancestors.insert(parent).inserted else { return false }
        panel = parent
        matchedWindow = matchedWindow || windowID(parent) == expected
    }
    guard matchedWindow,
          isSystemFilePanel(panel),
          (attribute(panel, "AXSubrole") as? String == "AXDialog"
           || attribute(panel, "AXRole") as? String == "AXSheet") else { return false }
    if pid == hostPID { return true }
    guard NSRunningApplication(processIdentifier: pid)?.bundleIdentifier
        == "com.apple.appkit.xpc.openAndSavePanelService" else { return false }
    var pending = [panel], seen = Set<AXUIElement>()
    while let element = pending.popLast(), seen.count < 5000 {
        if !seen.insert(element).inserted { continue }
        if actualPID(element) == pid { return true }
        pending.append(contentsOf: attribute(element, "AXChildren") as? [AXUIElement] ?? [])
    }
    return false
}

// A process-scoped lease owned by the adapter. EOF, inactivity, or target exit
// releases synthetic activation. It never activates a real application.
final class PanelLease {
    let pid: pid_t
    let token: Int64
    var tap: CFMachPort?
    init(_ pid: pid_t, _ token: Int64) { self.pid = pid; self.token = token }
    func release() {
        if let tap = tap { CGEvent.tapEnable(tap: tap, enable: false); CFMachPortInvalidate(tap) }
        if NSWorkspace.shared.frontmostApplication?.processIdentifier != pid {
            NSEvent.otherEvent(with: .appKitDefined, location: .zero, modifierFlags: [],
                timestamp: 0, windowNumber: 0, context: nil, subtype: 2, data1: 0, data2: 0)?.cgEvent?.postToPid(pid)
        }
    }
}
if CommandLine.arguments.count == 6 && CommandLine.arguments[1] == "panel-lease" {
    guard let pid = Int32(CommandLine.arguments[2]), pid > 0,
          let token = Int64(CommandLine.arguments[3]), token != 0,
          let hostPID = Int32(CommandLine.arguments[4]), hostPID > 0,
          let hostWindow = UInt32(CommandLine.arguments[5]), hostWindow > 0,
          hasSystemPanelReceiver(pid, host: hostPID, window: hostWindow),
          AXIsProcessTrusted(), CGPreflightPostEventAccess() else { refuse("panel_lease_unavailable") }
    let lease = PanelLease(pid, token)
    let mask: CGEventMask = [10, 11, 12, 13, 19, 20].reduce(0) { $0 | (1 << $1) }
    lease.tap = CGEvent.tapCreateForPid(pid: pid, place: .headInsertEventTap,
        options: .defaultTap, eventsOfInterest: mask, callback: { _, type, event, context in
            let state = Unmanaged<PanelLease>.fromOpaque(context!).takeUnretainedValue()
            if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                if let tap = state.tap { CGEvent.tapEnable(tap: tap, enable: true) }
                return Unmanaged.passUnretained(event)
            }
            let front = NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0
            if front > 0 && front != state.pid {
                if [10, 11, 12].contains(type.rawValue),
                   event.getIntegerValueField(.eventSourceUserData) != state.token {
                    event.postToPid(front)
                    return nil
                }
                if type.rawValue == 19 || (type.rawValue == 13 &&
                    event.getIntegerValueField(CGEventField(rawValue: 83)!) == 2) { return nil }
            }
            return Unmanaged.passUnretained(event)
        }, userInfo: Unmanaged.passUnretained(lease).toOpaque())
    guard let tap = lease.tap, let source = CFMachPortCreateRunLoopSource(nil, tap, 0)
    else { refuse("panel_lease_tap_unavailable") }
    CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
    CGEvent.tapEnable(tap: tap, enable: true)
    var expires = Date().addingTimeInterval(300)
    var ended = false
    DispatchQueue.global().async {
        while readLine() != nil {
            DispatchQueue.main.async { expires = Date().addingTimeInterval(300) }
        }
        DispatchQueue.main.async { ended = true }
    }
    print("{\"ready\":true}"); fflush(stdout)
    while !ended && Date() < expires && NSRunningApplication(processIdentifier: pid)?.isTerminated == false {
        RunLoop.current.run(until: Date().addingTimeInterval(0.1))
    }
    lease.release()
    exit(0)
}
let panelToken = ProcessInfo.processInfo.environment["BOX_CUA_PANEL_TOKEN"].flatMap(Int64.init)
func requirePanelLease() {
    guard panelToken != nil,
          let owner = ProcessInfo.processInfo.environment["BOX_CUA_PANEL_LEASE_PID"].flatMap(Int32.init),
          owner > 0, kill(owner, 0) == 0 else { refuse("panel_lease_required") }
}

// Shared construction for host and cold-receiver window focus preparation.
func panelWindowClickEvents(_ id: UInt32, origin: CGPoint, size: CGSize,
                            eventNumbers: [Int]) -> [CGEvent]? {
    let point = CGPoint(x: origin.x + size.width / 2, y: origin.y + size.height / 2)
    var events = [CGEvent]()
    for (index, type) in [NSEvent.EventType.leftMouseDown, .leftMouseUp].enumerated() {
        guard let event = NSEvent.mouseEvent(with: type, location: point, modifierFlags: [],
            timestamp: 0, windowNumber: Int(id), context: nil, eventNumber: eventNumbers[index],
            clickCount: 1, pressure: 1)?.cgEvent else { return nil }
        event.flags = []
        event.location = point
        event.setIntegerValueField(.mouseEventButtonNumber, value: 0)
        event.setIntegerValueField(.mouseEventSubtype, value: 3)
        for field: UInt32 in [91, 92] {
            event.setIntegerValueField(CGEventField(rawValue: field)!, value: Int64(id))
        }
        setWindowLocation(event, CGPoint(x: size.width / 2, y: size.height / 2))
        events.append(event)
    }
    return events
}

// Existing system-panel preparation; not used to synthesize editor/search focus.
func prepareWindow(_ pid: pid_t, _ window: AXUIElement) -> Bool {
    guard let id = windowID(window),
          let p = attribute(window, "AXPosition"), let s = attribute(window, "AXSize"),
          CFGetTypeID(p) == AXValueGetTypeID(), CFGetTypeID(s) == AXValueGetTypeID()
    else { return false }
    var origin = CGPoint.zero, size = CGSize.zero
    guard AXValueGetValue(unsafeBitCast(p, to: AXValue.self), .cgPoint, &origin),
          AXValueGetValue(unsafeBitCast(s, to: AXValue.self), .cgSize, &size),
          [origin.x, origin.y, size.width, size.height].allSatisfy({ $0.isFinite }),
          size.width > 0, size.height > 0,
          let focusType = NSEvent.EventType(rawValue: 21),
          let focus = NSEvent.otherEvent(with: focusType, location: .zero,
              modifierFlags: [], timestamp: 0, windowNumber: 0, context: nil,
              subtype: Int16(bitPattern: 0x8000), data1: 0, data2: 0)?.cgEvent
    else { return false }
    guard var events = panelWindowClickEvents(id, origin: origin, size: size,
                                             eventNumbers: [1, 1]) else { return false }
    events.append(focus)
    preparationStarted = true
    for event in events { event.postToPid(pid); usleep(50000) }
    let app = AXUIElementCreateApplication(pid)
    return elementAttribute(app, "AXFocusedWindow").map { CFEqual($0, window) } == true
}

struct PanelKeyboardTarget {
    let pid: pid_t
    let window: UInt32
}

func resolvePanelKeyboardTarget(app: AXUIElement, hostPID pid: pid_t,
                                focusedWindow: UInt32, requestedField: AXUIElement?) -> PanelKeyboardTarget {
    guard var candidate = elementAttribute(app, "AXFocusedUIElement")
    else { refuse("focused_element_unavailable") }
    if let requestedField = requestedField, !CFEqual(requestedField, candidate) {
        refuse("panel_field_identity_changed")
    }
    var visited = Set<AXUIElement>()
    var remote: (pid_t, UInt32)?
    while visited.insert(candidate).inserted {
        guard let owner = actualPID(candidate) else { refuse("ax_actual_owner_unavailable") }
        if owner != pid && owner > 0 {
            guard NSRunningApplication(processIdentifier: owner)?.bundleIdentifier
                    == "com.apple.appkit.xpc.openAndSavePanelService"
            else { refuse("not_file_panel_service") }
            // Remote path fields may expose no window ID through the host proxy.
            // Read the receiver's focused window, verifying the same focused field.
            let receiver = AXUIElementCreateApplication(owner)
            let receiverFocus = elementAttribute(receiver, "AXFocusedUIElement")
            let receiverWindow = elementAttribute(receiver, "AXFocusedWindow")
            let id = windowID(candidate) ?? (
                receiverFocus.map { CFEqual($0, candidate) } == true
                && receiverWindow.flatMap(actualPID) == owner
                ? receiverWindow.flatMap(windowID) : nil)
            guard let id = id else { refuse("receiver_window_unavailable") }
            remote = (owner, id)
            break
        }
        guard let parent = elementAttribute(candidate, "AXParent") else { break }
        candidate = parent
    }
    let (target, window) = remote ?? (pid, focusedWindow)
    return PanelKeyboardTarget(pid: target, window: window)
}

// Command policy is separate from the shared receiver/focus/event transport.
// These are the validated panel operations, not a general keyboard allowlist.
enum PanelCommand: UInt16 {
    case goToFolder = 5
    case confirm = 36
    case cancel = 53

    var modifiers: CGEventFlags { self == .goToFolder ? [.maskCommand, .maskShift] : [] }
    var permitsColdReceiverPreparation: Bool { self == .goToFolder }
    var permitsAddressedConfirmation: Bool { self == .confirm }
}

guard CommandLine.arguments.count == 4,
      let pid = Int32(CommandLine.arguments[1]), pid > 0,
      let expectedWindow = UInt32(CommandLine.arguments[2]),
      let code = UInt16(CommandLine.arguments[3]), let command = PanelCommand(rawValue: code)
else { refuse("invalid_arguments") }
guard AXIsProcessTrusted(), CGPreflightPostEventAccess() else { refuse("native_helper_permission_unavailable") }
let app = AXUIElementCreateApplication(pid)
guard let focused = elementAttribute(app, "AXFocusedWindow"), let focusedID = windowID(focused)
else { refuse("focused_window_unavailable") }
var host = focused
var ancestors = Set<AXUIElement>()
var windowIDs: Set<UInt32> = [focusedID]
while !isSystemFilePanel(host), let parent = elementAttribute(host, "AXParent") {
    if attribute(parent, "AXRole") as? String == "AXApplication" { break }
    guard ancestors.count < 128, ancestors.insert(parent).inserted else { refuse("ax_parent_cycle") }
    host = parent
    if let id = windowID(parent) { windowIDs.insert(id) }
}
guard windowIDs.contains(expectedWindow) else { refuse("focused_window_mismatch") }
guard isSystemFilePanel(host),
      (attribute(host, "AXSubrole") as? String == "AXDialog"
       || attribute(host, "AXRole") as? String == "AXSheet")
else { refuse("not_open_panel") }
// File panels require window focus without marking the panel host active.
// A host app-active notification raises its modal panels to layer 8 even when
// NSWorkspace still reports another app as foreground (VM regression).
// The keyboard path prepares host focus before resolving the remote receiver.
// Without this ordering AppKit may expose no focused element in the background.
requirePanelLease()
// An indexed confirmation may use this transport only for the exact field
// that currently owns focus. Do not turn an arbitrary indexed action into Return.
var requestedField: AXUIElement?
if let encoded = ProcessInfo.processInfo.environment["BOX_CUA_PANEL_FIELD_FRAME"] {
    guard command.permitsAddressedConfirmation, let data = encoded.data(using: .utf8),
          let frame = try? JSONSerialization.jsonObject(with: data) as? [Double],
          frame.count == 4, frame.allSatisfy({ $0.isFinite }),
          frame[2] > 0, frame[3] > 0,
          attribute(focused, "AXIdentifier") as? String == "GoToWindow",
          let field = elementAttribute(app, "AXFocusedUIElement"),
          attribute(field, "AXRole") as? String == "AXTextField",
          attribute(field, "AXIdentifier") as? String == "PathTextField",
          let p = attribute(field, "AXPosition"), CFGetTypeID(p) == AXValueGetTypeID(),
          let s = attribute(field, "AXSize"), CFGetTypeID(s) == AXValueGetTypeID()
    else { refuse("panel_field_identity_unverified") }
    var point = CGPoint.zero, size = CGSize.zero
    guard AXValueGetValue(unsafeBitCast(p, to: AXValue.self), .cgPoint, &point),
          AXValueGetValue(unsafeBitCast(s, to: AXValue.self), .cgSize, &size),
          [point.x, point.y, size.width, size.height] == frame
    else { refuse("panel_field_identity_unverified") }
    requestedField = field
}
if elementAttribute(app, "AXFocusedUIElement") == nil || focusedID != expectedWindow {
    let original = NSWorkspace.shared.frontmostApplication
    let before = original?.processIdentifier
    guard prepareWindow(pid, focused) else { refuse("host_focus_preparation_unverified") }
    if let before = before, before != pid,
       NSWorkspace.shared.frontmostApplication?.processIdentifier == before {
        _ = original?.activate(options: [])
        usleep(50000)
    }
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == before
    else { refuse("foreground_changed_during_preparation") }
}
// Every panel command uses the same live receiver resolution.
let resolved = resolvePanelKeyboardTarget(app: app, hostPID: pid,
                                         focusedWindow: focusedID, requestedField: requestedField)
let target = resolved.pid
let remoteWindow = resolved.window
if ProcessInfo.processInfo.environment["BOX_CUA_RESOLVE_PANEL"] == "1" {
    finish(["effect": "not_sent", "code": "panel_target_resolved",
            "resolved_pid": target, "receiver_window_id": remoteWindow])
}
requirePanelLease()
guard ProcessInfo.processInfo.environment["BOX_CUA_PANEL_PID"] == String(target),
      ProcessInfo.processInfo.environment["BOX_CUA_PANEL_WINDOW"] == String(remoteWindow)
else { refuse("panel_target_changed") }
let panelApp = AXUIElementCreateApplication(target)
var preparation = [CGEvent]()
var receiverWindow = remoteWindow
if elementAttribute(panelApp, "AXFocusedWindow") == nil {
    // A cold panel needs a window activation click, not just an app-active
    // notification. These window-directed mouse events use subtype 3 and never use HID.
    guard command.permitsColdReceiverPreparation,
          let hostID = windowID(host),
          let position = attribute(host, "AXPosition"), CFGetTypeID(position) == AXValueGetTypeID(),
          let size = attribute(host, "AXSize"), CFGetTypeID(size) == AXValueGetTypeID()
    else { refuse("panel_focus_unavailable") }
    var origin = CGPoint.zero
    var extent = CGSize.zero
    guard AXValueGetValue(unsafeBitCast(position, to: AXValue.self), .cgPoint, &origin),
          AXValueGetValue(unsafeBitCast(size, to: AXValue.self), .cgSize, &extent),
          origin.x.isFinite, origin.y.isFinite, extent.width.isFinite, extent.height.isFinite,
          extent.width > 0, extent.height > 0
    else { refuse("panel_geometry_unavailable") }
    guard let events = panelWindowClickEvents(hostID, origin: origin, size: extent,
                                              eventNumbers: [1, 2])
    else { refuse("event_allocation_failed") }
    preparation = events
    receiverWindow = remoteWindow
}
guard let latest = elementAttribute(app, "AXFocusedWindow"), windowID(latest) == focusedID
else { refuse("focused_window_changed") }

// Allocate every event before sending anything. Refusals above are all not_sent.
guard let activation = NSEvent.otherEvent(
    with: .appKitDefined, location: .zero,
    modifierFlags: NSEvent.ModifierFlags(rawValue: 0xc0000),
    timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: Int(receiverWindow),
    context: nil, subtype: 1, data1: 0, data2: 0)?.cgEvent,
    let focusType = NSEvent.EventType(rawValue: 21),
    let focus = NSEvent.otherEvent(
        with: focusType, location: .zero, modifierFlags: [],
        timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: 0,
        context: nil, subtype: Int16(bitPattern: 0x8000), data1: 0, data2: 0)?.cgEvent,
    let down = CGEvent(keyboardEventSource: CGEventSource(stateID: .hidSystemState), virtualKey: command.rawValue, keyDown: true),
    let up = CGEvent(keyboardEventSource: CGEventSource(stateID: .hidSystemState), virtualKey: command.rawValue, keyDown: false)
else { refuse("event_allocation_failed") }
down.flags = command.modifiers
up.flags = down.flags
// The adapter owns the lease across keys. No per-key deactivation: releasing
// that state here would also dismiss the current modal panel focus.
let originalFront = NSWorkspace.shared.frontmostApplication
let frontBefore = originalFront?.processIdentifier ?? 0
preparationStarted = true
for event in preparation { event.postToPid(pid); usleep(50000) }
if target != pid {
    activation.postToPid(target)
    usleep(50000)
}
focus.postToPid(target)
usleep(50000)
// Codex separates safe focus preparation from key delivery and restores the
// saved foreground application. Only do so while it is still the user's choice;
// never undo a real foreground switch that occurred during preparation.
var foregroundReasserted = false
if !preparation.isEmpty && frontBefore > 0 && frontBefore != pid && frontBefore != target,
   NSWorkspace.shared.frontmostApplication?.processIdentifier == frontBefore {
    foregroundReasserted = originalFront?.activate(options: []) == true
    usleep(50000)
}
guard NSWorkspace.shared.frontmostApplication?.processIdentifier == frontBefore else {
    // Preparation already ran; this is not permission to replay via Driver.
    finish(["effect": "unverifiable", "code": "foreground_changed_during_preparation",
            "keyboard_posted": false])
}
if let requestedField = requestedField,
   elementAttribute(app, "AXFocusedUIElement").map({ CFEqual($0, requestedField) }) != true {
    refuse("panel_field_identity_changed")
}
down.setIntegerValueField(.eventSourceUserData, value: panelToken!)
up.setIntegerValueField(.eventSourceUserData, value: panelToken!)
down.postToPid(target)
usleep(8000)
up.postToPid(target)
finish(["effect": "unverifiable", "code": "panel_key_posted",
        "requested_pid": pid, "resolved_pid": target,
        "requested_window_id": expectedWindow, "focused_window_id": focusedID,
        "host_window_id": windowID(host) ?? 0,
        "receiver_window_id": receiverWindow, "activation_prepared": !preparation.isEmpty,
        "focus_lifetime": "session", "foreground_reasserted": foregroundReasserted,
        "frontmost_before": frontBefore,
        "frontmost_after": NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0])
