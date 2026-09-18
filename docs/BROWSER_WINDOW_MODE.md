# Managed browser window modes

`mcp_config(action="inspect_browser")` reports the connected runtime's current
session mode before consulting the user MCP config file. A runtime response uses
`source=runtime`, `scope=session`, `mode=headed|headless`, `switch_supported`, and
`browser_started`. A config-file response is only a launch configuration, not
proof of a live browser. Shared or externally owned connections report mode
unknown and do not support session switching.

Use `mcp_config(action="set_browser_mode", mode="headed")` to open a visible
independent browser, or `mode="headless"` for background work. This requires the
existing owned, isolated Playwright pool. The operation waits for the session's
in-flight tool call, closes its old context, and verifies a page in the requested
mode. Other sessions and the default MCP configuration remain unchanged.

The pool continues sharing the default browser process. A session selecting the
alternate mode owns an additional managed MCP process; the existing pool limit,
idle reaper, session close and runtime shutdown clean it up. No duplicate server
entries or host-wide reconnects are used. Public `managed_browser_*` names stay
unchanged.

Changing mode resets tabs, cookies, login, form state and element refs. The result
reports `changed` and `state_reset`; callers must navigate again and obtain a new
snapshot after reset. There is no automatic state migration. Re-selecting an
already active mode preserves the context. Failed or cancelled startup is not a
successful switch and its client is closed. Session mode is ephemeral and ends
when the session or runtime closes.

OfficeV3 keeps `--headless --isolated` as the new-session default. Its system MCP
file and user-editable MCP file remain separate. The browser-use skill selects
headed for “打开浏览器” or a request to show a page, uses background mode for
independent retrieval, and preserves the mode for follow-up actions. Existing
user tabs and login state still require Browser Connector; a headed managed
window does not inherit them.

Verification: run `uv run pytest tests/test_playwright_session_pool.py
tests/test_mcp_playwright_multiplex.py tests/test_mcp_config_tool.py
tests/test_browser_use_builtin_skill.py`. A packaged host additionally needs a
rebuilt and installed runtime, restart, and a fresh live browser task.
