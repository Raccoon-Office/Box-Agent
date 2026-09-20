# PPT renderer lifecycle and custom font repair

> **For agentic workers:** Use superpowers:subagent-driven-development for independent implementation and review. Preserve the two issue commits and do not publish until verification passes.

**Goal:** Fix PR129's unowned browser termination and custom font name export, with separate commits and an updated PR.

**Architecture:** A render invocation owns one worker, Playwright session and browser; each page owns an explicit context. The supervisor owns deadlines, admission, cancellation and registered process cleanup, including Chromium's separate process group. Font registry IDs and HTML aliases retain their existing meanings; the PPTX source family comes from the original font name table.

**Tech stack:** Python 3.10+, synchronous Playwright, POSIX process lifecycle, fontTools, Node/PptxGenJS, pytest. Keep rendering diagnostics, CLI forms and the Agent kernel unchanged.

## Task 1: renderer lifecycle (first commit)

Files: `scripts/sync_presentation_suite.py`, explicit inputs under `scripts/presentation_suite_overlays/`, generated Standard `scripts/render.py`, its local lifecycle helper, `scripts/deck.py`, the `render_all` caller in `scripts/font_bundle.py`, `source.json`, owned Skill manifest, `tests/test_presentation_render_lifecycle.py`, and `docs/pptx-entry.md`.

- [x] Add failing regressions for import causing process termination, admission timeout, cleanup errors skipping later cleanup, retry before cleanup, and caller deadlines. Use real subprocesses for cancellation/ownership rather than asserting source strings alone.
- [x] Run `.venv/bin/python -m pytest tests/test_presentation_render_lifecycle.py -q` and preserve the expected failures.
- [x] Implement one session owner with explicit per-page contexts. Move launch/close out of page rendering and share the owner across single, batch and audit entrypoints. Remove SIGALRM and global atexit reaping.
- [x] Implement a bounded supervisor/worker lifecycle. Register job process ownership at launch, before browser work; include detached browser groups. An expired operation terminates this job only, reaps it, then releases admission. Failure to establish cleanup prevents retries. Deadline includes queue/start/work/cleanup and caller adapters leave cleanup time.
- [x] Keep browser reuse within a batch, finite page waits and finite classified transient retries. Report primary failure separately from cleanup failure; content/environment/cancellation failures do not retry. A configured full admission pool never fails open.
- [x] Preserve changes through explicit checked SN overlays. Regenerate using `.venv/bin/python scripts/sync_presentation_suite.py --source-checkout /path/to/sensenova-presentation-int`, then `.venv/bin/python scripts/generate_skills_manifest.py`.
- [x] Verify focused tests, reproducible synchronization, real Chromium normal/error/cancellation paths, repeated renders and an unrelated browser survivor. Record process ownership and environment versions.
- [x] Review spec compliance and code quality, fix findings, run relevant tests and commit only this issue with `fix(pptx): own renderer lifecycle and cleanup`.

## Task 2: custom font source family (second commit)

Files: checked overlay inputs for Standard `font_bundle.py` and `export_pptx/lib/pptx_builder.mjs`, generated counterparts, regression tests, source/Skill manifests and font documentation.

- [x] Add a red test using a real uploaded font whose ID differs from its family. Assert original name-table family in manifest and actual PPTX XML, while `User::<id>` and `Deck-*` stay internal/HTML identities.
- [x] Read family metadata from original font bytes before subsetting/renaming; preserve built-in font behavior, licensing, source path and SHA checks.
- [x] Reject missing/invalid/internal source names in custom font validation and exporter mapping. Existing broken manifests receive an actionable rebuild error; valid old manifests remain compatible.
- [x] Cover multiple weights, untrusted configured display names, missing metadata, legacy bad manifests and unchanged input IR. Run real font subsetting and Node exporter checks with the locked dependency versions.
- [x] Regenerate through the same owned sync/manifest tools, review spec and quality, rerun focused tests and commit only this issue with `fix(pptx): preserve uploaded font family in exports`.

## Delivery and evidence

- [x] Run `general_review/ci/preflight.sh` on the final source (locked install, compile, full pytest, sdist/wheel). Preserve failures rather than replacing them with focused passes.
- [x] Inspect built artifacts for helper inclusion and content identity; run targeted rendering/export probes from shipped sources. Do not claim a client E2E test or installed runtime update.
- [ ] Fetch the latest upstream main, rebase if needed, rerun affected verification after changes; push the two verified commits to `origin/dev/wangbo4/pptx-entry`.
- [ ] Update PR129 TPR with final behavior, exact SHAs/checks and remaining runtime boundaries; verify remote Head and new checks. No merge.
