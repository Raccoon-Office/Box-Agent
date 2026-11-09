# CuaDriver 0.38 alignment and retained compatibility routes

The installer pins official CuaDriver 0.38.0 and validates release SHA-256 for
all five platform archives. The upstream reference is
`cua-driver-rs-v0.38.0` (`85e2b1318bd77ccf3cfa88ccdf875a4bb7eaed19`).
Installing a resource does not replace a running daemon or a desktop client's
bundled download manifest. This change does not patch the Driver binary.

## Ownership

| Layer | Responsibility |
| --- | --- |
| `mcp_servers/cua_adapter.py` | Driver transport, fresh window/token ownership, delivery decisions and observation scope |
| `mcp_servers/cua_server.py` | Serialized public tools, depth validation and observation presentation |
| `mcp_servers/cua_panel_keyboard.py` and `native/panel_keyboard.swift` | Bounded file-panel receiver leases, keys, geometry and foreground verification |
| `plugins/cua/structured_result.py` and `wiring.py` | CUA model projection and session-owned screenshot capability |
| `tools/mcp_result_hooks.py`, `mcp_loader.py`, `image_inspection_tool.py` | Generic run-scoped hooks and image inspection; no Office-specific routing |
| `skills/office-gui-basics/SKILL.md` | Observable Office editing and selection workflow |
| `scripts/build_runtime.py` | Package the helper source with the standalone runtime |

Driver owns ordinary editor input, search Escape, field value writes, semantic
AX actions and Cmd+O. General editor/search/field-focus/activation helpers remain
removed. Native activation is retained only for an exactly identified file panel;
this is not an all-helper removal.

## Delivery and identity contract

- AX reads request `full_output=true`; actions retain the PID, source window and
  original element token. Chords use `hotkey`, single keys use `press_key`, and
  paste uses `clipboard_write` followed by `hotkey`.
- Click, scroll and keyboard retain PR225 background-first delivery. Drag retains
  its foreground default and supports explicit background delivery.
  `deliveryMode=background` is not a universal no-activation guarantee: known,
  eligible refusals may use the existing verified foreground fallback.
- Automatic fallback accepts `background_unavailable` only with an absent effect,
  `not_sent` or `refused`; `same_pid_keyboard_ambiguity` and
  `off_space_or_ax_unresolved` require `refused`. Partial, unknown, unverifiable,
  confirmed and transport outcomes never authorize replay. `target_focus_unconfirmed`
  and Driver `window_not_key` do not authorize replay either.
- Every activation or clipboard mutation invalidates the relevant inventory.
  Subsequent checks revalidate PID, frames, attachment chain and input receiver.
  Failed activation and explicit conflicting focus stop input.
- AX source windows and active attached surfaces are distinct. Driver 0.38 resolves
  tokens to their source snapshots, so changing `window_id` cannot retarget a token.
  Mismatched indexed keyboard requests stop before clipboard writes or input.
  Indexed Return on a proven GoTo `PathTextField` is the bounded exception below;
  the existing semantic `AXConfirm` fallback retains the original token.
- Driver's all-layer inventory supplies normal window discovery. Native AX focus
  and independent CGWindow order/frame checks supplement specific proofs; they
  do not supply a second general application inventory.

The restoration reference is PR225 merge commit
`4b85b3062b1bb0fb60da5acf668391ededa6f648`. Driver text, action reports,
`hint`, `path` and `delivered_chars` remain available for caller recovery.
Recommendations are returned, not automatically executed.

## Bounded file-panel helper

The helper recognizes exact native `open-panel`, `save-panel` and attached
`GoToWindow` identities, not titles or window layers. Layer-0 Office sheets are
valid. Nested Save/GoTo surfaces must match the complete live native ancestry,
including an outer document or Print window used as the capture source.

Only Cmd+Shift+G, Return and Escape use the panel keyboard transport. The resolver
proves the actual host/system-service receiver PID and window, retains scoped
host/receiver leases, and checks identity again before delivery. Cmd+Shift+G may
prepare a cold receiver with window-directed focus events; Return and Escape
require established receiver focus. Indexed Return additionally checks the live
GoTo path field's frame and focus. Driver still writes the path value.

A definite pre-delivery `not_sent` for unavailable focus, lease, permission or
helper launch/build may enter verified foreground fallback for an unaddressed
key, after a fresh AX root proves the panel. Malformed output, timeout after
launch, partial preparation and unknown/posted outcomes never cause key replay.
Cold preparation is not proof that every panel stays in the background.

Explicit foreground recovery may activate and AXRaise only the exact identified
file panel. It then rechecks focus, frame, ancestry and ordering before input.
The read-only `verify-panel-front` operation never activates or posts events:
its independent CG order checks same-app windows on the target display, together
with exact AX focus and the frontmost application. It is not a global guarantee
against every foreign overlay. It can correct the two known Driver focus errors
(`bring_to_front_exact_window_unverified`, `bring_to_front_window_not_ordinary`)
only for the same verified file-panel PID/window. Corrected results retain the
raw `driver_result` and an explicit `verification_source`.

Swift source ships in the package and compiles into a content-addressed cache.
Build timeout does not permanently poison that cache; missing compiler or
permissions remain explicit failures. Receiver leases close on retarget/session
end. The experimental native panel pointer transport is not retained.

## Capture, coordinates and unresolved surfaces

Driver 0.38 may return an AX-unresolved GoTo child image without usable snapshot
context, or a scaled parent composite mislabeled as the child. Read-only native
`panel-geometry` proves exact focus, parent ancestry and same-PID frames. The
adaptor then returns the full parent capture, labels its scope (including possible
clipped sheet edges), and keeps its own scale. Geometry is rechecked before input.
Coordinate input uses verified foreground desktop coordinates; an explicit
background request on this special route returns `background_unavailable` without
activation. This compensates for the released Driver, not its snapshot store.

Separately captured floating palettes retain their own window ID, bounds and
pixel dimensions. Their explicit foreground clicks verify current geometry,
app ownership and app-window stacking, without raising the underlying document.

Word Find can expose a top-level `AXToolbar` through another window's snapshot.
Those controls are observation-only: their tokens are not rebound, and the
potentially misleading capture is withheld. Indexed and pixel actions are
rejected. Unaddressed explicit foreground keys can recover (for example Escape)
only after exact native focus, active PID and current frame agree. Empty AX dialogs
use the same exact-window proof for eligible unaddressed foreground input;
background input is not silently promoted. An ordinary-window keyboard refresh
may retry one read-only inventory change before delivery, never replay an action.

## Observation cost and result projection

One observation reuses its app inventory and adjacent window reads. Known child
surfaces may start with a freshly read proven parent when frame and depth scope
still agree. Cold discovery checks ambiguity; missing/changed parents reopen it.
Old AX trees and action tokens never become fresh evidence. Checks after input,
clipboard changes and activation remain live. Launch readiness still samples live
PIDs under the original deadlines.

`get_app` and AX-enabled `get_state` accept positive `max_depth`; default traversal
is unchanged. Explicit shallow reads and automatic shallow timeout recovery report
that deeper controls may be omitted. Screenshot-only reads reject the option.
Root-only file-panel predicates request depth 1 when supported. A scoped attached
keyboard refresh falls back to full traversal if it cannot prove the same chain
and focus. Warm GoTo captures can read the parent directly after revalidation.

Row compaction and attachment ownership use indexed passes while preserving
control order, tokens, state flags and ambiguity rejection. Successful intermediate
queries may be summarized for the model, but errors, coded/unknown outcomes and
current grounding remain. Complete MCP envelopes remain in `rawOutput`; direct
Driver results are unchanged. The loader inspects already-decoded error objects
without serializing the entire AX payload again.

Image persistence and model projection share canonical validation only within one
MCP result; scope exit discards that memo. `inspect_images` can read immutable,
content-addressed images from the active CUA session through a generic run-scoped
capability. The sidecar validates path, symlink, content hash and media type on the
actual returned bytes. This does not grant directory access or authorize images
from another session. Ordinary file permissions apply outside that capability.

## Verification and deployment boundaries

Regression tests cover Driver translation, fallback/no replay, parent/source
ownership, nested sheets, stale focus/frames, toolbar recovery, query cost,
result projection, screenshot permission isolation and runtime packaging. See
`tests/test_cua_038.py`, `test_cua_pr225_restoration.py`, `test_cua_office_regressions.py`,
`test_cua_panel_pointer.py`, `test_cua_word_toolbar.py`, `test_cua_photoshop_focus.py`,
`test_cua_query_cost.py`, `test_cua_wiring.py` and `test_image_inspection_tool.py`.

The historical probes below have different scopes and are not a combined latency
benchmark. Their raw evidence remains in the local validation archives.

| Evidence | Observed result | Limit |
| --- | --- | --- |
| Recorded E07 observation replay | apps/windows/AX 4/4/17 → cold 1/2/11, warm 1/2/1 | Same 79 elements; not live latency |
| Local TextEdit read | 11 queries/0.570 s → 4/0.198 s | Edit/save separately verified |
| Word root predicate | 390 elements/0.9461 s → 27/0.0655 s | Root identity only |
| Archived E07/P01 model text | 2,555,650 → 37,792 / 4,020,073 → 1,573 bytes | Raw envelopes retained |
| Row/ownership processing | Equality on 17 archived and 1,000 generated trees | Python CPU measurements, not GUI task speed |
| Same-result image processing | Synthetic median 57.0 → 37.6 ms | 20 samples; CPU/storage only |
| Warm Word GoTo capture A/B/B/A | 3 reads → 2; .706/.491 s → .399/.408 s | Same rendered text/geometry; local probe |

The 2026-11-02 formal 20-case batch used r2 for W01–W02, r4 for W03–E06 and r5
for E07–P06. It is a mixed-version run, not acceptance of the final tree.
The later isolated Box-Agent r6 repair run passed W04 and E05 at 100 each
(459.28 s and 342.94 s), with saved artifact, business screenshot and unchanged
input-hash checks. W04 still had 2 recoverable tool errors and E05 had 4;
ACP completion alone was not treated as success. It explicitly loaded
`office-gui-basics` in addition to `mac-cua`, so it is not a pure code ablation
against the earlier run. It does not replace the original formal scores.

That r6 candidate was built, installed with 3,024 file hashes verified, and tested
through ACP. The normal installed application runtime link remained r5. Subsequent
source cleanup/test results belong in the PR's current Proof, not retroactively
in that runtime receipt. Neither two repaired cases nor offline tests establish
all-Office/background-input correctness. Native Driver fixes, desktop client
manifest migration and a final uniform-version full run remain separate work.
