# Output Contract

## Directory

All outputs must be under:

```text
{output_dir}/research/
```

## Citation Format

Use Markdown footnotes:

```markdown
This claim has evidence.[^stable-id]

[^stable-id]: Source title. Publication date. https://example.com
```

For file-only evidence:

```markdown
The source file says this.[^file-a-p3]

[^file-a-p3]: File: strategy.pdf, page 3, section "Market risks".
```

Rules:

- Reuse the same id for the same URL or file section.
- Every inline marker must have a definition.
- Every definition should map to one source.
- Keep verbatim excerpts short.
- Preserve source dates; if no date exists, write `N.D.`.

## Required Artifact Shapes

### Entity-Bound Evidence Ledger

Create `{topic}_evidence.json` before validation:

```json
{
  "schema_version": 1,
  "topic": "topic-slug",
  "target_entities": [
    {
      "entity": "Example Corp",
      "aliases": ["Example"],
      "official_domains": ["example.com"]
    }
  ],
  "evidence": [
    {
      "entity": "Example Corp",
      "claim": "Example Corp launched Product One in 2026.",
      "source_url": "https://example.com/news/product-one",
      "source_type": "first_party",
      "evidence_excerpt": "Example Corp launched Product One for customers in 2026.",
      "evidence_basis": "page_body",
      "confidence": "high",
      "status": "verified"
    }
  ]
}
```

Required evidence fields:

- `entity`: exact value from `target_entities`.
- `claim`: one atomic factual statement.
- `source_url`: the opened evidence page, never a search-results URL. For
  `source_type=user_input` in file-only routes, use a stable `file:` or
  `user-input:` reference with page/section context.
- `source_type`: `first_party`, `government`, `regulator`, `filing`,
  `standards_body`, `academic`, `reputable_media`, `secondary`, or
  `user_input`.
- `evidence_excerpt`: short, faithfully copied returned text supporting the
  claim. Identify the entity in that passage or separately bound source context
  below. Keep within copyright limits and label its actual provenance.
- `confidence`: `high`, `medium`, or `low`. A `verified` row cannot be `low`.
- `status`: `verified`, `conflicting`, or `unverified`.

Evidence provenance:

- Set `evidence_basis=search_summary` for a URL-bound search result,
  `page_summary` for a reader's generated summary of that exact page,
  `page_body` for actual source text, and `user_input` for supplied material.
  A page-reading tool call alone does not establish `page_body`: inspect what
  was returned. If a summarizing reader does not identify raw text, default to
  `page_summary`; use a non-summarizing capture for direct evidence. Keep its
  local path in the dimension/question map so review can inspect the actual
  text and observed controls. Summaries normally use medium confidence; preserve their exact
  returned wording without presenting it as a quotation from the publisher.
  If a summary omits a condition needed for the claim, read the original within
  the existing budget, narrow the claim, or leave it unverified. Never reconstruct
  a missing excerpt from memory or from the desired claim.
- When using `capture_source.js`, every row from it includes
  `source_capture: "sources/read-01.json"` (relative to the research directory)
  and uses the capture's `final_url` as `source_url`. Keep `evidence_excerpt` as
  one literal passage. Optional `evidence_context: ["literal heading", "literal
  publication-date passage"]` holds separate source passages identifying the
  subject or time; it requires `source_capture`. Copy actual values, not these
  placeholders. The validator checks the excerpt and every context passage
  against the saved body/title, ignoring whitespace layout differences only;
  it rejects rewritten text, fabricated context and another document's URL.
  Combined bound context may then support entity, number and lexical checks.
  Do not edit the capture to make a record pass. Unbound legacy rows keep the
  existing checks with an explicit warning that the quote was not checked
  against a saved source. The optional capture path, SHA-256 and context are
  retained in the handoff; they do not change canonical text or fact IDs.
- Summarization is not observation of interactive page state. For a claim whose
  value depends on a selected option, retain explicit primary terms or an
  observed page state supporting that condition. A summary's guessed selection
  is insufficient even when it sounds precise; otherwise leave the dependent
  claim unverified and explain the gap.
- Put retrieval time in research notes or an optional `retrieved_at` metadata
  field. It is not the publication date or part of the quoted page. Do not add
  it to the factual `claim` or `evidence_excerpt` to make numeric checks pass.
- The canonical `entity | claim | source_type | source_url` must fit 280
  characters. Keep the claim atomic, especially with long URLs; preserve
  necessary units/conditions rather than combining several claims into one row.

Conflict fields:

- When a source checks a fact supplied by the user, include
  `user_input_claim` and `user_input_alignment` (`supported`, `conflicting`, or
  `unverified`).
- A conflicting row must include `conflict_note`.
- An unverified row must include `unverified_reason`.
- When retrieval stops because of anti-bot, HTTP 403/429, CAPTCHA/challenge,
  permission, timeout, or unreadable-content failures, `unverified_reason` must
  name the failure class, the attempt count, and the attempted source or reading
  methods. Do not include credentials, cookies, tokens, or other secrets.

Entity/source rules:

- Missing verified evidence for a target entity is a coverage warning. It does
  not invalidate verified rows for other entities or block a downstream deck.
- When an entity declares `official_domains`, a `first_party` row is accepted
  only when its URL hostname matches that domain. Missing first-party coverage
  is reported as a warning.
- `first_party` means owned by the target entity; government, regulator,
  filing, standards, or academic pages retain their own source type.
- The validator checks claim numbers and lexical overlap against the excerpt
  plus any separately bound source context. These checks and literal binding
  do not establish semantic support, source authenticity or completeness; the
  researcher must still inspect the source and its conditions.
- Preserve dates, units, populations and conditions in the claim. Include the
  original heading, publication date or adjacent source context using the bound
  context fields when needed to identify its subject and time. For other source
  formats, the legacy excerpt may contain clearly separated verbatim fragments;
  retain their page/section locations in research notes and do not imply that
  separated fragments were one continuous quotation. Their literal binding is
  not programmatically checked. Do not rewrite a quotation to
  make it pass. Numeric formatting such as `08`/`8` and `1,500.00`/`1500` is
  normalized by the validator; different values and percent units remain distinct.
- Before accepting an atomic claim, read its adjacent qualification as well.
  Preserve the population, unit of analysis, conditions and numeric strength:
  an aggregate or average does not establish each unit's result. Keep observed
  outcomes distinct from projected ones, including when a source combines them.
  "About", "up to" and strict bounds remain different claims. Shortening or
  translating must not strengthen them or detach a condition from the component
  or population it limits. Numeric/lexical validation alone cannot prove entailment.
- When the source language differs from the deliverable, keep the ledger claim
  in the excerpt's language with its original entity context. Translate the
  downstream narrative after validation. Do not translate or pad quotations to
  satisfy lexical matching; unsupported numbers and subjects must still fail.
- Conflicting and unverified rows are retained for disclosure but excluded from
  `verified_evidence` in the machine-readable report.

### Validation Report

After the Markdown artifacts are complete, run the bundled validator with
`--report {output_dir}/research/qa/{topic}_research_check.json`. The report is
the machine-readable handoff to downstream presentation/report workflows. Its
stable boundary is `presentation_handoff` schema v1:

- `delivery_mode`: `full`, `partial`, `framework`, or `invalid`.
- `verified_facts`: source-bound fact objects with canonical
  `entity | claim | source_type | source_url` strings. Each has a generated
  stable `id` and the entity's declared `aliases`. IDs remain stable when rows
  are reordered; changing the claim or source changes the ID. Consumers may
  bind by ID and resolve the exact canonical text programmatically. Old
  consumers may continue using `canonical`; these fields are additive in v1.
- `gaps`: excluded claims and quality limitations.
- `quality_summary`: quality result and actual/recommended coverage.
- `context_files`: producer artifacts that may be read for narrative context.

The remaining top-level fields (`ok`, `quality_ok`, `delivery_allowed`,
`handoff_status`, route, evidence counts, and so on) describe this validator's
own QA process and are retained for diagnostics and compatibility. Presentation
consumers must not branch on them. `full` means the quality target passed,
`partial` means a verified subset is usable despite quality gaps, and
`framework` means delivery may continue with no external factual claims.
Dimension shortfalls and excluded unverified rows do not make the deck
undeliverable.
Do not create this JSON by hand. If any checked research file or the evidence
ledger changes, rerun the validator so the report is newer than its inputs.

### File Analysis

```markdown
# [Topic] File Analysis

Route: C or D
Time Check: [timestamp]
Constraints: [file-only or file-augmented]

## File Inventory
| File | Type | Size | Summary |

## Per-File Extraction

## Cross-File Mapping

## Gaps

## Consolidated Themes
```

### Cross-Verification

```markdown
# [Topic] Cross-Verification

Route: [A/B/C/D]
Evidence Budget: [actual searches/files/dimensions]

## High Confidence

## Medium Confidence

## Low Confidence

## Conflict Zones

## Validation Updates
```

### Targeted Validation

Optional per-conflict output before the main agent merges updates into
`{topic}_cross_verification.md`.

```markdown
# [Topic] Validation [NN]

Conflict: [short conflict statement]
Evidence Checked: [sources/files searched or inspected]
Resolution: [resolved / narrowed / unresolved]
Confidence: [high / medium / low]
Merge Notes: [exact update recommended for cross-verification]
```

### Final Markdown

Use when no downstream writing skill is available:

```markdown
# [Topic]

## Executive Summary

## Verified Findings

## Conflict Zones

## Derived Insights

## Method and Evidence Limits

## Sources
```
