---
name: computer-use
description: Operate native desktop applications through CuaDriver. Use for opening macOS, Windows or Linux apps, desktop UI interactions, system calculator, accessibility trees and desktop screenshots. Web tasks should prefer browser-use.
keywords:
  - 桌面控制
  - 打开计算器
  - 计算器
  - 原生应用
  - 操作飞书
  - computer-use
  - desktop application
---

# Native desktop computer-use

1. Call `ensure_cua_ready` without arguments. It prepares the configured driver and MCP connection. On failure report the exact blocker; never switch a host-owned external runtime to standalone, install a driver, change permissions, or run your own daemon shell commands.
2. Use `tool_search` to discover the computer-use MCP tools. Check permissions without prompting. Start a driver session before actions when required by the tool schema.
3. Enumerate applications/windows and read fresh window state. Read the CUA structured result: `list_windows` supplies real `windows[].window_id`; `get_window_state` supplies `snapshot_id` and `elements` with driver-issued tokens. Copy the exact returned token, or use the returned `snapshot_id` with that snapshot's `element_index`, if the tool schema supports it. Never synthesize a token from a displayed tree index or guess a window/snapshot ID. Pass `session` only when that specific tool declares it (for example `launch_app` does not). Do not reuse identifiers from old tasks. Prefer accessibility semantic actions over screenshot coordinates. Screenshots require an image-capable model; otherwise report that visual fallback is unavailable.
4. If a session ended, start/revive it, then obtain fresh window state. If a token is stale, refresh state before deciding a new action. For missing or unfocusable windows, re-enumerate rather than repeatedly targeting an old ID.
5. Verify every input by reading fresh state. An unverified keystroke is not proof of delivery. Never automatically replay clicks, submissions or messages after transport failure or reconnection; first inspect the resulting state.
6. Ask for any required approval before sending messages, deleting data or submitting externally. End your driver session when finished; never stop a daemon belonging to the host or another client.
