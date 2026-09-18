# ACP Host-like probe (`tests/acp_host`)

Spawns a **real** `python -m box_agent.acp.server` subprocess over stdio JSON-RPC.
This is Host-like (officev3-style), not an in-process mock and not a fake JSON-RPC server.

## Layout

| Path | Role |
|------|------|
| `probe.py` | `AcpHostProbe`, `RpcError`, `CaseResult` |
| `issue_draft.py` | T5 `to_issue_draft` |
| `test_t1_connect.py` | T1-01 / T1-02 (no LLM) |
| `test_t2_session.py` | T2-01 / T2-02 / T2-03 |
| `test_t3_tools.py` | T3-01 / T3-02 / T3-03 |

Product code under `box_agent/acp/` is **not** patched by this harness. Failures produce an `IssueDraft` only.

## Config / LLM

There is **no** mock-LLM injection path for the ACP subprocess today. Tests that need a model (T2-01, T2-02, T3-02, T3-03) look for a real config:

1. `$BOX_AGENT_HOME/config/config.yaml` (if set)
2. `box_agent/config/config.yaml` (dev tree)
3. `~/.box-agent/config/config.yaml`

Requirements:

- `api_key` set to a real value, **or**
- `auth.json` next to `config.yaml` (hosted officev3 / xiaohuanxiong refresh flow)

If config is missing or still a placeholder **and** there is no `auth.json`, those cases **skip** with a clear reason.

If config exists but hosted auth is expired (`登录态已过期` / HTTP 401 on refresh), LLM cases also **skip** — that is an environment failure, not a product ACP bug.

**Missing config is an environment failure (T1-02 class), not a product ACP bug.**

T1-01 / T1-02 / T3-01 / T2-03 run without calling the LLM (initialize, session/new, catalog, kill).

### Minimal setup

```bash
# optional: copy example then edit
cp box_agent/config/config-example.yaml ~/.box-agent/config/config.yaml
# set api_key / api_base / model / provider

# or rely on an existing ~/.box-agent install used by officev3
```

Optional CLI:

```bash
uv run python scripts/acp_host_probe.py --cwd /tmp/acp-probe-ws
```

## Run

```bash
uv run pytest tests/acp_host/ -q
```

## Notes

- Protocol NDJSON on **stdout**; diagnostics on **stderr**.
- Reverse RPC `session/request_permission` is auto-approved by the probe.
- T3-01 asserts the host-visible **skills** catalog (`session/new` `_meta.skills` / `_list_skills`) because ACP has no `tools/list` for agent tools. T3-02/T3-03 exercise real `read_file` tool calls via `session/prompt`.
