# ACP Host-like probe (`tests/acp_host`)

Spawns a **real** `python -m box_agent.acp.server` subprocess over stdio JSON-RPC.
This is Host-like (officev3-style), not an in-process mock and not a fake JSON-RPC server.

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
under pytest `tmp_path` and copy `minimal_config.yaml` into it:

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

Expired hosted auth (`登录态已过期`, `HTTP 401`, `Authentication failed`, …) → **skip**
(environment, not product).

Default `uv run pytest tests/acp_host/ -q` stays deterministic (no tokens / network).

### Minimal live setup

```bash
# optional: copy example then edit
cp box_agent/config/config-example.yaml ~/.box-agent/config/config.yaml
# set api_key / api_base / model / provider

export BOX_AGENT_ACP_HOST_LIVE=1
uv run pytest tests/acp_host/ -q
```

Optional CLI:

```bash
uv run python scripts/acp_host_probe.py --cwd /tmp/acp-probe-ws
# --command uses shlex.split (quoted args preserved)
```

## Run

```bash
# Deterministic (isolated home, no live LLM)
uv run pytest tests/acp_host/ -q

# Optional no-key simulation (fake HOME, isolated profile only)
# covered by test_t1_01_isolated_home_without_real_user_config
```

## Notes

- Protocol NDJSON on **stdout**; diagnostics on **stderr**. Non-JSON stdout fails pending requests as `protocol_error`.
- Reverse RPC `session/request_permission` is auto-approved by the probe.
- T3 live probes use workspace-scoped default permissions (not `full_access`).
- T3-01 asserts the host-visible **skills** catalog (`session/new` `_meta.skills` / `_list_skills`) because ACP has no `tools/list` for agent tools. T3-02/T3-03 exercise real `read_file` tool calls via `session/prompt` when live-opted-in.
- T2-03 kills a deterministic pending request after handshake — never depends on a live LLM.
