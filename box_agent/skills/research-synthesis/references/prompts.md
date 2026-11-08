# Research Task Templates

These templates work for both native subagents and local sequential rounds.
Replace bracketed fields before dispatching or executing.
Use the absolute task directory selected in `SKILL.md` for `[output_dir]`;
it may be a child of the unchanged session cwd.

## Wide Facet

```text
Mission: Explore one broad facet of [topic].

Route: A wide search.
Agent slot: [wide NN of MM in batch K].
Facet scope: [facet name and boundaries].
Context so far: [brief landscape findings].

Search requirements:
- Use the user's language for searches unless a specific locale is required.
- Prefer primary sources, official data, academic sources, reputable media, and
  original company or government materials.
- Avoid content farms, anonymous blogs, and SEO aggregators.
- Perform varied coarse-to-fine searches; do not recycle one keyword pattern.
- Before another batch, state the uncovered evidence gap it will resolve. Do not
  retry a near-equivalent entity/fact query just because a `site:` or authority-
  ranked variant returned no results; use an exact known first-party URL or mark
  the gap unresolved.
- Count anti-bot, HTTP 403/429, CAPTCHA/challenge, permission, timeout and
  unreadable-body failures against the document/access barrier. After one
  materially different permitted recovery also fails, stop that retrieval path;
  report methods, failures and the remaining question to the main agent;
  do not keep issuing variants of the same search. An independently accessible
  observed source may answer the same question under its own provenance and
  the main skill's adaptive reading allowance; never bypass access restrictions.

Output path: [output_dir]/research/[topic]_wide[NN].md
Output format:
- Facet
- Key findings with footnote citations
- Major players and sources
- Trends and signals
- Controversies and conflicting claims
- Recommended deep-dive areas
- Footnote definitions
```

## Dimension Deep Dive

```text
Mission: Research dimension [NN] of [topic].

Route: [A/B/C/D].
Agent slot: [dimension NN of MM in batch K].
Dimension scope: [scope].
Required angles:
- Current state
- Key evidence
- Stakeholders or affected systems
- History or context
- Tensions and counter-arguments

Context:
[relevant landscape/file findings]

Source rules:
- Route C: use only supplied file content and cite file name plus section/page.
- Route D: treat file evidence as primary and external sources as supplement.
- Search routes: prioritize primary and authoritative sources.
- Check that the source's date, product/version, locale and population apply to
  the question; distinguish a current specification from a historical example.
- Resolve the target entity before accepting a result. Open the source page;
  a URL-bound search-result summary may be medium-confidence evidence when its
  exact returned excerpt is preserved with `evidence_basis=search_summary`.
- Inspect page-reader output: generated summaries use `page_summary` and medium
  confidence; literal source text uses `page_body`. If a summarizing reader
  does not identify raw output, default to `page_summary`. For decisive terms,
  plan a direct reading channel using the main skill's source-reading procedure.
  Preserve the returned text
  and its basis instead of turning a summary into a publisher quotation.
- For every candidate claim, add or update one row in
  [output_dir]/research/[topic]_evidence.json using the output-contract schema.
- Mark a row `verified` only when the excerpt or separately bound literal
  source context names the entity and directly
  supports the claim. Mark cross-entity matches, unsupported snippets, and
  unresolved user/source conflicts `conflicting` or `unverified`.
- For `first_party`, the page hostname must match a declared
  `official_domains` entry for that entity.
- For the same document or access barrier, stop after two consecutive
  blocked-source failures. Record methods and failures in `unverified_reason`;
  surface the blocked-target note to the main agent so it can notify the user.
  A separately accessible observed source may still support the same question;
  do not confuse its evidence with reading the blocked original.

Output path: [output_dir]/research/[topic]_dim[NN].md
Output format:
Entity: [exact target entity]
Claim: [claim with inline citation]
Source: [source name or file name]
URL: [URL or file reference]
Source Type: [first_party / government / regulator / filing / standards_body / academic / reputable_media / secondary / user_input]
Date: [publication date or N/A]
Evidence Basis: [page_body / page_summary / search_summary / user_input]
Excerpt: [short exact excerpt from the returned source text or labeled summary]
Context: [surrounding context]
Confidence: [high / medium / low]
Status: [verified / conflicting / unverified]
```

## Targeted Validation

```text
Mission: Resolve or narrow one conflict from cross-verification.

Agent slot: [validation NN of MM in batch K].
Conflict:
[conflicting claims, source ids, dates, and dimensions]

Requirements:
- Find independent evidence where external search is allowed.
- Preserve all existing citations.
- If the conflict is genuine or unresolved, say so directly.
- Update the corresponding evidence-ledger rows. Preserve both the user claim
  and source-backed claim when they conflict; do not collapse them.

Output path: [output_dir]/research/[topic]_validation[NN].md
Main-agent merge target: [output_dir]/research/[topic]_cross_verification.md
```

## Final Handoff

```text
Research is complete. Do not launch additional research agents.

Use these artifacts:
- Insight file: [output_dir]/research/[topic]_insight.md
- Cross-verification file: [output_dir]/research/[topic]_cross_verification.md
- Evidence ledger: [output_dir]/research/[topic]_evidence.json
- Delivery handoff report: [output_dir]/research/qa/[topic]_research_check.json
- Dimension files: [output_dir]/research/[topic]_dim01.md through [topic]_dimNN.md
- File analysis: [output_dir]/research/[topic]_file_analysis.md, if present
- Wide exploration files: [output_dir]/research/[topic]_wideNN.md, if present

Preserve Markdown footnotes exactly. Do not renumber, strip, or replace them.
For factual downstream copy, use only
`presentation_handoff.verified_facts[].canonical` from the report. Respect its
`delivery_mode` (`full` / `partial` / `framework`). Do not promote
conflicting or unverified prose from the Markdown files.
```
