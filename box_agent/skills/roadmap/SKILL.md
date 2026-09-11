---
name: roadmap
description: Create structured project roadmaps and schedule swimlanes from ordinary conversation, tables, images, or an existing RoadmapSpec. Use for multi-lane project schedules with roadmap, planning, swimlane, month-scale, or milestone semantics.
keywords: [roadmap, project roadmap, schedule roadmap, swimlane, roadmap artifact, 路线图, 项目路线图, 排期, 泳道, 月份刻度, 月度刻度, 半月刻度]
---

# Roadmap Skill

Use this skill for a project schedule whose meaning depends on lanes and dates.
The Roadmap contract is the source of truth, and controlled HTML is the only
normal delivery format in the current scope.

## Route decision

Use the Roadmap pipeline when the request combines at least two relevant
signals and includes either a lane/calendar signal or a roadmap plus
schedule/Gantt pair. Relevant signals include roadmap, schedule, swimlane,
month or half-month scale, milestone, and Gantt.

Do not route a plain process, step list, horizontal timeline, or ordinary
single Gantt table to Roadmap. Those keep their existing workflow unless the
user also asks for multi-lane or calendar-scale roadmap geometry.

The route does not depend on presentation or document workflows. A request for
a presentation alone must not select this skill.

## Contract and HTML Artifact workflow

1. Preserve extracted input as `RoadmapDraft v1`. Every lane and item retains
   its raw value, typed source (`natural-language`, `table`, `image`, or
   `roadmap-spec`), provenance coordinates where applicable, and confidence.
2. Compile Draft to `RoadmapSpec v1`. Missing dates are never invented; report
   a pending question. A present date below confidence `0.8` remains usable but
   becomes `certainty: tentative` and requires confirmation.
3. Persist timezone-free `YYYY-MM-DD` dates and half-open `[start, end)` bars.
   Milestones use `start` only. `certainty` and `progress` remain independent.
4. Migrate persisted specs through the explicit version boundary. Version 1
   is lossless; missing or unknown versions are rejected.
5. Generate renderer-neutral geometry. The HTML renderer consumes this IR
   instead of recalculating dates, tracks, collisions, labels, or continuation
   markers.
6. Render ordinary conversation requests to the controlled HTML Artifact
   `roadmap-swimlane-v1` without loading another document skill.

Resolve `{skill_dir}` to this skill's installed directory and `<ROADMAP_DIR>`
to the absolute task directory selected in the conversation, or the unchanged
session cwd when no task directory was selected. For ordinary generation,
create one temporary Draft input and run the unified builder. It
compiles, migrates, lays out, renders, and self-checks in one process. A
successful build consumes the temporary Draft and leaves only a versioned HTML
deliverable in `<ROADMAP_DIR>`.

Keep this run's generator scripts, temporary JSON, logs, and other disposable
support files in a unique task directory below `$BOX_AGENT_SCRATCH_DIR`.
Place the temporary Draft and any helper there. Pass that Draft to the builder
with `--consume-input`. The builder
removes the task scratch directory after either success or failure, and the
session runtime clears any residue at the end of the turn. Never place one
task's files directly in the scratch root or reuse another task's directory.
Before completing the task, verify that every file created by this Roadmap run
under `<ROADMAP_DIR>` is a versioned HTML deliverable; preserve pre-existing files.
Do not create an automatic `output/` directory.

Box-Agent's Bash tool has no `workspaceDir` argument. Prefix every builder
invocation with a directory change in the same tool call:

- POSIX / bundled Git Bash: `cd '<ROADMAP_DIR>' &&`
- PowerShell: `Set-Location -LiteralPath '<ROADMAP_DIR>' -ErrorAction Stop;`

Use literal single-quoted paths. On POSIX, encode an embedded apostrophe by
closing the string, inserting a double-quoted apostrophe, and reopening it
(`O'Brien` becomes `'O'"'"'Brien'`). On PowerShell, double an embedded apostrophe
(`O'Brien` becomes `'O''Brien'`). Preserve dollar signs, backticks, and brackets
as filename characters. The prefix changes only this command's cwd; repeat it
for later calls rather than assuming a previous `cd` persisted.

`write_file` does not expand shell environment variables. Before writing the
Draft, resolve its real absolute path with `bash`: create the task directory and
print the Draft path, then copy that returned path exactly into `write_file`.
Never guess a scratch path, substitute a home-directory path, or pass a literal
`$BOX_AGENT_SCRATCH_DIR/...` string to a file tool. The builder must receive the
same absolute path that `write_file` reported writing successfully.

```bash
ROADMAP_SKILL_DIR="${BOX_AGENT_ROADMAP_SKILL_DIR:-{skill_dir}}"
ROADMAP_DRAFT="$BOX_AGENT_SCRATCH_DIR/<task-id>/roadmap-draft.json"
mkdir -p "$(dirname "$ROADMAP_DRAFT")"
printf '%s\n' "$ROADMAP_DRAFT"
# Use the printed absolute path with write_file, then run:
cd '<ROADMAP_DIR>' && ${BOX_AGENT_NODE:-node} "$ROADMAP_SKILL_DIR/scripts/build_roadmap_artifact.js" "$ROADMAP_DRAFT" --out roadmap.html --consume-input
```

Always inspect the builder report. If `pending_questions` is non-empty, the
generated HTML is a preview rather than a confirmed final deliverable. Ask the
listed questions, update the source dates and set the affected items to
`certainty: confirmed` from the user's answer, and rebuild before presenting
the Roadmap as final. Pending questions are persisted inside controlled HTML
and remain active across follow-up versions until the corresponding tentative
items are confirmed.

The builder never overwrites HTML. A fresh `roadmap.html` request produces
`roadmap-v1.html`; later generations produce `roadmap-v2.html`,
`roadmap-v3.html`, and so on. A legacy unversioned `roadmap.html` counts as v1,
so the next build starts at `roadmap-v2.html`. Never delete, rename, or overwrite
an earlier Roadmap HTML to reuse its version number; do not run `rm`, `unlink`,
or equivalent cleanup commands during normal generation. Do not create adjacent Draft,
Spec, Geometry, extraction, or QA JSON files during normal delivery. Use
`--debug-dir DIR` only when the user explicitly requests debugging evidence.

For a generated follow-up version, pass the current HTML directly to the same
builder. It reads the embedded source and emits the next HTML version without
overwriting the input. Never prefer an older adjacent JSON file:

```bash
cd '<ROADMAP_DIR>' && ${BOX_AGENT_NODE:-node} "$ROADMAP_SKILL_DIR/scripts/build_roadmap_artifact.js" roadmap-v1.html --out roadmap.html
```

The renderer writes a standard `.html` deliverable in the caller-selected task
directory (the command's cwd), with `mime_type=text/html`,
`layout_id=roadmap-swimlane-v1`, embedded source in `#deck-document`, and
structured diagnostics. Mention the resulting HTML filename so the shared
artifact detector can publish it. The host renders the standard artifact event
as the single clickable workspace file card, so do not add a Markdown
`workspace-file` or `local-file` link for the same HTML in the final response.
Do not mention a versioned filename that was not returned by a successful
builder invocation.
Normal Roadmap outputs are confined to that command cwd; absolute paths and
`..` traversal outside it are rejected. Select another task directory with the
documented command prefix, not with an output-root environment variable.

The form/table editor changes RoadmapSpec fields and then invokes the same
contract validator and geometry core. It never edits pixel positions directly.
The embedded `#deck-document` is the persisted source of truth after save.

Schemas:

- `references/roadmap-draft.schema.json`
- `references/roadmap-spec.schema.json`

Examples:

- `examples/draft-natural-language.json`
- `examples/draft-table.json`
- `examples/draft-image.json`
- `examples/roadmap-spec-v1.json`
- `examples/roadmap-geometry-1440x900.json`
- `examples/capacity-cases.json`

Runtime resources:

- `runtime/registry.json`
- `runtime/roadmap.css`
- `runtime/roadmap-editor.js`

## Protocol boundary

Schema, geometry, renderer, preview, and form/table editing are version 1.
The generated HTML must carry the controlled Roadmap protocol markers and
embedded `#deck-document` source. The runtime publishes `edit_mode=editable`
when the safe structure and declared protocol versions are supported. Safe,
recognizable artifacts with unsupported versions publish `edit_mode=read_only`.
Runtime JS and CSS bytes are not compared. Hosts treat missing or unknown modes
as read-only.

The recommended limit is 6 months, 8 lanes, and 80 items. Structural contract
violations block rendering. Dense but valid content stays available with
structured visual-degradation diagnostics and a deterministic scroll layout.
