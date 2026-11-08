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
tone/storyline to its role and scene, and activate `creative_image_mode` only
when its mode is `creative`. Do not ask for a choice already present in that
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

## Research and narrative framing

For a broad sourced deck, state one primary audience, the action the deck should
enable, and a provisional argument before gathering page material. A product
promotion/introduction brief normally addresses prospective users or buyers;
an internal campaign plan addresses the marketing team. Honor an explicit
audience or purpose. Do not mix channel plans, content calendars or advice on
how to present the product into a customer-facing story unless requested.
For product promotion, sales or enterprise-value decks, apply
`references/business-narrative.md`: business outcomes, relevant customer cases
and adoption evidence need different research questions from product features.

Use the argument to set research questions. For a persuasive product deck, useful
questions include: what work is difficult today, why the product's mechanism
changes that work, what a complete task produces, what evidence supports the
benefit, and how the audience can adopt it. Adapt these to the topic and brief;
they are not a mandatory chapter template or a fixed search/page count.
Pass the selected questions and public-source boundaries to `research-synthesis`.
Use broad discovery to find relevant facets, then read sources for the missing
claims. Product features and user totals alone do not answer case or outcome
questions. Before stopping, check whether the central argument has support and
record material gaps; do not inflate dimension files or repeat searches to
reach a count. Keep the research workflow's selected budget and blocked-source
stopping rules.

Turn facts into a sequence of claims the audience can follow. A useful product
arc is problem/change → mechanism → task case → evidence and limits → adoption.
A case should explain the input, important steps, deliverable and supported
result; a list of occupations or customer names is not a case. If only a proposed
demonstration is possible, label it as such and omit unverified outcome metrics.
For each page, connect evidence to its implication for this audience, without
converting popularity into effectiveness or correlation into causation. Put the
page's role and transition in `notes`. The visible `message` addresses the
audience, not the author with advice about making slides or marketing content.

Review the candidate outline before design:

- **Coverage:** Can the audience reach the requested understanding or decision
  from these pages? Map required topics and the central conclusion to supporting
  pages. Restore missing evidence, explanations or comparisons; a shorter deck
  that drops them has not improved. Respect supplied page order and count.
  For explanations, can the intended reader reconstruct how and why it works,
  including prerequisite concepts and intermediate causal steps? Topic labels
  alone do not supply a mechanism. Recheck essential links against the accepted
  evidence after research validation; a usable subset can still leave a core gap.
  When recommending adoption or expansion, connect the promised benefit to an
  observable outcome and the decision criteria. Feasibility and acceptable cost
  alone do not demonstrate that benefit. State what would support proceeding,
  keeping the current approach or stopping; do not invent result thresholds.
- **Contribution:** Read each page's message against every other page. In its
  existing `notes`, state the question answered and the specific new evidence,
  explanation or inference; naming a role such as "value" or "summary" is not
  enough. If a page merely restates an earlier answer, merge its useful detail
  into that page and remove it. Rewrite overlapping pages around different jobs
  only when each needs distinct reasoning. A new date, label or citation does
  not rescue an otherwise repeated explanation; move its isolated useful detail
  into the relevant page. Apply the edits before validation;
  a review that only lists repetitions leaves the problem unresolved.
- **Source fidelity:** Compare central factual claims in titles, messages and
  bullets with the returned source context, not just matching fact IDs. Keep
  the actor/object, applicable scope, timing, units and qualifier strength
  (approximation, bounds and conditions), including translated or summarized
  wording. Combining two facts must not transfer
  one system's result or policy to another. Carry known material conditions
  beside the claim; a generic "confirm later" note does not replace them.
  If a necessary condition is unknown, qualify or omit the assertion. Record
  the check briefly in existing notes; correct the ledger and revalidate when
  the factual claim itself needs repair. Review `visual` as part of this meaning:
  arrow direction, branching, comparison and qualifiers must express the same
  supported relationship as the text. Parallel consequences are not a causal
  sequence; a risk factor is not automatically a necessary or sufficient condition.

A feature list, a generic workflow and a summary often restate one benefit.
A concrete worked task earns space by showing inputs, choices, handoffs or
outputs the earlier mechanism did not explain. Likewise, "who it fits", "how to
try it" and "recommendation" need separate pages only when they contribute
different criteria or actions. A closing page may briefly recall the decision,
but should not repeat the feature/cost catalogue. These are contribution tests,
not mandatory page types. Tutorials, scientific explanations and supplied
agendas can legitimately revisit a fact while building new reasoning.

Keep headlines at the strength of their support. Preserve source dates, metric
definitions and material caveats in near-final visible copy, beside the claim
they qualify, not only in a visual suggestion, optional footer or private notes.
Use concise attribution; detailed retrieval diagnostics belong in the research
handoff, not in place of the audience's story. After content filling or a layout
correction, compare the actual page content with these claims and qualifiers,
including essential text embedded in figures. For controlled diagrams, use the
finalizer's rendered-text-loss findings: matching layout dimensions do not prove
that a label survived. When optional visual inspection runs, also read text in
images. Use complete shorter labels through the editable content fields, keeping
the necessary explanation and conditions visible. Check that the explanation
survived as well as the numbers: a case needs its task and intervention alongside
the outcome; a mechanism needs its causal steps. Restore a lost link in visible
copy instead of accepting a page that contains only its metric highlights.
The structural validator cannot judge narrative quality or factual entailment.

## When the request is under-specified or under-sourced

A new deck must not start from too little. But "too little" is never answered
with a cold refusal — it is answered by **asking back** or **routing to
research**. Pick the branch that fits:

1. **Content is sufficient.** The user or an upstream expert/research step
   already gave usable page-level material (titles, order, key points, data).
   Treat it as the source of truth and build the outline. Do not invent a
   competing storyline over usable input.

   Raw supplied facts plus requested headings are not finished page copy. For
   that input, map the facts to the distinct question each requested heading
   answers before drafting. Preserve the requested page count and order; apply
   the existing contribution check locally so pages do not repeat the same fact
   set without adding a supported explanation. Reusing a fact to support a
   different point is valid. Explain only what the supplied material supports,
   and label derived calculations as derived. If a required section has no
   supported distinct content, state the specific missing detail instead of
   copying another section or inventing a workflow. This adds no external
   research or review role. Do not reorganize finished or verbatim page copy
   that the user has asked to preserve.

2. **The request is a solution/design brief.** A prompt that names the goal,
   requested system or product components, integrations/processes to cover, and
   approximate page count is sufficient for a proposed architecture or
   implementation plan. Use `source_mode=user_provided`, frame unsupplied
   details as recommendations, and do not load `research-synthesis` or browse by
   default. If a named external product/API claim genuinely needs confirmation,
   use at most two targeted official-source lookups; switch to the research
   branch only if the requested deck becomes evidence-driven.

3. **Only the structure/framing is unclear** (facts are available, but audience,
   ordering, emphasis, or plan-vs-build is ambiguous). If the brief supports a
   conventional, reversible framing, state it and proceed. An unspecified page
   count alone needs no question. If competing choices would materially change
   the deliverable, these are finite preferences, not missing facts: never use
   `request_user_input` for them. Call `request_user_decision` with one focused question and 2-6 options,
   put the recommended option first, and—when it is low-risk, reversible, and
   preserves the request—set it as the default with a 30-second timeout. Examples:
   - "这套面向投资人还是内部评审?大概几页?" (then propose a default count)
   - "你已有这些要点,我按 问题→方案→市场→进展 的顺序排,可以吗?"
   Do not stall with an open-ended "tell me more", and do not silently guess an
   entire narrative the user never asked for.

4. **The topic needs facts you don't have** (claims, market/industry/company/
   policy data, anything that must be sourced) and the material is thin or
   absent. **Do not fabricate, and do not write a flat rejection.** Load the
   research workflow first, then build the outline from the sourced findings:

   ```
   get_skill(skill_name="research-synthesis")
   ```

   Follow the selected `research-synthesis` route's coarse-to-fine evidence
   budget and persist its research artifacts. Do not replace that workflow with
   a PPT-specific four-query scan: a one-line request for a sourced factual
   report still needs enough landscape, authority, and conflict coverage to
   support the slide plan, while a concise proposal brief stays in branch 2. After
   the route is complete, run its bundled artifact validator with
   `--report research/qa/{topic}_research_check.json`; even a reduced sequential
   run should preserve distinct dimensions, but the dimension target is a
   quality goal rather than a delivery gate. The workflow converts a fresh
   report into the generic `presentation_handoff` contract: `full` uses the
   complete verified facts, `partial` uses only its verified subset, and `framework` creates
   explicit unavailable-data placeholders. Cover, agenda, section-divider and
   nonnumeric call-to-action pages may keep `evidence: []`; framework data pages still
   need an explicit unavailable-data placeholder. Then omit or mark nonessential gaps instead of repeating the same
   queries. If bounded research or its validator is unavailable or incomplete,
   continue with supported findings, omit optional claims, and use explicit
   placeholders for required facts; never delay `index.html`. Pass resulting
   factual statements to the scaffold with repeated `--research-fact`; reserve
   `--fact` for verbatim user/source text. Treat `AuthLevel` as a ranking hint,
   not proof that a result is authoritative. When a first-party domain is known,
   prefer a `site:`-constrained query; discard SEO-looking, mirror, or unrelated
   results. Prefer at least one `claim | source | URL` evidence item on a
   public-research page when a verified source is available. Missing evidence,
   URL, or numeric traceability excludes that claim from the factual handoff,
   but never blocks outline or HTML delivery. Never label a source FIFA/IOC/official unless that returned
   URL belongs to the named institution, and never treat an unverified URL as
   verified evidence. If a `site:` query returns no matching host, never invent
   the expected URL; successfully read a known exact first-party URL, omit the
   optional claim, or use `本轮核验：暂无可验证公开数据` for a required field.
   This describes the current evidence gap, not a claim that no public data exists.
   A normal factual-deck request already permits research
   from public authoritative sources; do not pause later to ask for permission
   to use those sources. Respect a strict/private source boundary, use an
   explicit placeholder when the allowed sources do not contain a required
   fact, and disclose material source conflicts with neutral wording.
   If `research-synthesis` is unavailable in this session, use available
   search/browser tools for at most two targeted official-source attempts per
   unresolved slide-relevant claim and never repeat an equivalent failed
   search. This search is mandatory for a public-fact topic: do not write
   `outline.json` from memory. Start with plain queries (no `site:` operator —
   club/federation domains often return zero results), then `web_extract` the
   pages you will cite, and record each fact with its exact page URL and the
   exact figure/date it supports; every number in the slide's title, message or
   bullets must have the same numeric value in that slide's evidence text. When
   `prepare` returns a `checklist`, fix each listed line before re-running it
   (bind the matching verified fact ID, correct its source-backed claim and rerun
   research validation, or source a missing fact); do not remove supported dates
   or qualifiers to pass a formatting check. If those tools are unavailable or insufficient, omit optional claims
   and use explicit placeholders for required facts — never pause the deck or
   present unsourced content as fact. Keep the boundary clear: this skill turns
   researched/supplied content into a slide plan; it does not itself perform
   deep research, fact-checking, or source-credibility judgement.

Reserve `BLOCKED` for the `creative_image_mode` image-complete requirement only;
still deliver structurally valid degraded HTML. A normal deck that is merely under-specified is handled by a structural-choice
question, bounded research, omission, or an explicit placeholder — never by a
cold "I can't do this".

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

When a research handoff provides IDs, add `evidence_ids` to each factual slide
with the exact IDs of its supporting `verified_facts`, and keep `evidence: []`.
`design_plan.js prepare --research-handoff ...` resolves those references and
writes canonical strings into `evidence` before the design handoff. A standalone
`validate_outline.js --research-handoff ...` checks the same references without
rewriting the file. The validator maintains `evidence_from_ids` to identify only
the canonical entries it generated. Preserve this field during content edits;
do not write it by hand. After correcting a research fact, update its ID and
rerun preparation: only the old generated references are replaced. Explicit
canonical references and older unmarked input still receive strict validation.
Legacy canonical evidence remains supported. Unknown IDs,
unverified claims and cross-entity citations remain errors; never repair them by
dropping a citation while keeping its claim. Do not invent an ID or hand-edit the
research report. Revalidate changed research and use the resulting IDs.

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
6. Run `scripts/validate_outline.js outline.json` with the research handoff when
   applicable, and resolve reported issues before independent content review.
   Recheck changed evidence/copy before its focused review. Preserve the saved
   repair limits. For page production, design preparation and final QA remain
   required; outline-only tasks stop after validation and applicable content review.

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
- Repeated evidence is a review signal, not a duplicate-page count. Retain it
  when another page develops a necessary, different inference; combine pages
  repeating the same answer. Never rotate IDs or reword a fact to evade review.
- For public-research decks, omit nonessential gaps instead of planning visible
  placeholders. Use `本轮核验：暂无可验证公开数据` only for a required public fact
  not established in this investigation, with the specific unanswered condition;
  use `待补充` or `待客户确认` for required user/private inputs.
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

The validator checks structure, required fields and page numbering. With a
research handoff, verified reference identity and entity binding are hard checks;
source gaps and unsupported numbers enter the bounded repair/degradation flow.
Fix reference errors before treating a claim as unavailable. In repair flow,
valid references can be diagnosed while unresolved pages defer dependent support
checks; a binding error never partially writes the input. The saved report permits
at most one reference correction and one content correction, and retains
used budgets after later checks. Keep that report instead of deleting it to retry.
Correct all reported issues and numeric gaps together; the terminal report still
lists unresolved errors. Partial research can support a complete deck using its
verified subset. A generated fallback
file does not mean content validation passed. The validator is not a substitute
for human/model narrative judgment.

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
