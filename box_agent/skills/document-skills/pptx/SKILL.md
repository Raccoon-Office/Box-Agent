---
name: ppt-fast
displayName: 快速模式
description: Create, inspect, edit, validate, render, and QA presentation decks. Use when the user mentions PowerPoint, PPT, PPTX, HTML deck, slide deck, presentation, template slides, speaker notes, slide images, or asks to read, generate, create, make, design, or modify a presentation artifact. New decks default to controlled, editable HTML delivery; PPTX is an explicit optional export.
keywords: [ppt, pptx, slide, slides, deck, presentation, powerpoint, pitch deck, speaker notes, ppt制作, 做ppt, 可编辑ppt, 幻灯片, 演示文稿, 投影片, 演示, 宣讲, 汇报材料, 路演, 路演材料, 融资路演, 商业计划书, BP, 提案, 讲稿, 模板页, 路演ppt]
capabilities: [presentation.authoring]
related_skills: [html-templates]
metadata:
  user_visible: false
  allow_override: false
---

# PPT Fast Skill

Create controlled, editable HTML presentations. Export `.pptx` only when explicitly
requested. A request for a finished PPT includes pages, not just an outline, unless
it explicitly says “只要大纲/内容方案” or “不要生成页面”. Existing PPTX/template edits
preserve the original file structure; `python-pptx` must not create a new deck.

## Responsibilities

The main agent owns research, factual content, the outline, content filling, media
acquisition and delivery. An isolated design role owns the registered theme preset,
complete role-based palette, structured visual requirements, slide layouts and their visual options. Programs validate and
render. Do not select themes, composition families or whole-deck variants in the
main agent, and do not load the complete design catalog into its context.

The designer's instructions are in `references/design-role.md`. They are inputs
for the isolated role, not another full Skill to preload into the main agent.
Programs match theme visual traits independently of its original colors; an allowed
no-match result uses plain-neutral and records the relaxed features. Report that
fallback in delivery. Explicit user constraints must not be silently relaxed.
Themes and all existing HTML layout controls remain available. New designs have
no seed. Human edits always supersede an AI proposal; the content-patch lock must
never be applied to the HTML editor's user actions.

### Presentation directory contract

Resolve one absolute `<PRESENTATION_DIR>` from the current conversation before
reading or writing deck files. If the model created a task directory, use it;
otherwise use the unchanged session cwd. This directory is task organization,
not a new workspace, and it must never come from a legacy host output-root
setting.

Pass this absolute directory as `workspaceDir` only when the invoking tool
actually supports that argument. Box-Agent's Bash tool has no `workspaceDir`:
POSIX commands using relative deck paths must start with
`cd '<PRESENTATION_DIR>' &&`; PowerShell uses
`Set-Location -LiteralPath '<PRESENTATION_DIR>' -ErrorAction Stop;`.
PPT and deep research must use the same
directory for a research-backed deck. Use the literal path quoting rules below
for every directory prefix. Keep private build, `qa/`, `assets/`, and
final output paths inside it; do not add an automatic `output/` layer.

## New controlled deck

### 1. Content and outline

Bundled Skill files are read-only runtime code. Do not edit, replace, copy-and-patch,
or disable their scripts to make a presentation pass. Keep task writes in the
presentation directory; preserve completed artifacts and report a program failure.

Read `references/outline.md` when planning a new deck. Keep near-final page copy,
all required facts, units, counts, relationships and explicit user constraints.
The designer must not invent missing content to fit a visual.
Keep short source material short: do not repeat the same claim in a card title,
body and bullet just to fill space. Choose a layout with suitable capacity.
Do not compare different units on a shared numeric axis; use separate chart panels
or clearly labeled growth rates derived from the supplied values.

- Supplied page content is the source of truth; preserve its intended ordering.
- Proposed architectures/solutions with sufficient goals and components use
  `source_mode=user_provided`; they do not require research by default.
- Missing external/current/company/market facts use `research-synthesis` when
  available. Follow its selected research budget and use only the validated
  `presentation_handoff.verified_facts[].canonical` as researched facts. Do not
  ask again for permission to use public sources already authorized by the task.
- Preserve strict/private source boundaries. Omit optional unsupported claims;
  disclose required public gaps as `暂无可验证公开数据`, and private gaps as
  `待补充` or `待客户确认`. Assumptions require explicit user authorization.
- Ask one focused structural question only when competing audience/scope choices
  materially change the requested result. Do not ask about internal tooling.
- User-selected page counts are hard; suggested ranges are only planning hints.
  Derive an unspecified count from the material, never an arbitrary midpoint.

Do not use `request_user_input` for a missing fact. Missing case metrics, quote
amounts, contact names and other required private facts use disclosed placeholders
without pausing delivery. Source-bound decks never invent named clients, team
facts, awards or rates such as “复购率持续提升”. Never create a fake bitmap with
Pillow and claim it came from image generation. Do not convert visual styling
language into required semantic fields; corner labels are not `--require-field 1:tags`.

Removing numbers or rewriting a claim as qualitative prose does not verify it.
For framework research pages, the exact unavailable-data placeholder must appear in `message` or `bullets`.

Write `outline.json`. Preserve explicit theme selections in
`design_requirements.theme_id`, and exact palette/style/geometry wording in the
same design requirements. Put explicit per-page geometry/count requirements in
that page's `hard_requirements`, quoted verbatim from the real user request.
Do not put your own aesthetic choices there: outline layout/visual are suggestions
for the designer, and changing only those hints does not reset the design budget.
These constraints must be updated when the user changes
them. Do not add unrelated history or synthetic continuation text as user facts.

### 2. Prepare the isolated design input

All shell examples below run in the presentation directory using the directory
prefix above and the loader-expanded script paths.

```bash
${BOX_AGENT_NODE:-node} scripts/design_plan.js prepare outline.json --out design_input.json
```

For a research-backed outline, add `--research-handoff research/qa/<topic>_research_check.json`.
This command runs the outline validator and writes `qa/outline_check.json`. A
failure returns the named issues for a content correction; do not bypass it.
An optional `--title` sets the deck title. It writes the complete designer input
to bounded brief/index files, with theme/layout details available on demand.
Stdout contains only paths and reuse status. The metadata file is not the designer brief.

If `reusable: true`, reuse `design_plan.json` without another design call. If the
existing `deck.json` is already scaffolded, continue its content/finalization work;
never scaffold over it. For a content-only update that fits the existing fields,
patch it directly without rerunning design preparation. Re-enter design only for
changed design requirements or incompatible content shape/capacity. A saved HTML
with user edits is authoritative: preserve it and its embedded `#deck-document`;
never restore a stale sibling JSON over those edits.

### 3. Delegate and import the independent response

Call `sub_agent` with `required_tools: ["read_file", "search_files"]` and
`budget: {"max_steps": 24, "max_tool_calls": 48}`. Design is a critical stage;
do not impose a short whole-task timeout such as 180 seconds on this workflow.
Omit `files` to keep the
real tool-capable loop. Put the exact `designer_brief` absolute path returned by
prepare and the loader-expanded `references/design-role.md` path in the task.
Ask the role to read both and return theme/layout/visual choices plus explicit
background/text/primary/accent/secondary color values and accent_usage. User
colors remain locked per role; the designer fills missing values. Without user
colors it selects and states the complete palette from the brief and theme.
Both paths compile into the same frozen palette contract; the main agent must
not substitute colors after acceptance. The brief
contains compact indices; detailed theme/layout contracts are small linked files.
Do not read the catalog or make visual choices in the main agent.

The brief lists bounded content packets and theme/layout indices. Tell the role
to read all listed packets before selecting, shortlist up to three themes, then
read only chosen theme/layout details. Reserve the final steps for producing the
decision; more catalog searching is not useful once compatible choices are found.
The role returns a response, not a complete deck plan. Never write or repair
`design_plan.json` yourself. Import the completed child's actual Session Log:

```bash
${BOX_AGENT_NODE:-node} scripts/design_plan.js accept design_input.json
```

The importer finds only completed child sessions that read this exact brief in
this workspace. It binds version, hashes, ordered page numbers, and content
mappings, then validates the real choices and generates the canonical plan.
Manually modified plans or responses from another input/workspace are rejected.
No model needs to copy hashes, page IDs, or JSON content-reference paths.

Prepare first creates `fallback.html` and, when no existing HTML exists, `index.html`:
a registered-layout presentation rendered by the normal renderer and full editor,
containing the supplied outline before checking it.
If outline/research validation fails, it returns degraded delivery with unverified
content clearly reported; do not repeat research or validator debugging indefinitely. This is the delivery
floor, not a completed visual review. Keep it until the normal deck is rendered.
If `can_retry: true`, send the same brief path and `correction_file` to the designer
once with budget {"max_steps": 16, "max_tool_calls": 24}. This is a fresh role;
read `correction_file` and follow its `requires_full_read` value:

- `true`: read `brief_file` and its required packets using the design role's
  reading procedure, then return a complete decision object. The previous
  response is not a usable decision to patch; do not replace these reads with
  main-agent choices copied into the task.
- `false`: the correction file is sufficient. Return only its named fields as a
  JSON patch; the program preserves the rest. Do not reread all packets or the
  full catalog, or recreate the deck.

Run accept again. On `status: degraded`, design authoring is finished: do not
call `inspect_deck_contract`, `apply_deck_patch`, `finalize_controlled_deck`, edit
`deck.json`, delete slides, or start another design call. Continue the requested
format delivery from `primary_artifact` as described below. Missing,
malformed or still-invalid designer output must not leave the user without a deck.
Recovery uses plain-neutral with registered cover/cards/closing layouts and the
normal deck schema, renderer, playback, layout editor, save and export controls.
Never substitute a separate text-only HTML implementation.
Extra pages are tolerated in the fallback by splitting existing outline content;
never invent content or facts to fill them. Do not start a third design call or
alter input just to reset the attempt count. If later compilation fails, use the
existing fallback HTML with its report. Images or unavailable vision must never
prevent delivery of the files already available.

#### Finish delivery after design failure

`degraded` / `terminal` ends design retries, not outstanding format delivery.
Read `qa/design_delivery.json`; keep its actual `primary_artifact` HTML unchanged.
For HTML-only requests, deliver that file with the report. When PPTX is required,
run the existing exporter on that same file, using its absolute path in place of
`<PRIMARY_ARTIFACT>`:

```bash
${BOX_AGENT_NODE:-node} scripts/check_html_export_env.js
${BOX_AGENT_NODE:-node} scripts/html_to_editable_pptx.js '<PRIMARY_ARTIFACT>' output.pptx
${BOX_AGENT_PYTHON:-python} scripts/validate_pptx_package.py output.pptx
```

Then extract text and check actual page count/order, required content and picture
objects. Explain the design/content limitations and any page-count difference
from the request; fallback pagination can add pages to retain supplied content.
Claim independently replaceable images only for actual picture objects, not
shapes or artwork baked into a page background. Package validity alone does not
prove those content/editability requirements.

An actual export error should be repaired within the existing dependency/export
rules and retried. If it remains blocked, deliver the existing HTML and report
the missing PPTX as incomplete. Failed design validation does not authorize a
fresh HTML implementation, a new python-pptx deck or a switch to the escape route.

### 4. Scaffold the validated plan once

```bash
${BOX_AGENT_NODE:-node} scripts/inspect_deck_contract.js --design-plan design_plan.json --design-input design_input.json --outline outline.json --out deck.json
```

Use `cd '<PRESENTATION_DIR>' && ${BOX_AGENT_NODE:-node}` on that same line;
do not split `cd` and the inspector across lines.
The inspector must be the only command after the literal directory prefix: no
pipe, redirection, `tail`, or additional diagnostic command. It rejects attempts
to override the plan with theme/family/layout flags, and returns concrete design
errors rather than silently replacing a layout or splitting pages. It writes
`deck.json`, `assets/generated/manifest.json`, and `qa/deck_contract.json`.
The stdout is the selected **content** contract: fields, defaults, source pages,
content bindings and media slots. Reuse it; do not inspect every layout again.
Use `--fact` for verbatim user facts, `--research-fact` only after research, and
`--assumption` only for explicitly authorized assumptions. Use `--require-field`
only for user-required semantic data fields, never decoration.

### 5. Acquire media and fill content

Read `references/image-assets.md` for image acquisition. Follow the scaffolded
`acquire_via`: `user` keeps localized supplied assets; `web` uses image search;
`ai` uses generation; `none` stays image-free. Web rows use the exact search query
with `SearchType: "image"` and `Count: 5`, once per unique query. Localize selected
receipts with `scripts/localize_web_image.py`. Only after `exhausted` or
`unavailable` may a web job use its labelled AI-concept fallback. Never fabricate
a documentary image of a real subject. Call `generate_image` with
`watermark: false` and `publish_artifact: false`; use the manifest entry's
`output_path` verbatim as the tool's `output_path` (never invent a short alias such
as `cover-hero.png`). Independent jobs may share a tool batch. Generation success
is not insertion proof: the final manifest must bind the asset to the actual page
and field.

Then synchronize once with one tightly scoped Bash-tool command. A missing optional
generated asset is recorded as deferred and does not stop HTML delivery; an
explicitly required asset remains an incomplete-delivery finding. Use the exact
platform-specific directory prefix below; do not pass `workspaceDir` to the
Box-Agent Bash tool. Do not use a legacy output
environment variable in either the directory or manifest path. Copy the exact
loader-expanded absolute path shown for
`scripts/sync_image_manifest_status.js`; do not construct a path from a known
installation root. Quote that native path because any platform's installation
directory may contain spaces.

- macOS/Linux POSIX shells and the bundled Git Bash on Windows:
  `cd '<PRESENTATION_DIR>' && "$BOX_AGENT_NODE" '<LOADER_EXPANDED_SYNC_SCRIPT>' assets/generated/manifest.json`
- Windows PowerShell fallback when the Bash tool identifies itself as
  PowerShell:
  `Set-Location -LiteralPath '<PRESENTATION_DIR>' -ErrorAction Stop; & "$env:BOX_AGENT_NODE" '<LOADER_EXPANDED_SYNC_SCRIPT>' 'assets/generated/manifest.json'`

POSIX paths use literal single-quoted strings so dollar signs and backticks
remain filename characters. For an embedded apostrophe, close the string,
insert a double-quoted apostrophe, and reopen it: `O'Brien` becomes
`'O'"'"'Brien'`. Do not use double-quoted path literals containing `$` or
backticks. Keep `"$BOX_AGENT_NODE"` as shown to expand the trusted runtime variable.

PowerShell paths also use literal single-quoted strings: double any embedded
apostrophe (`O'Brien` becomes `O''Brien`), and preserve backslashes, dollar
signs, brackets, and backticks unchanged. Do not replace `-LiteralPath` with
`-Path` or omit `-ErrorAction Stop`; a failed directory switch must stop before
the synchronizer runs. Do not use typographic quotation marks in these commands.

The manifest argument above is the same literal presentation-directory-relative path on every
platform. The loader-expanded script path is native—normally `/...` on
macOS/Linux and a drive-qualified path such as `C:\\...` on Windows—and must be
copied exactly from the loaded skill. The synchronization invocation must be
the only command apart from the exact platform-specific directory prefix: do not add
`2>&1`, `echo`, another `;` or `&&`, pipes, redirects, command substitutions,
wrappers, aliases, or extra arguments. Read failure details directly from the
Bash tool result; never work around a rejection by manually editing the
manifest. Do not reread those files when their current contents are already in
context. For a routine
deck of roughly 12 slides or fewer, do not call
`plan_write` or `todo_write`; avoid micro-turns that only update coordination.

| Operation | Command convention |
| --- | --- |
| Sync generated image statuses | Use the platform-specific standalone example above, including its literal directory prefix. |

Write one `deck.patch.json` when it fits, with this exact envelope:

```json
{"slides":{"slide-01":{"props":{"title":"Page title"}}}}
```

Run `scripts/apply_deck_patch.js deck.json deck.patch.json`. Fill only the returned
content fields. The program rejects changes to locked visual enums, including
page `composition`, local `variant`, chart style/type and media-side controls.
Never rewrite full `deck.json`/manifest with file tools, Python, or ad-hoc shell.
Use ordered write chunks only after explicit output-length recovery, never guess
an accepted chunk index, and restart from durable files after session recovery.

For charts, every included series must contain a real numeric value for every
category. Never pad a gap with zero, placeholders, or an invented baseline/forecast.

Keep source names short in `source`; put conclusions in the proper content field.
Do not invent chart values or fill missing numeric cells with zero/placeholders.
Use a complete supported subset and preserve isolated facts in another content
field; incompatible data shape goes back to the design role. Keep optional copy
empty when the user supplied no text. The patch compiler binds ready media;
full-slide `background` is a slide field, never `props.background`.

Explicit image-rich briefs may activate `creative_image_mode`; at least one real
generated asset must be referenced for an image-complete result. If all required
generation fails, deliver structurally usable degraded HTML and accurately report
the unmet image requirement. Ordinary factual topics do not imply creative mode.

### 6. Finalize and deliver

```bash
${BOX_AGENT_NODE:-node} scripts/finalize_controlled_deck.js deck.json --out index.html
```

This is the single normal finalization command. Core schema/design-contract and
render failures block. Outline binding drift blocks by default. Only an explicit
`BOX_AGENT_ALLOW_DEGRADED_OUTLINE_BINDING=1` permits that semantic-only draft.
Image, post-render HTML/runtime and source findings are recorded as advisories;
retain usable HTML and state the actual impact instead of starting repair loops.
An actual frozen-palette mismatch is a design-contract failure, not an optional
visual review: report the concrete component/property from QA without replacing
the accepted colors or patching the compiler. Missing image understanding alone
does not block this deterministic color check or ordinary delivery.
`ok: true` does not imply warning-free. Never describe degraded output as clean.
A design-plan deck does not require a second semantic reviewer; the report records
that post-content model review was not performed. Program QA still runs normally.

When the image manifest has a background `layout_contract`, run
`scripts/validate_image_layout_contract.js` after finalization and preserve its
findings with the draft. Keep `outline.json`, `deck.json`, assets and reports beside
the HTML. The saved HTML's embedded model is authoritative after user editing;
the sibling `deck.json` is not automatically synchronized.

## Requested redesign and other routes

For an explicit design change, update the affected outline constraints, prepare
new designer input and import the revised child response with `design_plan.js accept`. With an existing deck of the same
page count, run `scripts/design_plan.js apply design_plan.json --input design_input.json
--deck deck.json`, then fill any newly required content and finalize. A failed
layout migration returns named content/contract issues; do not trim required facts.
The HTML editor remains free to change layouts, local enums, pages and media.

For an existing PPTX/template: copy it, extract text, edit while preserving OOXML,
validate the package, and inspect visually only when requested or needed. Read
`references/ooxml-editing.md`. Native PptxGenJS or legacy/custom HTML is an explicit
escape route when controlled layouts cannot express the requirement and the user
accepts the tradeoff. Read `references/pptxgenjs.md` or `references/html-first.md`
then. This authorization applies to the requested alternative, not to a failed
design acceptance; use the existing HTML delivery procedure above for that case.
Do not change route for convenience. Direct PPTX line geometry must use
nonnegative width/height.

For optional Archify diagrams, read `references/archify-diagrams.md`; use its
bundled runtime without installation. Its embedded diagram is an image. Keep
`technical-diagram-v1` for editable nodes/edges in HTML; PPTX receives a scalable
SVG picture, not guaranteed native node objects. Statistical data stays in the
editable chart workflow. Read `references/html-editable.md` for export internals.

### Theme preview intent (before deck authoring)

“先看看主题 / theme options” is theme discovery, before writing `outline.json` or
scaffolding. Run `scripts/render_theme_gallery.js --out theme-previews/index.html`,
show the real compiled previews, and wait for the chosen registered theme. This
opt-in discovery must not slow the default path. Composition comparison intent
uses `scripts/render_composition_gallery.js --out composition-previews/index.html`;
it displays existing internal presets without requiring main-agent family choices.
`html-templates` is optional richer design reference, not a hard dependency.

## Research and image evidence boundaries

For a factual public deck without supplied evidence, load `research-synthesis`
before searching. `prepare` records `research_status`; if it is
`handoff_unverified`, do not claim verified research or hide this limitation.
Use only supported facts, omit optional claims or disclose required gaps, and
continue production without treating research gaps as a rendering failure.

Image understanding is optional. If `inspect_images` cannot view the image or
reports no multimodal capability, record visual identity/quality as unverified
and continue immediately. Do not retry it, switch models, change user settings,
or block design, media binding, rendering or export. Actual file existence,
decode and geometry checks remain program checks, independent of model vision.

## Export and runtime

Default HTML generation needs no export-host preflight. Only explicit PPTX export
uses `scripts/check_html_export_env.js`, then `scripts/html_to_editable_pptx.js
index.html output.pptx`. Read `references/runtime-office-raccoon.md`,
`references/dependency-policy.md`, and `references/shell-safety.md` when needed.
Use managed `BOX_AGENT_NODE`, `BOX_AGENT_PYTHON`, `BOX_AGENT_NPM` executables and
managed package resolution, not bare require.resolve probes or system installs.
A blocked export does not suppress an existing editable HTML deliverable.

The controlled canvas remains exactly 1920px × 1080px. Do not pass --width/--height;
nonstandard exports require matching explicit --canvas WxH on the relevant tools.
Every created/modified PPTX needs package validation, text extraction, placeholder
scan, and page count/order checks. Missing optional render dependencies do not
block HTML. For maintenance, use `references/presentation-system.md` and the
all-theme/all-layout matrix; it is not a routine authoring stage.

### 4.2 Visual inspection is optional

Rendered visual inspection (`scripts/render_pptx.py` + reading the resulting images) is **opt-in**, not a required gate.

Model-based image inspection is also optional. If `inspect_images` is absent,
the selected model does not support images, the request fails/times out, or its
answer says it cannot see/read the image (including “Access Denied”), record
visual inspection as **unverified** and continue to deliver the usable HTML.
A tool-level `success: true` does not turn an “unable to read” answer into a
completed visual review. Do not retry the same inspection, switch models, ask
the user to configure a vision model, or repair the environment just to satisfy
this optional check. Deterministic HTML/layout checks remain independent of
model vision. Missing visual inspection alone never blocks the workflow or
marks an otherwise usable deck incomplete. Follow an explicit user request to
retry or use a particular vision model separately.
If preparing the screenshot inputs fails, skip the model-based inspection at
that point too. Do not cycle through capture methods or export a PPTX merely to
obtain pictures for checking an HTML-only delivery.

**Default behavior:** skip rendered visual inspection. The controlled HTML QA
reports above are sufficient for an HTML-only delivery; PPTX structural QA is
sufficient for an exported `.pptx`. Do not call `render_pptx.py` for visual
judgment on every deck.

**Trigger visual inspection only when:**
1. The user explicitly asks to see / review / render the deck.
2. A blocker-class issue is already suspected from structural QA (e.g. text-extract shows truncated content) and visual confirmation is needed to locate the failure.

**When visual inspection runs:**
1. One pass only. Classify findings per §4.1.
2. Fix blockers, accept cosmetics, report.
3. Do **not** re-render after the fix to verify cosmetics. Re-render only if the fix targeted a blocker.
4. Do **not** trigger a second visual pass to "double-check" your own judgment.

Rendering for the user's own preview (so they can open the PNGs) is fine and does not count as visual QA — just generate the images, do not narrate findings or self-critique.

### 4.3 Optional whole-deck overview

At final delivery, prefer one contact sheet showing every slide in order, if
current screenshots and the local renderer are available. This is a user preview,
not an additional visual QA gate. Never block delivery on this preview.
Use a four-column thumbnail grid in reading order (left to right, then top to
bottom): 12 slides form 4 columns by 3 rows. Fewer than four slides use one row;
an incomplete last row is fine. This is only a quick look at the overall style
and composition, not a detailed slide review.

Reuse screenshots from the final HTML export when available; identify them as
HTML previews, not screenshots of the exported PowerPoint. Otherwise use an
available browser to capture the final HTML slides, or an already usable PPTX
renderer (`scripts/render_pptx.py <deck.pptx> --out qa/overview-slides --format png`).
For browser capture, screenshot each slide element separately, preserving its
aspect ratio and excluding editor toolbars, navigation, and surrounding page
chrome. Do not use a full-page scrolling screenshot or a single tall strip of
the entire deck as the overview. Do not change the actual deck layout to make
the contact sheet.
Do not install dependencies just for this optional preview. If capture is
unavailable or fails, skip it without retries or asking the user to fix their
machine. Missing preview alone does not make the deck incomplete.

Use only current full-slide PNGs in a dedicated directory; check that their
count and numeric order match the final deck. Never use generated asset images,
stale screenshots, or a single Quick Look thumbnail as an all-slide overview.
If any slide is missing, omit the overview. Combine a complete set with:

```bash
${BOX_AGENT_NODE:-node} scripts/make_contact_sheet.js qa/overview-slides --out qa/deck-overview.png --cols 4 --thumb-width 480
```

If stitching fails or the dependency is missing, skip the preview. The helper's
vision-review prompt/status belongs to opt-in visual QA; it does not require a
reviewer or prevent delivery of this user preview. Embed only the successfully
created contact sheet once, before the generation-details section. Do not
embed individual slide screenshots by default.

## 6. Final Response Format

The normal user-facing response must be concise and use the active user-visible
response language. An explicit user language request wins; otherwise follow the
host `ui_language` instruction when present, then the language of the user's
task. Never override that contract with a locale-specific default, and do not
mix headings from one language with body text from another.

Treat generated or acquired slide illustrations, diagrams, backgrounds, and
individual screenshots as intermediate assets. Do not embed them in progress
messages or the final reply, or list them as standalone deliverables, unless
the user explicitly requests the individual images. Keep their local files and
manifest references for deck rendering and editing. For PPT assets call
`generate_image` with `publish_artifact: false`; this suppresses standalone
artifact publication without suppressing generation or insertion. A requested
individual-image deliverable may use `publish_artifact: true`.

Present information in this priority order:

1. Start with one plain-language completion sentence using the matching
   localized completion, editable-draft, or incomplete status below.
2. Name only the primary user deliverable: the editable presentation, requested
   PowerPoint export, or requested PDF export. Do not enumerate reproducibility
   or workflow files.
3. Add the localized usage-note label only when a finding changes how the user should preview,
   edit, download, or publicly use the result. Explain the concrete impact and
   recommended action; omit purely diagnostic findings.
4. End with concrete next steps in the active response language, for example
   previewing, downloading, continuing edits, or updating after adding sources.
5. Add a compact block using the matching localized generation-details heading;
   the host
   moves this section into its collapsed details area before the existing
   generated-files module. Retain QA totals and relevant technical artifacts.
   Translate fixed status concepts into the matching localized QA labels. Keep artifact
   names and directories such as `deck.patch.json`, `qa/`, and `research/`
   literal so the result remains traceable.

Use these stable labels for the host-supported locales. If the user explicitly
requests another language, translate the same semantic roles consistently:

| Role | `zh` | `en` | `ja` |
| --- | --- | --- | --- |
| Complete | `演示文稿已完成` | `Presentation complete` | `プレゼンテーションが完成しました` |
| Editable draft | `已生成可编辑草稿` | `Editable draft ready` | `編集可能な下書きができました` |
| Incomplete | `演示文稿尚未完成` | `Presentation not complete` | `プレゼンテーションは未完成です` |
| Usage note | `使用前注意` | `Before use` | `ご利用前の注意` |
| Generation details heading | `## 生成详情` | `## Generation details` | `## 生成の詳細` |
| QA passed label | `质量检查` | `Quality checks` | `品質チェック` |
| QA notice label | `检查提示` | `Check notices` | `確認事項` |

Never print raw status key names such as `qa_ok` or `qa_warnings`, internal
research delivery-mode ids such as `framework` or `partial`, validator names,
or commands. Their values may be retained only under clear localized labels.
Technical filenames and report directories remain literal under the localized
generation-details heading.
Do not turn a diagnostic count into a user warning: explain it under
the localized usage-note section only when it changes how the user should use the result.

Translate internal outcomes into user impact:

- Image-generation success means the file exists, not that it is inserted.
  Finalization binds ready manifest assets to missing/placeholder slots in the
  same invocation, preserving already chosen media. Use the final image report
  to describe insertion; an unreferenced image remains missing from the deck.
  Report affected pages in an editable draft without another review or retry.
- Local contrast warnings likewise mean a readable-display problem remains:
  deliver the editable draft, name the affected pages from the existing report,
  and do not label the color check as clean or start another review round.
- When the editable HTML is current and core checks pass, say it can be
  previewed and edited normally.
- When source or private-fact advisories exist, make clear that they do not
  prevent preview/editing and state whether public sharing needs source review.
- When a usable HTML draft exists but a presentation issue may affect display,
  use the localized editable-draft status and name the affected use, not the validator.
- When HTML is usable but a requested PPTX/PDF export is unavailable, deliver
  the presentation and say only that the requested format has not been exported.
- When no trustworthy HTML exists, do not claim completion.

Format-specific QA is explicit: HTML delivery treats chart recoverability and
dom-to-pptx compatibility findings as advisories; a requested native PPTX run
must invoke `finalize_controlled_deck.js ... --require-pptx`, which promotes
those findings to blocking export checks while preserving the HTML artifact for
diagnosis.

If a blocking structural, HTML, runtime, explicitly required image, or export
step is blocked, explain the user-visible consequence in the active response
language. Never treat a
source/URL/private-fact advisory as a blocked presentation.
