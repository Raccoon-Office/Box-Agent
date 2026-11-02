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

There are exactly two browser modes. Keep them conceptually and operationally separate:

1. **Managed browser automation** is an independent browser controlled by the Playwright MCP. Treat headless as its default mode; headed is a recoverable alternative when a visible window or a full browser environment is needed.
2. **The user's visible real browser** is the browser connector attached to the user's own tabs, cookies, login state, extensions, and intranet access.

Do not require the user to know internal component names. Both modes may be used in separate steps of one turn only when the user's request calls for both, and snapshots, element refs, tabs, cookies, and session state must never be reused across modes.

## Tool namespaces

Use only the current public tool names exposed by the runtime:

- `managed_browser_*` controls the independent browser through Playwright MCP, for example `managed_browser_navigate`, `managed_browser_snapshot`, and `managed_browser_click`.
- `user_browser_*` controls the user's visible, currently logged-in browser through browser-gateway, for example `user_browser_read_current_page`, `user_browser_snapshot`, and `user_browser_click`.

Treat these namespaces as a security boundary. Never pass snapshots, element refs, tab identifiers, cookies, or session IDs from one namespace to the other. Backend transport identifiers are internal and must not be used when selecting or calling a public tool.

## Routing contract

Follow the shared **Web Access Routing** policy supplied by the runtime before selecting tools. It owns explicit targets, continuation, user-state dependencies, visible interaction, public-body retrieval, discovery, and conditional recovery. This Skill supplies execution procedures for the selected browser; it does not define a competing route order.

- Use only tools actually exposed or discovered in the current session. A hidden deferred tool is not proof that its backend is unavailable.
- Once a browser is selected, obtain a fresh page or snapshot before using element refs. A missing tab requires reacquiring a tab in that browser, not changing the browser namespace.
- Keep page state within that namespace across follow-up steps. If the route changes under the shared policy, start a fresh operation and obtain new refs.
- Treat user review and personal submission as an interaction constraint. They do not by themselves require the user's existing browser. Stop before the final action whenever the user owns it, in either namespace.
- Showing the browser, selecting its backend, and verifying an action are separate. Use the runtime's documented presentation capability when provided; do not reset a working browser merely to reveal its existing page. The headed/headless procedure below applies when visibility is controlled through managed window mode.
- Browser routing does not authorize external side effects. Sending, publishing, purchasing, deleting, submitting, and similar actions keep their existing confirmation requirements.

## Parallel sub-agents and the managed browser

When per-session isolation is enabled, parallel `sub_agent` runs in the same session each get their own managed browser context. Shared stdio fallback configurations still serialize browser access and may return `BROWSER_RUNTIME_BUSY`. When a child returns, that context is closed. The parent can see the child's snapshots in the transcript, but must not reuse that child's snapshots, element refs, or tab identifiers.

## Switch the managed browser window

After the managed browser has been selected, background operations default to headless unless the host supplies an integrated visible page. Continue the existing session mode for follow-up steps. Do not switch merely because navigation or a selector failed.

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

- The `user_browser_*` namespace opens URLs, reads pages, takes snapshots, clicks, fills, and performs confirmed submissions in the user browser selected by Web Access Routing.
- Use `managed_browser_*` for navigation, snapshots, clicks, filling, screenshots, scripts, and network inspection. Do not substitute a user-browser read tool for the managed browser.
- A disconnected real browser cannot provide its page or login state. Report the specific failure; follow Web Access Routing for recovery and never pretend another channel retained that state.
- By default, describe the choices to the user as “independent browser”, “your currently logged-in browser”, and “show/hide the window”. Do not expose MCP or internal component names unless the user asks about the architecture.
- Filling is not submitting. Sending, publishing, purchasing, deleting, and other external side effects require explicit confirmation under the existing policy.
