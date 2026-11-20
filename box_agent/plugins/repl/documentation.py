"""Capability-specific manuals; the upstream harness manual is not exposed."""
from pathlib import Path


def manual(capability: str, mode: str | None) -> str:
    common = ("JS variables persist in this capability/mode. Reassign existing names instead of declaring them again. Await every action. "
              "Use top-level await, never top-level return. Await async IIFEs themselves. "
              "Use nodeRepl.write(value) and await nodeRepl.emitImage(bytes) for output. "
              "An action error skips later lines in the cell. After errors, timeouts or cancellation, observe in a separate cell before deciding whether to retry. "
              "Reset clears JS and refs, not application changes. "
              "Use cua.rewriteDocumentation() to read this manual again.\n")
    if capability == "cua" or mode == "managed":
        from box_agent._vendor import mac_cua
        source = Path(mac_cua.__file__).with_name("cua_api.md").read_text()
        # Include the pinned parser contract without exposing upstream browser-harness APIs.
        shortcuts = source.split("## CUA API\n\n", 1)[1].split("All methods are asynchronous:", 1)[0]
        if mode == "managed":
            shortcuts = shortcuts.replace("Use `typeText` for exact text.", "Use `tab.fill(ref, text)` for exact text.")
        common += shortcuts
        common += ("Do not pass an options object or a numeric modifiers argument. "
                   "InvalidArguments from pressKey means that key was never sent; "
                   "earlier actions in the same cell can already have taken effect.\n")
    if capability == "cua":
        # Import the App contract and its subsections. Recovery belongs to the host.
        app_section = source[source.index("## App API\n"):].split("\n## ", 1)[0]
        # Adapt the upstream example to the persistent binding introduced below.
        app_section = app_section.replace("TextEdit.", "app.").replace(
            "const state = await app.getAXState({disableDiffing:true});\n"
            "const windowMenuId = cua.axElementId(state, 'menu bar item \"Window\"');\n"
            "await app.click(windowMenuId);",
            "await app.click(cua.axElementId(\n"
            "  await app.getAXState({disableDiffing:true}), 'menu bar item \"Window\"'\n));",
        )
        return common + r"""
## Native App workflow

CUA uses cua_repl_js and cua_repl_reset. For web tasks use browser-use.
To call the bound App in a later cell, send this object to call_tool with the current reference:

```json
{"tool_ref":"<returned tool_ref>","arguments_json":"{\"code\":\"await app.getAXState();\",\"title\":\"Read the current window\"}"}
```

An unexpected outer fields error or Invalid arguments_json means JS did not run.
Serialize code and optional title together inside arguments_json and resend with the same valid reference.
If identifying the application first, run only `await cua.listApps()` in that
cell. Initialize the App in a separate cell using the observed name or bundle ID.

```js
var targetApp = 'TextEdit'; // Use the name or bundle ID requested for this task.
var app = await cua.getApp(targetApp);
```

```js
await app.listWindows();
await app.getAXState({disableDiffing: true});
```

`app.listWindows({emit?})` returns window records with `id`, `title`, `bounds`,
`layer`, `isActive` and, when available, `isFocused`. It observes windows without
switching them. Window IDs cannot be used as AX element IDs.

`listWindows()` is inventory only. On WindowChanged or AttachmentUnverified during
input, keep app. In the next cell only run `await app.getAXStateAndScreenshot({disableDiffing: true})`.
Check the document, attached panel and focus. If the intended control is verified,
focus it if needed and read back before continuing. In Finder's Go to Folder panel, use
a verified writable path field and read back its value. If only a floating image
is visible and AX still exposes no verified input target, stop and report the panel.
Switching to paste, Escape or reset does not establish focus.

- A failed cell can reserve `let`/`const` names even after the failing statement.
  Do not replay its declarations. Assign existing variables, or prepare repeatable
  temporary data with `var` in a separate cell before GUI actions.
- `WindowChanged` during getApp: in a new cell run only `app = await cua.getApp(targetApp)` with the same target. Verify the returned window title or menu before sending input.
- For text entry, check the intended document and focused editor in AX. If necessary, click its fresh AX ID and observe focus. Once focused, use `await app.typeText(text)` or `await app.paste(text)`, then read back the contents with `await app.getAXState({disableDiffing: true})`. Use `app.setValue(id, text)` for a verified writable AX field.
- `StaleElement` or target missing from an AX diff: read full AX with
  `await app.getAXState({disableDiffing: true})`. Select an ID from that result.
- On TargetUnverified during an AX click, this input was refused. Read full AX and
  verify the window and control. If a refreshed menu item is still refused, stop
  clicking it. Only in the same open menu with an observable highlight, navigate
  with `await app.pressKey("Down")` or Up and read back each change. Once the
  desired item is highlighted, press Return and check the parent dialog's value.
- For Finder inline rename, keep focus in the observed editor for the selected
  file. With the filename stem selected, use `await app.typeText(newStem)`.
  Read full AX to verify the edited name and extension before Return, then
  check the file listing. If the editor disappears or its owner changes, stop
  input and observe Finder again. Clicking the main window can end editing.
- Use coordinates only when you receive the current image. Follow the coordinate
  bounds below for attached surfaces; use AX when the image is unavailable.
  If a menu image includes the parent dialog or blank space and the item cannot
  be mapped to the menu bounds, stop coordinate input.
- For a coordinate click after Escape, end the first cell with
  `await app.pressKey('Escape'); await app.getScreenshot();`. Read the new image,
  then choose the point and click it in the next cell. For example, re-locate the
  slide thumbnail and verify the resulting page number after clicking.
- For scrolling, use `await app.scroll(axId, 'up', 4)` with the content area's
  latest numeric AX ID. In an ordinary window, a point has type
  `Vec2 = [x: number, y: number]`: use `await app.scroll([x, y], 'up', 4)`
  with the two numbers observed on the current screenshot.

""" + app_section + """

## Output and recovery

Observations emit AX text and available images automatically. With
`emit:false`, use `nodeRepl.write` for saved text and `nodeRepl.emitImage`
for saved image bytes. Use `await cua.rewriteDocumentation()` if only the
manual is missing and the session is still active.

- `Coordinates require a current screenshot of this window` means the click
  was blocked before sending input. Any screenshot after the rejected click
  in that cell was skipped. Run only `await app.getScreenshot()` or
  `await app.getAXStateAndScreenshot()` in the next cell, then pick a point
  from the new image. An intervening AX-only read, input or window switch requires a fresh image.
- If a single AX click reports `AXPress` returned `-25204`, its effect is
  unknown. In the next cell only observe: use
  `await app.getAXState({disableDiffing: true})` or
  `await app.getAXStateAndScreenshot()`. For example, if Excel's Sort dialog
  is open, continue with its fresh controls. Consider clicking again only
  when the observation establishes the action did not take effect. If the
  evidence is ambiguous, inspect further.
- An ordinary JS error preserves bindings. If a click returns an AXOpen
  warning, read the current AX in a separate cell to see whether it took
  effect before considering another click.
- The timeout covers the whole cell (default 60 seconds, maximum 120).
  If the error says `Session reset`, initialize the intended App in a fresh
  cell and read its initial AX to see which earlier actions took effect.
- For explicit reset, call `cua_repl_reset` with target input {}. Use the current
  tool_ref with call_tool; discover it first if missing or expired. On an interface
  that only offers names, use the offered name. Rebind app and inspect the
  current window or menu. Open documents and prior edits survive reset.
"""
    managed = r"""
## Managed browser workflow

Inspect the supported methods with `nodeRepl.write((await cua.getState({emit: false})).browsers[0].tabMethods)`.
This is the managed API inventory; the connection is checked on dispatch. Both
`getTab('current')` and `getTab(id)` return a Tab. Read rendered snapshot text
from `(await tab.getState({emit: false})).dom.text`. For title and URL inspect
the matching row from `await browser.listTabs()`.

Starting with an existing `tab`, assign the result of opening the next page:
`tab = await browser.newTab(url)`. Use that returned handle for the new page.
Opening or closing a tab expires previously listed managed IDs and handles.
A click or selection can update the same tab's URL. Continue with a new snapshot.
An independent redirect, new tab, or ambiguous navigation can change the inventory. On `StaleTab` or
`HandleScopeMismatch`, list again and bind the intended page by its fresh ID.
First inspect `await browser.listTabs()`. Compare the new URL, title and active
status with the intended page. Substitute its newly observed URL in this example:

```js
await (async () => {
  const rows = await browser.listTabs();
  const matches = rows.filter(row => row.url === 'https://example.com/');
  if (matches.length === 1) {
    tab = await browser.getTab(matches[0].id);
  } else {
    nodeRepl.write(rows);
  }
})();
```

Continue only after the binding displays the intended page. With zero or
multiple matches, inspect the inventory before choosing a target. A file
created on disk may still need to be opened in the browser. `getTab(id)`
selects the page and displays its snapshot. Use an element ref from that
snapshot. After an explicit `await tab.activate()`, use `await tab.getState()`
to obtain a fresh ref. After closing the page with `await tab.close()`,
relist before returning to an earlier page.

Use `await tab.back(); await tab.getState();` to return through this tab's
history. Read the snapshot before picking the next ref.

- `await tab.waitForText('Ready')` waits until matching text is visible.
  `await tab.waitForText('Loading', {state: 'hidden'})` waits until it is hidden
  or absent. The current cell's timeout applies. Read a new snapshot if acting next.
- `await tab.selectOption(ref, 'value')` picks an option in a native select;
  pass an array of strings for a multi-select. Use a fresh select ref.
- `await tab.click(ref, {button: 'right'})` uses the right mouse button.
  `doubleClick: true` requests a double click. `modifiers: ['Alt']`, for example,
  holds a modifier. Options allow left/right/middle and Alt/Control/ControlOrMeta/Meta/Shift.
- If an action reports a page dialog in `Modal state`, use the same tab in
  the next cell, including a current-tab handle when the host verifies the same
  page and dialog. On `DialogTabRequired`, list the tabs, identify the original
  page and bind its fresh ID. Binding displays the pending modal in place of the
  blocked snapshot. Verify the message, then handle the observed page dialog
  with `await tab.handleDialog({accept: false})` to dismiss it, or `{accept: true,
  promptText: 'text'}` for a prompt. Follow the user's intent before accepting;
  observe the page again afterwards. A file chooser uses uploadFile instead.

When human verification is visible, leave the page open and wait for the user. If switching from headless resets the session, reopen only the original navigation URL. Never replay the click, form submission or purchase that led to the challenge. After user confirmation, take a fresh snapshot in the same browser before continuing.

Use tab.hover(ref), tab.pressKey('super+a') (Command+A on a Mac), and tab.scroll({deltaY: 450}).
Tab rejects fn, F1–F12, Caps Lock and standalone modifier keys. Use tab.fill(ref, text)
for exact text in a managed input. For example, tab.pressKey('a', {modifiers: 4})
raises InvalidArguments before input; use tab.pressKey('super+a') to select all.
Use at least one nonzero wheel delta. Optional x and y together move the mouse
before scrolling, using observed page coordinates. waitForText accepts only the
state option; set a longer cell deadline with the tool's timeout_ms if needed.
Use tab.evaluate('() => document.title') to execute JS in the page and return its
result. Even read-only evaluate expires element refs. Run
`await tab.evaluate('() => document.title'); await tab.getState();` and select
the new ref for the next click. To read price lines, preserve both newline and
regex escapes across the REPL and page JavaScript parsers:

```js
await tab.evaluate(String.raw`() => {
  const lines = document.body.innerText.split('\n');
  return lines.filter(line => /^\d+\s*元$/.test(line.trim()));
}`);
```

Check the returned values before reporting prices. Use `tab.uploadFile(ref, 'relative/file.txt')` for an observed chooser
control. If the error says a file chooser is already open, call
`tab.uploadFile(null, path)` on the same tab before taking a new snapshot. Do not
click the chooser again. Supply a path or paths in the workspace selected by the user.
Use `tab.getScreenshot()` or `tab.getStateAndScreenshot()` for the visible viewport.
Both emit the image by default. Pass `{emit: false}` to use the returned
screenshot bytes without displaying them.
For BROWSER_VIEW_EMPTY check the intended task and host panel. Inspect layout with
`await tab.evaluate('() => [innerWidth, innerHeight]');`. For a zero viewport,
bring the view back into layout, verify positive dimensions and retry capture once.
With a positive viewport an empty surface, tile or output is a host capture
failure. Keep the page layout intact and report the capture error and only
AX-verifiable facts. For an offscreen target in a
nonzero viewport, scroll, observe and use the new ref before the next click.
""" if mode == "managed" else """
For active-tab work, bind `browser.getTab('current')`. Use `tab.getState()` for
a snapshot and `tab.fill(ref, text)` for an editable field. Use
`await tab.click(ref)` without click options in user mode. Rebind the active
tab and verify page identity after navigation or a focus change.
When asked to open a URL, use `await browser.openAndRead(url);` directly on the user browser.
It opens a new active tab and returns its Gateway extraction in one call. Use the returned
URL, title and body directly. This result does not assign `tab`. If the user also requested an authorized interaction, run only `tab = await browser.getTab('current');` in the next cell. Check the URL and title in that snapshot. If they match the intended page, use the target's fresh ref in the following cell; otherwise stop and ask. The compatibility method `tab.goto(url)` also opens
a new tab. On `url_mismatch`, report the redirect and ask before reading
the active page. For a current-page request compare the binding snapshot with the expected
URL and title first. If they differ, stop and ask before reading the body.

For the current page's article or body, use `await tab.readText();` in a new cell
after binding `tab = await browser.getTab('current');`. It takes no arguments
and displays and returns the Gateway extraction. Check its `url` and `title`
before using the content because the current handle follows the active tab.
`mode: 'full'` provides `content`. If the Gateway returns `mode: 'chunked'`,
only the outline and available summary hint are shown. Report any partial
coverage indicated by `truncated` or `qualityWarning`. If the connector is
unavailable, report the error. Before a later element action, get a fresh snapshot.
"""
    current_page = "For the current page, bind it once: `tab = await browser.getTab('current');`."
    if mode == "managed":
        current_page += (" To open a requested URL in a new managed tab, use "
                         "`tab = await browser.newTab(url);` instead, with the requested URL string. "
                         "Choose one of these entries.")
        current_page += " For a known URL on a bound tab, use `await tab.goto(url); await tab.getState();`."
    return common + f"""
Browser only, mode={mode}. Use call_tool with the browser_repl_js tool_ref from
the current tool description. If missing or expired, use tool_search and call
the returned reference on the next step. Put mode and code inside arguments_json.
If a legacy interface only offers names, use the offered name.

In a fresh REPL, initialize `var browser = await cua.getBrowser('{mode}'); var tab;`.
Use these names separately in both modes. When resuming an existing alias, use
the name that succeeded in this mode.
The current mode's `browserMethods` and `tabMethods` are listed by `await cua.getState()`.
If that initialization just displayed this manual, continue with the existing browser.
{current_page}
A tab binding displays its snapshot (or the pending dialog for a named managed tab). With a snapshot, in the next cell use the existing tab
and the exact ref string shown for the intended control: `await tab.click(ref)`
or `await tab.fill(ref, text)`. Verify the result with `await tab.getState()`.

For a field, fill directly. Clicking first invalidates the ref even in the same cell.
On StaleObservation, in a new cell run only `await tab.getState()` on the intended page.
Read that result, then copy the target's entire ref into the next cell.
Refreshing the snapshot cannot update a ref literal already written in the cell.
Keep temporary names in local scope. Inspect partial effects before resuming a failed cell.

Keep using tab across cells. Reassign an existing name only to change the binding.
Each new snapshot replaces earlier refs, including one produced by getTab on the
same page. After click, fill, goto or a change of active page, observe again and
pick the next ref from the new snapshot. The 'current' handle follows the active page.
Click and fill take only observed opaque r_ refs as targets. Click/fill return undefined.
App APIs and dedicated submit are unavailable.
Do not use click to bypass a required submit authorization.
Two modes have separate variables and observations. User cookies remain in the
real browser; managed does not inherit them. After an error verify the outcome in
the selected channel. Follow Web Access Routing for any route change, respecting
explicit browser choices, state dependencies and access restrictions. Bind fresh
tabs and refs in the new channel. User-owned final action constraints still apply.
""" + managed
