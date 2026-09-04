# Offline ACP Trace Viewer

FastAPI/Jinja2/HTMX viewer for `box-agent-acp-eval/v1` output. It is intended for a trusted local network and has no authentication or token. In addition to reading results, the home page has one narrowly scoped mutation: launching the existing ACP evaluator from a RaccoonOps dataset.

## Start

From the Box-Agent repository root:

```bash
uv sync --project test_workspace/trace_viewer
uv run --project test_workspace/trace_viewer trace-viewer \
  --repo-root "$PWD" \
  --host 0.0.0.0 \
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
Datasets with attachments require explicit confirmation in the dialog.

For a built-in hosted model, the server checks authentication before it fetches
the Ops query set. An access token expiring within five minutes is refreshed by
the standalone `test_workspace/refresh_box_agent_auth.py` helper and re-read
before launch. Refresh failure stops before dataset materialization or output
creation. The helper accepts only the fixed refresh path on approved HTTPS
hosts, rejects redirects, never logs tokens, and atomically writes `auth.json`
with mode `0600`.

Open `http://<machine-ip>:8000/` from the local machine or a trusted LAN peer.

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
