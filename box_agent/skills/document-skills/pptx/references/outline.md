# PPTX Outline Planning

Every new controlled deck writes this planning artifact before scaffolding.
Do not turn a complete user-supplied page list into a new invented storyline:
map it directly into `outline.json` and keep that pass lightweight. Broad or
partial prompts require the fuller narrative/evidence planning described below.

The goal is to make the storyline, per-slide message, visual intent, and
evidence explicit before visual layout starts without overriding user intent.

## Decision Gate

Create `outline.json` for every new controlled deck request. When the user's
prompt already contains a page-by-page breakdown with titles, content, and
order, preserve it as a direct traceability mapping rather than expanding or
reordering it.

When the host includes a `<presentation_config>` block, use each field's
`source` to determine its strength. An `explicit` value from the user's
original request or an actively changed card choice is a hard constraint;
`recommended` and `default` values are soft framing and do not override the
source material or original prompt. `confirmed_by=user` alone does not upgrade
unchanged soft defaults. Use its audience to shape the outline, adapt
tone/storyline to its role and scene. This offline package always uses
`local_only` media, including when its mode is `creative`. Do not ask for a choice already present in that
block.

For page count, first identify the independent narrative beats and the amount
of content each beat can carry clearly, then create one slide per beat (or map
the user's supplied page structure directly). `page_count_auto`, or a range
whose source is `recommended` or `default`, is only a soft planning hint. Do
not aim for the midpoint of `5-10`, treat eight slides as a default, pad thin
material, or merge distinct conclusions just to hit a suggested range. Only a
concrete page count or range explicitly stated by the user, including an
actively selected range in the card, is a hard constraint.

Typical cases that benefit from an outline:

- The user gives only a broad topic, goal, or document type.
- The user provides partial structure (some titles, some content) that needs
  gap-filling, reordering, or evidence-to-slide mapping.
- The deck is narrative-heavy (BP, strategy, consulting, research,
  investment, product launch, annual review, data-story, etc.).
- Page count, slide order, audience, evidence, or key claims are unclear.
- The prompt asks for "帮我规划", "大纲", "结构", "storyline", or equivalent.

Do not create a second outline, and do not use `plan_write` as a substitute for
this artifact.

## When the request is under-specified or under-sourced

Use supplied local files and the user's actual brief. Use `source_mode=user_provided`
for factual materials and proposed solutions, or `creative_brief` for explicitly
creative content. Do not perform external research or treat model memory as
verified current facts. A local report may be summarized with its actual file and
page references; preserve its dates and uncertainty without claiming a fresh check.

A sufficient solution/architecture brief can proceed as a proposal. Clearly label
recommendations; do not invent product capabilities, customer names or metrics.
For optional unsupported facts, omit the claim. Required factual gaps use
`待补充（离线未核验）`; private gaps use `待补充` or `待客户确认`. Preserve explicit
requirements and report the gap. Ask one focused structural question only when
scope/audience ambiguity materially changes the result, never for each missing fact.

Do not load research skills, call search/fetch tools, download images, or activate
`creative_image_mode`. Use local media or editable charts/diagrams/typography.
Image-rich requirements without supplied media remain a disclosed limitation;
continue to a usable local draft instead of waiting for unavailable services.

## Required Output

When the decision gate says an outline is needed, create an `outline.json`
beside the future `deck.json`:

Pass `<PRESENTATION_DIR>` as the file tool's `workspaceDir` when supported, or
use an absolute path under that directory. Do not add an automatic `output/`
prefix. Prefer
one initial call without `chunk_index` or `final` whenever the complete JSON fits
in the current model response. Start ordered chunks only after explicit
output-length/tool-argument recovery. Within the same active turn, continue
only from the immediately preceding
`write_file` result and its exact path/index; never guess an index. If the
accepted prefix is already complete JSON, send an empty next chunk with
`final=true`. On a later turn or recovered Session, validate the canonical
target. If it is absent or invalid, restart at chunk 0 because temporary write
transactions are not durable Session state.

For `source_mode=user_provided`, user-stated solution requirements and proposed
architecture scope are valid planning inputs; make unsupplied implementation
details visibly advisory rather than claiming they already exist. Exact numeric facts in a page's `message` or
`bullets` are valid quantitative evidence for chart/KPI layout selection; keep
`evidence: []` when no external URL is needed. The URL-bearing evidence ledger
is mandatory only for public-research claims, not for facts supplied directly
by the user.

When that quantitative page is authored, labels, units, and values may occupy
separate editable KPI/chart fields. Preserve all page quantities and matching
labels, but do not repeat a long source sentence in every card or data cell.
For strict source-only briefs, omit optional explanatory copy when the user did
not supply it; reserve visible `待补充` for genuinely required missing fields.

```json
{
  "deck_goal": "What this deck must achieve",
  "audience": "Who will read or hear it",
  "source_mode": "user_provided | public_authoritative_research | creative_brief",
  "tone": "Visual and narrative tone",
  "design_requirements": {
    "palette": "Explicit user palette wording, for example 深蓝、米白、少量橙色点缀",
    "rule": "Preserve explicit color, geometry, direction, relationship, and count requirements verbatim; omit when none were supplied"
  },
  "storyline": "One-sentence narrative arc",
  "slides": [
    {
      "page": 1,
      "title": "Slide title",
      "message": "One core claim this slide proves",
      "bullets": [
        "3-5 concise points that support the message",
        "Each point should be a fact, argument, or data highlight",
        "Keep bullets parallel in structure and length"
      ],
      "layout": "cover | section | comparison | dashboard | timeline | matrix | chart | cards | closing",
      "visual": "Main chart/card/composition to build",
      "evidence": ["Public-research claim | source name | https://actual-source.example/page; use [] for non-evidence slides"],
      "notes": "Speaker intent, caveats, or visible assumption disclosure"
    }
  ]
}
```

Keep this as a planning artifact. Do not put CSS, HTML, or PowerPoint object
details into `outline.json`.

`design_requirements` is optional, but when the user explicitly names colors,
palette roles, a visual geometry such as a pyramid, flow direction, or an exact
item count, preserve that wording here and on the affected slide. The scaffold
turns those requirements into a persisted `design_contract`; explicit entries
are hard constraints and may not be weakened by theme or layout inference.

`audience` and `storyline` may each be either one non-empty string or a
non-empty array of strings. The validator and scaffold normalize the array form
to newline-separated text so a natural multi-audience or multi-beat outline
does not get trapped in a repair loop.

## Generation Steps

For a broad project review, sales proposal, or operating analysis request with
no supplied page order, query the registered soft recipes before drafting:

```bash
${BOX_AGENT_NODE:-node} scripts/query_outline_recipes.js --text "项目复盘"
```

The matched beats are a completeness checklist, not a fixed deck. Omit beats
without source support, merge closely related beats, split dense beats, and
preserve any explicit user order or page count. The registry lives in
`references/outline-recipes.json`; do not copy its full contents into this file.

1. Restate the user request as a deck goal, audience, and decision/action the
   deck should drive. Stay within facts supplied by the user or clearly marked
   assumptions that the user explicitly authorized.
2. Pick a storyline arc before listing slides only when the user did not already
   provide a clear order. Examples:
   - BP: problem -> solution -> product -> market -> traction -> business model
     -> competition -> team -> financing ask.
   - Strategy report: context -> diagnosis -> options -> recommendation ->
     roadmap -> risks -> next steps.
   - Analysis deck: question -> data -> findings -> implications ->
     recommendations.
3. Draft one slide per narrative beat, or map directly from the user's supplied
   page list. Each slide must have exactly one core `message`.
4. When available, bind evidence to analytical, chart, market, traction, or
   financial slides. If the user authorized assumed or illustrative evidence,
   say so in `evidence` or `notes` and plan a visible `假设`/`示意` disclosure on
   that slide; do not imply fabricated data is sourced. Assumptions may fill
   disclosed illustrative metrics or scenarios, but never private identity
   facts such as a company/project name, financing round or stage, founding
   date, team member/history/size, client, award, or ranking. For a required
   missing private fact, use `待补充` or `待客户确认` and continue; otherwise omit
   it.
5. Choose the intended `layout` and `visual` for each slide before selecting
   controlled layout ids and filling `deck.json`.
   These are executable semantic requirements, not disposable prompting hints:
   the scaffold copies them into `slide.outline_intent`, may normalize an
   incompatible requested layout, and final QA checks both compatibility and
   explicit counts such as “三段式”, “四条标签”, “四个里程碑”, or “四象限”.
   Write the intended geometry precisely enough that this check is meaningful.
   When a slide contains quantities, rankings, trends, proportions, KPIs,
   market sizing, financials, benchmarks, or operational metrics, the `visual`
   must name a concrete data display such as `KPI strip`, `bar
   chart`, `line chart`, `matrix`, `comparison table`, `heatmap`, or
   `mini-dashboard` whenever the page contains at least two real quantitative
   values; with only one value this remains strongly preferred. Do not use just
   `cards` or `text layout`. For scenario,
   use-case, capability, or demo pages, a plain `cards` layout is acceptable only
   when the cards are content-rich. If each card has just a title and 1-2 short
   lines, the `visual` must name a second layer such as a demo flow, customer
   journey, role swimlane, before/after comparison, KPI strip, icon/owner row,
   maturity ladder, cause tree, or capability matrix. Use the dedicated
   controlled layout when that relationship is the page's primary semantic.
   When a page should be image-led, name the composition rather than only saying
   “配图”: use `wide image feature` / `横向大图叙事` for
   `image-feature-v1`, or `full-bleed image` / `整页生图` for
   `image-full-bleed-v1`. Reserve the full-bleed form for one short message whose
   fixed text-safe region can coexist with a right-side visual focus.
   For an ordinary deck of four or more pages that contains no image-led inner
   page, the scaffold promotes the first eligible qualitative cards page to
   `image-feature-v1`. Data/diagram pages, explicit text-only pages, and content
   requiring real or official imagery are not eligible for this automatic
   promotion.
6. Run `scripts/validate_outline.js outline.json` and fix failures before
   scaffolding `deck.json`.

## Quality Bar

- Every slide has one job. If a slide has two unrelated claims, split it.
- Titles should be short and presentation-ready.
- `message` should be a claim, not a topic label. Prefer "AI cuts manual QC
  scheduling from hours to minutes" over "Product overview".
- `bullets` should have at least 2 items; each supports `message` and maps to
  distinct content on the slide. When a qualitative collection declares an
  item count, provide exactly one bullet per item and keep counts in the title,
  message, and visual consistent. The scaffold splits overflow into linked
  slides; do not trim or merge items to fit a template. Avoid restating the title.
- Avoid repetitive slides with the same title, message, layout, or visual.
- Do not reuse the same evidence as the main support for more than two pages;
  combine repetitive pages or use another supported fact from the local materials.
- For public-research decks, omit nonessential gaps instead of planning visible
  placeholders. Use `暂无可验证公开数据` only for a required unavailable public
  fact; use `待补充` or `待客户确认` for required user/private inputs.
- Avoid sparse content slides that leave the lower half of the canvas empty.
  Divider, cover, quote, and cinematic pause slides may use intentional
  whitespace; normal business/product/demo/training slides should turn spare
  space into a chart, flow, matrix, process schematic, role lane, or other
  message-bearing visual.
- Use a section-divider slide only when it helps pacing.
- Put explicitly authorized assumptions in `evidence` or `notes` and disclose
  them visibly on the affected slide; do not hide missing data.
- For data-heavy slides with at least two distinct normalized real values, require a chart/table/KPI/
  dashboard visual instead of a plain bullet list. With only one value, prefer
  the same visual treatment unless the data is too sparse or text-only output
  was requested. Repeating one metric across message, bullets, and evidence
  counts as one value.
- Keep page numbers consecutive and aligned with the final slide count.

## Validation

Run:

```bash
${BOX_AGENT_NODE:-node} scripts/validate_outline.js outline.json --report qa/outline_check.json
```

The validator treats structure, required fields, page numbering, and the basic
storyline contract as the hard gate. Missing evidence on data-heavy slides,
page-level numeric traceability, public-research source URLs, and similar source
findings are advisory warnings: they never prevent scaffolding or `index.html`.
It is not a substitute for human/model narrative judgment.

After validation passes, pass it directly to the scaffold with
`--outline outline.json`. The scaffold writes `source_outline_page` onto every
slide. When a typed visual collection exceeds one layout's readable capacity,
the scaffold emits multiple deck slides for the same outline page and records a
contiguous `source_outline_item_range` on each split slide; the ranges must cover
the original collection exactly once. For `public_authoritative_research`, it
imports each non-empty
`slides[].evidence` string into `truth_contract.research_facts`. The slide title,
core message, visual intent, and evidence notes must stay on the same numbered
page through the content patch. Compile the validated deck to `index.html`; do
not hand-author the controlled HTML.
