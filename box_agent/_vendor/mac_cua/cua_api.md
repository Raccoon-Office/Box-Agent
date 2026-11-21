# Mac-CUA

Call `mcp__cua_repl.js({code, title?, timeout_ms?})` and
`mcp__cua_repl.js_reset()` directly as model tools. CUA is unavailable inside
`functions.exec`; that tool remains available for shell/file operations.
Returned text/images reach the model directly; no outer
`text()`/`image()` forwarding wrapper is needed. JS variables persist inside `js`.

First call, also after reset, must contain only one initialization:

```javascript
let TextEdit = await cua.getApp("TextEdit");
```

For tasks with browser support, initialize with only
`let browser = await cua.getBrowser("chrome");`, or use `await cua.getState()`
to discover available apps and browsers. An empty browser list means browser
support is not configured for this task. Discovery does not connect a browser.
The first initialization in each JS session emits this manual and state; read both before acting.
Use `await cua.rewriteDocumentation();` to display it again without reading or changing app state,
including when the conversation no longer contains the manual but JS bindings still exist.
`getApp` activates a windowless app and asks it to perform its standard macOS reopen behavior.
Apps that own no ordinary window may still expose their application menu. In that state the AX
output explicitly reports `Window: none`, lists the menu separately, and has no screenshot.
Reacquire the App reference after a process restart.

## CUA API

App and Tab share `pressKey(key: string)`, accepting exactly one argument.
Use a single key or a `+`-joined shortcut, e.g. `Return`, `shift+Left`,
or `super+s`. Modifiers: `super` (Command), `ctrl`, `alt`, `shift`.
Use `Backspace` for backward deletion, `ForwardDelete` for forward deletion,
and `plus` for Shift+Equals. Letter key names are case-insensitive; only explicit
`shift` changes letter case (`A` means `a`, `shift+a` means `A`). Shifted symbols
use US key positions. Use `typeText` for exact text. Unknown or unsupported keys
are rejected before dispatch.

Named keys accept driver aliases such as `PgDn`, `Bksp`, `Del` (forward delete),
and `KP_Enter`. Leading and trailing whitespace is ignored in key names;
multi-character names also ignore spaces, underscores and hyphens, so
`Page_Down` and `page-down` mean `PageDown`. A literal space or `-` remains a key.

All methods are asynchronous: use `await`. A return value is data available to
JavaScript; automatic output is text or images shown to the model. They are separate.
`void` means the method returns no data (`undefined` after `await`).
A `?` marks an optional parameter or property; options objects may be omitted.

```typescript
type Vec2 = [x: number, y: number];
type ObservationOptions = { emit?: boolean };
type StateOptions = ObservationOptions & { disableDiffing?: boolean };
type AppObservation = { state: string; screenshot?: Uint8Array };
```

| Method | Return value after `await` | Behavior |
|---|---|---|
| `cua.axElementId(state: string, description: string)` | `number` | Returns the unique element ID whose rendered AX description exactly matches, ignoring tree indentation and a diff `+`/`~` prefix. Throws when none or multiple match. |
| `cua.getState(options?: ObservationOptions)` | `{apps, browsers}` | Discovers apps and available browsers; emits the inventory by default. |
| `cua.listApps(options?: ObservationOptions)` | App information array | Emits the app list by default. |
| `cua.getApp(nameOrBundleIdOrPath: string)` | `App` reference | Binds to or starts the app, activates a windowless app and asks it to perform its standard reopen behavior, then emits its initial AX state. Accepts an app name, bundle ID, or installed app path. |
| `cua.getBrowser(browser: string)` | `Browser` reference | Connects to the configured browser (`"chrome"`) and emits its tab list. |
| `cua.rewriteDocumentation()` | `void` | Emits this manual again; does not read or change app state. |

Use recognizable variable names and reuse one App reference per application,
including across its windows.

## Browser and Tab API

Browser operations share this `cua_repl` session. Use one Agent; do not spawn browser workers.
An existing task Chrome and dedicated profile are prepared before you start.
A connection failure is an error: do not launch another browser, change profile,
fall back to native clicking, or replay a possibly delivered browser action.

Read the actual `targetId` list. Then select exactly that ID in another call:

```javascript
let tab = await browser.getTab("ACTUAL_TARGET_ID");
```

Never select by URL/title guesses. Call `tab.getState()` or
`tab.getStateAndScreenshot()` to obtain numbered DOM before using element numbers.

| Browser method | Return value after `await` | Behavior |
|---|---|---|
| `listTabs(options?: ObservationOptions)` | Tab information array | Lists tabs, including their `targetId`; emits by default. |
| `getTab(targetId: string)` | `Tab` reference | Selects an existing tab without bringing the window to the front; emits page metadata only. |
| `newTab(url?: string)` | `Tab` reference | Creates a tab and emits page metadata only; the default URL is `about:blank`. |

`Tab.getState()` returns an object containing page metadata and `dom`, not an App
AX string. `getStateAndScreenshot()` returns `{state, screenshot}`, where `state`
is that object and `screenshot` is PNG bytes or `undefined` when unavailable.
Tab actions return `void`, except `evaluate()`, which returns its JSON value.

| Tab method | Contract |
|---|---|
| `getState({emit?, disableDiffing?})` | Page metadata plus compact numbered DOM enriched with accessibility information. Consecutive observations emit full/diff/unchanged. |
| `getScreenshot({emit?})` | PNG bytes (`Uint8Array`), emitted as an image by default. |
| `getStateAndScreenshot({emit?, disableDiffing?})` | `{state, screenshot}`; same DOM baseline as getState, plus `pixels.perCssX/perCssY` and a full PNG. With a page dialog, emits unavailable DOM and no image. |
| `goto(url)`, `activate()` | Navigate, or explicitly focus this exact tab. Observe to verify. |
| `fill(refOrSelector, text)` | Positive integer DOM reference, or CSS matching exactly one top-level element; replaces editable contents. Numbered fill supports open Shadow DOM. |
| `typeText(text)` | Inserts into the currently focused page element. |
| `pressKey(key: string)` | Uses the shared shortcut syntax above. `fn`, function keys F1–F12, Caps Lock and standalone modifier keys are unsupported. |
| `click(refOrPoint, {button?, clickCount?})` | Positive integer DOM reference, or viewport CSS `[x,y]`. Coordinates require a fresh screenshot. Button left/right/middle; clickCount 1/2/3. |
| `scroll({x,y,deltaX?,deltaY?})` | CSS viewport point and wheel deltas; positive deltaY scrolls down. Requires a fresh screenshot. |
| `waitForLoad({timeoutMs?})` | Wait for load, bounded by this JS cell's remaining time. |
| `waitForElement(selector, {timeoutMs?, visible?})` | Top-level CSS; default timeout 10000 ms, visible=false. |
| `evaluate(expression)` | String of page JavaScript; returns and emits its JSON value. This runs inside the page, not Node. |
| `uploadFile(selector, absoluteVmPath)` | Exactly one top-level file input and an existing VM file. |
| `handleDialog({accept, promptText?})` | Explicitly accept/dismiss a page alert, confirm or prompt; inspect `getState()` first. |
| `close()` | Explicitly closes this tab; all its references then expire. |

The code in `cua_repl.js` runs in persistent Node; `tab.evaluate("...")` runs the
string inside Chrome. Page code cannot use `cua` or Node variables. DOM scope is the
rendered main document (including offscreen content) and open Shadow DOM; iframe
contents are excluded, with the embedded-page boundary shown. Closed Shadow DOM is
not traversed; identifiable boundaries are marked. There is no Playwright locator.

Use only references from this tab's observations. Refs are positive integers, not
App AX indexes or numeric strings. They persist for the same live DOM node, including
across tab switches; replaced nodes, reload/navigation, closed tabs and reset expire
them. Stale refs fail without retargeting. Only click/fill accept numbered refs.
After input, observe the result; stable identity does not imply unchanged state.

Observations return full/diff/unchanged DOM. In a diff,
`+` adds, `~` replaces the whole line, and `- [ref]` removes a row. Upserts include
`parent` and `after` (null/None means root/first sibling), including node moves.
Unchanged means only the displayed DOM is unchanged.
`disableDiffing:true` forces full. Unavailable DOM
is not an empty page; inspect reason/dialog and observe again after resolving it.

If an observation contains `[12] textbox "Message"`, use that observed reference:

```javascript
await tab.fill(12, "Hello");
await tab.getStateAndScreenshot();
```

Click coordinates use CSS pixels relative to the viewport, not screen pixels or
full-page scroll offsets. Divide screenshot pixels by `pixels.perCssX/perCssY`.
Get a new screenshot after any input, navigation, scroll, page JS or native App
operation. Do not reuse coordinates across those changes. Screenshots are not a
promise that the page cannot change asynchronously; observe again when uncertain.

Native macOS dialogs and Chrome toolbar controls use an App reference, for example
`let GoogleChrome = await cua.getApp("Google Chrome");`. This is an explicit choice
for a native surface, not an automatic recovery path for failed browser input.
Refresh observations when moving between App and Tab operations. While a page alert
is open, use `handleDialog`; screenshots and page JS are blocked.
If a close shortcut returns `BeforeUnloadPending`, inspect `getState()` and
resolve the confirmation with `handleDialog({accept})` in a new JS cell.
Downloads go to the task's declared download directory.

## App API

| Method | Return value after `await` | Behavior |
|---|---|---|
| `getAXState(options?: StateOptions)` | `string` | Current interaction AX tree, including the active menu or verified attached surface; emits by default. Subsequent observations prefer diffs; `disableDiffing:true` requests full state. |
| `getScreenshot(options?: ObservationOptions)` | `Uint8Array` | Emits the screenshot by default; errors when unavailable. |
| `getAXStateAndScreenshot(options?: StateOptions)` | `AppObservation` | Emits AX text and then the image. If no screenshot is available, returns `{state}` and reports the missing image. |
| `click(indexOrXY, {mouseButton?, clickCount?})` | `void` | Element ID (`number`) or `Vec2`. Buttons: left/right/middle or l/r/m. Indexed multiple clicks support only a left double click. |
| `drag(fromXY: Vec2, toXY: Vec2)` | `void` | Two coordinate pairs from the same current window screenshot. Foreground delivery; never automatically replayed. Attached-surface drag is unsupported. |
| `pressKey(key: string)` | `void` | Uses the shared shortcut syntax above; also supports `fn`. Unsupported driver key names fail. |
| `typeText(text: string)` | `void` | Inserts text into the focused control. Driver may use AX insertion or keyboard events; do not assume every character generates a key event. |
| `setValue(elementIndex: number, value: string)` | `void` | Replaces the complete value of an editable AX control that supports setting its value. Use a current element ID and observe afterward; a displayed value alone does not prove writability. |
| `scroll(indexOrXY, direction, pages=1)` | `void` | Element ID (`number`) or `Vec2`; direction up/down/left/right or u/d/l/r. Pages: integer 1–50 driver steps, not exact viewport heights. |
| `paste(text: string, {format?})` | `void` | Pastes plain text into the focused control; takes no element index. Omit `format` or use `text`; rich formats are unsupported. Leaves text on the shared clipboard. Observe to verify insertion. After errors the clipboard may have changed; observe before continuing and do not replay. |

### Observations and targets

Await actions and observe their effects; a successful call does not prove task completion.
Use `getAXStateAndScreenshot()` when both elements and coordinates are needed.

- Element IDs belong to the latest AX snapshot. Refresh before indexed input after an action, switching apps, or reading AX through another App reference.
- AX tree lines are indented. To select an element from a saved observation, use `cua.axElementId` instead of assuming the ID starts in column one:

```javascript
const state = await TextEdit.getAXState({disableDiffing:true});
const windowMenuId = cua.axElementId(state, 'menu bar item "Window"');
await TextEdit.click(windowMenuId);
```

- Prefer default diffing: `~` changes a line, `+` adds one, and removed IDs are listed. A no-change response also refreshes targets.
- Keyboard calls can run consecutively. For rejected or uncertain input, follow Output and recovery below.
- An AXOpen warning means the action may already have occurred. Observe before retrying; never answer the warning by immediately repeating the click or switching to coordinates.
- Windows are selected automatically; switch documents through app menus or shortcuts. The system Apple menu is omitted from AX.
- A frontmost app with no ordinary window can expose a standalone application menu. Its observation says `Window: none`; menu items remain actionable, while screenshots, coordinates, and non-menu input remain unavailable until a window appears.
- Application menu clicks navigate a logical menu context. After clicking a menu or submenu, call `getAXState()` to read its options; no on-screen dropdown is opened. Only a final command is sent to the driver. Press `Escape` to leave the context. Menu screenshots remain actual window images, without these logical options. Missing dynamic submenu options are unsupported; do not guess their IDs.

### Screenshots and coordinates

Each emitted image is preceded by `Image dimensions: width=W px, height=H px.`
These are the attached image's pixel dimensions, including explicit
`nodeRepl.emitImage(...)` output. They do not change the coordinate rules below
or the browser's CSS pixel mapping. `emit:false` emits neither image nor size.
An unreadable image header is reported as `Image dimensions unavailable`;
do not guess its size. A cropped or resized image does not establish a new input
coordinate frame.

For ordinary windows, use screenshot-local coordinates. For a verified attached
surface, clicks use the root window's original local coordinates: `[0,0]` is its
top-left, with the range given by the reported bounds.
An application menu is the exception: its screenshot and coordinates use the menu
surface's own bounds.
Do not interpret screenshot pixels as root bounds units. Clicking
outside a popover but inside its root window is allowed. Drag and coordinate
scrolling on attached surfaces are unsupported.

Use a current screenshot; refresh after keyboard input, an AX-only observation,
or a change that could move the target. A screenshot alone does not refresh element
IDs. After an action creates a window, observe it before another action or browser
call. If the observation uses the parent window's AX and screenshot for an attached
surface, follow the reported coordinate bounds.
Captures may include background or transparent areas; if the mapping is
unclear, use AX or keyboard actions rather than guessing scaling or offsets.

## Output and recovery

Observation methods emit automatically. Do not wrap their default output in
`nodeRepl.write` or `nodeRepl.emitImage`, which would duplicate it.
With `emit:false`, observation data remains in JS until explicitly emitted.
First-use documentation is still displayed, even with `emit:false`:

```javascript
const observation = await TextEdit.getAXStateAndScreenshot({emit:false});
nodeRepl.write(observation.state);
if (observation.screenshot) await nodeRepl.emitImage(observation.screenshot);
```

Plain expression values are not automatically printed. `nodeRepl.write` displays
text/data; `nodeRepl.emitImage` accepts bytes or `{bytes, mimeType}`. Do not stringify
images. Combined observations emit state before the image; actions emit no new observation.

In `await action(); await observe();`, an action error prevents the observation.
A failed JS cell may have partially executed.
Correct rejected calls without replaying earlier actions.
If execution is uncertain, observe before retrying.

Default `js` timeout is 60 seconds, maximum 120 seconds. Prefer omitting `timeout_ms`;
when setting it explicitly, budget at least 30 seconds for GUI actions and 60 seconds
for combined actions plus observation. This is a total cell budget, including internal
AX, screenshot, and focus checks, not a per-line or per-action timeout. An ordinary JS exception
preserves bindings; timeout/reset discards bindings and references but does not
close apps or roll back changes. After a disconnection, call `js_reset`.
After timeout/reset, initialize again to receive the manual, rediscover browser tabs,
and observe before continuing.
Do not leave background promises or timers
issuing GUI actions: calls from an ended JS cell are rejected.

Each task uses a single agent. Do not issue concurrent GUI actions.
This documentation does not itself authorize unrelated shell/file operations.
