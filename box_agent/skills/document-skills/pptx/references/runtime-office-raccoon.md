# Runtime for Office Raccoon

## 1. Path and variables

1. Read runtime variables with priority: `$BOX_AGENT_NODE`, `$BOX_AGENT_NPM`, `$BOX_AGENT_PYTHON`, `$BOX_AGENT_RUNTIME_PREFIX`, `$BOX_AGENT_RENDER_RUNTIME`, `$BOX_AGENT_SOFFICE`, `$BOX_AGENT_PDFTOPPM`.
2. Use local defaults when a variable is not set.

## 2. Export runtime rules

1. Default controlled HTML delivery does not require an export preflight. When
   the user explicitly requests PPTX, prefer the editable export flow with
   `scripts/html_to_editable_pptx.js`.
2. Do not use npm `dom-to-pptx`; export only through bundled `scripts/dom-to-pptx.bundle.js`.
3. For preflight checks use `scripts/check_html_export_env.js` before full deck authoring when final PPTX is expected.
4. If no host renderer exists during an explicit PPTX export, keep the finished
   HTML deliverable and ask whether to defer export or switch to native PPTX.
5. When browser export is blocked and user chooses `PPTX`, switch to `PptxGenJS`.

## 3. Offline dependencies

Dependencies are supplied by the installer, never installed during a task.
Use managed runtime paths and the exact host Playwright/Chromium contract.
If they are absent, keep the HTML and report an incomplete offline package.
Do not invoke npm/pip, browser downloads, CDN fallbacks or system installers.
See `dependency-policy.md` for the package acceptance boundary.

## 4. Render path

1. Use `scripts/render_pptx.py` for visual QA.
2. Use `BOX_AGENT_RENDER_RUNTIME`, `BOX_AGENT_SOFFICE`, and `BOX_AGENT_PDFTOPPM` when present.
3. Never claim "rendering done" without produced slide images or per-slide preview files.
4. If render is unavailable, report `render: BLOCKED` and keep previous steps marked as stale if deck changed after checks.

## 5. Workspace boundary

1. Keep temporary logs, previews, generated images, and QA outputs inside the
   `<PRESENTATION_DIR>` selected in the conversation. Do not add an automatic
   `output/` prefix.
2. Do not create or delete outside workspace unless explicitly requested.
3. Never `npm install` in deliverable workspace folders.

## 6. API exposure

1. If the task is exposed through API, route actions through `scripts/` checks and deterministic files.
2. Treat this file as source of truth for managed runtime constraints.
