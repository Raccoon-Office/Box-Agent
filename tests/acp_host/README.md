# ACP Host-like probe (`tests/acp_host`)

Spawns a **real** `python -m box_agent.acp.server` subprocess over stdio JSON-RPC.
The test adapter reuses `test_workspace/acp_eval/src/acp_eval/transport.py` for
framing, request/response exchange, and reverse RPC, and `lifecycle.py` for
stderr capture and shutdown. The existing evaluator uses the same transport.
Only strict framing is probe-specific; permission requests retain the evaluator's
deny-by-default policy.

## Layout

| Path | Role |
|------|------|
| `probe.py` | `AcpHostProbe`, `RpcError`, `CaseResult` |
| `minimal_config.yaml` | Isolated non-network profile for handshake probes |
| `issue_draft.py` | T5 `to_issue_draft` |
| `test_t1_connect.py` | T1-01 / T1-02 (no LLM) |
| `test_t2_session.py` | T2-01 / T2-02 / T2-03 |
| `test_t3_tools.py` | T3-01 / T3-02 / T3-03 |

Product code under `box_agent/acp/` is **not** patched by this harness. Failures produce an `IssueDraft` only.

## Isolated `BOX_AGENT_HOME` (default suite)

No-LLM cases (T1-01, T1-02, T2-03, T3-01) provision an **isolated** `$BOX_AGENT_HOME`
under pytest `tmp_path` and copy `minimal_config.yaml` into it.
`PLAYWRIGHT_BROWSERS_PATH` is also scoped to that profile so a CI host cache
cannot violate the child process state-path boundary:

- `api_key: acp-host-probe-fixture-key` — accepted by `Config.load` (not `YOUR_API_KEY_HERE`)
- `api_base: https://example.invalid/v1` — non-routable; handshake never needs a live provider
- `enable_skills: true` — so T3-01 can list the host-visible catalog
- memory / MCP / bash disabled

This avoids clean-CI failures where missing `~/.box-agent/config` would make
`Config.load()` write a placeholder into the **real** home, reject the key, close
stdout, and EOF T1-01 / T3-01 / T2-03. The subprocess never mutates `~/.box-agent`.

`Config.load` validation (non-hosted): missing/`YOUR_API_KEY_HERE` raises
`ValueError("Please configure a valid API Key")`. Hosted gateways may omit the key
and use `auth.json`. The harness fixture key is a third path — load succeeds, no network.

## Live LLM cases (opt-in)

T2-01, T2-02, T3-02, T3-03 call a real provider. They are **skipped** unless:

```bash
export BOX_AGENT_ACP_HOST_LIVE=1
```

and a usable config exists (`api_key` or `auth.json`) under the usual search path
(`$BOX_AGENT_HOME` / `box_agent/config` / `~/.box-agent/config`).

Missing configuration skips live tests. Once explicitly enabled, provider/authentication
failures remain visible failures with IssueDraft diagnostics; they are not silently skipped.

Default `uv run pytest tests/acp_host/ -q` stays deterministic (no tokens / network).

### Minimal live setup

```bash
# optional: copy example then edit
cp box_agent/config/config-example.yaml ~/.box-agent/config/config.yaml
# set api_key / api_base / model / provider

export BOX_AGENT_ACP_HOST_LIVE=1
uv run pytest tests/acp_host/ -q
```

Existing manual entrypoints remain available:

```bash
uv run python scripts/test_acp_streaming.py --prompt "hello"
uv run python test_workspace/run_acp_eval.py --count 3
```

These commands use the configured provider and are separate from the isolated tests.

## Run

```bash
# Deterministic (isolated home, no live LLM)
uv run pytest tests/acp_host/ -q

# Optional no-key simulation (fake HOME, isolated profile only)
# covered by test_t1_01_isolated_home_without_real_user_config
```

## Notes

- Protocol NDJSON on **stdout**; diagnostics on **stderr**. Non-JSON stdout fails pending requests as `protocol_error`.
- Reverse RPC `session/request_permission` is cancelled, using the evaluator's existing policy.
- T3 live probes use workspace-scoped default permissions (not `full_access`).
- T3-01 asserts the host-visible **skills** catalog (`session/new` `_meta.skills` / `_list_skills`) because ACP has no `tools/list` for agent tools. T3-02/T3-03 exercise real `read_file` tool calls via `session/prompt` when live-opted-in.
- T2-03 kills the real process with an initialize request pending after handshake — never depends on a live LLM.
