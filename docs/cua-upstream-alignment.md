# CuaDriver 0.38.0 alignment

The installer pins 0.38.0 and validates release SHA-256 for all five platform archives. Installing a resource does not replace an already running daemon.

- All get_window_state calls request full_output=true to retain elements and window relationships.
- Chords use hotkey; single keys use press_key; paste uses clipboard_write plus hotkey.
- Click, scroll and keyboard operations retain PR225 background-first delivery. Drag retains its foreground default; an explicit background drag is supported by Driver 0.38. Foreground requests and eligible fallback first verify focus through the adaptor and Driver bring_to_front. Explicit input fields retain PID, source window and element_token; untargeted foreground keys use the verified desktop route.
- Driver all-layer list_windows replaces the duplicate CGWindow inventory. AX focus reading, attachment validation, screenshot geometry and stale-target guards remain.
- Elements without AXPress retain PR225's center-point click after a fresh screenshot, frame validation and attachment occlusion checks. A verified attached surface can use desktop-coordinate foreground clicks; coordinates are converted from its screenshot through window bounds and desktop scale.
- PR225 fallback eligibility is retained for background_unavailable (no effect field, not_sent or refused) and same_pid_keyboard_ambiguity / off_space_or_ax_unresolved (refused only). Partial, unverifiable, confirmed and transport outcomes never trigger automatic replay. Preserve hint, path and delivered_chars for recovery.
- Removed native editor input, search Escape, sheet action, field focus, activation and Cmd+O helpers. These operations continue to use Driver tools.
- A bounded native file-panel key route supplements Driver 0.38 for Cmd+Shift+G, Return and Escape. It resolves the actual Open and Save Panel Service receiver and verifies the live window before posting. Cmd+Shift+G can prepare a cold receiver with window-directed events; Return/Escape require an established receiver focus. Cmd+O remains on the Driver menu-equivalent route. Swift source is bundled and compiled into a content-addressed cache; missing compiler/permissions return not_sent and use the fallback rules below. Unknown or partial helper outcomes never replay.

Upstream source: cua-driver-rs-v0.38.0 (85e2b13), tool schemas, macOS input routing and window enumeration.

Attachment graph behavior remains covered for recursive sheets/popovers, stale and ambiguous targets, window movement and closure. The 0.38 suite covers full observations, targeted foreground input, hotkey routing, pixel selection, partial results and no unsafe replay. Restored PR225 tests cover failed activation, contradictory control focus and foreground ownership checks before desktop input.

## Attached field keyboard correction

Driver 0.38 resolves a token to the source snapshot window in ResolvedElement::into_parts; changing window_id cannot retarget it. The adaptor therefore distinguishes AX source from the proven active surface. A background indexed Return on an attached AXTextField advertising AXConfirm retains a Driver semantic click(action=confirm) on the original token as its fallback. Before that action, the panel helper may confirm only a live GoToWindow PathTextField whose frame and current focus match the addressed field; it cannot retarget an arbitrary element. Other mismatched indexed keyboard requests stop before clipboard writes or input; tokens are never rewritten or silently dropped.

target_focus_unconfirmed does not trigger automatic replay. window_not_key permits verified foreground fallback only for an untargeted file-panel key whose native receiver check returned panel_focus_unavailable/not_sent and whose Driver refusal explicitly recommends foreground. Generic or indexed window_not_key failures still stop. Known PR225 refusal codes retain verified foreground fallback on the same active/source window. Explicit foreground cannot bypass the attached-token guard. This prevents parent-scoped keyboard input from being silently redirected to a different surface.


## Delivery contract

As in PR225, `deliveryMode=background` requests the background-first route and retains eligible automatic foreground fallback; it is not a strict no-activation mode. Fallback requires the same active/source window, a known refusal and successful foreground focus verification. Recommendations such as `px` or `page` are returned to the caller rather than replaced with foreground replay.

Action responses retain Driver text messages and structured escalation recommendations, including successful but unverifiable results. These recommendations describe possible next steps; the adapter does not automatically execute them.

## Deployment and verification scope

Desktop download manifests belong to the client repository and are not changed here. The CLI installer pin does not update a desktop client's bundled manifest or replace an already running daemon. A standalone wrapper configured with an application-bundle path uses that path; the installer pin does not redirect it.

Regression tests cover routing and response preservation. Source inspection and fake-transport tests alone do not establish physical GUI behavior or Office task completion. Validation results and deployment boundaries are recorded in the PR description.

## PR225 restoration baseline

The retained adaptor behavior is compared with PR225 merge commit `4b85b3062b1bb0fb60da5acf668391ededa6f648`. Editor, search, AX-action, field-focus, activation and Cmd+O helpers stay deleted; remote file-panel keys are the bounded exception described below. Driver 0.38 full-output reads, hotkey argument translation, all-layer window enumeration, structured result reporting and attached-token ownership checks remain. A sheet's owning document is activated with Driver bring_to_front and its foreground ownership, attachment chain and focused-control evidence are re-read before desktop input; the removed native activation helper is not restored.

Driver 0.38 also lists nonordinary windows such as Word's layer-8 Open panel, while bring_to_front rejects them as non-layer-0 targets. The adaptor retains PR225's verified supplemental-window foreground route using the already observed, PID/frame-matched AXWindow and confirmed active application. The source of the window inventory is Driver; no duplicate native window inventory is introduced.


## Local file-panel validation (2026-10-30)

The current native keyboard entry accepts an `open-panel` AXDialog and its
attached GoToWindow. General Save-panel support is not established by this
implementation. The shared resolver proves the actual system-service PID and
receiver window, retains host/receiver focus leases, and rechecks the addressed
PathTextField for indexed Return. Cmd+Shift+G may prepare a cold receiver with
window-directed mouse events and synthetic focus events; this is not proof
that every cold panel can remain in the background. Some preparation paths
reassert the original application only if it is still frontmost. Return and
Escape do not use cold-receiver preparation.

Local Word tests with official Driver 0.38 confirmed: Driver menu-equivalent
Cmd+O opens the panel; helper Cmd+Shift+G opens GoToWindow; Driver set_value fills
the path; helper indexed Return confirms it; Driver AXPress on Open dismisses
the panel and displays the fixture document. With the helper and its leases
disabled for Return, Driver AXConfirm reported unverifiable and GoToWindow
remained open. Return therefore remains in the helper. This is a source-adaptor
probe, not a packaged client or Office-20 acceptance run. Foreground effects are
observed by the user; tool receipts alone do not prove no transient activation.

Unused editor, search Escape, field-focus, general Sheet-action and application
activation helper entry points have been removed. Driver owns path value writes.
The native source now contains only the bounded panel keyboard transport, its
lease/receiver support, and read-only file-panel front verification. Panel Escape
is retained pending a replacement test.

### File-panel foreground result correction (2026-10-30)

Driver 0.38 can activate a Word Open panel while the panel changes from CGWindow
layer 0 to layer 8. Its layer-0-only postcondition then reports
`bring_to_front_exact_window_unverified`; a subsequent request can instead report
`bring_to_front_window_not_ordinary`. The adaptor preserves that raw result in
its evidence log and runs a read-only native verifier for these exact errors and
the same PID/window. Only an active app with the exact focused `open-panel` or
`save-panel`, a visible layer-8 window owned by that PID, and no higher same-app
window on its display can be corrected to success. All other failures remain
failures. No activation or keyboard input is performed by this verifier; the
Driver binary and subsequent keyboard route are unchanged. Corrected structured
results retain `driver_result` and identify `verification_source` explicitly.

Local Word open-panel verification passed against official Driver 0.38.0. This
is source-level adaptor validation, not an installed-client runtime update.

### Panel keyboard routing and foreground fallback (2026-10-30)

Unaddressed supported file-panel keys first use the native panel receiver helper.
A definite `not_sent` caused by unavailable focus, lease, permission, or helper
launch/build can enter the existing foreground path after a fresh exact AX root
proves a file panel and window identity checks still pass. This bypasses Driver's
known ineffective host-PID background keyboard route for that fallback. The
foreground path remains `bring_to_front` then desktop-scope keys. Unknown/posted
results, stale identities, and addressed fields are not replayed as desktop keys.
Cmd+O remains Driver-owned menu routing.
