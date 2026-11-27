---
name: browser-use
description: "Execute browser tasks after the shared Web Access Routing policy selects a channel. Covers independent managed browser automation and the user browser connector, page interaction, forms, screenshots, browser sessions, visible/background browsing, and recovery. Browser choice, page state, and visibility are separate; preserve the selected namespace and user-owned final actions."
keywords:
  - 浏览器
  - 网页
  - 网站
  - 页面
  - 标签页
  - 当前页
  - 打开网址
  - 打开链接
  - 系统浏览器
  - 默认浏览器
  - 真实浏览器
  - 可见浏览器
  - 有头浏览器
  - 带头浏览器
  - 无头浏览器
  - 登录态
  - Cookie
  - 内网
  - 网页抓取
  - 网页自动化
  - 公开检索
  - 批量抓取
  - 爬虫
  - 表单
  - 人工接管
  - 让我检查
  - 我最后提交
  - browser
  - current tab
  - headed
  - headless
  - playwright
  - chrome
  - scrape
  - crawl
---

# Browser Use

## Routing contract

Follow the shared **Web Access Routing** policy before selecting a tool. There are exactly two browser modes:

- Independent managed browser automation uses Playwright MCP.
- The user's visible real browser uses the connector and the user's existing page, login, extensions and network access.

After a failure, check the outcome in the selected channel. Switch only as permitted by Web Access Routing, respecting explicit choices, state dependencies and access restrictions. Start with fresh tabs and refs in the new channel.
Stop before the final action whenever the user owns it, in either namespace. Review or personal submission alone does not select the user's browser. Sending, purchasing, publishing, deleting and submitting retain their confirmation requirements.
Browser interaction belongs to the parent agent. Sub-agents cannot perform it through delegation or shell tools.

## Browser REPL

Look for the current description of `browser_repl_js` in the conversation.
If it contains a `tool_ref`, call **`call_tool`** with that exact reference now.
The descriptor's `name` identifies the target. The model's function call is
named `call_tool`. If the reference is missing or expired, use `tool_search`
with `{"tool_names":["browser_repl_js"]}`, wait for its result and use the
returned reference on the next model step. Reuse a current reference across
cells. On a legacy interface that offers names directly and has no call_tool,
use only the name actually offered in that model step. Apply the same rule to `browser_repl_reset` if a reset is needed.

For example, replace the placeholder with the returned reference.
The outer object has two fields. All Browser inputs, including an optional
`title`, belong inside the serialized JSON string.

```json
{"tool_ref":"<returned tool_ref>","arguments_json":"{\"mode\":\"managed\",\"code\":\"var browser = await cua.getBrowser('managed'); var tab;\",\"title\":\"Connect to the managed browser\"}"}
```

Use the mode selected by Web Access Routing on every call. If discovery returns no
Browser REPL, report that browser control is unavailable in this session.

### Work on a single page

In a fresh REPL, run the initialization shown above once. For user mode, use
`user` in both the mode field and `getBrowser`. Read the returned manual, then choose one entry:

- Open a requested URL in a new managed tab: `tab = await browser.newTab("https://example.com/");`
  (substitute the requested URL).
- When asked to open a URL in the user browser: `await browser.openAndRead(url);`
- Work on the current page in either mode: `tab = await browser.getTab("current");`

Binding displays a snapshot or, for a named managed tab with a pending modal,
its modal notice. To navigate a bound managed tab to a known URL, run
`await tab.goto(url); await tab.getState();`. Reserve `newTab` for a new page.
For a field, use `await tab.fill(ref, text)` directly. A click consumes its ref,
including inside the same cell. Use a new snapshot before any subsequent ref action.

**Keep the tab, refresh the ref.** Reuse `browser` and `tab` separately in each
mode. Assign to `tab` to change pages; keep an already initialized alias when
resuming an older session. New observations replace previous refs. After a
failed cell inspect the effects already applied, then continue only the unfinished
steps. Keep temporary declarations inside an awaited async function.

### Navigate and read in the user's browser

When the user asks to open a URL, `await browser.openAndRead(url);` opens a new active Chrome tab and returns the Gateway extraction from that tab in one call. Use its URL, title and content directly. On `url_mismatch`, report the redirect and ask before reading the active page or opening another tab.

For a request such as “open the event page in my Chrome and click Registration details”, `openAndRead` returns content without assigning `tab`. If the interaction is authorized, run only `tab = await browser.getTab('current');` in the next cell. Compare the snapshot URL and title with the intended page. If they match, click with the link’s fresh ref in the following cell; otherwise stop and ask. For a read-only request, the extraction suffices.

A URL given as the expected current page is a check on page identity. For that request, compare the URL and title in the binding snapshot with the requested page before calling `await tab.readText();`. If they differ, stop and ask whether to open the target or let the user switch tabs. The current handle follows the active page, so verify the returned URL and title again before using the body. Report outline-only or partial coverage.

### Managed multi-tab workflow

Opening or closing a tab expires previous managed IDs. On `StaleTab` or
`HandleScopeMismatch`, use the managed manual's list-and-rebind example:
inspect the latest URL, title and active status in `browser.listTabs()`, then assign
`tab = await browser.getTab(id)` using its fresh ID. Inspect ambiguous matches.

A managed `evaluate()` invalidates all element refs, including a read-only
evaluation. For example, run these in order:

```js
await tab.evaluate('() => document.title');
await tab.getState();
```

In the next cell, click with the complete ref for the intended control.
For page code containing newline escapes or regex backslashes, use a
`String.raw` template as in the managed manual's price example.

Managed actions also include `tab.scroll({deltaY: 450})`,
`tab.pressKey('super+a')` (Command+A on Mac), `tab.hover(ref)`, and
`tab.evaluate('() => document.title')`. Use `tab.back()` for history,
`tab.waitForText(text)` for a page condition, and `tab.selectOption(ref, value)`
for a native dropdown. Managed clicks accept options such as
`await tab.click(ref, {button: 'right'})`. In user mode call `await tab.click(ref)`
without options. Both modes require an observed element ref for click and fill.
The managed manual shows how to handle an observed dialog on the same tab.
On `DialogTabRequired`, bind the original page by its listed ID and check the
pending dialog before answering it. For keys pass exactly one string, e.g.
`await tab.pressKey('shift+Left')`. A second argument such as
`tab.pressKey('a', {modifiers: 4})` or an unknown key raises `InvalidArguments`
before sending that key. Earlier actions in the same cell may have completed.
Tab rejects fn, F1–F12, Caps Lock and standalone modifiers. This keyboard interface
is available only in managed mode.

| Browser result | Recovery |
| --- | --- |
| `StaleObservation` | In a new cell run only `await tab.getState()` on the intended page. Read its returned snapshot. In the next cell copy the target's complete ref from that output. A literal copied before the refresh still refers to the old snapshot. |
| File chooser already open | Use `tab.uploadFile(null, path)` on the same tab. Snapshots or a second click may fail while the chooser is open. To open it from the page, use `tab.uploadFile(ref, path)` with the latest ref of its control. Use an existing, user-selected file in the session workspace. |
| Target outside a nonzero viewport | Confirm the intended page, scroll the target into view, then get a new ref before clicking. |
| `BROWSER_VIEW_EMPTY` or a more specific empty-capture code | Check the intended view and viewport dimensions. For a zero viewport restore the panel and retry once. With positive dimensions, keep the layout and report the capture error alongside the snapshot evidence. |

Write top-level `await` statements without a top-level `return`. If using an
async IIFE, await the IIFE itself.
User mode has no screenshots, tab management, or managed-only actions above.
CUA App methods are unavailable here. Native App tasks use `computer-use` and
`cua_repl_js`. Await all operations; after timeout/cancellation reobserve rather
than replaying the code. Reset leaves user tabs open.

## Switch the managed browser window

Background operations default to headless unless the host provides an integrated visible page. When the host can display the current page, use its presentation control and preserve the session. Continue the existing session mode for follow-up steps.

Use the discovered `mcp_config` tool with the following target inputs via the current tool interface.

1. Call `mcp_config(action="inspect_browser")`. Prefer `source=runtime`; a config-file result does not prove a running browser exists.
2. Use `mcp_config(action="set_browser_mode", mode="headed")` for a visible window, or `mode="headless"` for an explicitly background operation. This verifies browser startup for the current session; it does not modify global configuration or restart other sessions.
3. Switching an existing session resets tabs, cookies, login, forms and old element refs. Explain this before switching when the session contains user work; do not imply a seamless state transfer. If preserving unsaved work is required, keep the current mode.
4. After a successful switch with `state_reset=true`, navigate afresh to the task URL and obtain a new snapshot. Never reuse old refs. No page state is automatically restored.
5. If switching is unsupported or fails, report that a visible window has not been verified. Do not patch global MCP arguments, create duplicate config entries, or silently use the user's real browser.
6. Confirm the integrated page is presented or that the headed launch succeeded before claiming the page is visible. Headless navigation means “已在后台访问页面”. Do not suggest enabling Playwright when runtime tools are already working.

## Recover from headless-only failures

Use headed recovery when the current managed MCP is headless and there is concrete evidence that the browser environment is blocking progress, such as a human-verification challenge that cannot be completed or displayed, a persistent anti-bot/interstitial loop, or behavior that works only in a full visible browser. Do not use headed mode as a generic first retry for timeouts, bad selectors, network errors, or application bugs.

If the verification page is visible, leave it open and wait for the user. If headless and the current page cannot be shown, use the switch procedure above once; after a reset reopen only the original navigation URL. Never replay the click, form submission or purchase that led to the challenge. After the user confirms completion, take a fresh snapshot in the same browser and verify the page before continuing. Do not solve, outsource, or circumvent the verification.

Window mode is session-local and lasts until the session/runtime closes. Host configuration remains the default for new sessions. Even with `mode=headed`, `isolated=true` still means an independent managed Chromium that does not inherit the user's Chrome login state.

Describe the two modes to users as the independent browser and their currently logged-in browser.
