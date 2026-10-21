# Regenerating an edited conversation

ACP advertises `session_continuation_versions: [1, 2]` in `session/new` metadata.
Version 1 keeps its bounded recovery behavior. Version 2 accepts the complete
retained user/assistant history for a new runtime branch:

```json
{
  "schema_version": "officev3-session-continuation/v2",
  "product_session_id": "new-private-runtime-session-id",
  "reason": "history_edit",
  "messages": [{"role": "user", "content": "earlier question"}],
  "truncated": false
}
```

The host authorizes the edited turn, reads the complete preceding visible history,
allocates a fresh private runtime identity, and preserves the public conversation,
working directory, model and permissions. It passes the snapshot in the first
`session/prompt` metadata. The new Agent receives the retained semantic messages
followed by the edited input. The old runtime log remains available for auditing.
An empty snapshot supports editing the first turn.

Version 2 preserves message boundaries and text exactly. It rejects invalid roles,
partial snapshots, more than 4096 messages, or more than 4,000,000 text characters.
Its session identity must match the new runtime's upstream identity. Hosts must
check negotiated support and keep the original branch if preparation or dispatch
fails. A successful host history write must commit its runtime binding and remove
the pending snapshot together so a restart follows the edited branch.

The new branch restores user/assistant conversation context. Tool execution state,
completed file operations and global memory are retained in their existing owners;
editing a conversation does not undo their external effects. Historical tools may
need to be called again when generating the replacement response.
