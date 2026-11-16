# Understand Anything Code Map

Box-Agent uses Understand Anything as a versioned architecture index for code
discovery, ownership tracing, dependency inspection, and guided onboarding. The
graph accelerates navigation, but the source tree, focused tests, logs, and
runtime probes remain the source of truth.

## Repository scope

The shared scope is defined by
[`../.understand-anything/.understandignore`](../.understand-anything/.understandignore).
The current configuration includes the core runtime, ACP adapters, tools,
configuration, examples, documentation, tests, and every repository-owned Skill's
entry points, implementation, references, and configuration. Test nodes and
`tested_by` edges help locate existing checks; inclusion does not mean a test
was executed or passed. Third-party dependencies, vendor directories, fonts,
media, binaries, workspaces, virtual environments, and generated output remain
excluded, including vendored PPTX runtime payloads and generated bundles.

Only these shared files belong in Git:

- `.understand-anything/.understandignore`
- `.understand-anything/config.json`
- [`.understand-anything/knowledge-graph.json`](../.understand-anything/knowledge-graph.json)
- `.understand-anything/meta.json`
- `.understand-anything/fingerprints.json`

The versioned `knowledge-graph.json` lets contributors explore the same
architecture snapshot immediately after cloning the repository. `meta.json`
provides its refresh timestamp, analyzed source commit, format version, and file
count. `fingerprints.json` gives the plugin a structural comparison baseline so
later runs can distinguish unchanged, cosmetic, and structural changes without
reanalyzing the entire repository.

Keep these three generated files together as one refresh baseline. Do not
hand-edit them or update only `meta.json`: the plugin requires fingerprints to
support reliable incremental updates. Do not commit `last-run-summary.json`,
intermediate files, trash directories, dashboard tokens, or caches. Those
files remain local refresh state and are ignored by `.gitignore`.

## Explore the shared graph

After cloning the repository, run `/understand-dashboard` from a client with the
Understand Anything plugin. The dashboard reads the committed
`.understand-anything/knowledge-graph.json`; open the tokenized URL printed by
the plugin. The JSON file can also be inspected directly by other graph-aware
tools without running a refresh first.

For a quick freshness check, compare `meta.json.gitCommitHash` with the graph's
`project.gitCommitHash`, then inspect source changes after that baseline. When
running `/understand`, the plugin uses `fingerprints.json` automatically; it is
not intended to be queried for architecture answers.

## Refresh workflow

1. Review `.understand-anything/.understandignore` before changing graph scope.
2. Run `/understand --full --language zh` from a client with the Understand
   Anything plugin when the graph is missing, materially stale, or the scope has
   changed. Use `/understand` for a normal incremental refresh.
3. Confirm that validation reports no critical issues and that every analyzed
   file-level node belongs to exactly one architecture layer.
4. After validation succeeds, update `knowledge-graph.json`, `meta.json`, and
   `fingerprints.json` together in the same reviewable change. Do not hand-edit
   these generated files.
5. Keep `scan-result.json` locally so later incremental runs can reuse the
   deterministic file inventory.
6. If the dashboard is launched, open the tokenized URL emitted by the plugin;
   the bare local server URL is not sufficient.

A refresh reads the current working tree. If it includes uncommitted files, the
commit hash identifies the base commit, not an exact committed snapshot. Check
working-tree changes and the matching fingerprints before sharing. A refresh
must not automatically pull, overwrite, or commit someone else's work.

Refresh the graph after changes to entry points, subsystem boundaries, shared
tools, ACP contracts, runtime packaging, or documentation that should appear in
the guided tour. Small source changes that do not affect graph structure or the
guided tour do not require an immediate graph refresh.

## Verification and review

Use the graph to identify likely files and relationships, then verify important
conclusions with direct file reads, `rg`, focused `uv run pytest` commands, logs,
or runtime probes. Compare the graph's analyzed baseline with later source
changes; describe the graph as stale when those changes affect its architecture
or guided tour.

The graph's `project.gitCommitHash` identifies the analyzed source baseline. A
dedicated graph commit normally follows that baseline commit, so this hash can
legitimately point to the graph commit's parent. For graph updates, include
validation statistics and remaining warnings in the pull request's Task, Proof,
and Risk sections. If shared scope or language settings change, include the
relevant configuration diff and explain the intended coverage.
