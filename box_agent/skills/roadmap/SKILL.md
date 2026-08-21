---
name: roadmap
description: Create structured project roadmaps and schedule swimlanes from ordinary conversation, tables, images, or an existing RoadmapSpec. Use for multi-lane project schedules with roadmap, planning, swimlane, month-scale, or milestone semantics.
keywords: [roadmap, project roadmap, schedule roadmap, swimlane, roadmap artifact, 路线图, 项目路线图, 排期, 泳道, 月份刻度, 月度刻度, 半月刻度]
capabilities: [roadmap.contract, roadmap.generate.html, roadmap.preview, roadmap.edit]
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

Resolve `{skill_dir}` to this skill's installed directory. For ordinary
generation, create one temporary Draft input and run the unified builder. It
compiles, migrates, lays out, renders, and self-checks in one process. A
successful build consumes the temporary Draft and leaves only a versioned HTML
deliverable in `output/`:

Treat `output/` as a deliverables-only boundary. Never create generator scripts,
temporary JSON, logs, or other support files there. Create a unique task
directory below `$BOX_AGENT_SCRATCH_DIR` and place the temporary Draft and any
helper there. Pass that Draft to the builder with `--consume-input`. The builder
removes the task scratch directory after either success or failure, and the
session runtime clears any residue at the end of the turn. Never place one
task's files directly in the scratch root or reuse another task's directory.
Before completing the task, verify that every file created by this Roadmap run
under `output/` is a versioned HTML deliverable.

```bash
ROADMAP_SKILL_DIR="${BOX_AGENT_ROADMAP_SKILL_DIR:-{skill_dir}}"
ROADMAP_DRAFT="$BOX_AGENT_SCRATCH_DIR/<task-id>/roadmap-draft.json"
${BOX_AGENT_NODE:-node} "$ROADMAP_SKILL_DIR/scripts/build_roadmap_artifact.js" "$ROADMAP_DRAFT" --out roadmap.html --consume-input
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
${BOX_AGENT_NODE:-node} "$ROADMAP_SKILL_DIR/scripts/build_roadmap_artifact.js" roadmap-v1.html --out roadmap.html
```

The renderer writes a standard `.html` deliverable under
`$BOX_AGENT_OUTPUT_DIR`, with `mime_type=text/html`,
`layout_id=roadmap-swimlane-v1`, embedded source in `#deck-document`, and
structured diagnostics. Mention the resulting HTML filename so the shared
artifact detector can publish it. The host renders the standard artifact event
as the single clickable workspace file card, so do not add a Markdown
`workspace-file` or `local-file` link for the same HTML in the final response.
Do not mention a versioned filename that was not returned by a successful
builder invocation.
When `$BOX_AGENT_OUTPUT_DIR` is configured, normal Roadmap outputs are confined
to that directory; absolute paths and `..` traversal outside it are rejected.

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

## Capability boundary

Schema, geometry, renderer, preview, and form/table editing are version 1.
Do not claim Roadmap HTML generation, preview, or editing until the corresponding
`roadmap.generate.html`, `roadmap.preview`, or `roadmap.edit` capability is
present. Missing capabilities are disabled, not inferred from bundled files.
The ACP runtime advertises these capabilities only when Skills are enabled, the
Roadmap resources are complete, and a managed Node runtime is available.

The recommended limit is 6 months, 8 lanes, and 80 items. Structural contract
violations block rendering. Dense but valid content stays available with
structured visual-degradation diagnostics and a deterministic scroll layout.

The frozen runtime payload and schema are:

- `references/runtime-capabilities-v1.json`
- `references/runtime-capabilities-v1.schema.json`
