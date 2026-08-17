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
- Save every research artifact under `research/`, relative to the current
  working/artifact root; create the directory before writing.
- In officev3 session-output mode, the current working directory is already the
  artifact root whose host path ends in `output/`. Never prepend another
  `output/` segment (for example, do not use `$(pwd)/output/research`).
- Never save research artifacts directly in the current working/artifact root.
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
- If an exact public URL is already known, read it with an actually available
  browser tool. In officev3, standalone Playwright MCP tools are separate from
  the read-only browser gateway: do not request `source_preference: playwright`
  on the gateway as a substitute; use the standalone tools, or gateway `auto` /
  `browser_connector` for real-browser reads.
- Treat search results as discovery, not evidence. Open the source page before
  marking a claim verified, and capture a short excerpt that actually supports
  the claim and names the target entity. Never transfer a search-result snippet
  directly into the verified evidence ledger.
- After bounded search, verify no more than five unique exact article, report,
  filing, or data-page URLs. Never use an origin homepage or retry the same URL
  with the same browser backend. If two consecutive direct reads yield no usable
  source text, stop browsing, mark the remaining candidates `unverified`, and
  finish the handoff so downstream work can continue in `partial` or `framework`
  mode.
- Maintain `research/{topic}_evidence.json` while researching. Every downstream
  factual claim must be bound to one target entity, source URL, source type,
  page excerpt, confidence, and `verified` / `conflicting` / `unverified`
  status. Use the exact schema in
  [output_contract.md](references/output_contract.md).
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
- Use standard Markdown footnotes in research artifacts: `[^id]` inline plus
  `[^id]: Title. Date. URL` definitions.

## Start

1. Treat the current working directory as `{workspace}` and the artifact root.
2. Resolve `{skill_dir}` to the active installed directory containing this
   `SKILL.md`. Do not assume the current working directory is the skill
   directory.
3. Create `research/` directly under that root.
4. Pick a stable topic slug:
   - Prefer short ASCII, lowercase, hyphenated words.
   - If the topic is mostly non-ASCII or ambiguous, use
     `research-YYYYMMDD-HHMMSS`.
5. State the selected route and one-sentence rationale before researching.
6. Follow the route details in [routes.md](references/routes.md).

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

All files live under `research/`, relative to the current working/artifact root.

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
${BOX_AGENT_PYTHON:-python3} "$RESEARCH_SYNTHESIS_SKILL_DIR/scripts/validate_research_artifacts.py" --research-dir "research" --topic "{topic}" --route A --report "research/qa/{topic}_research_check.json"
```

Adjust `--route` for the selected route. `--min-dimensions N` is a recommended
quality target, not a deck-delivery threshold. Record the actual reduced budget
in the cross-verification file. The validator emits a producer-neutral
`presentation_handoff` object. Downstream presentation workflows consume its
`delivery_mode`, `verified_facts`, `gaps`, and `quality_summary`; they do not
interpret this skill's internal QA state. `full`, `partial`, and `framework`
remain deliverable even when research quality is incomplete. Only verified rows
that passed entity, excerpt, source, first-party, and conflict checks enter
`verified_facts`; conflicting and unverified rows stay in gaps/limitations.

## Final Handoff

If a report-writing, paper-writing, document, or presentation skill is available
and the user requested that output type, hand off explicit file paths and state
the generic `presentation_handoff.delivery_mode`. For `partial`, downstream
work uses only the verified subset and marks or omits gaps; for `framework`, it
creates a usable placeholder structure without external factual claims. For a
presentation, hand off only `presentation_handoff.verified_facts[].canonical`;
prose artifacts are context, not an independent fact source. Otherwise, produce
`{topic}_final.md` from the verified research artifacts.

For technical product or codebase research tasks:

- Preserve product/runtime ownership boundaries in the synthesis when relevant.
- Distinguish UI assumptions from runtime evidence.
- Call out whether a finding is source-tree evidence, runtime-bundled evidence,
  log evidence, user-file evidence, or external-source evidence.
- Keep recommendations narrow and reversible unless the user asked for broader
  strategy.
