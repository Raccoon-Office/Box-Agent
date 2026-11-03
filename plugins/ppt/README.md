# PPT resource plugin

This independent package exposes the existing eight PPT Skills. It includes
the complete relative resource trees, 45 bundled fonts, exporters, templates,
and their upstream notices. It does not read the former `box_agent/skills`
location at runtime and does not execute plugin Python when installed.

## Build and install

From the Box-Agent repository root:

```sh
uv run python scripts/build_ppt_plugin.py --output workspace/plugin-dist/ppt-plugin-1.0.3.zip
```

The deterministic archive is written to
`workspace/plugin-dist/ppt-plugin-1.0.3.zip`. `package-files.json` contains the
SHA-256 and size of every other file. The build prints the archive SHA-256.
The package manager installs a directory: extract the archive into a fresh
directory, then pass that directory as `package_path` to `plugins.install`.
The managed package store copies it into an immutable snapshot.

The optional `--import-builtin` migration command only creates an absent
`skills/` source directory, using Git-tracked resources and explicit migration
overlays. Ordinary builds never read the old builtin tree. Subsequent resource
changes must be made in this package, then regenerate the inventory with the
build script. `presentation-suite/source.json` preserves upstream provenance
and records integration overlays; use `refresh_suite_provenance` in the build
script after changing those resources.

## Deployment binding

Use one instance, such as `ppt`, for all eight contributions. Existing public
names may be retained through the deployment's `aliases` mapping:

```yaml
aliases:
  pptx: pptx
  ppt-fast: ppt_fast
  sn-ppt-entry: sn_ppt_entry
  sn-ppt-story: sn_ppt_story
  sn-ppt-standard: sn_ppt_standard
  sn-ppt-dazzle: sn_ppt_dazzle
  sn-ppt-tools: sn_ppt_tools
  sn-ppt-doctor: sn_ppt_doctor
```

Aliases are host configuration, not package identity. Explicit Skill calls use
`{{skill:contribution_id}}`, which the host resolves for this instance. Package
paths keep their relative hierarchy. The existing public names must be excluded
from builtin discovery when their deployment binding is migrated; disabling
this instance must not silently reveal a leftover builtin copy.

## Host runtime contract

`ppt-host-v1` requires the host's existing file/process and agent tools. The
Manifest lists per-Skill hard tool requirements. Image generation is a
conditional capability: use the existing native → bundled → none policy when
the task needs images. Missing generation alone must not prevent package load.
A task that explicitly requires generated images must report that condition
if no permitted generation provider is available.

Prepare dependencies in the host runtime **before** enabling the plugin:

- Python: the host interpreter with Pillow, fontTools, Brotli, Playwright,
  pypdf, python-docx, and psutil. Declare the module names
  `PIL`, `fontTools`, `brotli`, `playwright`, `pypdf`, `docx`, and `psutil`
  in `ppt-host-v1.python_modules`. Standard and Dazzle both import Python
  `playwright.sync_api`; Node Playwright cannot replace it. Font subsetting
  writes WOFF2 files and requires Brotli support in fontTools. Playwright's
  normal host installation supplies its `pyee` and `greenlet` dependencies.
- Node: `pptxgenjs`, `jszip`, and Playwright, with the host dependency directory
  supplied via `NODE_PATH`. `BOX_AGENT_PLAYWRIGHT_MODULE_PATH` is authoritative
  when supplied. The standard exporter's `package-lock.json` records its
  upstream dependency versions; it is not an instruction to install into the
  immutable plugin directory.
- ECharts is read from the package's licensed `assets/vendor/echarts.min.js`;
  it does not require a second installation in the host runtime.
- Chromium: a prepared host executable, supplied through
  `BOX_AGENT_PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH`, which both Python renderers
  honor. `PLAYWRIGHT_BROWSERS_PATH` must stay inside the selected `BOX_AGENT_HOME`
  when an isolated profile is used; the explicit managed executable may be
  shared. Runtime scripts do not download browsers or install Node dependencies
  into this package. A task encountering a missing dependency must stop and
  report it; it must not run pip/npm/apt or browser installers.
- Existing PDF/Office conversion tools remain conditional on the operation;
  the included setup/doctor commands report their availability.
- Optional operations retain their own dependencies: image cutout needs
  OpenCV/NumPy, the bundled image-search script uses Requests, and specific
  document/image conversions may use MarkItDown, openpyxl, PyMuPDF, pdfminer,
  or CairoSVG. These are not mandatory for supplied-text static/dynamic decks;
  report an unavailable requested operation rather than installing during a task.

Run these read-only probes with the **same environment** used by host tool
execution. Replace `<PACKAGE_ROOT>` with the installed snapshot path:

```sh
node "<PACKAGE_ROOT>/skills/document-skills/pptx/scripts/check_html_export_env.js"
python "<PACKAGE_ROOT>/skills/presentation-suite/skills/sn-ppt-doctor/ppt_doctor/check_environment.py"
```

The doctor launches a temporary local browser to verify rendering. Do not add
`--probe-search` or `--probe-image-generation` when only inspecting dependencies:
those flags perform provider requests. A successful package install is not proof of renderer readiness or
end-to-end PPT quality.

Optional bundled media scripts only use host process configuration or an
explicit `SN_PPT_ENV_FILE`. They do not discover other applications' `.env`
files. Credentials remain in host configuration and are never settings-page
fields or Skill parameter projections.

## HTML configuration

`ui/config.html` is a self-contained Chinese/English/Japanese page with light
and dark themes. The host supplies the display language and theme; the
`language` setting controls new presentation content (`auto`, `zh-CN`,
`en-US`), not the settings UI language. `preferred_mode` (`ask`, `fast`,
`design`) influences recommendations and never bypasses mode confirmation.
An explicit presentation-language request or an explicitly confirmed language for
the same task takes priority over the instance default. The language of a prompt
alone is not an explicit choice of output language. Otherwise `zh-CN` and `en-US`
apply to newly authored or rewritten content, including an addition to an existing
deck. Preserve unchanged and explicitly quoted source text verbatim; the default
does not authorize translating the original deck. `auto` follows the existing
deck when editing and the task language for a new deck. Chat replies follow the
host response-language contract independently. The six language-configured Skill
entries carry the same contract so direct entry and delegated work use this rule.

Version 1.0.1 clarifies this presentation-language contract. The 1.0.0 artifact
remains separate for reproducing earlier acceptance results; host APIs and runtime
requirements are unchanged. Language behavior remains model-executed Skill policy,
so contract tests alone do not prove output compliance.

Version 1.0.2 fixes saving inside the host's `sandbox="allow-scripts"` iframe.
Save uses an explicit `type="button"` click listener that calls the configuration
bridge. It does not depend on a native form submission, which the sandbox blocks
before the form's `submit` handler can run. The form only prevents default
submission; no `allow-forms` permission or CSP relaxation is needed. Keep the
1.0.1 archive when reproducing the earlier language acceptance.

The sandbox page only uses the version-1 MessageChannel bridge:
`config.get`, `config.validate`, and `config.save`. The parent owns instance
identity and package/schema binding. Save uses `expected_revision` and a stable
`request_id`; conflicts retain the draft. Saved settings become active only
through the host's cold-apply/restart lifecycle. Theme/language context messages
update labels without discarding the draft. The page has no network, storage,
shell, activation, or credential API.

## Verification boundary

`tests/test_ppt_plugin_package.py` checks the real resource inventory, independent
installation and relative resource resolution, default settings, explicit
environment loading, and the HTML bridge behavior. Browser rendering and real
PPT task delivery require separate host runtime acceptance.

Version 1.0.3 declares `enabled_by: [pptx]` for the seven internal methods. The
ordinary user can disable the PPT capability without changing the installed
plugin configuration or activation generation. Internal methods then become
unavailable too; enabling the public entry restores them. Exporters and Skill
instructions are unchanged from 1.0.2.
