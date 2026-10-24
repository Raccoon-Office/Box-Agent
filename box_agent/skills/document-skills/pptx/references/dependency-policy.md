# Offline dependency policy

All runtime dependencies must be provisioned before the offline package is shipped.
During a presentation task, never run npm/pip/uvx/npx installs, download a browser,
fetch a font, or install system packages. Missing files are package defects, not
requests to try an online installer. `scripts/setup_check.py` is diagnostic only.

Use managed `BOX_AGENT_NODE` and `BOX_AGENT_PYTHON`. Load the exact host Playwright
module and Chromium executable when supplied. The normal editable export uses
bundled `scripts/dom-to-pptx.bundle.js`; do not install npm `dom-to-pptx`.
The package needs Node, Python and required Python libraries, Playwright/Chromium,
the complete skill resources (themes/layouts/vendor libraries), and suitable local
fonts for each target platform. Native PptxGenJS is an optional route only if
already bundled. LibreOffice and pdftoppm are only needed for their optional preview path.

Run `scripts/check_html_export_env.js` before required PPTX export. If a dependency
is missing, report its exact path and preserve the existing HTML. Do not claim PPTX
completion or switch generators silently. Missing optional preview tools do not
block package/text validation or HTML delivery. Never install solely for a preview.

The source checks do not establish installer completeness. Package validation must
use a fresh target-machine profile with external networking disabled, then verify
local authoring, HTML reopening, PPTX export, text/page checks and local media.
