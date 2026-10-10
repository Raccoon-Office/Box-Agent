# Turn file change receipts

The desktop previously inferred task changes from a shared directory's whole-turn
snapshot. Concurrent tasks therefore appeared in each other's file lists. Requiring
only the exclusive `write_file` creation receipt then hid script-generated outputs,
modifications and deletions.

## Execution and protocol

Standard foreground Bash/Python, write/append/edit and image-generation tools
capture file changes at their validated invocation seam. Tools sharing the same
Box-Agent profile serialize execution for canonical workspaces that overlap,
including a directory and its subdirectory. Bash/Python additionally acquire a
profile-wide exclusive lease because scripts can write outside their configured
workspace. Even calls with `changed_files: []` or no explicit workspace participate;
that declaration is a capture optimization, not proof of a script's write boundary.
A short OS-locked registry records
active workspace leases; each lease remains OS-locked through execution and
capture, and a crashed process's released lease is reclaimed on the next call.
Tools with known destinations in disjoint workspaces can execute concurrently
when no script holds the exclusive lease. Direct file writes atomically
acquire leases for both the configured workspace and the actual destination directory,
including permitted absolute paths and active roots outside the workspace.
Paths are re-resolved on every acquisition attempt so aliases retargeted while
waiting cannot leave stale destination leases. Reciprocal cross-workspace writes
therefore wait without acquiring partial leases. Staged file writes use
the same coordination for staging operations and the final commit destination.
Staged commits also protect the final path's directory when replacing a symlink.
Successful staged commits capture the final target's before/after hashes under
those leases; staging chunks do not appear in the receipt list. Replacing a
final symlink tracks the replaced path rather than attributing its referent.
Outside-workspace writes retain their existing permissions and do not emit
workspace-relative receipts.
Image generation also leases its actual output destination, including configured
output directories outside the workspace and possible MIME-selected extensions.
Destination leases include the directories owning file or directory symlink
entries, so those aliases cannot be retargeted by cooperating tools during a call.
Models and tasks can continue reasoning concurrently. Browser snapshot and
screenshot persistence acquire workspace, destination and alias-owner leases at
the local write seam, recheck containment after waiting, and use the same resolved
path for capture and persistence. A target retargeted outside the workspace is
rejected without writing. Delegated tools
invoke the underlying validated interface and forward receipts through the parent
tool call, retaining the parent task and turn identity.
Delegated file tools recheck their assigned write scope against the resolved
destination after acquiring its leases, then use that same path for execution
and receipt capture. Retargeting an alias while the child waits cannot authorize
a write outside its assignment.

Bash and Python automatically compare complete before/after SHA-256 snapshots
while holding the lock. This works without output-path printing, publication, an
isolated task directory, or knowledge of a file extension. Regular files, nested
files, hidden files, temporary files and extensionless files participate. Existing
dependency/internal-directory exclusions remain; hidden publication sidecars
(`.<filename>.artifact.json`) are not user changes. Ordinary files such as
`report.artifact.json` participate. Each snapshot is bounded to 2,000 files and
512 MiB, in addition to the existing artifact traversal limits. An incomplete scan
emits no guessed list.
Optional `changed_files` specifies exact targets to avoid scanning large directories.
Direct file tools use their actual target path. Background commands cannot use
this option; managed background writers are registered in the shared profile
before their invocation releases its lease. They suppress attribution in every
Agent process in the profile until the process (the entire process
group on POSIX) exits. Expired registrations are removed on the next receipt
check. Foreground shells use the same liveness registration at subprocess
creation, so a shell surviving an Agent crash also suppresses attribution after
the Agent's workspace lease is reclaimed. Python cancellation stops and joins the execution worker before
releasing the workspace lock.

ACP tool starts add `_meta.tool_name`, `task_id` and `turn_id`. Tool results add:

```json
{
  "file_changes_version": 1,
  "session_id": "host-session",
  "task_id": "task",
  "turn_id": "turn",
  "file_changes": [
    {"path": "output/report.xlsx", "before_sha256": null, "after_sha256": "sha256"}
  ]
}
```

Paths are relative to the execution workspace. Null before/after hashes represent
absence (creation/deletion). Unchanged bytes produce no entry. Result metadata is
additive and existing artifact/result fields are preserved. The largest automatic
list is 4,000 entries (2,000 deletions plus 2,000 additions). Child receipt progress
uses `type=sub_agent_progress`, `event=tool_result` and `parent_tool_call_id` with
the same receipt fields. Receipts survive normal result/session-log persistence.

The client binds each tool ID to its original operation, validates task/turn
identity, and accepts only a connected receipt hash chain from the operation's
before snapshot to its after snapshot. A peer write between calls breaks that
chain. The task file list and counts use confirmed entries; the existing client
diff, preview and restore implementations continue to use their snapshots.

## Boundaries

These receipts establish ownership for cooperating foreground tools. They are
not an OS audit facility for arbitrary external writers, detached script children,
or independent Agent profiles using different lock namespaces. Such writes require
process-level isolation or tracing for absolute attribution. Same-path interleaved
versions and old records without reliable receipts remain unconfirmed rather than
being assigned to a task. Cancellation can wait for an in-process Python worker
to stop; Python threads cannot be forcefully terminated safely.

Source tests do not install this protocol into the desktop's bundled runtime. Both
Agent and desktop need updated builds before a live-session verification can prove
deployed behavior.
