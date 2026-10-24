# Editing history within one session

## Contract

The business conversation ID is the Box-Agent durable session ID and stays unchanged.
The host supplies it as `_meta.session_id` on `session/new`. Only the temporary
ACP handle returned for the current connection is associated with that ID;
`session/prompt.sessionId` uses this handle. There is no persisted execution-session
alias or migration of sessions created by the removed history-edit scheme.
The host sends one ordinary ACP `session/prompt` request. New-session metadata
advertises `history_revision_versions: [1]` and `history_revision_id` (initially
`initial`). Editing requires that capability; continuation v1 remains a bounded
migration seed for ordinary recovery, not a history-edit mechanism.

```json
{
  "sessionId": "existing-acp-handle",
  "prompt": [{"type": "text", "text": "replacement input"}],
  "_meta": {
    "turn_id": "new-turn",
    "history_edit": {
      "target_turn_id": "previous-user-turn",
      "expected_revision_id": "initial"
    }
  }
}
```

The host authorizes the visible target and constructs `history_edit` from native
state. It does not send preceding messages. `turn_id` identifies the new execution
and is also its deduplication key. A revision uses that same ID, avoiding another
branch or operation identity. A replacement input is appended only by the normal
prompt path, after restoration.

## Log and recovery

Before processing a host turn, Box-Agent flushes a `turn/binding` event associating
the supplied `turn_id` with the exact pre-input log boundary. The binding precedes
attachment processing, Goal changes, Skill delivery and the model loop. Existing
sessions without an exact binding reject the edit with `HISTORY_ANCHOR_UNAVAILABLE`;
visible text is not used to guess a boundary.

After checking the target and expected revision, the runtime reserves the edit,
cancels an existing prompt and waits for its full finalization (up to 30 seconds).
Other prompts and rebindings cannot enter while the edit owns the session. A stop
timeout fails without committing the revision. Hosts with their own in-flight
request registry must also finish cancellation before replacing that registry.

The runtime validates the prefix and Skill sources, then appends and flushes one
required control record in the original JSONL:

```json
{"type":"history/revise","seq":8,"time":1792454400900,"data":{"revision_id":"new-turn","previous_revision_id":"initial","restore_through_seq":3,"target_turn_id":"previous-user-turn","turn_id":"new-turn","prompt_fingerprint":"sha256-of-replacement-prompt"}}
```

Original records remain unchanged. Ordinary subsequent events need no revision
field. Replay resolves each revision to the effective prefix through its boundary,
then applies later events. The same timeline drives Surface replacement and crash
repair. Later compression summaries and copied recent messages are excluded.
Repeated edits can target a retained original turn or a replacement turn.
Malformed revision boundaries fail closed, including recovery mode; they must not
be replaced by an empty session. Numeric loop turns and physical event sequence
numbers remain monotonic across the audit log.

Messages (including paired tool calls/results), Goal, Plan, Todo and Skill facts
are restored from the prefix. Pending approvals, injections, follow-up suggestions
and per-turn Skill grants are cleared. Session browser contexts and owned Python
kernels are discarded. File changes, remote actions, global memory and installed
Skills are not undone. A revision-bearing log requires a supporting runtime;
do not downgrade the runtime against these logs without restoring a pre-edit
backup or an explicit migration.

## Retries and failures

The revision stores the target, expected previous revision, new turn ID and input
fingerprint. A retry after the revision was committed but before `turn/binding`
can resume without appending another revision. Once the new turn is bound,
`TURN_ALREADY_ACCEPTED` prevents a duplicate from cancelling or repeating it.
The host queries its existing prompt outcome/task state rather than blindly
replaying tools. Reusing an ID with a different input fails with an idempotency
conflict. This is deduplicated execution admission, not exactly-once external
side effects.

Edit failures include the authoritative `history_revision_id` in ACP error data;
normal prompt results and new-session replies include it in metadata. Hosts keep
that revision synchronized after success, failure and restart. A post-commit live
restore failure blocks ordinary prompts until the same edit is retried or the
session is reloaded. Continuation v2 snapshots are explicitly rejected, even on
an already-restored session.

## Desktop ownership and implementation sequence

The active desktop project is `raccoon-lab`, branch
`codex/office-desktop-cloud-20260914`, under `apps/office-desktop`.
OfficeV3 is not a development target for this change.

1. Add revision-aware Session Log replay and exact turn bindings in Box-Agent.
   Reuse the Agent's persistence restoration for initial loading and revision.
2. Handle `history_edit` in ACP before the normal prompt path; preserve normal
   continuation v1 and remove continuation v2 and its fresh-session tests.
3. Replace the desktop snapshot builder with native target/capability checks.
   Forward only the target and expected revision in the existing prompt metadata.
   Use the business session ID as the durable identity for creation and editing.
   Remove execution-session aliases and old workspace-recovery paths that allocate
   another durable ID. Workspace mismatches propagate as errors.
4. Keep SQLite's transactional replacement: after native send acceptance, mark
   the old target and tail inactive, insert replacement user/assistant rows, and
   consume the pending edit descriptor in the same transaction. Keep audit rows.
   Pending descriptors contain only the small revision request, not history text.
   Before dispatch acceptance, failures preserve the old effective business history;
   after acceptance, failures are shown on the replacement turn. SQLite and JSONL
   are separate durable owners, so native acceptance is not a distributed commit.
5. Verify first/middle/repeated edits, compression, tool pairs, attached state,
   cancellation ordering, missing legacy anchors, deduplication, interrupted
   revision retry, SQLite reopen and error revision synchronization. Then build,
   install and probe the runtime, restart the host and verify fresh live tasks.

Future client -> business backend -> runtime gateway -> ACP deployments reuse this
same contract. The business backend owns visible history and authorization; the
runtime owns exact prefix restoration and its log. A gateway transports the edit
metadata and preserves session/turn correlation.
