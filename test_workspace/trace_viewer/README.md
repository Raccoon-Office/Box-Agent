# Offline ACP Trace Viewer

FastAPI/Jinja2/HTMX viewer for `box-agent-acp-eval/v1` output. It binds to loopback by default. Remote viewing is intended only for a trusted network; no authentication or token is provided. In addition to reading results, the home page has one narrowly scoped mutation: launching the existing ACP evaluator from a RaccoonOps dataset.

## Start

From the Box-Agent repository root:

```bash
uv sync --project test_workspace/trace_viewer
uv run --project test_workspace/trace_viewer trace-viewer \
  --repo-root "$PWD" \
  --host 127.0.0.1 \
  --port 8000
```

The launch dialog reads query sets from RaccoonOps and tested-model profiles
from `test_workspace/evaluation_models.json`. The model catalog can be switched
without changing application code by setting `BOX_AGENT_EVAL_MODEL_CATALOG` to
another JSON file with the same schema. Configure the same-machine service
before startup:

```bash
export BOX_AGENT_OPS_URL=http://127.0.0.1:8080
export BOX_AGENT_OPS_PROJECT_KEY=office-raccoon
export BOX_AGENT_EVAL_MODEL_CATALOG=/path/to/evaluation_models.json  # optional
export BOX_AGENT_EFFECT_EVAL_URL=http://127.0.0.1:8766  # optional agents-eval effect service
export BOX_AGENT_EVAL_AUTH_FILE=/path/to/auth.json  # optional; defaults to desktop auth.json
```

The browser never receives attachment paths or credentials. The server copies
the selected Ops query set and task type into an isolated temporary dataset,
then calls `test_workspace/run_acp_eval.py` with the requested execution count
and serial ACP execution. Auto model profiles are resolved per task before the
ACP session is created. Runs continue to
land under `test_workspace/outputs/` and appear on the existing home page.
Datasets with attachments require explicit confirmation in the dialog. The server forwards that approval and rechecks the actual fetched attachments before copying or execution; stale option counts cannot bypass confirmation.

For a built-in hosted model, the server checks authentication before it fetches
the Ops query set. Every hosted launch uses the standalone `test_workspace/refresh_box_agent_auth.py` validator, including fresh tokens. It applies the same file size/symlink guards and refreshes tokens expiring within five minutes. The default path follows `BOX_AGENT_HOME/config/auth.json` when a profile is active, otherwise `~/.box-agent/config/auth.json`; `BOX_AGENT_EVAL_AUTH_FILE` remains an explicit override. Refresh failure stops before dataset materialization or output
creation. The helper accepts only the fixed refresh path on approved HTTPS
hosts, rejects redirects, never logs tokens, and atomically writes `auth.json`
with mode `0600`.

Open `http://127.0.0.1:8000/` locally. A deliberate `--host 0.0.0.0` allows remote viewing; starting evaluations remotely additionally requires `--allow-remote-evaluations` and a trusted, access-controlled deployment. Cross-origin launch requests are rejected. Raw HTML in Markdown is displayed as text, and downloaded artifacts carry a sandbox policy so untrusted reports cannot become launch controls.

The data root is always `<repo-root>/test_workspace/outputs/`. There is no alternate output-root setting, legacy-format adapter, authentication, or redaction layer. The launch endpoint accepts only dataset and model identifiers returned by the configured Ops service; it does not accept commands, arbitrary URLs, paths, or credentials.

## Pages

- Evaluation directory list
- Run detail table with task type, result/process scores, auditable Agent Token
  cost, ACP/completeness status, timing, and compact stderr counts
- Evaluation launch dialog with task-type filtering, Ops dataset, execution
  count, and tested-model selection
- Case list with search and stderr category counts
- Case overview with task input and final answer
- Unified timeline
- Independent Agent, ACP, process/stderr, diagnosis, and file pages
- Effect metrics with evidence-bound scores, performance timing, cost, and
  explicit unavailable-metric reasons from optional `effect_evaluation.json`

Record pages start at the earliest event and paginate only between complete records.

An optional case-level diagnosis is read from `cases/<case-id>/diagnosis.md`.
The viewer makes no assumptions about its Markdown structure. If the file does
not exist, the diagnosis page shows an empty state.

Effect evaluation is attempt-scoped and read from
`attempts/<attempt-id>/effect_evaluation.json`. Existing attempts without this
file remain readable and show an empty state; `service_error` documents display
the local service error without changing the underlying Case status.

Incomplete, malformed, or explicitly mismatched effect results are shown as unavailable diagnostics with scores hidden. Viewing does not rewrite the stored response or the original ACP/completeness result; missing optional confidence and evidence remain readable.
