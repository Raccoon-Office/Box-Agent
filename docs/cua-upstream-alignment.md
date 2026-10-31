# CuaDriver 0.38.0 alignment

The installer pins 0.38.0 and validates release SHA-256 for all five platform archives. Installing a resource does not replace an already running daemon.

- All get_window_state calls request full_output=true to retain elements and window relationships.
- Chords use hotkey; single keys use press_key; paste uses clipboard_write plus hotkey.
- Click, drag, scroll and keyboard operations default to background delivery. Foreground requests and eligible fallback keep PID, window_id and element_token. Driver owns activation and foreground dispatch; the adaptor no longer calls bring_to_front followed by desktop input.
- Driver all-layer list_windows replaces the duplicate CGWindow inventory. AX focus reading, attachment validation, screenshot geometry and stale-target guards remain.
- Collection items without AXPress remain semantic Driver clicks instead of adaptor-generated center clicks.
- Only known background refusals with effect not_sent/refused may fall back. Partial, unknown and transport outcomes never trigger automatic replay. Preserve hint, path and delivered_chars for recovery.
- Removed native editor input, search Escape, sheet action, field focus, activation and Cmd+O helpers. Driver always runs first.
- All native input helpers are removed, including cross-process file-panel keys and focus leases. File-panel keys use the same Driver background/eligible scoped-foreground paths as other keys. No Swift compilation or native input subprocess is required. This removal does not establish that every file-panel interaction succeeds with Driver.

Upstream source: cua-driver-rs-v0.38.0 (85e2b13), tool schemas, macOS input routing and window enumeration.

Attachment graph behavior remains covered for recursive sheets/popovers, stale and ambiguous targets, window movement and closure. The 0.38 suite covers full observations, scoped foreground input, hotkey routing, semantic selection, partial results and no unsafe replay.

## Attached field keyboard correction

Driver 0.38 resolves a token to the source snapshot window in ResolvedElement::into_parts; changing window_id cannot retarget it. The adaptor therefore distinguishes AX source from the proven active surface. A background indexed Return on an attached AXTextField advertising AXConfirm becomes a Driver semantic click(action=confirm) on the original token. Other mismatched indexed keyboard requests stop before clipboard writes or input; tokens are never rewritten or silently dropped.

window_not_key, target_focus_unconfirmed, same_pid_keyboard_ambiguity and off_space_or_ax_unresolved no longer escalate to foreground. Only a definite background_unavailable refusal on the same active/source window retains automatic foreground fallback. Explicit foreground cannot bypass the attached-token guard. This prevents a wrong-window refusal from activating the parent.


## Delivery contract

An explicit `deliveryMode=background` never escalates to foreground. When the caller leaves delivery mode unspecified, a definite `background_unavailable` refusal on the same active/source window may still fall back if Driver recommends foreground or supplies no recommendation. Recommendations such as `px` or `page` are returned to the caller rather than replaced with foreground replay.

Action responses retain Driver text messages and structured escalation recommendations, including successful but unverifiable results. These recommendations describe possible next steps; the adapter does not automatically execute them.

## Deployment and verification scope

Desktop download manifests belong to the client repository and are not changed here. The CLI installer pin does not update a desktop client's bundled manifest or replace an already running daemon. A standalone wrapper configured with an application-bundle path uses that path; the installer pin does not redirect it.

Regression tests cover routing and response preservation. Source inspection and fake-transport tests alone do not establish physical GUI behavior or Office task completion. Validation results and deployment boundaries are recorded in the PR description.
