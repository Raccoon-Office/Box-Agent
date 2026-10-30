# Desktop memory launch and recovery

The desktop host selects the package's fixed cloud/local mode. For a cloud process it supplies `BOX_AGENT_MEMORY_MODE=desktop`, `BOX_AGENT_MEMORY_BOOTSTRAP_FILE` and `BOX_AGENT_MEMORY_IDENTITY_EPOCH`. These are private launch metadata, not model/tool parameters. Shared YAML may keep `enable_memory: false` for older runtime compatibility.

The shared config loader applies `desktop_memory.launch.desktop_launch_agent` before constructing a memory manager. A valid native grant enables the desktop backend and disables local extraction; it does not read or wait for the bootstrap file. Invalid native metadata disables memory without falling back to local memory. The capability consumes those environment variables and keeps a process-private copy for reloads, so shells, MCP servers and tools do not inherit the bootstrap location.

ACP initialize advertises `_meta.desktop_memory` with `protocol_version: 2`, `backend: desktop`, `enabled`, and `attempt_log_v1: true`. Hosts must validate the current process and lease as well as this capability before publishing the cloud route. The response contains no launch path, epoch or credentials.

Before context loading, the desktop runtime appends and flushes an ignorable `memory/desktop-attempt` extension in the existing canonical Session Log. It stores only a hashed turn key and original timestamp. Recovered attempts send `require_existing: true`: the host restores an existing binding, or records unavailable if the first attempt never reached it. This prevents bootstrap delays or migration completion from retroactively making an old turn learnable. Failure to persist the attempt skips memory for that turn; it never sends an unprotected context. Temporary programmatic sessions without a Session Log keep only an in-process attempt set and have no cross-process recovery guarantee.

## Evidence and background Experience production

The runtime can hand off bounded, redacted views of the current turn's canonical Session Log with its QA through the native control capability. Missing evidence permits ordinary QA but cannot produce an evidence-backed Experience. A `durable_unknown` receipt is not confirmation that QA was saved; the host retains the original input for reconciliation.

For eligible online cloud sessions, `ExperienceRuntime` uses the control-only `experience/job` operation to claim work, obtain a host-frozen model input, submit a proposal and advance persistence. Model generation runs in the background without tools or changes to the main conversation history. It has a 30-second timeout and a capped output budget. The host validates evidence and proposals, owns durable leases and idempotency, and saves Entity, Fact and Observation records. Box-Agent never writes those records directly to an external database. An uncertain persistence result stops advancement for that activation; later activations resume the host-owned state rather than inventing a replacement plan. An empty supported proposal may finish with no candidate.

Hosts must implement the named proxy operations and enforce identity, session, read-visibility and write-protection boundaries. Backend deployment, authorization policy and product rollout remain the host's responsibility. The default local backend does not require this service.

## Verification boundary

Focused checks: `uv run pytest tests/test_desktop_memory_launch.py tests/test_desktop_memory_attempts.py tests/test_desktop_memory_backend.py tests/test_desktop_memory_transport.py tests/test_desktop_memory_visibility.py tests/test_desktop_memory_evidence.py tests/test_desktop_memory_experience.py tests/test_desktop_memory_windows_files.py tests/test_cli_config.py -q`.

POSIX bootstrap checks cover ownership, permissions and links; Windows uses handle-bound private-ACL checks. Portable Win32 test doubles and conditionally skipped native tests do not establish Windows machine acceptance. Source tests do not establish that a packaged desktop runtime was built, installed, restarted, or used by a fresh live task, or that an external host/backend deployment passed end-to-end acceptance.
