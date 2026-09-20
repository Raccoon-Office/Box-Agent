---
name: browser-use
description: "Route browser tasks between two distinct modes: managed Playwright MCP automation, which defaults to headless and can recover in headed mode when headless limitations block progress, and the user's visible real browser with their current tabs and login state, which is preferred when the task depends on the current page, login state, cookies, extensions, intranet access, review, takeover, or personal submission. Use for opening URLs or websites, current-page interaction, logged-in or intranet tasks, forms, screenshots, scraping, crawling, web testing, visible or background browsing, headed/headless requests, anti-bot or human-verification failures, and browser runtime configuration. Continue with the browser mode established by recent context unless the user explicitly switches modes or the task clearly starts an independent browser operation."
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

There are exactly two browser modes. Keep them conceptually and operationally separate:

1. **Managed browser automation** is an independent browser controlled by the Playwright MCP. Treat headless as its default mode; headed is a recoverable alternative when a visible window or a full browser environment is needed.
2. **The user's visible real browser** is the browser connector attached to the user's own tabs, cookies, login state, extensions, and intranet access. Use it when the task depends on that user-owned browser state or the user needs to review, take over, or personally submit.

Do not require the user to know internal component names. Both modes may be used in separate steps of one turn only when the user's request calls for both, and snapshots, element refs, tabs, cookies, and session state must never be reused across modes.

## Tool namespaces

Use only the current public tool names exposed by the runtime:

- `managed_browser_*` controls the independent browser through Playwright MCP, for example `managed_browser_navigate`, `managed_browser_snapshot`, and `managed_browser_click`.
- `user_browser_*` controls the user's visible, currently logged-in browser through browser-gateway, for example `user_browser_read_current_page`, `user_browser_snapshot`, and `user_browser_click`.

Treat these namespaces as a security boundary. Never pass snapshots, element refs, tab identifiers, cookies, or session IDs from one namespace to the other. Backend transport identifiers are internal and must not be used when selecting or calling a public tool.

## Choose the browser mode

- For a new browser task, start with managed browser automation when it can be completed independently of the user's current browser state.
- Use the user's real browser directly when the task depends on the current page, existing login state, cookies, extensions, intranet access, system/default browser, user review, takeover, or personal submission. This routing decision does not require a separate authorization prompt.
- Public-web search, retrieval, crawling, bulk collection, testing, screenshots, DOM inspection, and network inspection use managed browser automation unless the task also depends on user-owned browser state.
- “打开浏览器”, “打开网页给我看”, “open the browser”, and requests for a visible window select a headed managed browser. Call `mcp_config(action="set_browser_mode", mode="headed")` before navigation, even if tools are already available. A request to inspect the user's existing page selects their real browser.
- Treat headed/headless only as the managed browser's window visibility. It does not inherit the user's Chrome login state. Do not treat a visible managed window as proof that the real browser is in use.
- Do not ask the user to choose a mode when the task requirements identify the correct browser. Ask once only when the request is genuinely ambiguous and either mode would materially change the result or user experience.

## Switch between browser modes

- Switching to the user's real browser does not require a separate authorization prompt when the task depends on current tabs, login state, cookies, extensions, intranet access, review, takeover, or personal submission.
- When switching modes, begin a fresh browser operation. Never reuse snapshots, element refs, tab identifiers, cookies, or session IDs from the other mode.
- If the real browser is disconnected, explain how to connect it. Use managed mode as a fallback only when the task does not depend on user-owned browser state; never imply that the fallback retained the user's session.
- Browser routing does not authorize external side effects. Sending, publishing, purchasing, deleting, submitting, and similar actions keep their existing confirmation requirements.

## Continue across turns

- For follow-up requests such as “继续”, “下一页”, “点进去”, or “提交吧”, infer the mode from the most recent successful browser interaction and continue in that same mode.
- Explicit mode selection in the latest user message overrides earlier context. If the user says to switch to their real browser, system browser, managed browser, headed browser, or headless browser, follow the new selection from that point onward.
- If there is no reliable recent browser context, treat the request as a new task and choose the mode from its current state and interaction requirements.
- If a later step genuinely requires the other mode, switch using a fresh browser operation and keep the two modes' state isolated. Briefly explain the switch when it changes what the user will see or which session state is available.

## Parallel sub-agents and the managed browser

When per-session isolation is enabled, parallel `sub_agent` runs in the same session each get their own managed browser context. Shared stdio fallback configurations still serialize browser access and may return `BROWSER_RUNTIME_BUSY`. When a child returns, that context is closed. The parent can see the child's snapshots in the transcript, but must not reuse that child's snapshots, element refs, or tab identifiers.

## Switch the managed browser window

Background retrieval, scraping, screenshots and testing default to headless. Continue the existing session mode for follow-up steps. Do not switch merely because navigation or a selector failed.

1. Call `mcp_config(action="inspect_browser")`. Prefer `source=runtime`; a config-file result does not prove a running browser exists.
2. Use `mcp_config(action="set_browser_mode", mode="headed")` for a visible window, or `mode="headless"` for an explicitly background operation. This verifies browser startup for the current session; it does not modify global configuration or restart other sessions.
3. Switching an existing session resets tabs, cookies, login, forms and old element refs. Explain this before switching when the session contains user work; do not imply a seamless state transfer. If preserving unsaved work is required, keep the current mode.
4. After a successful switch with `state_reset=true`, navigate afresh to the task URL and obtain a new snapshot. Never reuse old refs. No page state is automatically restored.
5. If switching is unsupported or fails, report that a visible window has not been verified. Do not patch global MCP arguments, create duplicate config entries, or silently use the user's real browser.
6. Only claim a visible browser opened after a successful headed launch. Headless navigation means “已在后台访问页面”. Do not suggest enabling Playwright when runtime tools are already working.

## Recover from headless-only failures

Use headed recovery when the current managed MCP is headless and there is concrete evidence that the browser environment is blocking progress, such as a human-verification challenge that cannot be completed or displayed, a persistent anti-bot/interstitial loop, or behavior that works only in a full visible browser. Do not use headed mode as a generic first retry for timeouts, bad selectors, network errors, or application bugs.

1. Stop issuing calls to the current Playwright MCP attempt. Do not repeatedly trigger the challenge or try to bypass CAPTCHA or anti-bot protections.
2. Call `mcp_config(action="inspect_browser")`. If the reported mode is already headed, diagnose another cause instead of toggling modes again.
3. If it is headless, call `mcp_config(action="set_browser_mode", mode="headed")`. This rebuilds only the current session. Do not keep using the old refs.
4. Wait for a successful result, then navigate back to the target page and obtain a fresh snapshot. Login and form state from the previous instance are not restored.
5. Retry the blocked step once in headed mode. If the site still requires human verification, leave the managed headed window open and ask the user to complete the challenge. Resume only after the user confirms completion. Do not solve, outsource, or circumvent the verification.
6. If session switching is unavailable or fails, report the failure. Do not silently switch to the user's real browser.

Window mode is session-local and lasts until the session/runtime closes. Host configuration remains the default for new sessions. Even with `mode=headed`, `isolated=true` still means an independent managed Chromium that does not inherit the user's Chrome login state.

## Tools and safety

- Use `user_browser_*` to open URLs, read the current page, take snapshots, click, fill, and perform confirmed submissions when the task depends on user-owned browser state or continues an established real-browser interaction.
- Use `managed_browser_*` for navigation, snapshots, clicks, filling, screenshots, scripts, and network inspection. Do not substitute a user-browser read tool for the managed browser.
- When a real-browser task depends on the current page or login state and the real browser is disconnected, guide the user to connect it. Never silently switch to an independent browser and pretend the state remains available.
- By default, describe the choices to the user as “independent browser”, “your currently logged-in browser”, and “show/hide the window”. Do not expose MCP or internal component names unless the user asks about the architecture.
- Filling is not submitting. Sending, publishing, purchasing, deleting, and other external side effects require explicit confirmation under the existing policy.
