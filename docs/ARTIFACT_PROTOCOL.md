# Artifact Protocol

How Box-Agent reports files produced during a session, and what the host
must do to render them.

## Contract in one paragraph

Every file the agent produces lands under `{workspace}/output/`. Box-Agent
sends one `tool_call_update` per artifact, with `rawOutput.type ==
"artifact"` as the only discriminator. The host listens for that type, reads
the structured payload, and renders or downloads the file from `rel_path` /
`uri`. Markdown links inside `agent_message_chunk` text are decoration only —
do not parse them as the source of truth for files.

## Wire format

`session/update` → `tool_call_update` → `rawOutput`:

```json
{
  "type": "artifact",
  "kind": "image",
  "filename": "chart.png",
  "rel_path": "output/chart.png",
  "abs_path": "/Users/me/ws/output/chart.png",
  "uri": "file:///Users/me/ws/output/chart.png",
  "mime": "image/png",
  "size": 12480,
  "sha256": "a1b2c3d4e5f60718",
  "produced_at": "2026-05-14T09:41:40+08:00",
  "tool_call_id": "call_xxx",
  "output_dir": "/Users/me/ws/output"
}
```

### Field reference

| Field          | Type           | Notes |
| -------------- | -------------- | ----- |
| `type`         | string, const  | Always `"artifact"`. Dispatch on this. |
| `kind`         | enum           | One of: `image`, `document`, `spreadsheet`, `presentation`, `data`, `code`, `archive`, `video`, `audio`, `file`. Use this to pick the renderer. `file` is the catch-all. |
| `filename`     | string         | Bare filename, e.g. `chart.png`. Display label. |
| `rel_path`     | string         | POSIX path relative to the session workspace, e.g. `output/chart.png`. **Prefer this** for any download / link generation; it's portable across host machines. |
| `abs_path`     | string         | Absolute filesystem path on the runtime machine. Only useful when host and runtime share a filesystem. |
| `uri`          | string         | `file://` URI. Convenient for `<img src>` / `<a href>` when the host is local. |
| `mime`         | string         | RFC-2046 MIME, e.g. `image/png`, `text/markdown`. Always present (`application/octet-stream` if unknown). |
| `size`         | integer        | Byte size. `-1` if unavailable. |
| `sha256`       | string         | First 16 hex chars of SHA-256. Stable cache/dedup key — same content ⇒ same hash. Empty when the file is too large to hash (>64 MB). |
| `produced_at`  | string (ISO-8601) | Timezone-aware timestamp of detection. |
| `tool_call_id` | string         | Tool call that produced the artifact. The same id appears on the `tool_call_update`, so the host already knows which call to attach this to. |
| `output_dir`   | string         | Absolute path of `{workspace}/output/` for this session. Useful when listing all artifacts in a panel. |

### Kinds → suggested renderers

| `kind`         | Render with                                |
| -------------- | ------------------------------------------ |
| `image`        | `<img>` preview, lightbox on click          |
| `video` `audio`| HTML5 `<video>` / `<audio>` with controls   |
| `document`     | Inline markdown / HTML / PDF preview        |
| `spreadsheet`  | "Open in Excel" CTA + sheet/row count chip  |
| `presentation` | Deck thumbnail + "Open in PowerPoint" CTA   |
| `data`         | Tabular preview (first N rows) for csv/tsv/json |
| `code`         | Syntax-highlighted code block               |
| `archive`      | "Download" CTA + listing of contents on hover |
| `file`         | Generic download chip                       |

## What the host needs to implement

### 1. Listen on `rawOutput.type`

Add a branch to your `tool_call_update` reducer:

```ts
function handleToolCallUpdate(update: ToolCallUpdate) {
  const ro = update.rawOutput;
  if (!ro || typeof ro !== "object") return;

  switch (ro.type) {
    case "artifact":      return upsertArtifact(update.toolCallId, ro);
    case "web_search":    return upsertWebSearch(update.toolCallId, ro);
    case "memory_search": return upsertMemorySearch(update.toolCallId, ro);
    case "sub_agent_progress": return appendSubAgentProgress(update.toolCallId, ro);
    default: return;
  }
}
```

### 2. Maintain a per-session artifact list

Key by `sha256` (or `rel_path` if hash is empty) so the same file delivered
twice in the same session collapses to one entry. New emissions of the same
path replace the previous metadata (size / produced_at can change after a
rewrite).

```ts
type ArtifactKey = string; // sha256 || rel_path
const artifacts = new Map<SessionId, Map<ArtifactKey, Artifact>>();
```

### 3. Resolve files for the UI

- **Local host (CLI / desktop)**: open `uri` directly, or read from
  `abs_path`.
- **Remote host (web)**: stream the file via your own download endpoint, e.g.
  `GET /api/sessions/:sid/files?rel_path=output/chart.png`. The endpoint
  must restrict reads to `output_dir` (which Box-Agent reports per-session).

### 4. Strip artifact references from the rendered transcript

The agent will reference files in markdown like `[chart](output/chart.png)`
and `![preview](output/chart.png)` for human readability. These are *not*
the dispatch path — they exist because users read them. The host should:

- Render the markdown as-is in the transcript bubble (so users see the
  reference inline), **and**
- Show the structured artifact in a dedicated panel / chip alongside the
  bubble (so users can preview, download, copy link).

Do not deduplicate the structured emission against the markdown link.

### 5. Empty session config

The host **does not need to send** anything special on `session/new`.
Box-Agent creates `{workspace}/output/` automatically when the session is
created and uses `params.cwd` (or `config.agent.workspace_dir`) as the
workspace root. If you want the artifact directory in a custom place,
override the workspace itself — `output/` always lives under it.

## Runtime capability discovery

`initialize._meta.runtime_capabilities` is a conservative startup snapshot and
may report Roadmap disabled while background Skill discovery is still running.
A host restoring persisted artifacts without creating a conversation session
can request the stable runtime snapshot through the read-only ACP extension
`_runtime/capabilities`. The request has no parameters and does not create a
workspace or session.

```json
{
  "contract": "box-agent.runtime-capabilities-query",
  "contractVersion": 1,
  "ready": true,
  "_meta": {
    "runtime_capabilities": {}
  }
}
```

The query waits for Skill discovery using the same bounded wait as
`session/new`. When the bound expires, `ready` is `false` and the returned
capabilities remain conservative. Hosts should fail closed, may retry later,
and must treat an `unknown_method` response from older runtimes as unsupported.
Capability snapshots belong to the current runtime connection and should not
be persisted with conversation history.

## Non-goals

- **No streaming chunks for artifact content.** The artifact payload is
  metadata only; the host fetches bytes from `uri` / `rel_path` on demand.
- **No artifact deletion events.** Files in `output/` are append-only from
  the agent's perspective. If the host deletes them, that's a host-side
  concern.
- **No legacy field aliases.** The wire schema is exactly the fields above.
  Older field names (`artifact_type`, `path`, `mime_type`, `size_bytes`,
  `sandbox_workspace`) are removed — update host code in lockstep with the
  Box-Agent release.

## Example: end-to-end

User asks for "a sales chart".  Agent runs `execute_code`, which writes
`output/sales-q3.png` and prints `Saved [sales-q3.png]`. Box-Agent sends:

1. `tool_call` (start) — `tool_call_id=call_42`, `tool_name=execute_code`.
2. `tool_call_update` — `status=completed`, `content=[…stdout…]`.
3. `tool_call_update` — `rawOutput.type="artifact"`, with the envelope shown
   above.
4. `agent_message_chunk` — `Here's the chart: ![sales-q3](output/sales-q3.png)`.

The host's reducer:

- Step 2 closes the tool call status.
- Step 3 appends `sales-q3.png` (kind=`image`) to the session's artifact
  panel; the chip shows `12.4 KB · output/sales-q3.png`.
- Step 4 renders the markdown image inline. The `<img>` `src` resolves the
  same file (host is free to substitute its own download URL for the relative
  path).

## Versioning

The schema is versioned by Box-Agent's PyPI release. Treat additive fields
(new `kind` values, new optional keys) as backwards-compatible; treat
renames or removals as breaking, in which case Box-Agent ships a major bump
and this document is updated in the same commit.
