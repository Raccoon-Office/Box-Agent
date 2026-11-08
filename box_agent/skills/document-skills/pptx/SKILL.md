---
name: ppt-fast
displayName: 快速模式
description: Create, inspect, edit, validate, render, and QA presentation decks. Use when the user mentions PowerPoint, PPT, PPTX, HTML deck, slide deck, presentation, template slides, speaker notes, slide images, or asks to read, generate, create, make, design, or modify a presentation artifact. New decks use controlled, editable HTML; PPT/PowerPoint requests also receive a PPTX file unless the user requests HTML only.
keywords: [ppt, pptx, slide, slides, deck, presentation, powerpoint, pitch deck, speaker notes, ppt制作, 做ppt, 可编辑ppt, 幻灯片, 演示文稿, 投影片, 演示, 宣讲, 汇报材料, 路演, 路演材料, 融资路演, 商业计划书, BP, 提案, 讲稿, 模板页, 路演ppt]
capabilities: [presentation.authoring]
related_skills: [html-templates]
metadata:
  user_visible: false
  allow_override: false
---

# PPT Fast Skill

Create controlled, editable HTML presentations. A request to make a PPT/PowerPoint
requires a `.pptx` file alongside the HTML; the user need not separately ask to
“export”. Carry that format request through mode selection and task continuation.
Explicit HTML-only or outline-only requests need no PPTX. A request for a finished PPT includes
pages, not just an outline, unless it says “只要大纲/内容方案” or “不要生成页面”. Existing PPTX/template edits
preserve the original file structure; `python-pptx` must not create a new deck.

## Responsibilities

Use the host's Playwright runtime for rendering. When `BOX_AGENT_PLAYWRIGHT_MODULE_PATH`
is supplied, load that exact Node module and pass
`BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` as `executablePath`; do not install a
second SDK/browser or use a bare `require('playwright')` that can select an old
workspace dependency. Prefer the bundled rendering/export scripts, which implement
this contract. A missing host runtime must be reported for repair in the client.

The main agent owns research, factual content, the outline, content filling, media
acquisition and delivery. An isolated design role owns the registered theme preset,
complete role-based palette, structured visual requirements, slide layouts and their visual options. Programs validate and
render. Do not select themes, composition families or whole-deck variants in the
main agent, and do not load the complete design catalog into its context.

Invoke native role tools directly from their exposed function schema. In Box-Agent,
`sub_agent` is a direct local tool and is not a `tool_search` discovery candidate.
Do not search for or try to activate it through that catalog. A catalog result
cannot establish whether this already exposed function is callable. Use the
actual function definition and invocation result when assessing availability.
If you would fall back because its schema seems missing in Box-Agent, first run
this read-only check from the presentation directory, using the expanded path:

```bash
"$BOX_AGENT_NODE" scripts/native_delegation_status.js --invocation <fresh-literal-UUID-v4>
```

Choose a fresh UUID v4 and put it literally in this standalone Bash command;
do not reuse it or generate it through shell substitution. The helper binds this
pending invocation to the host's current request header, not a tool catalog or
another session. `present` supplies the native schema: invoke that function for
the required role. `absent` establishes its absence in this request. `unknown`
means the evidence could not be established; it does not mean the tool is absent.
Do not inspect or repair session logs to change the result. A direct invocation
or explicit host restriction remains valid availability evidence.

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
For product promotion, sales or enterprise-value decks, also read
`references/business-narrative.md`:
1. State the primary audience and next decision before research. A broad product
   promotion brief (including “市场推广PPT”) defaults to prospective users/buyers;
   channel plans, campaign calendars and marketing KPIs require an internal
   campaign brief. Keep that audience through the outline; do not reinterpret it
   after collecting facts just to justify a different story.
2. Use the research question map to find support for the promised value and
   adoption decision, including relevant organizational cases and commercial
   terms when applicable. Evaluate what each source can establish before
   choosing the central case; research notes alone cannot replace validated
   evidence. Keep unresolved questions explicit within the selected budget.
3. Build the argument from the evidence, then review coverage and page
   contribution using `references/outline.md`. A research dimension, buyer
   question or product feature does not automatically deserve its own slide.
   Preserve distinct explanations, cases and comparisons that the audience
   needs; merge repeated answers. Do not default every deck to a pilot proposal.
The designer must not invent missing content to fit a visual.
Keep short source material short: do not repeat the same claim in a card title,
body and bullet just to fill space. Choose a layout with suitable capacity.
Do not compare different units on a shared numeric axis; use separate chart panels
or clearly labeled growth rates derived from the supplied values.

- Supplied page content is the source of truth; preserve its intended ordering.
- Proposed architectures/solutions with sufficient goals and components use
  `source_mode=user_provided`; they do not require research by default.
- Substantive external/current/company/market research uses `research-synthesis`
  when available; load it before that research's first public search. Preserve
  `outline.md`'s targeted official-source check exception for an otherwise
  complete solution brief. An outline-only request skips design/export, not a
  research-backed story's evidence handoff. First frame the audience, intended
  action and claims that need
  evidence using `references/outline.md`; let missing support guide the research.
  Follow its selected research budget and use only the validated
  `presentation_handoff.verified_facts[].canonical` as researched facts. Do not
  ask again for permission to use public sources already authorized by the task.
- Preserve strict/private source boundaries. Omit optional unsupported claims;
  disclose required public gaps as `本轮核验：暂无可验证公开数据`: this run did
  not obtain or verify supporting public evidence, not that no public information
  exists. Private gaps use `待补充` or `待客户确认`. Assumptions require explicit
  user authorization.
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
For framework research pages, keep the literal marker `暂无可验证公开数据` inside
the scoped wording above in `message` or `bullets`.

For a research-backed outline, put the selected `verified_facts[].id` values in
each slide's `evidence_ids` and leave `evidence: []`. Preparation resolves IDs
to exact canonical evidence; legacy handoffs without IDs still use canonical
strings. Read the facts before selecting IDs. Never implement a substring matcher
or discard unmatched citations: a missing reference is a handoff error to fix,
not proof that the source lacks the fact. Keep verified dates, units and conditions.
Use only entity aliases declared by the research handoff. Complete the narrative
check in `references/outline.md` before entering design.

Write `outline.json`. Preserve explicit theme selections in
`design_requirements.theme_id`, and exact palette/style/geometry wording in the
same design requirements. Put explicit per-page geometry/count requirements in
that page's `hard_requirements`, quoted verbatim from the real user request.
Do not put your own aesthetic choices there: outline layout/visual are suggestions
for the designer, and changing only those hints does not reset the design budget.
These constraints must be updated when the user changes
them. Do not add unrelated history or synthetic continuation text as user facts.

Before preparation, read the actual outline against the research check. Every
fact-backed page must name its subject or a declared alias in visible narrative,
including problem/mechanism pages using entity-bound evidence. Put case attribution
(vendor report, anonymous customer or pilot) and material conditions in `message`
or `bullets`; `notes` and `visual` alone do not reach every rendering path. Keep
the claim and its essential qualifiers together in near-final copy so a layout
change cannot silently detach the source, time period, unit or limitation. When
condensing a result, retain who reported or measured it and whether it is an
individual self-report, organizational observation or other evidence type; a
publisher or anonymity label alone does not preserve that distinction. Check
that relevant commercial terms survived into the outline. When an action requires
an access destination or an owner/channel, put the observed or supplied entry in
`message` or `bullets`; a link only in `notes` does not reach the audience. Keep
local research/audit paths in working files or delivery metadata unless the
requested audience needs those paths. Source appendices should offer usable
citations, not author-only file locations. If an excerpt and a claim use different
languages, preserve a faithful claim in the excerpt's language
and its exact supporting text with entity context in the research ledger; translate
the page copy afterwards. Do not invent translated quotations, pad excerpts with
the claim to pass token matching, or discard supported adoption facts because of
a language mismatch. Revalidate corrected records and bind their returned IDs.

#### Validate the current copy before content review

Run the existing standalone check before the initial review, adding the research
handoff when applicable:

```bash
"$BOX_AGENT_NODE" scripts/validate_outline.js outline.json --report qa/outline_check.json
```

For researched content, add `--research-handoff research/qa/<topic>_research_check.json`.
Use the presentation directory and expanded script path. This check creates no
pages and also applies to outline-only tasks. Read `issues`, `repairable` and
`claim_strength_review`, not just the exit code. The last list locates wording
for semantic review, not proven errors; do not remove legitimate requirements
or promises merely to clear it. Correct mechanical gaps together and recheck;
do not expand this into an open-ended repair loop or reset saved repair limits.
After any research revision, check every page against the newly generated
handoff: changed or excluded facts can affect pages outside the intended edit.
Resolve all unknown IDs from their actual source claim; never type a remembered
hash, guess a replacement or discard a citation while retaining its claim.
Complete this same check on reviewer-driven changes before the focused recheck,
so the review sees the current validated copy. Page production still requires
design preparation and final QA. Outline-only tasks stop after their applicable
validation and content review; do not run page preparation or export for them.
Mechanical success does not establish semantic correctness.

#### Independent content review

Before design or outline-only delivery of a broad research-backed story, obtain
one initial isolated content review using `references/content-review-role.md`. Direct
mapping of complete supplied pages and small content edits need no extra role.
Invoke the host-authorized native `sub_agent` directly using its exposed schema,
as explained above; do not use `tool_search` for this tool. Use
`required_tools: ["read_file", "search_files"]`; for permitted public research,
also include the available `web_search` tool for the role's bounded independent
verification queries. Never enable external research for a file-only task.
Use `budget: {"max_steps": 24, "max_tool_calls": 18}`; omit `files` to keep the
tool-capable loop. This leaves room for Box-Agent's default ten-step wrap-up
reserve without stopping the review at its third step. The tool-call and external
verification caps remain unchanged; finish when the material checks are complete.
Give the exact role path, original request, current outline, `qa/outline_check.json`,
and validated research check. Its `presentation_handoff.verified_facts` supplies
the selected IDs, canonical claims, provenance and capture paths. Resolve those
capture paths against the actual research directory. For facts without a capture,
retain their source URL/file and the report's actual context-file locators; these
are optional source locations, not another required inventory. Pass the
audience/decision and unresolved question-map rows as planning context to check, not an intended
verdict. Do not append a default inventory of dimension, insight, ledger or
cross-verification files; they remain available for a specific conflict or gap.
Include the report's claim-strength items in the existing support audit and save
their dispositions in `qa/content_review.md`; this adds no review round or repair
budget.
Call the native tool named `sub_agent` with an argument object, for example:

```json
{
  "task": "Your sole task is to return an independent content audit and a usable/revise verdict. Returning that report, including unresolved findings, completes this delegation; the parent saves it and owns all revisions and deck delivery. Read <absolute role path>. Original user request, for context only: <request>. Planning context to check: <selected audience/decision and unresolved question-map rows>. Review <outline path> using <outline_check report, research check and actual research directory/source-capture paths>.",
  "required_tools": ["read_file", "search_files"],
  "budget": {"max_steps": 24, "max_tool_calls": 18}
}
```

Replace the placeholders with the actual request and absolute paths. Confirm
those paths against the current files or validated handoff; do not abbreviate
topic-prefixed filenames or invent an inventory from naming conventions. Include
the current page IDs and exact titles as a compact locator beside the outline
path, not a suggested verdict. Keep this task compact: include the original
request once and reference source files rather than embedding their bodies or
repeating the research inventory. Apply the source-bound tool rules above. This
is the initial review call, not a separate availability probe or a command for
Bash/execute_code.
Do not give an intended verdict, your self-review or a previous finished deck.
The role checks copy and intended diagram meaning, not theme/layout choices.
It does not write or open web pages;
when public research is permitted it may run at most two targeted searches to
find controlling terms or evidence the initial source set missed. Search results
are leads, not directly verified facts. Keep the research reserve for this stage.

Save the returned review unchanged in `qa/content_review.md`, followed by a
separate author-resolution section. Check page-specific findings against the
current copy before adopting them; reject quotations or page mappings that do
not match it. Verify reviewer-proposed factual corrections against the actual
source before adopting added dates, scope, units or computed values. Keep deductions explicitly labeled as inference; a review verdict is not
new evidence. Record the tool-call title/reference and
child-session ID when exposed; do not invent an unavailable ID. Resolve material
findings before preparation:
use the research workflow's reserved verification reads for named gaps, correct
the ledger/copy, or remove an optional unsupported claim. Include unresolved
central questions from the coverage map even if the reviewer omitted them. While
budget and an authorized source remain available, attempt the highest-impact
missing premise or adoption condition before settling for a partial answer;
adding a disclaimer alone does not use the available verification opportunity.
Prefer this over chasing a removable metric's precision. Keep a required missing
premise explicit; moving a condition into notes does not resolve it. Append
the author's resolutions with source/fact references, then rerun changed research
and outline validation. Preserve unresolved findings in delivery; do not rename
them as an independent pass or restart the whole research/design workflow. If
only a review summary was saved, label it as a summary and preserve the actual
tool reference; never label rewritten text as the review's original wording.
If verification adds material evidence or changes the central argument after that
review, first finish the updated research and outline checks above, then make one
focused recheck before design or outline-only delivery. Use the
same role with no external tools and `budget: {"max_steps": 24, "max_tool_calls": 18}`.
This is a ceiling for the focused task, not a target; return as soon as its
material checks are complete. Leave room after the last read to produce the report.
Keep the same completion boundary: the child returns its review, and the parent
owns saving and revisions. Give it the original request, prior findings, updated
outline and outline check, plus only the changed facts, their source-context
paths, affected research-summary passages and author resolutions. For claim-strength items, recheck unresolved or
changed entries; do not repeat resolved unchanged ones. Ask it to check whether
the findings are resolved and whether the revisions introduced a new material
error or omission; do not ask it to approve an intended verdict. Do not repeat
broad research or the unchanged source inventory. Save its actual response and
resolve remaining issues within the existing evidence budget; disclose any that
cannot be resolved. If the call exhausts its budget without a usable audit,
preserve that result and complete the remaining checks locally; label them as
local checks, not an independent pass. Do not restart broad research or discard
completed work to obtain a preferred verdict. This recheck does not certify later layout rewrites.
In Box-Agent, use the availability check above before a missing-schema fallback;
record its actual result, an explicit host restriction, or a direct invocation
establishing that delegation itself is unavailable. A catalog miss or an
unverified claim about the tool list is insufficient. With `unknown`, use any
exposed native schema; if independent review still cannot be performed, report
that it was not performed and availability was unverified, then check locally.
On other hosts that genuinely provide no native delegation function, state that
host limitation and perform the same checks locally. Label local review honestly;
do not claim independence or ask permission for the authorized review.

### 2. Prepare the isolated design input

All shell examples below run in the presentation directory using the directory
prefix above and the loader-expanded script paths. In the managed desktop host,
use the quoted `"$BOX_AGENT_NODE"` and `"$BOX_AGENT_PYTHON"` executables exactly;
do not use shell default-value expressions as executable names. If a variable
is absent in an unmanaged environment, use the known executable `node` or
`python3` directly instead.

```bash
"$BOX_AGENT_NODE" scripts/design_plan.js prepare outline.json --out design_input.json
```

For a research-backed outline, add `--research-handoff research/qa/<topic>_research_check.json`.
This command runs the outline validator and writes `qa/outline_check.json`. A
failure returns the named issues for a content correction; do not bypass it.
An optional `--title` sets the deck title. It writes the complete designer input
to bounded brief/index files, with theme/layout details available on demand.
On success, stdout contains paths and reuse status. Failure output carries the
issues and repair stage. The metadata file is not the designer brief.

If `reusable: true`, reuse `design_plan.json` without another design call. If the
existing `deck.json` is already scaffolded, continue its content/finalization work;
never scaffold over it. For a content-only update that fits the existing fields,
patch it directly without rerunning design preparation. Re-enter design only for
changed design requirements or incompatible content shape/capacity. A saved HTML
with user edits is authoritative: preserve it and its embedded `#deck-document`;
never restore a stale sibling JSON over those edits.

### 3. Delegate and import the independent response

Use the native function itself. A `tool_search` result is neither a designer
response nor an availability check for an exposed local tool.
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
"$BOX_AGENT_NODE" scripts/design_plan.js accept design_input.json
```

Attempt the available native designer before acceptance; do not call `accept`
with no child response as a tool-availability probe. For uncertain availability,
use the same request-header check above. Actual unavailability still uses the
existing neutral recovery path and its disclosure.

The importer finds only completed child sessions that read this exact brief in
this workspace. It binds version, hashes, ordered page numbers, and content
mappings, then validates the real choices and generates the canonical plan.
Manually modified plans or responses from another input/workspace are rejected.
No model needs to copy hashes, page IDs, or JSON content-reference paths.

If a valid accepted design later fails page-level render QA, do not start a fresh
designer with only the old brief. Bind the original decision and failed pages:

```bash
"$BOX_AGENT_NODE" scripts/design_plan.js correct design_input.json --pages 4 --issue "Page 4 overflows"
```

Send the returned correction_file to the design role, then run accept and apply.
Complete the affected-page content migration in "Finalize and deliver" before
finalizing; applying a corrected layout does not finish its copy.
The program changes only the named pages, preserving palette, subject profile
and all other pages. The one-correction limit still applies. A second unbound
response cannot replace an already valid design. Explicit theme-only revisions
use --fields theme_id instead; never widen scope just to accept unrelated changes.

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
Recovery uses plain-neutral with registered cards/closing layouts and the
normal deck schema, renderer, playback, layout editor, save and export controls.
Never substitute a separate text-only HTML implementation.
Preserve one recovery page per outline page when its content fits. Extra pages
are tolerated only when needed to retain content that exceeds a page's capacity;
never invent content or facts to fill them. Do not start a third design call or
alter input just to reset the attempt count. If later compilation fails, use the
existing fallback HTML with its report. Images or unavailable vision must never
prevent delivery of the files already available.

#### Finish delivery after design failure

If acceptance returns `correction_failed` with `retained_plan`, the correction
failed but the original accepted plan remains valid. Keep its palette, subject
identity and page count; apply that retained plan if needed and finalize the
existing deck. Do not switch to neutral recovery HTML or restart design. Report
any remaining confirmed visual defects as a degraded draft with partial outcome.
Do not enter a manual CSS debugging loop after this terminal result. Export the
retained artifact and report the affected pages and failed checks.

`degraded` / `terminal` ends design retries, not outstanding format delivery.
Read `qa/design_delivery.json`; keep its actual `primary_artifact` HTML unchanged.
For HTML-only requests, deliver that file with the report. When PPTX is required,
run the existing exporter on that same file, using its absolute path in place of
`<PRIMARY_ARTIFACT>`:

```bash
"$BOX_AGENT_NODE" scripts/check_html_export_env.js
"$BOX_AGENT_NODE" scripts/html_to_editable_pptx.js '<PRIMARY_ARTIFACT>' output.pptx
"$BOX_AGENT_PYTHON" scripts/validate_pptx_package.py output.pptx
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

Stopping authoring still permits exporting the existing `primary_artifact`.
If PPTX was requested, use the existing-HTML export command below without
rerunning finalization or changing the deck. Deliver the available files and
explain the limitations from `qa/design_delivery.json`.

### 4. Scaffold the validated plan once

```bash
"$BOX_AGENT_NODE" scripts/inspect_deck_contract.js --design-plan design_plan.json --design-input design_input.json --outline outline.json --out deck.json
```

Use `cd '<PRESENTATION_DIR>' && "$BOX_AGENT_NODE"` on that same line;
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

After scaffolding, append missing evidence with `apply_deck_patch.js`, for example
`{"truth_contract":{"add_source_facts":["verbatim supplied text"],"add_research_facts":["verified document fact with its table/page reference"]}}`.
Write that JSON to a patch file and pass its path as the second argument, as in
`apply_deck_patch.js deck.json deck.patch.json`; do not pass inline JSON as a path.
Fictional examples authored for the presentation are not user source facts or
research facts. Label them as examples and do not register them as evidence.
Source additions must match the runtime user source; document/research additions
retain the existing strict-source restrictions. This validates provenance rules,
not the truth of a statement: read the source before registering it. Additions are
deduplicated and preserve existing evidence, content and design. Never move patches
aside or run scaffold `--force` just to add facts or clear advisories.
Review fact advisories once, correct unsupported content or append verified evidence
in one batch, then continue to rendering. Do not retry solely to reach zero warnings.

### 5. Acquire media and fill content

Before issuing any image-generation batch, read `assets/generated/manifest.json`
and collect each selected row's exact `output_path`. Generate directly to those
paths; do not generate descriptive aliases first and rename them afterwards.

Read `references/image-assets.md` for image acquisition. Follow the scaffolded
`acquire_via`: `user` keeps localized supplied assets; `web` uses image search;
`ai` uses generation; `none` stays image-free. Web rows use the exact search query
with `search_type: "image"` and `count: 5`, once per unique query. Localize selected
receipts with `scripts/localize_web_image.py`. Only after `exhausted` or
`unavailable` may a web job use its labelled AI-concept fallback. Never fabricate
a documentary image of a real subject. Before generating that fallback, use the
`localize_web_image.py ... mark` command in the image-assets reference to record
the actual terminal search status and reason in the manifest; describing the
search result in conversation does not update the manifest. Call `generate_image` with
`watermark: false` and `publish_artifact: false`; use the manifest entry's
`output_path` verbatim as the tool's `output_path` (never invent a short alias such
as `cover-hero.png`). Independent jobs may share a tool batch. Generation success
is not insertion proof: the final manifest must bind the asset to the actual page
and field.

Before finalization, inspect every required image row. A pending web search or
pending generation is not an unavailable service: actually attempt the planned
search and, when eligible, its fallback. If acquisition fails, record the real
failure and deliver the resulting degraded draft; do not silently skip the job
or repeat the same failed acquisition indefinitely.

After required generation jobs finish, synchronize once with one tightly scoped Bash-tool command. A missing optional
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
When the accepted plan includes per-page `subject_expression`, implement that
expression in the corresponding content/media fields. Before finalization,
compare each key page with its expression: naming an axis is not a route diagram,
and naming materials is not a material-detail visual. Report any expression that
could not be implemented; a profile in JSON is not proof of visible subject fit.

Before finalization, apply the claim-and-qualifier comparison in
`references/outline.md` to the filled visible fields, including table labels,
metric cards and diagram text. Compare them with the validated outline and its
bound evidence, not only with the earlier review verdict. Restore meaning lost
while condensing through the same content patch; notes and a successful binding
check do not replace visible conditions. This is the author's content check,
not another delegated review or research round.

Explicit image-rich briefs may activate `creative_image_mode`; at least one real
generated asset must be referenced for an image-complete result. If all required
generation fails, deliver structurally usable degraded HTML and accurately report
the unmet image requirement. Ordinary factual topics do not imply creative mode.

### 6. Finalize and deliver

```bash
"$BOX_AGENT_NODE" scripts/finalize_controlled_deck.js deck.json --out index.html
```

This is the single normal finalization command. Core schema/design-contract and
render failures block. Outline binding drift blocks by default. Only an explicit
`BOX_AGENT_ALLOW_DEGRADED_OUTLINE_BINDING=1` permits that semantic-only draft.
Image, optional inspection and source findings are recorded as advisories.
Run the finalizer directly, without a pipeline, `tail`, or a trailing diagnostic
command that masks its exit status. Hard stage failures also write a machine-readable
`qa/finalization.json` with `stage`, `blocking_issues`, and `next`; stdout progress
lines are not a JSON document. Use the final JSON result, not shell success alone.
On any nonzero finalization result, use its named reports. If it names
`qa/render_quality.json`, read that report before
classifying other advisories. Its `ok: false`, non-advisory issues and the
finalizer's `blocking_issues` are confirmed visual failures. A local
`text.status: fit` only describes text sizing and does not clear container
overflow warnings; package validity and successful image generation also do
not clear these failures. Follow the report's `next` action, not a guessed
explanation for the exit status.
If finalization returns `correction_required: true`, confirmed unreadable text,
text overflow or truncated diagram text is a repair request. If the named
issue can be resolved by wording alone within the existing editable fields, apply
one content-patch pass without changing meaning, conditions, palette or layout,
then finalize again. Otherwise use `render_quality.affected_pages` and its concrete
issues with the existing
`design_plan.js correct` page-bound correction, accept and apply once.
Before finalizing this correction, read each changed layout's field contract with
`"$BOX_AGENT_NODE" scripts/inspect_layout.js LAYOUT_ID` (read-only; do not scaffold
again). Against the original outline, accepted `content_bindings` and
`subject_expression`, refill the affected content fields and remove template copy.
Check all material arguments, relationships and conditions, including nonempty
migrated fields; a structurally valid mapping does not prove content completeness.
Apply the content patch with `apply_deck_patch.js`, then finalize once. This
migration is part of the same correction, not another repair or review round.
Preserve the accepted palette, theme and all unaffected pages.
If the correction was already used or the defect persists, retain HTML/PPTX as
an explicitly degraded editable draft; do not report the deck as fully completed
or mark visual quality passed. Report execution outcome as `partial`, with the
failed visual check non-advisory, and describe the artifact as a degraded draft
in the final response. Finalization returns `ok: false` and a nonzero exit status
for these confirmed defects even when it preserved usable files.
Missing optional inspection alone never requests
this correction. Do not start an unbounded review/repair loop.
An actual frozen-palette mismatch is a design-contract failure, not an optional
visual review: report the concrete component/property from QA without replacing
the accepted colors or patching the compiler. Missing image understanding alone
does not block this deterministic color check or ordinary delivery.
`ok: true` does not imply warning-free. Never describe degraded output as clean.
When `delivery_status` is `degraded`, report the execution outcome as `partial`,
identify the named degraded stages, and keep the artifact as a draft. Do not mark
failed checks advisory merely to submit `completed`, or repeat finalization just
to make the status pass. Report the remaining issue and finish.
A design-plan deck does not require a second semantic reviewer; the report records
that post-content model review was not performed. Program QA still runs normally.
For a requested PPT/PPTX, add `--require-pptx --pptx output.pptx` to this command.
It exports and checks the current deck before reporting the PPTX path. Without
these flags it delivers HTML only. An export failure keeps the HTML, returns a
nonzero exit status and reports `delivery_status: partial`; it is not a completed
PPTX delivery, even if a previous `.pptx` exists.
The CLI exporter carries the current HTML's embedded research facts into the
first slide's notes as a deck-level source list. This preserves references, not
per-page support or factual verification; keep attribution and material
conditions in visible copy.

When the image manifest has a background `layout_contract`, finalization runs
`scripts/validate_image_layout_contract.js` and includes its result in the delivery
status. Do not repeat it after finalization. Keep `outline.json`, `deck.json`, assets and reports beside
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

For a factual public deck without supplied evidence (people, organizations,
events, products, data), initial research happens **before** `outline.json`.
Later reads are limited to targeted verification within the original research
budget, as described above.

Load `research-synthesis` when available; when it is unavailable or hidden in
this session (fast mode), call the search tools yourself — open with general
queries without `site:`, then `web_extract` the specific pages you will cite.
Writing `outline.json` for such a deck before any search or page read is a
process error. Use `source_mode=public_authoritative_research` only for facts
retrieved in this task with their actual provenance. When research provides a
handoff, bind its current verified IDs; do not replace canonical references with
handwritten quotations. Otherwise keep exact source URLs and supporting text
beside the claims. If nothing was searched, use only supplied facts and disclose
required gaps without claiming public research.
If `prepare` answers `repair_required: true`, design has not started. It allows
one reference correction and one content correction, each at most once. In the
`references` round, resolve the reported IDs and fix the independent issues already
listed; support checks on unresolved pages are deferred, not failed. In the
content round, fix every `issues` and `checklist` item together. Correct a
source-backed ledger claim and revalidate it when needed, then bind its current
ID; use only the remaining research budget for a genuine evidence gap. Omit an
optional unsupported assertion or disclose a required gap. Re-run the same
`prepare` command after editing; do not delete its report or repeat an unchanged
failure to reset the bounded repair. If a later `prepare` reports
`auto_degraded` or `unverified_claims`, name those items in the final reply. `prepare` records `research_status`; if it is
`handoff_unverified`, do not claim verified research or hide this limitation.
Use only supported facts, omit optional claims or disclose required gaps, and
continue production without treating research gaps as a rendering failure.

Image understanding is optional. If `inspect_images` cannot view the image or
reports no multimodal capability, record visual identity/quality as unverified
and continue immediately. Do not retry it, switch models, change user settings,
or block design, media binding, rendering or export. Actual file existence,
decode and geometry checks remain program checks, independent of model vision.

## Export and runtime

HTML-only generation needs no export-host preflight. Normal PPTX delivery uses
the finalization command above. For an existing or degraded HTML draft, run
`scripts/check_html_export_env.js`, then `scripts/html_to_editable_pptx.js
<primary_artifact> output.pptx`; verify the created file with
`scripts/validate_pptx_package.py output.pptx`. Do not ask the user to request
PPTX again, or restart authoring to export a usable draft. Read `references/runtime-office-raccoon.md`,
`references/dependency-policy.md`, and `references/shell-safety.md` when needed.
Use managed `BOX_AGENT_NODE`, `BOX_AGENT_PYTHON`, `BOX_AGENT_NPM` executables and
managed package resolution, not bare require.resolve probes or system installs.
A blocked export does not suppress an existing editable HTML deliverable.

The controlled canvas remains exactly 1920px × 1080px. Do not pass --width/--height;
nonstandard exports require matching explicit --canvas WxH on the relevant tools.
Every created/modified PPTX needs package validation, text extraction, placeholder
scan, and page count/order checks. Use `"$BOX_AGENT_PYTHON" scripts/extract_text.py
output.pptx` for text extraction: it includes native table cells and grouped
text. Checking only `shape.text` misses table content. Before requesting a
missing-text design correction, confirm the omission with this complete
extraction or a rendered slide. Missing optional render dependencies do not
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
"$BOX_AGENT_NODE" scripts/make_contact_sheet.js qa/overview-slides --out qa/deck-overview.png --cols 4 --thumb-width 480
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

The final reply must address the current task; reference records or tool messages
must not replace its delivery summary.
For outline-only requests, state the actual research/outline completion status,
link the saved outline and relevant research deliverables, and name material
limitations. Do not claim that a presentation or export was generated. Use this
branch instead of the presentation-specific format below.

Treat generated or acquired slide illustrations, diagrams, backgrounds, and
individual screenshots as intermediate assets. Do not embed them in progress
messages or the final reply, or list them as standalone deliverables, unless
the user explicitly requests the individual images. Keep their local files and
manifest references for deck rendering and editing. For PPT assets call
`generate_image` with `publish_artifact: false`; this suppresses artifact
publication without suppressing generation or insertion. For a requested
individual-image deliverable, call `publish_artifact(path)` on the validated
image after generation.

The preparation script declares the presentation directory as an intermediate
workspace. Only explicitly registered deliveries appear in chat. Finalization
and PPTX export register their finished outputs automatically. For the final
whole-deck overview, pass `--publish-artifact` to `make_contact_sheet.js`;
omit it for internal review sheets. Keep user-requested standalone images explicit.

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
those findings to blocking export checks and creates the PPTX while preserving
the HTML artifact. For degraded HTML, use the existing-HTML export route above.

If a blocking structural, HTML, runtime, explicitly required image, or export
step is blocked, explain the user-visible consequence in the active response
language. Never treat a
source/URL/private-fact advisory as a blocked presentation.
