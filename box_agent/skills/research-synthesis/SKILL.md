---
name: research-synthesis
description: >
  Source-backed research synthesis for market, industry, company, policy,
  technical, risk, and file-based analysis. Use for substantial research that
  needs current evidence or cross-source conclusions, not simple lookups.
keywords:
  [research, synthesis, deep-research, evidence, sources, market-research,
  industry-analysis, competitive-research, policy-analysis, company-research,
  technical-research, file-analysis, 研究, 行业研究, 市场研究, 竞品分析,
  政策分析, 公司研究, 资料综述, 交叉验证]
metadata:
  short-description: Source-backed research synthesis
---

# Research Synthesis

Use this skill when the answer needs real files or current external sources.
Do not use it for a simple factual lookup, one-source Q&A, ordinary code work,
or a summary that can be completed from the material already in the prompt.

## Core Rules

- Inspect supplied files or source artifacts before drawing conclusions.
- Keep all saved research artifacts under `research/` relative to the current
  artifact root. In officev3 output mode, do not add another `output/` segment.
- File-only requests never trigger external search.
- Search in the user's language unless the task requires another locale.
- Search to close distinct evidence gaps. Do not repeat a query with superficial
  wording changes after it has returned usable candidates.
- Search results are discovery only. A fact may be marked `verified` only after
  opening the exact article, report, filing, or data page and copying a short
  supporting excerpt from that page.
- In officev3, use `managed_browser_navigate` plus `managed_browser_snapshot`
  for independent public pages. Use `user_browser_*` when the read depends on
  the user's current page, login state, cookies, extensions, or intranet. A
  configured `web_extract` tool may be used for direct public HTTP(S) pages,
  but search snippets and generated summaries are not verbatim excerpts.
- Verify no more than five unique exact source URLs after search. Do not retry
  the same URL with the same browser backend. After two consecutive direct reads
  return no useful source text, stop browsing and mark remaining candidates
  `unverified`.
- Prefer first-party or authoritative sources where useful, but missing
  first-party coverage is not a delivery blocker.
- Preserve genuine conflicts and unresolved gaps. Do not search indefinitely to
  force a single answer.
- Before handoff, compare rows describing the same fact. Use optional
  `fact_key`, `fact_value`, `time_basis`, `scope`, and `unit` for quantitative,
  time-sensitive, forecast, ranking, or disputed facts so old/new snapshots are
  not blended. Leave unresolved conflicts out of verified downstream facts.
- Use subagents only when explicitly authorized by the user or runtime. Parallel
  agents must cover distinct questions, request network tools explicitly, and
  must not write the shared evidence ledger.

## Routes

Select one route and state it briefly before researching:

| Signal | Route |
| --- | --- |
| Explicitly restricted to supplied files | C: File-only |
| Supplied files may be supplemented | D: File-augmented |
| Broad external landscape question | A: Wide search |
| Bounded external question | B: Focused search |

Choose the smallest route that answers the request. Do not promote a focused
question to a wide landscape merely to satisfy a template. Detailed guidance is
in [routes.md](references/routes.md).

## Depth

Research dimensions are derived from the question; there is no default,
minimum, or target count. Use one dimension when one is enough, and add another
only when it represents a genuinely different evidence gap, stakeholder,
comparison, conflict, or decision implication.

The same applies to searches and insights. Stop when the important gaps are
covered or the bounded source-reading budget is exhausted. Record material
limitations instead of manufacturing more dimensions.

## Outputs

Default to only two research artifacts:

1. `research/{topic}_research.md` — one consolidated narrative containing the
   useful findings, conflicts, conclusions, and limitations.
2. `research/{topic}_evidence.json` — the source ledger used for factual handoff.

Additional facet, dimension, validation, or file-analysis files are optional.
Create them only when parallel work, unusually large input, or explicit user
requirements make separate files materially useful. Do not create one file per
dimension by default.

Use the compact ledger shape from
[output_contract.md](references/output_contract.md). The required evidence
fields are `claim`, `source_url`, `evidence_excerpt`, and `status`. `entity`,
`source_type`, and `confidence` are useful but optional. Do not require fact
keys, normalized values, time/scope/unit fields, agent shards, or a fixed
Markdown section layout.

## Validation

Run the validator once after the narrative and ledger are ready:

```bash
RESEARCH_SYNTHESIS_SKILL_DIR="${BOX_AGENT_RESEARCH_SYNTHESIS_SKILL_DIR:-${RESEARCH_SYNTHESIS_SKILL_DIR:-{skill_dir}}}"
${BOX_AGENT_PYTHON:-python3} "$RESEARCH_SYNTHESIS_SKILL_DIR/scripts/validate_research_artifacts.py" --research-dir research --topic "{topic}" --route B --report "research/qa/{topic}_research_check.json"
```

Adjust only `--route`. Do not add a dimension target. The validator checks the
small handoff structure and verified source URLs; prose layout, footnote style,
dimension count, claim wording, and optional metadata are not delivery gates.

`full`, `partial`, and `framework` are valid outcomes:

- `full`: verified sources are available and no material gaps were recorded.
- `partial`: use the verified subset and disclose the remaining gaps.
- `framework`: deliver a useful structure without unsupported external facts.

Do not rerun search or rewrite prose merely to turn `partial` into `full`.
Rerun validation only after a real artifact change or when a downstream
checkpoint says the report is stale.

## Final Handoff

If another writing or presentation skill will consume the research, pass the
report path and its `presentation_handoff` object. Factual downstream copy uses
only `verified_facts`; the consolidated Markdown remains narrative context.

If no downstream writing skill is involved, answer from the consolidated
research and verified facts. State meaningful conflicts and limitations without
including an internal QA checklist.

For technical or codebase research, distinguish source-tree evidence, runtime
evidence, logs, user files, and external sources when that distinction affects
the conclusion.
