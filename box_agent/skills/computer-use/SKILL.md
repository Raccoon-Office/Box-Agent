---
name: computer-use
description: "Operate native desktop applications through Cua Driver. Use for opening or controlling macOS, Windows, or Linux apps; reading application windows; clicking, typing, selecting menus, and verifying native UI results. Before the first native desktop action, ensure the Cua runtime is ready."
keywords:
  - desktop
  - computer use
  - native app
  - Cua Driver
  - 桌面控制
  - 电脑操作
  - 原生应用
  - 打开应用
  - 操作应用
  - 窗口
  - 计算器
  - 飞书
---

# Desktop Computer Use

Use Cua Driver for native desktop applications. Browser pages still use the browser tools unless the task specifically requires the native browser application's chrome or another desktop-only surface.

## Start safely

1. Before the first native desktop action in a task, call `ensure_cua_ready()` exactly once.
2. If it returns `status=ready`, use `tool_search` with `server_name="cua-computer-use"` to activate only the Cua tools needed for the next step.
3. If it returns `needs_permission`, stop and ask the user to grant the operating-system permissions. Do not retry in a loop.
4. If it returns `host_unavailable`, explain that the Electron-hosted desktop runtime is unavailable. Do not start another daemon or switch to standalone mode.
5. If it returns `not_configured` or `failed`, report the returned error. Do not use shell commands to start Cua Driver, and do not invent executable paths, sockets, process IDs, policies, or session IDs.

Call `ensure_cua_ready()` again only after a concrete Cua connection/lifecycle failure. It is idempotent, but repeated readiness calls do not repair ordinary UI targeting errors.

## Observe, act, verify

1. Find the application or window with the Cua inventory tools.
2. Get fresh window state before targeting an element.
3. Prefer accessibility-backed elements. Use screenshot coordinates only when semantic elements are unavailable.
4. Use an element token only with the `pid` and `window_id` from the same latest window snapshot. Never reuse a token across windows or after the UI changes.
5. After clicks, typing, menu actions, navigation, or window changes, observe again and verify the result before continuing.
6. Treat sending messages, submitting, deleting, purchasing, installing, and other external side effects according to the existing confirmation policy. Readiness is not authorization.

If the runtime fails after an action whose outcome is unknown, do not automatically replay the action. Reconnect, observe current state, and decide from evidence.
