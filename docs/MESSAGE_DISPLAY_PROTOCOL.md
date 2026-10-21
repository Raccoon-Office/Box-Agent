# ACP message display metadata (v1)

This additive extension preserves existing ACP text notifications. Old hosts can ignore it.

- `agent_message_chunk._meta.messageDisplay`: `{version: 1, messageId: string, outputId: string}`.
  All deltas from one logical text message share an ID. IDs are unique across internal runs.
- `llm_output.messageDisplay`: the same identity for the complete output snapshot.
  This is a fallback for missing text deltas, not another copy to append.
- `session/prompt` response `_meta.messageDisplay`:
  `{version: 1, finalMessageIds: string[], durationMs: number}`.
  Sent only after the outer prompt, including autopilot and cleanup, settles.

Until the final receipt arrives, text is unclassified and stays visible. Only a
successful terminal receipt with all referenced messages available permits the
host to classify tagged text: referenced IDs are final answers; other IDs are
process commentary. Preserve actionable cards and artifacts independently.

Text truncation continuations reuse the preceding answer ID (including mid-word or code-fence continuations). Task continuation,
recovery and autopilot produce new candidates. `InjectedMessageEvent.kind` carries
the existing typed injection kind so adapters do not parse instruction text.
Budget and no-progress advice preserve a pending explicit text continuation;
these runtime nudges do not guarantee that the model will rewrite the full answer.
A model-call `finish_reason=stop` alone never establishes finality.

Failures, cancellations, waiting and exhausted budgets have no final IDs. Missing,
unknown-version or incomplete metadata must not hide partial answers. Legacy
histories keep their existing display rules. The host may persist this metadata
alongside its ordinary message snapshot; it is not a new runtime session store.

`outputId` identifies a single model output, while `messageId` identifies the
logical answer. A text continuation keeps `messageId` but gets a fresh `outputId`.
Deltas and the complete snapshot for one output carry the same `outputId`, so a
snapshot-only continuation is appended once and duplicate snapshots are ignored.
This is additive metadata: neither ACP text content nor `llm_output.content`
changes, and old clients can ignore it. It does not replace partial-delta streams
with snapshots; this fallback only covers outputs with no received text deltas.
