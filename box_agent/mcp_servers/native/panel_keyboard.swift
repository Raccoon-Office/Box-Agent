// Native menu/file-panel input. No target AXRaise or global HID post.
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

// Only dispatch to the current editable responder. Role, ancestry, actual
// receiver PID and writable text attributes are required; app names are not.
func writable(_ element: AXUIElement, _ name: String) -> Bool {
    var value: DarwinBoolean = false
    return AXUIElementIsAttributeSettable(element, name as CFString, &value) == .success
        && value.boolValue
}
func editableResponder(_ element: AXUIElement, in window: AXUIElement, pid: pid_t,
                       textAttribute: String) -> Bool {
    guard actualPID(element) == pid,
          (attribute(element, "AXEnabled") as? NSNumber)?.boolValue == true,
          (attribute(element, "AXFocused") as? NSNumber)?.boolValue == true,
          writable(element, textAttribute) else { return false }
    var cursor = element
    var seen = Set<AXUIElement>()
    while seen.count < 128 && seen.insert(cursor).inserted {
        if CFEqual(cursor, window) { return true }
        guard let parent = elementAttribute(cursor, "AXParent") else { return false }
        cursor = parent
    }
    return false
}

// Foreground-only app activation for Drivers that require an ordinary window.
if CommandLine.arguments.count == 4 && CommandLine.arguments[1] == "activate-app" {
    guard let pid = Int32(CommandLine.arguments[2]), pid > 0,
          let running = NSRunningApplication(processIdentifier: pid),
          running.bundleIdentifier == CommandLine.arguments[3], !running.isTerminated
    else { refuse("application_identity_changed") }
    let accepted = running.activate(options: [])
    finish(["effect": "unverifiable", "code": "app_activation_requested",
            "accepted": accepted, "pid": pid])
}

if CommandLine.arguments.count == 5 && CommandLine.arguments[1] == "search-escape" {
    guard let pid = Int32(CommandLine.arguments[2]), pid > 0,
          let expected = UInt32(CommandLine.arguments[3]), expected > 0,
          let bytes = CommandLine.arguments[4].data(using: .utf8),
          let frame = try? JSONSerialization.jsonObject(with: bytes) as? [Double], frame.count == 4,
          AXIsProcessTrusted(), CGPreflightPostEventAccess()
    else { refuse("search_escape_unavailable") }
    let app = AXUIElementCreateApplication(pid)
    guard let window = elementAttribute(app, "AXFocusedWindow"), windowID(window) == expected,
          axFrame(window) == frame,
          (attribute(window, "AXSheets") as? [AXUIElement] ?? []).isEmpty,
          let field = elementAttribute(app, "AXFocusedUIElement"),
          attribute(field, "AXRole") as? String == "AXTextField",
          attribute(field, "AXSubrole") as? String == "AXSearchField",
          windowID(field) == expected,
          editableResponder(field, in: window, pid: pid, textAttribute: "AXValue"),
          let down = CGEvent(keyboardEventSource: nil, virtualKey: 53, keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: 53, keyDown: false)
    else { refuse("search_escape_not_focused") }
    guard elementAttribute(app, "AXFocusedUIElement").map({ CFEqual($0, field) }) == true,
          elementAttribute(app, "AXFocusedWindow").map({ CFEqual($0, window) }) == true,
          axFrame(window) == frame,
          editableResponder(field, in: window, pid: pid, textAttribute: "AXValue"),
          (attribute(window, "AXSheets") as? [AXUIElement] ?? []).isEmpty
    else { refuse("search_escape_target_changed") }
    let front = NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0
    for event in [down, up] {
        event.flags = []
        for n: UInt32 in [91, 92] {
            event.setIntegerValueField(CGEventField(rawValue: n)!, value: Int64(expected))
        }
        preparationStarted = true
        event.postToPid(pid); usleep(10000)
    }
    finish(["effect": "unverifiable", "code": "search_escape_posted", "keyboard_posted": true,
            "requested_window_id": expected, "frontmost_before": front,
            "frontmost_after": NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0])
}

// Focused editor insertion preserves rich text: never replace AXValue or move the
// selection implicitly. Navigation remains a real document navigation key.
if CommandLine.arguments.count == 5 && CommandLine.arguments[1] == "focused-editor" {
    guard let pid = Int32(CommandLine.arguments[2]), pid > 0,
          let expected = UInt32(CommandLine.arguments[3]), expected > 0,
          let bytes = CommandLine.arguments[4].data(using: .utf8),
          let request = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any],
          let frame = request["frame"] as? [Double], frame.count == 4,
          AXIsProcessTrusted(), CGPreflightPostEventAccess()
    else { refuse("focused_editor_unavailable") }
    let app = AXUIElementCreateApplication(pid)
    guard let window = elementAttribute(app, "AXFocusedWindow"), windowID(window) == expected,
          axFrame(window) == frame, attribute(window, "AXRole") as? String == "AXWindow"
    else { refuse("focused_editor_window_mismatch") }
    guard (attribute(window, "AXSheets") as? [AXUIElement] ?? []).isEmpty
    else { refuse("focused_editor_sheet_open") }
    guard let focus = elementAttribute(app, "AXFocusedUIElement"), windowID(focus) == expected,
          actualPID(focus) == pid
    else { refuse("focused_editor_focus_mismatch") }

    // Some editors expose the focused document container, with the selection on
    // a descendant AXTextArea. Background Word also reports AXEnabled=false on
    // that text area. Resolve within the current focus, without changing focus
    // or treating the background enabled flag as a keyboard capability check.
    func editorSelection(_ element: AXUIElement) -> (String, CFTypeRef)? {
        guard attribute(element, "AXRole") as? String == "AXTextArea",
              windowID(element) == expected, actualPID(element) == pid,
              writable(element, "AXSelectedText") || writable(element, "AXValue"),
              let text = attribute(element, "AXValue") as? String,
              let selection = attribute(element, "AXSelectedTextRange"),
              CFGetTypeID(selection) == AXValueGetTypeID() else { return nil }
        var range = CFRange()
        guard AXValueGetValue(unsafeBitCast(selection, to: AXValue.self), .cfRange, &range),
              range.location >= 0, range.length >= 0,
              range.location <= text.utf16.count,
              range.length <= text.utf16.count - range.location else { return nil }
        return (text, selection)
    }
    func resolveEditor() -> AXUIElement? {
        if editorSelection(focus) != nil { return focus }
        guard ["AXSplitGroup", "AXGroup", "AXScrollArea"].contains(
            attribute(focus, "AXRole") as? String ?? "") else { return nil }
        var pending = [focus], seen = Set<AXUIElement>(), candidates = [AXUIElement]()
        while let element = pending.popLast() {
            if !seen.insert(element).inserted { continue }
            if seen.count > 2048 { return nil }
            if editorSelection(element) != nil { candidates.append(element) }
            pending.append(contentsOf: attribute(element, "AXChildren") as? [AXUIElement] ?? [])
        }
        if candidates.count == 1 { return candidates[0] }
        let focused = candidates.filter { (attribute($0, "AXFocused") as? NSNumber)?.boolValue == true }
        return focused.count == 1 ? focused[0] : nil
    }
    guard let body = resolveEditor(), let (before, selection) = editorSelection(body)
    else { refuse("focused_editor_selection_unresolved") }
    let document = attribute(window, "AXDocument") as? String
    func unchangedTarget() -> Bool {
        return elementAttribute(app, "AXFocusedWindow").map { CFEqual($0, window) } == true
            && elementAttribute(app, "AXFocusedUIElement").map { CFEqual($0, focus) } == true
            && windowID(body) == expected && axFrame(window) == frame
            && resolveEditor().map { CFEqual($0, body) } == true
            && attribute(window, "AXDocument") as? String == document
            && (attribute(window, "AXSheets") as? [AXUIElement] ?? []).isEmpty
    }
    var events = [CGEvent]()
    if let text = request["text"] as? String {
        // CGEvent Unicode payloads are bounded; split at scalar boundaries so a
        // surrogate pair is never divided. Allocate all events before delivery.
        var chunks = [[UniChar]](), chunk = [UniChar]()
        for scalar in text.unicodeScalars {
            let units = Array(String(scalar).utf16)
            if chunk.count + units.count > 20 { chunks.append(chunk); chunk = [] }
            chunk.append(contentsOf: units)
        }
        if !chunk.isEmpty { chunks.append(chunk) }
        for units in chunks {
            for down in [true, false] {
                guard let event = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: down)
                else { refuse("event_allocation_failed") }
                event.flags = []
                event.keyboardSetUnicodeString(stringLength: units.count, unicodeString: units)
                events.append(event)
            }
        }
    } else if let keycode = request["keycode"] as? UInt16,
              let modifiers = request["modifiers"] as? [String] {
        let flagMap: [String: CGEventFlags] = ["cmd": .maskCommand, "shift": .maskShift,
            "ctrl": .maskControl, "option": .maskAlternate, "fn": .maskSecondaryFn]
        guard modifiers.allSatisfy({ flagMap[$0] != nil })
        else { refuse("focused_editor_operation_unsupported") }
        let flags = modifiers.reduce(CGEventFlags()) { $0.union(flagMap[$1]!) }
        for down in [true, false] {
            guard let event = CGEvent(keyboardEventSource: nil, virtualKey: keycode, keyDown: down)
            else { refuse("event_allocation_failed") }
            event.flags = flags; events.append(event)
        }
    } else { refuse("focused_editor_operation_unsupported") }
    guard unchangedTarget(), attribute(body, "AXValue") as? String == before,
          attribute(body, "AXSelectedTextRange").map({ CFEqual($0, selection) }) == true
    else { refuse("focused_editor_target_changed") }
    let front = NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0
    for (index, event) in events.enumerated() {
        // Never leave a delivered key-down without its key-up. Stop between
        // pairs if focus changes; already posted input must never be replayed.
        if index % 2 == 0 && !unchangedTarget() { refuse("focused_editor_target_changed") }
        event.setIntegerValueField(.eventSourceUserData, value: 7312026)
        for field: UInt32 in [91, 92] {
            event.setIntegerValueField(CGEventField(rawValue: field)!, value: Int64(expected))
        }
        preparationStarted = true
        event.postToPid(pid); usleep(10000)
    }
    finish(["effect": "unverifiable", "code": "focused_editor_posted",
            "keyboard_posted": !events.isEmpty, "requested_window_id": expected,
            "focus_role": attribute(focus, "AXRole") as? String ?? "",
            "editor_role": attribute(body, "AXRole") as? String ?? "",
            "frontmost_before": front,
            "frontmost_after": NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0])
}

// Re-resolve an observed attached sheet without reusing parent-window Driver tokens.
func axFrame(_ element: AXUIElement) -> [Double]? {
    guard let pos = attribute(element, "AXPosition"), let size = attribute(element, "AXSize"),
          CFGetTypeID(pos) == AXValueGetTypeID(), CFGetTypeID(size) == AXValueGetTypeID() else { return nil }
    var p = CGPoint.zero, s = CGSize.zero
    guard AXValueGetValue(unsafeBitCast(pos, to: AXValue.self), .cgPoint, &p),
          AXValueGetValue(unsafeBitCast(size, to: AXValue.self), .cgSize, &s) else { return nil }
    return [p.x, p.y, s.width, s.height]
}

if CommandLine.arguments.count == 5 && CommandLine.arguments[1] == "sheet-action" {
    guard let pid = Int32(CommandLine.arguments[2]), pid > 0,
          let expected = UInt32(CommandLine.arguments[3]), expected > 0,
          let bytes = CommandLine.arguments[4].data(using: .utf8),
          let request = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any],
          let frame = request["frame"] as? [Double], frame.count == 4,
          let sheetFrame = request["sheet_frame"] as? [Double], sheetFrame.count == 4,
          let role = request["role"] as? String, let action = request["action"] as? String,
          AXIsProcessTrusted() else { refuse("sheet_action_invalid_request") }
    let app = AXUIElementCreateApplication(pid)
    guard let sheet = elementAttribute(app, "AXFocusedWindow"), windowID(sheet) == expected,
          attribute(sheet, "AXRole") as? String == "AXSheet", axFrame(sheet) == sheetFrame
    else { refuse("sheet_action_window_changed") }
    func hasLabel(_ element: AXUIElement, _ label: String) -> Bool {
        if ["AXTitle", "AXDescription", "AXValue"].contains(where: { attribute(element, $0) as? String == label }) { return true }
        var pending = attribute(element, "AXChildren") as? [AXUIElement] ?? [], seen = Set<AXUIElement>()
        while let child = pending.popLast() {
            if !seen.insert(child).inserted { continue }
            if attribute(child, "AXRole") as? String == "AXStaticText",
               attribute(child, "AXValue") as? String == label { return true }
            pending.append(contentsOf: attribute(child, "AXChildren") as? [AXUIElement] ?? [])
        }
        return false
    }
    func matches(_ element: AXUIElement) -> Bool {
        guard attribute(element, "AXRole") as? String == role, axFrame(element) == frame else { return false }
        if attribute(element, "AXEnabled") as? Bool == false { return false }
        if let label = request["label"] as? String, !hasLabel(element, label) { return false }
        if let value = request["old_value"] as? String,
           attribute(element, "AXValue") as? String != value { return false }
        if let identifier = request["identifier"] as? String,
           attribute(element, "AXIdentifier") as? String != identifier { return false }
        return true
    }
    var pending = [sheet], seen = Set<AXUIElement>(), candidates = [AXUIElement]()
    while let element = pending.popLast() {
        if !seen.insert(element).inserted { continue }
        if matches(element) { candidates.append(element) }
        var children: CFTypeRef?
        let error = AXUIElementCopyAttributeValue(element, "AXChildren" as CFString, &children)
        guard error == .success || error == .attributeUnsupported || error == .noValue
        else { refuse("sheet_action_tree_unavailable") }
        pending.append(contentsOf: children as? [AXUIElement] ?? [])
    }
    guard candidates.count == 1 else { refuse("sheet_action_target_not_unique") }
    let target = candidates[0]
    if action == "set_value" {
        var writable: DarwinBoolean = false
        guard role == "AXTextField", request["value"] is String,
              AXUIElementIsAttributeSettable(target, "AXValue" as CFString, &writable) == .success,
              writable.boolValue else { refuse("sheet_value_not_writable") }
    } else {
        var actions: CFArray?
        guard ["AXPress", "AXOpen", "AXPick", "AXShowMenu", "AXConfirm", "AXCancel"].contains(action),
              AXUIElementCopyActionNames(target, &actions) == .success,
              (actions as? [String] ?? []).contains(action) else { refuse("sheet_action_not_advertised") }
    }
    guard elementAttribute(app, "AXFocusedWindow").map({ CFEqual($0, sheet) }) == true,
          windowID(sheet) == expected, axFrame(sheet) == sheetFrame, matches(target)
    else { refuse("sheet_action_target_changed") }
    let front = NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0
    let pointer = CGEvent(source: nil)?.location ?? .zero
    // AX errors can follow successful application actions. Never authorize replay.
    let result = action == "set_value"
        ? AXUIElementSetAttributeValue(target, "AXValue" as CFString, request["value"] as! CFString)
        : AXUIElementPerformAction(target, action as CFString)
    let after = CGEvent(source: nil)?.location ?? .zero
    finish(["effect": "unverifiable", "code": "sheet_action_invoked", "ax_result": result.rawValue,
            "action_posted": true, "requested_pid": pid, "requested_window_id": expected,
            "resolved_pid": actualPID(target) ?? pid, "receiver_window_id": windowID(target) ?? expected,
            "frontmost_before": front, "frontmost_after": NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0,
            "pointer_before": [pointer.x, pointer.y], "pointer_after": [after.x, after.y]])
}

// Resolve lease eligibility through the logical host. The remote panel service
// need not expose AXWindows or AXFocusedWindow of its own.
func hasSystemPanelReceiver(_ pid: pid_t, host hostPID: pid_t, window expected: UInt32) -> Bool {
    let app = AXUIElementCreateApplication(hostPID)
    guard var panel = elementAttribute(app, "AXFocusedWindow") else { return false }
    var ancestors = Set<AXUIElement>()
    var matchedWindow = windowID(panel) == expected
    while let parent = elementAttribute(panel, "AXParent") {
        if attribute(parent, "AXRole") as? String == "AXApplication" { break }
        guard ancestors.insert(parent).inserted else { return false }
        panel = parent
        matchedWindow = matchedWindow || windowID(parent) == expected
    }
    guard matchedWindow,
          attribute(panel, "AXIdentifier") as? String == "open-panel",
          attribute(panel, "AXSubrole") as? String == "AXDialog" else { return false }
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
    let point = CGPoint(x: origin.x + size.width / 2, y: origin.y + size.height / 2)
    var events: [CGEvent] = []
    for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
        guard let event = NSEvent.mouseEvent(with: type, location: point,
            modifierFlags: [], timestamp: 0, windowNumber: Int(id), context: nil,
            eventNumber: 1, clickCount: 1, pressure: 1)?.cgEvent else { return false }
        event.location = point
        event.setIntegerValueField(.mouseEventSubtype, value: 3)
        for number: UInt32 in [91, 92] {
            event.setIntegerValueField(CGEventField(rawValue: number)!, value: Int64(id))
        }
        setWindowLocation(event, CGPoint(x: size.width / 2, y: size.height / 2))
        events.append(event)
    }
    events.append(focus)
    preparationStarted = true
    for event in events { event.postToPid(pid); usleep(50000) }
    let app = AXUIElementCreateApplication(pid)
    return elementAttribute(app, "AXFocusedWindow").map { CFEqual($0, window) } == true
}

// Indexed field clicks request semantic focus. Coordinate clicks retain the
// Driver path and its caret-placement semantics.
if CommandLine.arguments.count == 8 && CommandLine.arguments[1] == "focus-field" {
    guard let pid = Int32(CommandLine.arguments[2]), pid > 0,
          let expected = UInt32(CommandLine.arguments[3]),
          let x = Double(CommandLine.arguments[4]), let y = Double(CommandLine.arguments[5]),
          let width = Double(CommandLine.arguments[6]), let height = Double(CommandLine.arguments[7]),
          [x, y, width, height].allSatisfy({ $0.isFinite }), width > 0, height > 0,
          AXIsProcessTrusted() else { refuse("field_focus_unavailable") }
    let app = AXUIElementCreateApplication(pid)
    guard let sheet = elementAttribute(app, "AXFocusedWindow"), windowID(sheet) == expected,
          attribute(sheet, "AXRole") as? String == "AXSheet"
    else { refuse("field_sheet_mismatch") }
    var host = sheet
    var ancestors = Set<AXUIElement>()
    while let parent = elementAttribute(host, "AXParent") {
        if attribute(parent, "AXRole") as? String == "AXApplication" { break }
        guard ancestors.insert(parent).inserted else { refuse("ax_parent_cycle") }
        host = parent
    }
    guard attribute(host, "AXIdentifier") as? String == "open-panel"
    else { refuse("not_open_panel") }
    var pending = [sheet], seen = Set<AXUIElement>(), matches = [AXUIElement]()
    while let element = pending.popLast() {
        if !seen.insert(element).inserted { continue }
        if attribute(element, "AXRole") as? String == "AXTextField",
           let p = attribute(element, "AXPosition"), CFGetTypeID(p) == AXValueGetTypeID(),
           let s = attribute(element, "AXSize"), CFGetTypeID(s) == AXValueGetTypeID() {
            var point = CGPoint.zero, size = CGSize.zero
            if AXValueGetValue(unsafeBitCast(p, to: AXValue.self), .cgPoint, &point),
               AXValueGetValue(unsafeBitCast(s, to: AXValue.self), .cgSize, &size),
               point.x == x, point.y == y, size.width == width, size.height == height {
                matches.append(element)
            }
        }
        if let children = attribute(element, "AXChildren") as? [AXUIElement] { pending.append(contentsOf: children) }
    }
    guard matches.count == 1 else { refuse("field_target_ambiguous") }
    let field = matches[0]
    var settable: DarwinBoolean = false
    guard AXUIElementIsAttributeSettable(field, "AXFocused" as CFString, &settable) == .success,
          settable.boolValue, let latest = elementAttribute(app, "AXFocusedWindow"), windowID(latest) == expected
    else { refuse("field_focus_unavailable") }
    let front = NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0
    let result = AXUIElementSetAttributeValue(field, "AXFocused" as CFString, kCFBooleanTrue)
    let verified = result == .success && (attribute(field, "AXFocused") as? NSNumber)?.boolValue == true
        && elementAttribute(app, "AXFocusedUIElement").map { CFEqual($0, field) } == true
    finish(["effect": verified ? "confirmed" : "unverifiable", "code": "panel_field_focus",
            "ax_result": result.rawValue, "requested_pid": pid, "requested_window_id": expected,
            "frontmost_before": front,
            "frontmost_after": NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0])
}

guard CommandLine.arguments.count == 4,
      let pid = Int32(CommandLine.arguments[1]), pid > 0,
      let expectedWindow = UInt32(CommandLine.arguments[2]),
      let key = CGKeyCode(CommandLine.arguments[3]), [5, 31, 36, 53].contains(key)
else { refuse("invalid_arguments") }
guard AXIsProcessTrusted(), CGPreflightPostEventAccess() else { refuse("native_helper_permission_unavailable") }
let app = AXUIElementCreateApplication(pid)
guard let focused = elementAttribute(app, "AXFocusedWindow"), let focusedID = windowID(focused)
else { refuse("focused_window_unavailable") }
// Cmd+O uses the app's declared, enabled menu equivalent, without focus synthesis.
if key == 31 {
    guard focusedID == expectedWindow else { refuse("focused_window_mismatch") }
    guard let menu = elementAttribute(app, "AXMenuBar") else { refuse("menu_unavailable") }
    var pending = [menu]
    var seen = Set<AXUIElement>()
    var matches = [AXUIElement]()
    while let element = pending.popLast() {
        if !seen.insert(element).inserted { continue }
        if attribute(element, "AXRole") as? String == "AXMenuItem",
           (attribute(element, "AXMenuItemCmdChar") as? String)?.uppercased() == "O",
           (attribute(element, "AXMenuItemCmdModifiers") as? NSNumber)?.intValue == 0,
           (attribute(element, "AXEnabled") as? NSNumber)?.boolValue == true {
            matches.append(element)
        }
        if let children = attribute(element, "AXChildren") as? [AXUIElement] { pending.append(contentsOf: children) }
    }
    guard matches.count == 1 else {
        refuse("no_unique_open_menu_equivalent")
    }
    var owner: pid_t = 0
    guard AXUIElementGetPid(matches[0], &owner) == .success, owner == pid,
          let latest = elementAttribute(app, "AXFocusedWindow"),
          windowID(latest) == expectedWindow, CFEqual(latest, focused)
    else { refuse("focused_window_changed") }
    let front = NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0
    let result = AXUIElementPerformAction(matches[0], "AXPress" as CFString)
    // Once invoked, an AX error is not proof that no action occurred.
    finish(["effect": "unverifiable", "code": "menu_equivalent_invoked",
            "menu_key": "o",
            "ax_result": result.rawValue, "requested_pid": pid,
            "requested_window_id": expectedWindow, "frontmost_before": front,
            "frontmost_after": NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0])
}
var host = focused
var ancestors = Set<AXUIElement>()
var windowIDs: Set<UInt32> = [focusedID]
while let parent = elementAttribute(host, "AXParent") {
    if attribute(parent, "AXRole") as? String == "AXApplication" { break }
    guard ancestors.insert(parent).inserted else { refuse("ax_parent_cycle") }
    host = parent
    if let id = windowID(parent) { windowIDs.insert(id) }
}
guard windowIDs.contains(expectedWindow) else { refuse("focused_window_mismatch") }
guard attribute(host, "AXIdentifier") as? String == "open-panel",
      attribute(host, "AXSubrole") as? String == "AXDialog"
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
    guard key == 36, let data = encoded.data(using: .utf8),
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
// Resolve the receiving process from the focused element's ancestry. A remote
// sibling elsewhere in the document must not become the keyboard recipient.
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
// Codex falls back to the application's own PID when its focused element has
// no out-of-process container. Preserve the exact focused window in this case.
let (target, remoteWindow) = remote ?? (pid, focusedID)
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
    guard key == 5,
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
    let point = CGPoint(x: origin.x + extent.width / 2, y: origin.y + extent.height / 2)
    // The host modal window needs focus, not application activation.
    for (index, type) in [NSEvent.EventType.leftMouseDown, .leftMouseUp].enumerated() {
        guard let event = NSEvent.mouseEvent(with: type, location: point, modifierFlags: [],
            timestamp: 0, windowNumber: Int(hostID), context: nil, eventNumber: index + 1,
            clickCount: 1, pressure: 1)?.cgEvent else { refuse("event_allocation_failed") }
        event.flags = []
        event.location = point
        event.setIntegerValueField(.mouseEventButtonNumber, value: 0)
        event.setIntegerValueField(.mouseEventSubtype, value: 3)
        for field: UInt32 in [91, 92] {
            event.setIntegerValueField(CGEventField(rawValue: field)!, value: Int64(hostID))
        }
        setWindowLocation(event, CGPoint(x: extent.width / 2, y: extent.height / 2))
        preparation.append(event)
    }
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
    let down = CGEvent(keyboardEventSource: CGEventSource(stateID: .hidSystemState), virtualKey: key, keyDown: true),
    let up = CGEvent(keyboardEventSource: CGEventSource(stateID: .hidSystemState), virtualKey: key, keyDown: false)
else { refuse("event_allocation_failed") }
down.flags = key == 5 ? [.maskCommand, .maskShift] : []
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
