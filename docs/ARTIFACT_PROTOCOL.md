# Artifact Protocol

How Box-Agent reports files produced during a session, and what the host
must do to render them.

## Contract in one paragraph

`session/new.params.cwd` is the session's stable working directory. Box-Agent
does not allocate a per-session output directory and never changes that cwd.
The shared system prompt selects task directories: use cwd when it is empty,
contains few files, or holds only this task's files; create an ordinary task
subdirectory when cwd contains many unrelated files. Multiple related outputs
alone do not require a new directory. Honor an explicit user destination.
This is file organization, not workspace state. Artifact discovery scans the
original cwd and also consumes explicit paths returned by tools. Box-Agent sends one
`tool_call_update` per artifact, with `rawOutput.type == "artifact"` as the
discriminator. Markdown links inside `agent_message_chunk` text are decoration
only—do not parse them as the source of truth for files.

Changed-file discovery requires complete snapshots both before and after tool
execution. If either scan exceeds its file/time limit or encounters a filesystem
access error, Box-Agent skips that diff instead of treating the failed scan as an
empty directory. Explicit file references and structured tool outputs remain
available even when diff-based discovery is skipped.

## Wire format

`session/update` → `tool_call_update` → `rawOutput`:

```json
{
  "type": "artifact",
  "kind": "image",
  "filename": "chart.png",
  "rel_path": "sales-analysis/chart.png",
  "abs_path": "/Users/me/ws/sales-analysis/chart.png",
  "uri": "file:///Users/me/ws/sales-analysis/chart.png",
  "mime": "image/png",
  "size": 12480,
  "sha256": "a1b2c3d4e5f60718",
  "produced_at": "2026-05-14T09:41:40+08:00",
  "tool_call_id": "call_xxx"
}
```

### Field reference

| Field          | Type           | Notes |
| -------------- | -------------- | ----- |
| `type`         | string, const  | Always `"artifact"`. Dispatch on this. |
| `kind`         | enum           | One of: `image`, `document`, `spreadsheet`, `presentation`, `data`, `code`, `archive`, `video`, `audio`, `file`. Use this to pick the renderer. `file` is the catch-all. |
| `filename`     | string         | Bare filename, e.g. `chart.png`. Display label. |
| `rel_path`     | string         | POSIX path relative to the session cwd, e.g. `sales-analysis/chart.png`. **Prefer this** for download/link generation when the artifact is inside cwd. |
| `abs_path`     | string         | Absolute filesystem path on the runtime machine. Only useful when host and runtime share a filesystem. |
| `uri`          | string         | `file://` URI. Convenient for `<img src>` / `<a href>` when the host is local. |
| `mime`         | string         | RFC-2046 MIME, e.g. `image/png`, `text/markdown`. Always present (`application/octet-stream` if unknown). |
| `size`         | integer        | Byte size. `-1` if unavailable. |
| `sha256`       | string         | First 16 hex chars of SHA-256. Stable cache/dedup key — same content ⇒ same hash. Empty when the file is too large to hash (>64 MB). |
| `produced_at`  | string (ISO-8601) | Timezone-aware timestamp of detection. |
| `tool_call_id` | string         | Tool call that produced the artifact. The same id appears on the `tool_call_update`, so the host already knows which call to attach this to. |
| `layout_id`    | string, optional | Controlled layout identifier. Present only for recognized artifacts such as `roadmap-swimlane-v1`. |
| `edit_mode`    | enum, optional | `editable` means the host may enable the trusted editor after its own artifact-identity checks. `read_only` means the host must keep editing and persistence disabled. Missing is read-only. |

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
  `GET /api/sessions/:sid/files?rel_path=sales-analysis/chart.png`. Resolve it
  against the host-owned session cwd and enforce the host filesystem policy.

### 4. Strip artifact references from the rendered transcript

The agent may reference files in markdown like
`[chart](sales-analysis/chart.png)` for human readability. These are *not*
the dispatch path — they exist because users read them. The host should:

- Render the markdown as-is in the transcript bubble (so users see the
  reference inline), **and**
- Show the structured artifact in a dedicated panel / chip alongside the
  bubble (so users can preview, download, copy link).

Do not deduplicate the structured emission against the markdown link.

### 5. Session directory contract

The host must pass the user-selected workspace as `session/new.params.cwd`.
Do not preallocate a separate directory per conversation. Box-Agent creates
`cwd` if it does not exist, then keeps it unchanged for the session lifetime.
The model alone decides whether a particular General-mode task benefits from a
child directory, unless the user names an explicit destination.

`_meta.workspace_layout.selected_root_dir` remains optional display metadata.
The following legacy inputs are accepted during the current compatibility
series, logged once per session as a warning, and ignored:

- `_meta.artifact_mode` / `_meta.artifactMode`
- `_meta.artifact_root` / `_meta.artifactRoot`
- `_meta.artifact_root_dir` / `_meta.artifactRootDir`
- `_meta.session_workspace_dir` / `_meta.sessionWorkspaceDir`
- `_meta.workspace_layout.artifact_root` / `artifactRoot`
- `_meta.workspace_layout.artifact_root_dir` / `artifactRootDir`
- `_meta.workspace_layout.session_workspace_dir` / `sessionWorkspaceDir`

They must not affect tool cwd, artifact scanning, prompts, or output paths.
Remove them from host integrations now; Box-Agent will remove the fields from
its accepted schema in the next major release. There is no output-directory
environment variable replacement.

## Roadmap protocol handling

Box-Agent validates the controlled Roadmap HTML and publishes a simple host
contract. A recognized artifact receives `layout_id=roadmap-swimlane-v1` and
`edit_mode=editable` when its safe structure and generator, artifact, schema,
geometry, and renderer versions are supported. A safe, recognizable artifact
with unsupported protocol versions receives `edit_mode=read_only`. Runtime JS
and CSS bytes are not compared, so formatting, comments, and compatible
same-version runtime changes do not disable editing. Hosts may run
a recognized renderer in an isolated sandbox to preserve responsive layout,
but must hide editing controls and reject persistence unless the mode is
`editable` and their own artifact identity, path, hash, and revision checks
pass. Missing/unknown modes are read-only. Unsupported protocol versions use
a script-free fallback. Malformed or unsafe controlled HTML receives no
`layout_id` and must be blocked by the host.

## Non-goals

- **No streaming chunks for artifact content.** The artifact payload is
  metadata only; the host fetches bytes from `uri` / `rel_path` on demand.
- **No artifact deletion events.** If the host deletes a delivered file,
  that's a host-side concern.
- **No legacy field aliases.** The wire schema is exactly the fields above.
  Older field names (`artifact_type`, `path`, `mime_type`, `size_bytes`,
  `sandbox_workspace`) are removed — update host code in lockstep with the
  Box-Agent release.

## Example: end-to-end

User asks for "a sales chart" in a cluttered workspace. The model creates
`sales-analysis/`, then `execute_code` writes `sales-analysis/sales-q3.png`.
Box-Agent sends:

1. `tool_call` (start) — `tool_call_id=call_42`, `tool_name=execute_code`.
2. `tool_call_update` — `status=completed`, `content=[…stdout…]`.
3. `tool_call_update` — `rawOutput.type="artifact"`, with the envelope shown
   above.
4. `agent_message_chunk` — `Here's the chart: ![sales-q3](sales-analysis/sales-q3.png)`.

The host's reducer:

- Step 2 closes the tool call status.
- Step 3 appends `sales-q3.png` (kind=`image`) to the session's artifact
  panel; the chip shows `12.4 KB · sales-analysis/sales-q3.png`.
- Step 4 renders the markdown image inline. The `<img>` `src` resolves the
  same file (host is free to substitute its own download URL for the relative
  path).

## Versioning

The schema is versioned by Box-Agent's PyPI release. Treat additive fields
(new `kind` values, new optional keys) as backwards-compatible; treat
renames or removals as breaking, in which case Box-Agent ships a major bump
and this document is updated in the same commit.
