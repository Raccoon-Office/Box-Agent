# Route Details

## Shared Epistemic Reset

Before analysis:

1. Check the current date/time with the available shell/time tool.
2. Inventory provided files and explicit constraints.
3. Avoid factual claims until files or search results have been reviewed.
4. Create `{output_dir}/research/` using the absolute task directory selected
   in `SKILL.md`; do not substitute the session cwd when they differ.
5. Record the route, time check, and search/file constraints in the first
   artifact written for the task.

## Blocked Search and Retrieval

Apply this protocol across Routes A, B, and D:

1. Count failures by evidence target and exact document/access barrier, not
   merely by query string. Anti-bot responses, HTTP 403/429, CAPTCHA/challenge,
   robots or permission denials, timeouts and unreadable bodies are failures.
2. For the same document or access barrier, make at most one materially different
   permitted recovery after the first failure. Do not repeat the same URL/backend
   or bypass a restriction. A new URL behind the same barrier does not reset it.
3. If recovery also fails, stop that retrieval path for this turn. Record the
   document/barrier, attempted methods, failure signals, usable evidence and
   remaining question in the existing ledger and cross-verification artifact.
   A separately accessible source found through search or supplied links may
   still answer that question; assess it on its own provenance. Do not label it
   as a successful read of the blocked original.
4. Send a concise user-visible status update when this stop threshold is reached.
   Continue useful work under the main skill's adaptive reading allowance; do not
   imply that the blocked retrieval is still running or repeat near-equivalent
   attempts. Keep a claim unverified when no source supports it, and report the
   actual remaining coverage limits at handoff.

## Route A: Wide Search

Use for broad, exploratory landscape work.

1. Start with a landscape scan to identify the questions that could change the
   answer. Stop scanning once further queries repeat known leads.
2. Group those questions into complementary facets; explain each facet's
   contribution rather than targeting a file count.
3. If subagents are authorized, launch multiple facet agents in bounded rounds,
   with one facet and one output file per agent. Otherwise, process the facets
   sequentially.
4. Save each facet to `{topic}_wideNN.md`.
5. Decompose the remaining substantive questions into distinct dimensions.
   Reuse evidence across them; do not split one finding into several files.
6. Deep dive each dimension and save `{topic}_dimNN.md`. If subagents are
   authorized, launch multiple dimension agents in bounded rounds, with one
   dimension and one output file per agent.
7. Cross-verify, validate conflicts if search is allowed, extract insights.

Facet output:

```markdown
## Facet: [name]

### Key Findings
- [finding with citation]

### Major Players and Sources
- [entity]: [role]

### Trends and Signals
- [trend with citation]

### Controversies
- [conflict with citations from both sides]

### Recommended Deep-Dive Areas
- [area]: [why it matters]
```

## Route B: Focused Search

Use for specific but multi-dimensional research questions.

1. Plan coarse-to-fine searches from the question map. Overview, mechanism,
   observed use, outcome evidence, and constraints are five distinct evidence intents
   to consider, not five mandatory searches or sections. Select only those needed
   by the task, and use one source for several questions when appropriate.
   Consume the returned result set before opening another batch.
   A later query must target a named uncovered
   dimension, conflict, or missing first-party source; reordered or lightly
   reworded versions of an already-run entity/fact query do not add depth.
2. Group the remaining questions into distinct dimensions appropriate to the
   requested answer. Record the planned scope before deep dives; dimensions
   are research organization, not automatic slide boundaries.
3. Deep dive each dimension.
   - If subagents are authorized, launch multiple dimension agents in bounded
     rounds, with one dimension and one output file per agent.
4. Cross-verify, validate conflicts if needed, extract insights.

## Route C: File-Only

Use when the user explicitly restricts the answer to uploaded or referenced
files.

1. Build a file inventory with type, size, and one-line content summary.
2. Extract from each file:
   - Core themes.
   - Key claims and conclusions.
   - Data points and figures with page/section references.
   - Methodology, limitations, and caveats.
3. Map overlaps, contradictions, complementarities, and gaps.
4. Save `{topic}_file_analysis.md`.
5. Decompose dimensions from file themes only.
6. Analyze each dimension using file content only.
   - If subagents are authorized, launch multiple dimension agents in bounded
     rounds, with one dimension and one output file per agent. Pass the file
     inventory and relevant excerpts to each agent.
7. Cross-verify across file analyses.
8. Skip external targeted validation. Carry unresolved conflicts forward.

## Route D: File-Augmented

Use when files are primary evidence but external sources may supplement them.

1. Perform Route C file intake first.
2. Search only the material gaps identified by file analysis, within the task's
   evidence budget. Do not add searches just to reach a quota.
3. Decompose dimensions from both file themes and external evidence.
4. In each dimension, clearly separate file-sourced evidence from external
   evidence.
   - If subagents are authorized, launch multiple dimension agents in bounded
     rounds, with one dimension and one output file per agent. Pass file
     evidence first and targeted-search boundaries explicitly.
5. Cross-verify, validate conflicts, extract insights.

## Dimension Requirements

Each dimension should include:

- Current state.
- Key evidence with citations or file references.
- Stakeholders or affected systems.
- History or context when relevant.
- Tensions, counter-arguments, or failure modes.
- Confidence notes.

The question map determines depth: preserve each material question and combine
overlapping dimensions. Record unanswered questions and their significance;
neither a large file count nor a short outline proves adequate coverage.

## Cross-Verification

Classify findings:

| Tier | Criteria |
| --- | --- |
| High confidence | Direct authoritative evidence for the specific claim, or independent corroboration with compatible scope and measurement |
| Medium confidence | URL-bound summaries, indirect evidence, or a reported outcome whose generality or causal attribution is untested |
| Low confidence | Insufficient reliable support, unresolved applicability, or outdated evidence that does not cover the claim |
| Conflict zone | Disagreement, temporal mismatch, metric mismatch, or framing split |

Save all tiers and conflicts to `{topic}_cross_verification.md`. Temporal
inconsistencies are conflict zones unless the report explicitly explains the
time periods.
Count independent origins, not dimensions, URLs or reprints. Reusing the same
source in several dimensions does not corroborate it. An official specification
can establish a documented capability; a supplier/customer report establishes
what was reported, not a general causal benefit. Explain that boundary when
assigning confidence.

## Insight Extraction

Save the supported synthesis in `{topic}_insight.md`. Each insight must connect
evidence to a new implication, not repeat a finding or fill a quota. When no
cross-dimension inference is justified, say so and hand off the verified
findings and unresolved questions.

For each insight:

```markdown
## Insight: [short statement]

Derived From: Dim NN, Dim MM
Evidence Cluster: [citation ids or file references]
Rationale: [why the pattern emerges]
Implications: [impact]
Confidence: high | medium | exploratory
```
