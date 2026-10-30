# Optional host runtime preparation

Hosts may advertise `_meta.host_capabilities.runtime_environment: 1` on session creation.
The ACP adapter then registers `local_environment` (status/load/ensure, Python/Node/Git/browser)
and checks Python before `execute_code` starts its kernel. CLI and older
hosts are unchanged. This is not an installer in the Agent process.

Reverse method: `session/runtime_environment`, params: `sessionId`, `capability`, `action`.
The host validates the session and capability, owns installation and deduplication,
and returns `status` (`ready`, `missing`, `installing`, `failed`), component/progress
facts, `installerRunning`, `error`, and a bounded redacted `logTail` plus `logPath`. Historical log text is
diagnostic data, not a lock or proof that a current installation succeeded.

Model-facing `local_environment` results omit installer logs, component details and
progress when ready, retaining paths, shell instructions and MCP state. Not-ready,
probe-failure and unconnected-browser results retain diagnostics. Successful
`execute_code` continues to return only execution output; internal status checks
and installer coordination are unchanged.

On readiness, the host may return an `env` object containing execution paths. Only
allowlisted runtime keys reach future tool subprocesses; they are not model output.
The Agent does not mutate global environment variables or wait for its own restart.
Cancelling an Agent wait does not cancel the host's shared installer. A 600-second
wait timeout reports that installation may still be running; inspect status next.

Delegated Python execution borrows the parent's host readiness check and refreshes
the child's managed runtime paths after it succeeds. Child kernels retain their
own owner namespaces, and concurrent initialization keeps the shared readiness lock.
Changes to Node/browser paths also invalidate a cached Python environment when the
Python executable stays the same. New kernels receive the refreshed environment at
process launch, without persisting the host process environment in kernel.json.
Unix shells replay the refreshed host PATH after login profiles so standard runtime
commands keep the managed executable precedence.

Office Desktop retains its startup download plan and paths. Explicit tool `ensure`
only supplements the requested capability and its packages, sharing the startup
installer lock. Settings retain explicit forced repair. `execute_code` returns
not-ready facts immediately instead of silently waiting/retrying: the model uses
`ensure` to wait/install and `load` to inspect readiness, then retries with the managed runtime.
Do not write into a managed environment while its installer is running.
This change does not add arbitrary pip/npm installs or LibreOffice installers.
Source changes require a new Box-Agent runtime build/install before packaged hosts
can use the capability.

## Alternate Python (2026-10-13)

Policy update: model prompts and tool guidance now require application-managed
Python, Node and Git. Missing runtimes use the existing host installer via `ensure`;
active installs are joined, and failures are diagnosed/retried without switching
environments or asking users to install another runtime. Ordinary chat does not wait.
The alternate-path mechanics below remain compatible at the API level, but are no
longer recommended to the model. This is prompt guidance, not a new execution gate.

- Desktop startup preparation is enabled again, fire-and-forget, without an automatic
  dialog or an ordinary-chat gate. The existing installer/download plan is unchanged.
- `local_environment(capability="python", action="load", modules=["matplotlib"])`
  returns known host/project `.venv`/user venv/PATH candidates, absolute paths, Python
  version, package location and import failures. Optional `python_path` probes a specific
  interpreter. It never installs or waits for an installer; host `installing` status
  and usable alternate candidates are reported separately. Each Python probe is bounded
  to 15 seconds; no recursive disk search or global environment modification is involved.
- `execute_code(code="...", python_path="/absolute/path/to/python", session_id="chosen")`
  bypasses host preparation and verifies `ipykernel_launcher` before starting a kernel.
  Follow-up calls on that kernel retain the interpreter even when `python_path` is omitted.
  Switching interpreters requires a new kernel `session_id`, preserving the old variables.
- Explicit interpreters do not inherit Python fallback packages from another installation.
  Missing executables, failed/timeout probes, unavailable ipykernel, and business import
  failures are distinct. Missing business packages in a user-selected environment are
  reported, not auto-installed there; use its own `-m pip` or `%pip` when authorized.
- Shell tools already accept absolute executable paths; no separate Node/Git execution
  parameter or second installer is added. Rebuild/install Box-Agent and restart the host
  before evaluating packaged-client behavior.

## Windows shell refresh

Windows shell refresh: when the host returns a prepared Git runtime, existing Bash tools
reselect their shell and utility paths. The environment tool returns the current shell and
syntax instruction for subsequent calls. PowerShell fallback uses its SystemRoot-based
absolute executable path, so a stripped PATH does not prevent starting it.

## Browser preparation

- Desktop startup silently prepares missing browser files once, using the existing Chromium
  installer. A disabled browser preference is preserved; ordinary chat never waits.
- `local_environment(capability="browser", action="status"|"load"|"ensure")` returns
  installation state, component readiness, current-attempt redacted log tail and log path.
  `ensure` joins the same installer used by settings and HyperFrames.
- All installer entry points publish progress through one host event. Completion writes system
  MCP configuration, then actively requests Box-Agent reconciliation and environment refresh;
  no file watcher or automatic client restart is added.
- `installationReady` does not prove a connected MCP. `mcp.state`, `toolCount` and `error`
  report the Agent's actual Playwright connection separately. Failed connection is not a
  reason to download browser files again. Runtime environment keys remain internal.


## Standalone Python Chromium

`local_environment(capability="chromium", action="ensure")` reuses the host's standalone
Chromium installer. Ready results refresh browser paths for future subprocesses; this
capability does not change Electron/MCP preparation or existing Python kernels.
Standard, Dazzle and Doctor prefer this preparation before Chromium-dependent work.
Unsupported hosts retain the original rendering workflow. On preparation failure, the
Agent decides the next step using the error and existing environment, while respecting
installer concurrency, user cancellation and existing acceptance gates.
Chromium failures preserve bounded diagnostics in model-visible error summaries.
Python/Node/Git behavior and Doctor's Python checks remain unchanged. Skills are generated
through the host-chromium-preparation overlay with provenance validation. Client and
Agent are packaged together; no per-capability negotiation or automatic retry is added.
