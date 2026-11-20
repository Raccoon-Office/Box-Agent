# mac-cua import patches

Source: `dev/codex_v2.0`, current pin `9ef2fa2604968f056326b2cbcc9ca8a3addc5c31`.
The importer applies numbered patches to clean Git objects, then records original
and patched SHA-256 hashes. Never edit the generated vendor files directly.

- 0001: inject the whole Browser backend, enforce capability dispatch, expose
  cancellation and worker configuration. Browser-only mode never starts harness.
- 0002: trim the JS entry points and inventory to the selected capability.
- 0003: package-relative Python imports, avoiding process-wide `cua_*` aliases.
- 0004: inject the bounded native AX helper command. Box-Agent supplies its
  dedicated frozen-runtime entry instead of treating the ACP executable as Python.
- 0005: keep Box-Agent managed Tab.hover on the injected Browser backend.
- 0006: reconcile a z-order-only keyboard activation refusal using fresh window,
  attachment and AX focus checks before dispatching the requested input.
- 0007: expose managed Browser REPL actions and emit Playwright modal notices.
- 0008: expose connected user-browser page text through `Tab.readText()`.
- 0009: preserve Finder's icon-view inline rename editor during typing and Return.
  Match the observed selected item, root window, transient window and live AX field
  before sending exact-window input. Refuse if the editing target changes.
- 0010: route attached-menu navigation to its verified AX keyboard owner and
  bind menu-local pointer clicks to the exact menu screenshot and focus owner.
- 0011: preserve the single AXPress error `-25204` and ask the model to read
  the UI before deciding whether another click is needed.
- 0012: return the user connector extraction from openAndRead and user-tab goto.
- 0013: expose Browser and Tab methods from the injected backend's mode-specific
  method lists.
- 0014: emit the managed binding's snapshot text or pending modal notice and
  validate wheel, wait and dialog options before dispatch.
- 0015: remove the native App placeholders `selectText` and
  `performSecondaryAction` from the public JS API and its manual.
- 0016: terminate trailing line comments before Node parses top-level await.

All patches are local, not submitted upstream. On upgrade, compare contracts,
remove patches already incorporated upstream, regenerate and run the REPL tests.
An applicable patch is not evidence of behavioral compatibility.

Reproduce: `uv run python scripts/import_mac_cua.py --repo /path/to/mac-cua --check`.
Import: omit `--check`; use a full `--commit` reachable from `dev/codex_v2.0`.

The selected source tree has no root LICENSE file; retain upstream source headers
and provenance. This import makes no new claim about distribution licensing.
