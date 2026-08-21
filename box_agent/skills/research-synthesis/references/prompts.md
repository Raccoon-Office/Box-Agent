# Research Task Templates

These are lightweight prompts, not mandatory output schemas.

## Independent Research Facet

```text
Mission: Investigate this distinct evidence gap for [topic]: [gap].

Use the user's language. Prefer primary or authoritative sources. Search-result
snippets are discovery only; identify exact source-page URLs. Do not repeat a
near-equivalent query and do not write the shared evidence ledger.

Return:
- concise findings;
- exact source URLs;
- short supporting excerpts when pages were opened;
- conflicts or unresolved gaps.
```

The main agent merges useful results into `{topic}_research.md` and
`{topic}_evidence.json`. Do not create one artifact per agent or dimension
unless the returned material is too large to merge safely in context.
Before marking merged rows verified, compare claims describing the same fact.
For quantitative, time-sensitive, forecast, ranking, or disputed facts, add
`fact_key`, `fact_value`, `time_basis`, `scope`, and `unit`; use different time
bases only when the sources genuinely describe different snapshots.

## Conflict Check

```text
Mission: Resolve or narrow this conflict: [conflict].

Check one genuinely independent source when search is allowed. Preserve the
disagreement when it remains unresolved. Return the exact source URL and excerpt
instead of forcing a resolution.
```

## Final Handoff

```text
Research is complete. Do not launch additional searches or research agents.

Use:
- research/[topic]_research.md for narrative context;
- research/[topic]_evidence.json for provenance;
- research/qa/[topic]_research_check.json for presentation_handoff.

Use only presentation_handoff.verified_facts for downstream factual claims.
Respect full, partial, or framework delivery and do not repair prose merely to
make the internal quality summary cleaner.
```
