# Route Details

## Shared Epistemic Reset

Before analysis:

1. Check the current date/time with the available shell/time tool.
2. Inventory provided files and explicit constraints.
3. Avoid factual claims until files or search results have been reviewed.
4. Create `{workspace}/research/`.
5. Record the route, time check, and search/file constraints in the first
   artifact written for the task.

## Route A: Wide Search

Use for broad, exploratory landscape work.

1. Run a quick landscape scan with 3-5 broad searches.
2. Identify 5-8 complementary exploration facets.
3. If subagents are authorized, launch multiple facet agents in bounded rounds,
   with one facet and one output file per agent. Otherwise, process the facets
   sequentially.
4. Save each facet to `{topic}_wideNN.md`.
5. Decompose into 10-20 dimensions.
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

1. Run 5 coarse-to-fine searches:
   - 1-2 macro overview searches.
   - 2-3 structure, actors, data, and authority searches.
   - 1 emerging issue, controversy, or recent-development search.
   Treat these as five distinct evidence intents. Consume the returned result
   set before opening another batch. A later query must target a named uncovered
   dimension, conflict, or missing first-party source; reordered or lightly
   reworded versions of an already-run entity/fact query do not add depth.
2. Decompose into distinct slide-relevant dimensions; ten is a quality target
   for a broad landscape, not a delivery-blocking file quota.
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
2. Use the gap analysis to guide 3-5 targeted searches.
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

Target 10-20 dimensions for large topics. Use fewer only when the user's scope
is deliberately narrow; state the reason.

## Cross-Verification

Classify findings:

| Tier | Criteria |
| --- | --- |
| High confidence | Confirmed by at least two independent sources or dimensions |
| Medium confidence | One authoritative source or strong file evidence |
| Low confidence | Weak, old, indirect, or single-source evidence |
| Conflict zone | Disagreement, temporal mismatch, metric mismatch, or framing split |

Save all tiers and conflicts to `{topic}_cross_verification.md`. Temporal
inconsistencies are conflict zones unless the report explicitly explains the
time periods.

## Insight Extraction

Save `{topic}_insight.md` with at least 5 insights unless the source corpus is
too small. Each insight must be a cross-dimension inference, not a repeated
finding.

For each insight:

```markdown
## Insight: [short statement]

Derived From: Dim NN, Dim MM
Evidence Cluster: [citation ids or file references]
Rationale: [why the pattern emerges]
Implications: [impact]
Confidence: high | medium | exploratory
```
