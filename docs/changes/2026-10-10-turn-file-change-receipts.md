# Producer-bound file change receipts

Workspace snapshots discover differences but do not prove which task wrote them.
The desktop uses receipts emitted by Box-owned writers at the actual commit point,
with ACP session, task, turn and tool-call binding. The receipt records an exact
workspace-relative path and the before/after SHA-256 versions.

## Production contract

`write_file`, `append_file`, `edit_file`, `staged_file_write` commit,
`generate_image`, Box-managed browser persistence and transactional
`publish_artifact` commits emit `file_changes_version: 2`. Version 1 from the
previous draft described serialized before/after observations; hosts must not
upgrade those script observations into producer evidence.

```json
{
  "file_changes_version": 2,
  "session_id": "host-session",
  "task_id": "task",
  "turn_id": "turn",
  "file_changes": [{
    "path": "report.pdf",
    "before_sha256": null,
    "after_sha256": "64 lowercase hexadecimal characters"
  }]
}
```

The after digest comes from the producer's committed bytes, not a later directory
scan. Unknown old hashes suppress receipts rather than pretending the file was
new. Unchanged writes and idempotent final-chunk retries do not claim a new change.
Known destinations coordinate through short cross-process destination locks.
Unrelated paths, script execution and image network requests remain concurrent.
Cancellation is checked while waiting and immediately before persistence. Normal
Bash/Python execution has no receipt lock, extra hash scan, `changed_files`
parameter, subprocess patch or nested-SDK restriction. Python cancellation retains the existing runtime behavior; script execution
is never held behind an ownership barrier.

## Script-generated deliverables

Plain `publish_artifact(path)` retains its existing publication behavior. Publishing
an existing file declares delivery intent, not its generation owner. To produce a
verifiable self-contained single-file script artifact without changing the final output location:

1. Call `publish_artifact(action="begin", path="report.pdf")`.
2. Generate and check the file at the returned `staging_path` (its extension is
   preserved). Keep the returned `write_id` in this tool session.
3. Call `publish_artifact(action="commit", write_id=...)`.

Commit rechecks the destination, write permissions and delegated scope. It seals
an independent copy of the script input before atomically replacing the final
path, so an open script FD cannot mutate the committed inode. Another task's ID,
a retargeted destination, or a cancelled commit cannot publish the transaction.
`abort` and turn-end cleanup discard this instance's pending input directories;
post-commit cleanup errors cannot erase a successful receipt. Main user-facing
files stay at their requested paths; no per-conversation workspace is allocated.

HTML with relative resource directories and directory builders retain their existing
generation/publication flow; the single-file transaction must not be used to relocate
only their entry file. Unrestricted scripts can write arbitrary files without using this protocol.
Their existing discovery and execution behavior remains available, but directory
differences and a script's output text are candidates, not trusted ownership.
This mechanism is cooperative attribution, not an OS sandbox against malicious
processes or filesystem actors.

## Desktop dependency and rollback

Raccoon-lab !890 consumes version 2 and binds it to the original operation. It
retains confirmed write history even if a checkpoint cannot be made or is later
cleaned. Diff/restore require matching checkpoint versions; restoring an older
version never adopts a peer's intervening write. Old histories remain visible as
legacy snapshots, with version-checked manual restore, while automatic historical
artifact synchronization accepts only confirmed ownership.

Deploy Box-Agent first, then the desktop consumer. An older desktop can still use
ordinary artifact display but will not understand the new receipt version or
transaction-aware change history. Source tests and wheel builds do not update an
installed desktop or a running ACP process. Roll back both source revisions to
restore their prior protocols; do not rewrite saved legacy history.
