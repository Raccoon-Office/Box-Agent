---
name: research-synthesis
description: >
  Evidence-first research synthesis workflow for market research, industry
  analysis, competitive research, policy or company information synthesis,
  file-only or file-augmented analysis, technical architecture reviews, risk
  assessment, and report-ready conclusions. Use when the user asks for
  comprehensive, cross-verified research or deep summarization rather than a
  simple lookup.
keywords:
  [
    research,
    synthesis,
    summary,
    summarization,
    report,
    market,
    industry,
    competitor,
    policy,
    company,
    evidence,
    sources,
    deep-research,
    cross-verification,
    source-backed,
    market-research,
    industry-analysis,
    competitive-research,
    policy-analysis,
    company-research,
    risk-assessment,
    technical-research,
    file-analysis,
    multi-agent,
    parallel-research,
    深度总结,
    总结,
    研究,
    行业研究,
    行业分析,
    市场研究,
    竞品分析,
    政策分析,
    公司研究,
    资料综述,
    报告,
    证据,
    来源,
    交叉验证,
    多智能体,
    并行研究,
  ]
metadata:
  short-description: Evidence-first research synthesis
---

# Research Synthesis

Use this skill for substantial research where the answer must be grounded in
real files, logs, sources, or current external evidence. Do not use it for
simple factual lookup, one-source Q&A, or ordinary code changes.

## Hard Rules

- Inspect the real provided files, logs, repo paths, or source artifacts before
  explaining behavior or generating conclusions.
- Resolve one absolute `{output_dir}` from the current conversation before
  writing. If the model created a task directory, use it; otherwise use the
  unchanged session cwd. Save every research artifact under
  `{output_dir}/research/` and pass absolute paths to tools and scripts.
- Reserved research artifact templates override the generic hyphen-separated
  filename style. Keep hyphens inside `{topic}`, but join every reserved suffix
  with the literal underscore shown in the template. For topic
  `ai-quality-scheduling`, write `ai-quality-scheduling_dim01.md` and
  `ai-quality-scheduling_cross_verification.md`; never write
  `ai-quality-scheduling-dim01.md` or
  `ai-quality-scheduling-cross_verification.md`. Copy the Required Outputs
  filenames exactly for every dimension and handoff artifact.
- Do not infer an output root from environment variables or legacy host
  metadata. The chosen `{output_dir}` is ordinary task organization and does
  not change the session cwd or workspace.
- Never save research artifacts directly in `{output_dir}`; keep them under its
  `research/` child.
- Avoid CLI-specific assumptions. Use only tools actually available in the
  current host runtime.
- Use native subagents only when the user explicitly asks for multi-agent,
  parallel-agent, or delegation work, or when the host runtime has
  clearly authorized subagent use for the task.
- When subagents are authorized, use multiple subagents for independent facets
  or dimensions instead of defaulting to a single delegated worker.
- If subagents are not authorized or unavailable, run the same workflow locally
  in sequential rounds with a smaller but explicit evidence budget.
- For file-only requests, do not perform external search.
- For current or time-sensitive claims, check the current date/time before
  analysis and make search windows explicit.
- Match the user's search language unless the task requires a specific locale.
- Search depth comes from covering distinct evidence gaps, not from rephrasing a
  query that already returned usable evidence. Before every later batch, name
  the still-uncovered dimension or conflict; do not rerun a near-equivalent
  entity/fact query merely because an authority-ranked or `site:` query was empty.
- Treat HTTP 403/429 responses, CAPTCHA or challenge pages, robots or permission
  denials, repeated timeouts, and repeatedly empty or unreadable responses as
  blocked-source signals. For the same document or access barrier, allow at most
  one materially different permitted recovery after the first failure; do not
  repeat the same URL/backend or bypass an access restriction.
- After two consecutive blocked-source failures for the same document or access
  barrier, stop that retrieval path for the current turn. Promptly tell the user what
  source is blocked, what was attempted and which claim remains unsupported;
  do not silently spend the remaining search budget on variants of that path.
  An independently accessible source already found in search or supplied links
  may still answer the same question. It is new evidence, not a retry of the
  blocked document: preserve its own provenance and limits. Different URLs that
  encounter the same challenge or permission barrier do not reset the stop rule.
  Continue useful research and disclose any remaining gap honestly.
- Select the page-reading channel with the runtime's shared **Web Access Routing**
  policy; this Skill adds evidence requirements, not another channel priority.
  When the selected channel is the managed browser, activate
  `managed_browser_navigate` and `managed_browser_snapshot` together, then
  verify one URL at a time with navigate followed by snapshot.
  Navigation metadata alone is not a failed body read; the snapshot body is the
  evidence content bound to that URL. A body-reading result is not proof that a
  visible browser was opened. Do not route between browser modes through a
  `source_preference` argument.
- Search-result summaries may support medium-confidence evidence when each row
  is bound to that result's exact URL, uses `evidence_basis=search_summary`, and
  copies an excerpt returned for that same result. Never cross-bind one result's
  summary to another URL or locally rewrite the excerpt. Read the exact page when
  stronger evidence is useful or the summary does not support the claim.
- Inspect the reading result before labeling its provenance. A reader's
  generated page summary uses `evidence_basis=page_summary` with medium
  confidence; only actual source text uses `page_body`. Copy returned text
  faithfully and do not call a generated summary the publisher's quotation.
  The tool name or a successful read alone does not establish raw-page evidence.
  If a reader can summarize and its returned output does not explicitly identify
  raw text, default to `page_summary`; formatting and confident wording are not
  provenance. Use a non-summarizing reader for claims that need direct support.
- Claims depending on an interactive selection need evidence of that state.
  A generated summary cannot prove a selected billing period, filter or option
  that the reading tool did not observe. Reserve budget for explicit primary
  terms or the relevant page interaction under **Web Access Routing**. If that
  condition remains unverified, keep it unknown and omit the dependent assertion
  from factual handoff; an exact-looking figure plus a later-check note is not
  a substitute.
- Use eight discovery reads and four verification reads as initial planning
  allowances, not quotas or hard caps. Allocate them to questions that could
  change the central explanation or audience decision. Draft once there is
  enough support for the requested depth; do not spend unused reads on more
  background. When an isolated content review is planned, retain verification
  capacity for its findings, while checking known governing conditions during
  discovery. The reviewer may make at most two targeted search queries when
  external research is permitted; the author owns source reads and the ledger.
  At an allowance checkpoint, a material gap with a promising observed source
  can justify another batch of at most two reads. Before each extension, record
  the exact question, why it matters and the next observed URL in the existing
  question map. Reassess after that batch. Stop extending a question when a batch
  adds no material support, no permitted promising source remains, or the
  requested decision is adequately supported with its limitations. Do not
  broaden the requested task to keep researching. Explicit user time, cost and
  source limits still apply; an internal planning allowance needs no new approval.
  Count each external page-reading request as one attempt and one row in the
  existing cross-verification artifact, across channels and agents. Record the
  phase, attempt number, target gap and URL/backend before the request, then
  append its outcome. Failures, backend fallbacks and extensions all count;
  repeated URLs do not merge attempts. Local reading of a saved capture is free
  of this external count. Keep phase totals and extension reasons visible, not a
  separate parallel log. Use already collected source context before another read.
  Spend verification on a missing premise, material condition or conflict, not
  cosmetic improvements to an already supported claim. Use URLs observed in
  search results, page links or supplied sources; do not guess documentation
  paths. Read the exact article, report, filing or data page. A product, pricing
  or homepage is valid when its own body contains the needed evidence; it does
  not substitute for an unread linked document. Do not repeat the same URL with
  the same backend or issue parallel Playwright navigations; finish each
  navigate/snapshot pair first. Apply the document/access-barrier stop above,
  leave unsupported claims unverified and finish with the usable evidence.
- Maintain `{output_dir}/research/{topic}_evidence.json` while researching. Every downstream
  factual claim must be bound to one target entity, source URL, source type,
  page excerpt, confidence, and `verified` / `conflicting` / `unverified`
  status. Use the exact schema in
  [output_contract.md](references/output_contract.md).
- Keep the ledger claim in the source excerpt's language; translate only the
  downstream narrative. Record retrieval time in research notes or metadata,
  not as a source claim or inside `evidence_excerpt`. If validation rejects a
  date/subject, narrow the claim or bind separate literal heading/context using
  `source_capture` and `evidence_context` as described below. Never
  insert a retrieval date, entity label or paraphrase into a quotation just to
  pass validation. Mark search-only evidence with `evidence_basis=search_summary`.
- Plan a direct reading channel for decision-critical conditions during
  discovery; do not spend the whole budget on summaries first. Use the direct
  source reading procedure below when a summarizing reader cannot supply it.
- Before handoff, directly verify decision-critical operating conditions such
  as pricing, usage allowances, eligibility and data-handling commitments against
  the relevant first-party terms or technical policy, not only an overview.
  If support is only a search/reader summary, use available discovery or reserved
  reads to inspect actual primary text or the necessary page state under
  **Web Access Routing**. A second generated summary is not direct verification.
  Prioritize conditions that could change the decision; if budget or access
  prevents verification, exclude the dependent exact assertion and carry the
  specific gap forward. Retain supported capabilities and cases, rather than
  replacing the whole story with warnings.
- Before validation, compare each central claim with its returned source context,
  including adjacent restrictions and qualifications in other collected sources.
  Keep its actor, scope, timing, unit of analysis and numeric qualifiers,
  distinguishing aggregate from individual results and observed from projected
  outcomes; an atomic claim is not a claim stripped of necessary conditions.
  For case or study evidence, retain the material population, starting event or
  intervention, and measured outcome. When applying it outside that setting,
  identify the inference or analogy; a similar outcome does not establish the
  same causal conditions.
  For an entitlement or policy commitment listed in an overview, follow any
  collected link to its governing terms within the reading budget. Verify the
  covered components, options and exceptions before asserting the commitment.
  If those terms were not inspected, narrow the claim to the overview's listed
  offering and mark applicability unverified, or omit the optional guarantee.
  A first-party badge or plan label does not establish a blanket guarantee.
  Follow the source-fidelity rules in `output_contract.md` when shortening or
  translating. Passing token/number checks does not establish semantic support.
- Record known official domains for target entities and prefer verified
  first-party sources when a claim concerns that entity's own disclosure.
  Missing per-entity or first-party coverage is a research-quality warning, not
  a reason to discard other verified evidence or block downstream delivery.
  `first_party` is a claim about URL ownership, not a synonym for a high search
  authority score.
- When user-provided material conflicts with a source, preserve the user claim,
  mark `user_input_alignment=conflicting`, explain the conflict, and exclude
  that record from downstream synthesis until resolved. Never silently prefer
  or merge one side.
- Apply the same conflict boundary between collected sources. Resolve scope,
  version and date before preferring one; an official source is not automatically
  current or controlling. If conflicting claims remain unresolved, mark them
  `conflicting` and exclude them from factual handoff. A conflict listed only in
  research notes must not become an unqualified downstream assertion.
- Use standard Markdown footnotes in research artifacts: `[^id]` inline plus
  `[^id]: Title. Date. URL` definitions.

## Direct source reading

Follow the host's **Web Access Routing** and any user-specified browser/source.
Use an available raw reader for static text and a browser for necessary page
state. When only a summarizing reader is available, the bundled
`scripts/capture_source.js` can read one permitted URL through the existing host
Playwright runtime, without an LLM, clicks, login or a new installation:

```bash
"$BOX_AGENT_NODE" "$RESEARCH_SYNTHESIS_SKILL_DIR/scripts/capture_source.js" "<OBSERVED_URL>" --out "{output_dir}/research/sources/read-01.json"
```

Resolve the skill path from the loaded skill; use the known Node executable if
`BOX_AGENT_NODE` is absent. The helper requires the host's Playwright module and
browser paths. If unavailable, use another authorized raw reader/browser or keep
the specific gap; do not install a substitute or bypass access restrictions.
Each navigation attempt counts against the existing reading budget.

Read the resulting `text`, `title`, `controls` and `final_url`. They describe one
observed main-document state, not every option, subframe or future state. Keep the
capture unchanged and cite its path in the dimension/question map; supply it to
content review. For every ledger row using this capture, set `source_capture` to
its path relative to `research/` and `source_url` to its `final_url`. Copy one
literal passage into `evidence_excerpt`; if its subject or date is elsewhere,
put those separate literal passages in `evidence_context` (an array). The
validator checks each passage against this file before using their combined
context. Read or slice the saved text programmatically when needed; do not
retype, join distant passages into a quote, or replace a pronoun to pass a check.
Keep conclusions in `claim`, not in the quoted fields. Legacy rows without a
capture remain compatible, but do not claim their quotations were checked by
this binding. Record observed control state
as observation, not as a publisher quotation; keep retrieval metadata separate.
Missing controls, truncation or `network_idle=false` cannot prove absence or full
loading. Capture success does not establish that the page is a valid article or
that a claim follows from it. Stop on login/challenge/access blocks under the
existing routing rules; do not cite those screens as the requested source.

## Start

1. Set `{output_dir}` to the absolute directory selected in the current
   conversation. Use a model-created task directory when one exists; otherwise
   use the unchanged session cwd. Keep this choice consistent for related
   follow-ups.
2. Resolve `{skill_dir}` to the active installed directory containing this
   `SKILL.md`. Do not assume the current working directory is the skill
   directory.
3. Create `{output_dir}/research/`.
4. Pick a stable topic slug:
   - Prefer short ASCII, lowercase, hyphenated words.
   - If the topic is mostly non-ASCII or ambiguous, use
     `research-YYYYMMDD-HHMMSS`.
5. State the selected route and one-sentence rationale before researching.
6. Follow the route details in [routes.md](references/routes.md).

Before spending the reading budget, map the questions needed for the requested
answer in the existing cross-verification or insight artifact:
`question | support needed | candidate/source | finding and limit | status`.
Choose the kind of support for the claim: a specification establishes a feature,
an adoption count establishes reach, a described case establishes that reported
use, and an outcome comparison needs its baseline, population and conditions.
None substitutes automatically for the others. Allocate reads to the gaps that
could change the conclusion, and reuse a source when its body answers several
questions. Revise the provisional argument when evidence does not support it.
Keep observation, source interpretation and your recommendation distinguishable.
Before calling a required answer unavailable, inspect the relevant parts of the
complete saved source, including observed options and linked sections already
captured. A displayed excerpt or first read window is not the whole source.
Search local captures using the unresolved question's terms, then read the
matching context and follow pagination; preserve any actual truncation/access
limit. Use already collected support before spending another web read or writing
a generic unknown. Describe the limit as this investigation's unverified or
unobtained evidence, not proof that no public information exists. This local
check adds no external research round.
At handoff, update the map with accepted fact IDs or a specific unresolved gap;
artifact/dimension counts alone do not establish substantive coverage. File-only
and noncommercial tasks use the same reasoning with their permitted evidence,
without adding web searches, customer cases or purchase questions they do not need.

## Routing

| Signal | Route |
| --- | --- |
| Files plus "only based on files", "no search", or equivalent | C: File-only |
| Files plus "refer to", "combine with", or no explicit restriction | D: File-augmented |
| No files plus broad landscape question | A: Wide search |
| No files plus specific bounded question | B: Focused search |

When unclear, prefer Route D over Route C for file tasks, and Route A over
Route B for broad multi-faceted topics. If that would conflict with an explicit
user constraint, honor the constraint.

## Subagent Policy

This skill supports multi-agent research but does not require it.

When subagents are authorized:

- Dispatch multiple bounded, independent research tasks using the host's native
  subagent tool. Prefer one subagent per facet, dimension, or validation
  conflict when those tasks can write separate artifacts.
- Default to up to 8 concurrent subagents per round unless the host states a
  different limit. For larger decompositions, launch additional bounded rounds.
- Give each agent the mission, context, allowed/disallowed sources, exact output
  format, and exact output path.
- Do not assume subagents inherit main-agent context; pass paths or excerpts.
- Do not launch duplicate agents on the same question.
- Do not let two subagents write the same artifact path; merge or synthesize in
  the main agent after their outputs are complete.

When subagents are not authorized or unavailable:

- Execute dimensions sequentially in the main agent.
- Preserve the same artifact names and citation contract.
- Reduce search counts pragmatically and record the reduced budget in
  `{topic}_cross_verification.md`.

Use [prompts.md](references/prompts.md) for subagent/local-round templates.

## Required Outputs

All files live under `{output_dir}/research/`.

| File | Route | Purpose |
| --- | --- | --- |
| `{topic}_file_analysis.md` | C, D | File inventory, extraction, cross-file mapping |
| `{topic}_wideNN.md` | A | Wide exploration facets |
| `{topic}_dimNN.md` | A, B, C, D | Dimension-specific evidence |
| `{topic}_validationNN.md` | Optional | Conflict-specific validation output before merge |
| `{topic}_cross_verification.md` | A, B, C, D | Confidence tiers and conflict zones |
| `{topic}_insight.md` | A, B, C, D | Cross-dimension insights |
| `{topic}_evidence.json` | A, B, C, D | Entity-bound evidence ledger and verified handoff |
| `{topic}_final.md` | Optional | Final Markdown report when no writing skill is available |

Before final handoff, run:

```bash
RESEARCH_SYNTHESIS_SKILL_DIR="${BOX_AGENT_RESEARCH_SYNTHESIS_SKILL_DIR:-${RESEARCH_SYNTHESIS_SKILL_DIR:-{skill_dir}}}"
if [ ! -f "$RESEARCH_SYNTHESIS_SKILL_DIR/scripts/validate_research_artifacts.py" ] && [ -f "$HOME/.box-agent/skills/research-synthesis/scripts/validate_research_artifacts.py" ]; then
  RESEARCH_SYNTHESIS_SKILL_DIR="$HOME/.box-agent/skills/research-synthesis"
fi
if [ ! -f "$RESEARCH_SYNTHESIS_SKILL_DIR/scripts/validate_research_artifacts.py" ] && [ -f "$HOME/.box-agent/skills/deep-research-swarm-officev3/scripts/validate_research_artifacts.py" ]; then
  RESEARCH_SYNTHESIS_SKILL_DIR="$HOME/.box-agent/skills/deep-research-swarm-officev3"
fi
if [ ! -f "$RESEARCH_SYNTHESIS_SKILL_DIR/scripts/validate_research_artifacts.py" ]; then
  echo "ERROR: validate_research_artifacts.py not found under $RESEARCH_SYNTHESIS_SKILL_DIR/scripts" >&2
  exit 1
fi
${BOX_AGENT_PYTHON:-python3} "$RESEARCH_SYNTHESIS_SKILL_DIR/scripts/validate_research_artifacts.py" --research-dir "{output_dir}/research" --topic "{topic}" --route A --report "{output_dir}/research/qa/{topic}_research_check.json"
```

Adjust `--route` for the selected route. Choose `--min-dimensions N` from the
distinct question groups planned before research, and record that plan and the
actual coverage in cross-verification. It is a coverage diagnostic, not a
deck-delivery threshold: do not split duplicate files to reach it or lower it
afterward merely to clear a warning. The validator emits a producer-neutral
`presentation_handoff` object. Downstream presentation workflows consume its
`delivery_mode`, `verified_facts`, `gaps`, and `quality_summary`; they do not
interpret this skill's internal QA state. `full`, `partial`, and `framework`
remain deliverable even when research quality is incomplete. Only verified rows
that passed entity, excerpt, source, first-party, and conflict checks enter
`verified_facts`; conflicting and unverified rows stay in gaps/limitations.

When correcting the ledger, locate the intended record by its exact current
entity, claim and source, or an existing unique record key; assert one match.
Do not use an array position alone as its identity. Read that record before and
after the edit, checking that its excerpt, context and capture still belong to
the intended fact. A rejected row is not permission to replace its subject or
source with another record's material. Preserve unrelated rows, then revalidate.

After validation, resolve every row marked `verified` that the validator
rejected: repair it from the actual source or mark it `unverified` with the
specific reason, then rerun validation. Do not leave failed rows certified in the
ledger merely because other facts are usable. Partial handoff remains valid for
explicitly unresolved evidence.

After validation, reconcile the question map with the accepted fact IDs. If a
necessary explanation or decision premise lost its evidence, inspect why before
handoff. For an entity/excerpt mismatch, use genuine aliases or faithfully copied
subject/heading context already present in the source; do not fabricate context
or broaden the claim to force acceptance. Repair and revalidate usable evidence
before treating the premise as unavailable. If it remains unsupported, carry the
specific unanswered question into the handoff rather than silently dropping an
essential causal link or decision premise. Counts of accepted rows are not this
coverage check.

Before final handoff, reconcile derived dimension notes, insights,
cross-verification summaries and coverage labels with the final evidence state,
including later content-review corrections.
A correction to scope, translation or qualifier strength also updates affected
summary passages even when the ledger fact and its ID did not change.
A claim excluded from usable evidence must not remain presented as supported.
After evidence changes and revalidation, carry the current ledger and validation
report's provenance, confidence, scope, validation status and unresolved items
through related conclusions and coverage labels. Update only affected passages
and page references, preserving unchanged material and raw captures. Retain
failed attempts and earlier-stage findings as labeled history; do not upgrade
unrelated unverified items. This is a local consistency check, not a new research round.

## Final Handoff

If a report-writing, paper-writing, document, or presentation skill is available
and the user requested that output type, hand off explicit file paths and state
the generic `presentation_handoff.delivery_mode`. For `partial`, downstream
work uses only the verified subset and marks or omits gaps; for `framework`, it
creates a usable placeholder structure without external factual claims. For a
presentation, factual content comes only from
`presentation_handoff.verified_facts[].canonical`; also pass each fact's generated
`id` and declared `aliases` so the consumer can resolve references without
rewriting or fuzzy-matching claims. Preserve supported dates and conditions
in those claims. Prose artifacts are context, not an independent fact source. Otherwise, produce
`{topic}_final.md` from the verified research artifacts.

For technical product or codebase research tasks:

- Preserve product/runtime ownership boundaries in the synthesis when relevant.
- Distinguish UI assumptions from runtime evidence.
- Call out whether a finding is source-tree evidence, runtime-bundled evidence,
  log evidence, user-file evidence, or external-source evidence.
- Keep recommendations narrow and reversible unless the user asked for broader
  strategy.
