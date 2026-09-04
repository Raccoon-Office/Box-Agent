# Offline ACP evaluation capture

`acp-eval` runs the existing Box-Agent ACP server without changing Box-Agent
code. Each selected dataset case is captured as a self-contained
`box-agent-acp-eval/v1` evidence package for post-run diagnosis.

## Setup and commands

Prepare both the Box-Agent runtime and this standalone runner from the repository
root:

```bash
uv sync
uv sync --project test_workspace/acp_eval
```

Run a local full JSONL dataset (the dataset and its input files are not committed):

```bash
uv run --project test_workspace/acp_eval acp-eval \
  --repo-root . \
  --dataset test_workspace/inputs/hermes_antilia_v2/dataset.jsonl \
  --run-dir test_workspace/outputs/260821-acp \
  --timeout-seconds 2700 \
  --parallelism 4
```

Run an exact subset by repeating `--case-id`:

```bash
uv run --project test_workspace/acp_eval acp-eval \
  --repo-root . \
  --dataset test_workspace/inputs/hermes_antilia_v2/dataset.jsonl \
  --run-dir test_workspace/outputs/260821-acp \
  --case-id Q5 \
  --case-id Q36
```

By default, a case whose latest attempt has a terminal manifest is skipped only
when its stored case fingerprint exactly matches the current record and input
bytes, and when its stored producing runtime has the same comparable Python
version/implementation, Box-Agent version, and Box-Agent Git commit. If any
runtime identity source is unavailable, resume is conservative and executes a
new attempt. Changing the query, any other record field, an input path, or an
input file's bytes also creates a new immutable attempt automatically. Request
a new attempt even when the fingerprint and runtime are unchanged with:

```bash
uv run --project test_workspace/acp_eval acp-eval \
  --repo-root . \
  --dataset test_workspace/inputs/hermes_antilia_v2/dataset.jsonl \
  --run-dir test_workspace/outputs/260821-acp \
  --case-id Q36 \
  --retry-terminal
```

The dataset is newline-delimited JSON. Every non-empty line must be an object
with a direct-directory `id`, a non-empty string `query`, and an `input_files`
list. Input paths are relative to the dataset directory and must resolve to
regular files inside it. The runner validates the complete dataset and exact
case selection before creating a run directory.

## v1 evidence layout

```text
test_workspace/outputs/<evaluation>/
├── manifest.json
├── summary.json
└── cases/<case-id>/
    ├── input.json
    ├── latest.json
    └── attempts/<attempt-id>/
        ├── manifest.json
        ├── workspace/
        ├── protocol.jsonl
        ├── acp-stdin.raw
        ├── acp-stdout.raw
        ├── stderr.log
        ├── process.jsonl
        ├── agent/*.jsonl
        ├── assistant.txt
        ├── run.json
        ├── files-before.json
        ├── files-after.json
        ├── artifacts.json
        ├── completeness.json
        └── effect_evaluation.json  # optional agents-eval response
```

`latest.json` atomically points to the latest attempt using its ID and a path
relative to the case directory. Attempts are never overwritten or appended by
a retry. `summary.json` separates ACP outcome from collection completeness and
reports `error`, `timeout`, and `warning` stderr counts.

The root manifest stores a deterministic dataset fingerprint. Every attempt's
`run.json` stores its case fingerprint, comprising the canonical JSON record
hash plus the SHA-256 and relative path of every referenced input file. The
attempt fingerprint is derived from `files-before.json`, so its input hashes
describe the bytes actually copied into the evaluated workspace. If those
hashes differ from the source fingerprint computed before execution, the
attempt is preserved as `corrupt`, the mismatch is explicit in `run.json` and
`completeness.json`, and `latest.json` is not advanced. Resume never trusts
record IDs alone. An unsafe `latest.json`, a traversal, or a symlink in the
case/attempt index is reported as an indexing failure and is not followed.

The root manifest, summary, every per-case summary entry, and each indexed
attempt's `run.json` also retain the producing Python executable, version and
implementation plus the Box-Agent package version and Git commit. Per-case
runtime fields remain the attempt's producing identity even when the current
batch invocation uses a different executable. If version metadata or Git is
unavailable, the value is `null` and its adjacent status is `unavailable`; the
runner does not invent an identity. These fields remain usable after the
evaluation directory is copied away from the source checkout.

There is deliberately no compatibility layer for older evaluation layouts.
Delete obsolete output directories and rerun them with this runner.

## Optional synchronous effect evaluation

Set `BOX_AGENT_EFFECT_EVAL_URL` or pass `--effect-eval-url` to call an
independently running agents-eval service after each newly executed Case has
already reached its original terminal state:

```bash
BOX_AGENT_EFFECT_EVAL_URL=http://127.0.0.1:8766 \
uv run --project test_workspace/acp_eval acp-eval \
  --repo-root . \
  --dataset test_workspace/inputs/smoke_test/dataset.jsonl \
  --run-dir test_workspace/outputs/260826-effect \
  --effect-eval-timeout-seconds 180
```

Only the absolute Attempt path, Case ID, Attempt ID, and optional dataset
`benchmark_case_id` are sent to the local service. No Judge credentials are
accepted or forwarded by this runner. A service failure is recorded separately
and never changes the ACP or evidence-completeness outcome.

The optional `--model` and `--model-max-tokens` flags bind the tested model on
every ACP `session/new`. They select the product model under test; they do not
configure or expose the independent agents-eval Judge model.

## Diagnostic interpretation and data sensitivity

Raw files are evidence, not sanitized exports. `acp-stdin.raw`,
`acp-stdout.raw`, `protocol.jsonl`, Agent traces, stderr, prompts, tool
arguments, model output, and copied workspace files can contain credentials,
private data, or other sensitive content. Store and share an evaluation
directory only within the trusted network and according to the source data's
handling requirements.

`process_exit_code` and `acp_status` describe different facts. In particular,
an exit code of `-15` can mean that the capture runner sent SIGTERM after it had
already received the final ACP response; `-15` alone does not make the ACP task
a failure. Inspect `process.jsonl` for the signal initiator and reason.

`completeness.json` assesses whether emitted evidence was captured reliably. It
does not judge the Agent's reasoning, answer, or artifacts. Provider-internal
retries and cleanup steps that Box-Agent never emits remain unsupported rather
than being reconstructed.
