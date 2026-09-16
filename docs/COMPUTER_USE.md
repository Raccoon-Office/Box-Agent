# Computer Use Integration and Lifecycle

This document defines how Box-Agent uses Cua Driver for native desktop control and separates the standalone CLI lifecycle from the Electron-hosted lifecycle.

## 1. Components and call path

- The `computer-use` Skill tells the model to prepare, observe, act, and verify.
- `ensure_cua_ready` establishes the Cua runtime on demand. It neither downloads a binary nor widens permissions.
- Box-Agent owns the MCP client and talks over stdin/stdout to `cua-driver mcp`.
- The Cua runtime or daemon owns accessibility, capture, input, recording, and cursor state.

```text
model → ensure_cua_ready → Box-Agent MCP client
      → cua-driver mcp (stdio) → Cua runtime/daemon → operating system
```

## 2. Mode one: standalone Box-Agent CLI

When `box-agent` runs without Electron, it registers Cua at startup and connects only after the first `ensure_cua_ready` call. A process-local lock coalesces concurrent readiness calls.

The platform implementation differs:

- **macOS:** Box-Agent starts bare `cua-driver mcp`. The standard Cua path starts or connects to the daemon owned by `CuaDriver.app`, preserving its stable Accessibility and Screen Recording TCC identity. Box-Agent must not pretend to be an embedding host or start `serve --embedded`.
- **Windows/Linux:** Box-Agent starts private `cua-driver serve --socket <endpoint>`, then connects `cua-driver mcp --socket <endpoint>`. It stops only the daemon it created when the capability is removed or Box-Agent exits.

“The CLI starts the daemon” therefore means that the CLI autonomously prepares the Cua runtime. On macOS, the concrete daemon launch remains delegated to `CuaDriver.app` for correct permission attribution.

## 3. Mode two: Electron-owned daemon

Electron sets `BOX_AGENT_CUA_RUNTIME_MODE=embedded`, starts `cua-driver serve --embedded --socket <electron-endpoint>`, and supplies Box-Agent with the complete trusted MCP definition. Box-Agent's stdio client starts `cua-driver mcp --embedded --socket <electron-endpoint>` and only connects to that daemon.

Box-Agent must not start a second daemon or silently fall back to standalone mode. If the host endpoint is unavailable, readiness reports `host_unavailable`. Electron stops its daemon; Box-Agent only closes its MCP stdio connection.

## 4. Why Electron owns the product daemon

1. macOS privacy grants remain attached to one signed product identity.
2. The host centrally controls permission mode, capability manifests, user and managed policies, and approval boundaries.
3. Startup, feature disable, shutdown, crash recovery, and upgrades have one lifecycle owner.
4. Private sockets or named pipes stay inside the trusted process tree.
5. Cursor overlays, recording, and runtime state remain stable across Agent sessions.
6. The host selects, verifies, and signs the correct platform and architecture resource.

Standalone mode is appropriate for development, training, and direct CLI use. The desktop product uses Electron-owned mode.

## 5. Binary supply contract

Do not commit `cua-driver`, `cua-driver.exe`, release archives, or extracted runtimes to this repository. Do not include them in the Box-Agent Python wheel or standalone runtime bundle.

Standalone discovery order is:

1. Absolute path in `BOX_AGENT_CUA_DRIVER_PATH`.
2. A `cua-driver` resource adjacent to or inside `BOX_AGENT_RUNTIME_ROOT`.
3. The development machine's `PATH`.

The final host supplies exactly one platform/architecture resource, passes its absolute path, pins and verifies its compatible version, and decides whether to install it with the app or download it after explicit feature enablement. Box-Agent never downloads, installs, or updates Cua Driver. Missing binaries leave Computer Use unconfigured with an explicit error.

This contract lets Electron later deliver Cua Driver as an optional downloadable resource without changing the Box-Agent protocol.

## 6. Security and configuration rules

- A standalone-capable explicit `cua-computer-use` definition is an operator security boundary and is preserved unchanged, including command, arguments, policies, permission mode, capability manifest, and telemetry settings. An Electron `--embedded --socket` definition is valid only in app-hosted mode; standalone CLI ignores that private endpoint and synthesizes its own definition.
- `disabled: true` always wins; mode migration must not re-enable Computer Use after the user has disabled it.
- Auto-generated CLI definitions forward every host `CUA_DRIVER_*` environment variable.
- Readiness proves connectivity, not business approval for a high-impact action.
- Existing confirmation rules still apply to sending, submitting, deleting, purchasing, and installing.
- Daemon stdout must never enter the CLI or ACP protocol stream.

## 7. Development verification

```bash
BOX_AGENT_CUA_DRIVER_PATH=/absolute/path/to/cua-driver \
uv run python -m box_agent.cli
```

Verify on-demand connection, idempotent readiness, permission errors, MCP recovery, disable/exit cleanup, and native permission, capture, input, focus, and cursor behavior on real macOS, Windows, and Linux interactive desktops before release.
