# Image context and SessionLog persistence

Uploads, `inspect_images(strategy="native")`, and CUA screenshots share one
session-owned retention policy. CLI, ACP and Python Agent sessions use the
same configuration:

```yaml
agent:
  image_context:
    keep_rounds: 3
    max_encoded_bytes: 10485760
```

Both values must be positive integers. The byte limit counts the base64 bytes
actually sent, independently for each occurrence, even when occurrences share
one file. Provider full-request limits also include text, tools and JSON
framing. A 10 MiB image budget does not guarantee that a provider accepts 10 MiB
of images. No source has priority or a first-delivery exemption.

## Files and version-one event metadata

`SessionImageStore` saves exact PNG/JPEG bytes under
`<session>/images/<sha256>.png|jpg`. It verifies the actual MIME and dimensions,
writes and fsyncs a temporary file, atomically publishes the immutable blob,
then flushes its directory. The occurrence reference is subsequently written
and flushed with its original message event before any model request.

`SessionLog` remains **version 1**. Existing logs are neither migrated nor
rewritten. Optional fields extend existing events:

| Event | Optional metadata |
| --- | --- |
| `user/message` | `data.images` |
| `tool/result` | `data.images`, outside `data.message` |
| `request/context` | `data.imageContext` |
| `assistant/message` | `data.modelRound`, outside `data.message` |

Each `ImageRef` has a unique occurrence `imageId`, source, hash, session-relative
`contentRef`, MIME, dimensions, file bytes, `base64Bytes`, `createdRound` and
message/tool-call association. Metadata contains no image base64. Failed
snapshots retain an unavailable occurrence and explicit text instead of a
broken image reference. `Message.content` remains ordinary history, so image
metadata does not participate in surface equality or trigger history rewriting.

The new reader accepts old text logs and existing CUA structured sidecar
references. An old reader can still restore the text surface, but cannot
maintain image lifetime state while writing new responses. Unknown versions
or required event types are rejected even with recovery enabled; missing or
corrupt image files never reset the conversation.

## Requests and lifetime

At request preparation, the shared `ImageContext` first removes over-age
occurrences, then removes the oldest occurrences to satisfy the encoded-byte
budget, then checks the complete provider request. Each selected image and
its label are appended in one request-only multimodal overlay. The label
identifies the occurrence, source, tool call, image index and dimensions (and
filename when available). The overlay is never logged as a new user message.

An image available for request R can participate in R, R+1 and R+2. Only an
accepted, committed main-model response advances the counter. Empty or
discarded attempts, network retries, preparation failures, summary requests
and proxy calls do not. Accepted partial text counts when it is retained as a
response. The counter is persisted with the assistant event, not the loop step.

Request metadata records preparation separately from response commitment:
selected IDs, exact file hash/dimensions/encoding cost, exclusions and reasons,
and provider body-size metrics where supported. Selection does not claim that
the model saw an image. If a new image is excluded, the request includes an
explicit not-delivered notice and hosts receive a nonfatal notification. A
notice survives interruption until a main-model response commits it.

Managed images never undergo provider-side quality reduction or resizing.
Existing source decoding/encoding happens before archival, so the archived
version is the sent version. CUA coordinate dimensions remain unchanged.
If the body is still too large after all images are removed, preparation fails
explicitly. A persistence or transmission failure never reexecutes a completed
CUA operation.

`inspect_images` checks the current main-model capability on every invocation:
omitting strategy selects native only for declared image support; otherwise it
uses the configured proxy route. Explicit proxy results remain text input to
the main model; their optional archive is ineligible for automatic injection.
Other tools' `transient_followup_content` retains its one-request semantics.
ACP path attachments still receive a permission-checked snapshot when no
inspection model is available, with an explicit not-delivered explanation.
Standard ACP image blocks are redacted to MIME and size metadata in diagnostic
traces as well as kept out of SessionLog message content.

## Restore, summary, reset and copy

Replay rebuilds the image catalog from original events independently of the
compacted text surface. Compaction preserves still-active images; summaries
respect image and body budgets without advancing rounds. A smaller summary
provider may exclude an image just for that summary, without retiring it from
the main model. `surface/reset` clears the active catalog while keeping files
and the committed counter. Increasing a budget or restarting never revives an
evicted image; rereading it creates a new occurrence.

`export_session_bundle(source_log, destination_directory)` copies the original
JSONL plus all referenced image files, including historical/evicted images,
into a new directory. It refuses to overwrite an existing destination. Missing
images keep their audit references; they remain unavailable on restore.
Existing referenced Skill snapshots are copied too, so the text history stays
restorable without relying on the source session directory.

Existing continuation protocol versions accept optional per-message
`image_refs` and a `source_session_id`. Import resolves images only through a
trusted source SessionLog with matching workspace and original message
association. It copies snapshots and preserves age, eviction and delivery
state. Edited/deleted message associations cannot silently pick up other
images. Tool images use the source assistant tool call and tool result to
identify their original user task, and travel in that task's optional
`image_refs` with source tool provenance. Reordering messages does not change
image age or eviction order. The imported inference baseline uses optional `request/context`
metadata; no synthetic assistant response or version change is introduced.

Files are retained with their session directory. There is no expiry deletion
or background garbage collection in this change.
