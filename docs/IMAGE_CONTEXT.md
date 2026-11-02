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
Explicit `strategy="native"` also requires confirmed image support. A model
switch to an unsupported or unknown binding retires retained images from live
context while preserving their archived files and the text history. An unseen
image produces a not-delivered notice. Unsupported inspection returns a tool
error so the agent can explain the limitation and continue with available text
and tools instead of sending image blocks that could fail the main request.
Other tools' `transient_followup_content` retains its one-request semantics.
With CUA `persist_images: false`, screenshots also use a one-request overlay
when feeding is enabled and the model supports vision; they create no archived
image files or durable image references. Disabling `feed_screenshots` independently
prevents automatic model input while permitting archival when configured.
ACP path attachments still receive a permission-checked snapshot when no
inspection model is available, with an explicit not-delivered explanation.
Standard ACP image blocks are redacted to MIME and size metadata in diagnostic
traces as well as kept out of SessionLog message content.

Same-session history edits retain the current `history_edit` protocol. Restoring
a prefix restores its image occurrences and committed inference counter together;
images from discarded turns remain in the audit archive but leave live context.
Cross-session image continuation uses the bounded v1 continuation protocol.

## Image-input capability

CLI, ACP, image inspection and CUA share `image_input_support()` in
`box_agent/llm/capabilities.py`. It resolves support in this order:

1. Explicit adapter `supports("image_input")` or `capabilities.image_input`.
   YAML `image_input: true/false` and model-profile `imageInput` populate this
   declaration and remain authoritative, including explicit `false`.
2. The current model's catalog `tags`. A valid collection containing `vision`
   enables image input. A collection without it, including `[]`, disables it
   even for an allowlisted model.
3. The maintained `_IMAGE_INPUT_MODELS` allowlist, only when the current
   model's tags are absent or malformed. Matching is case-insensitive substring
   containment: `sn-kimi-k3`, `openai/gpt-6.1-sol` and dated/provider-specific
   variants match their registered model name. Broad vendor names alone do not.
4. Otherwise the result is unknown (`None`). Unknown models do not enable
   automatic CUA screenshot injection, retained image input, or native inspection
   (including explicit `strategy="native"`). A custom vision model must declare
   image support through the configuration or catalog before using those paths.
   Proxy inspection can still probe an unknown binding; explicit image-support
   rejections are cached for that proxy binding and returned as tool failures.

The host's `/api/web/llm/v2/model_catalog` response supplies the tags through
`autoRouting.models`; this lookup does not fetch the catalog itself. A host
that omits these tags, including a manual binding, uses the allowlist or an
explicit capability. Gateway aliases that contain no registered model name
must carry a declaration to gain automatic native image support. Generic
substrings such as `vision` and provider URLs are not capability declarations.
If an alias reuses a vision model's name for a text-only backend, set an explicit
`false` capability or pass its non-vision tags to override the fallback.

The initial allowlist covers verified flagship and vision models from the
following providers. Its model-name patterns live in the Python constant.
Check the provider's image-input documentation before adding a pattern; do not
add an entire vendor prefix. References checked on 2026-10-25:

| Provider | Image-input references |
| --- | --- |
| OpenAI | [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra), [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol), [GPT-6 Sol/Luna and GPT-5.6](https://developers.openai.com/api/docs/changelog), [image inputs](https://developers.openai.com/api/docs/guides/images-vision) |
| Anthropic | [Claude model capabilities and IDs](https://platform.claude.com/docs/en/models/overview) |
| Google | [Gemini 4 Argon](https://ai.google.dev/gemini-api/docs/models/gemini-4-argon), [Gemini 3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash), [Gemini 3.1 Pro](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-pro-preview) |
| Alibaba | [Qwen visual understanding models](https://help.aliyun.com/en/model-studio/vision-model/) |
| Moonshot | [Kimi vision inputs](https://platform.kimi.ai/docs/guide/use-kimi-vision-model), [K3.1 Pro and Highspeed](https://platform.kimi.ai/docs/get-your-api-key), [K2.5](https://forum.moonshot.ai/t/kimi-k2-5-api-is-now-available/218) |
| Z.ai | [GLM-5.3-Flash/FlashX](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash), [GLM-4.6V](https://docs.z.ai/guides/vlm/glm-4.6v); [GLM-5.3](https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3) is text-only and excluded |
| ByteDance | [Ark model list](https://console.volcengine.com/ark/region%3Acn-beijing/docs/model-list), [image understanding model versions](https://docs.volcengine.com/docs/vector_database_vikingdb/chat_completionsNew?lang=en) |
| DeepSeek | [Flash vision](https://api-docs.deepseek.com/guides/vision/), [current model modalities](https://api-docs.deepseek.com/api/list-models/); V4 Pro is text-only and excluded |
| MiniMax | [M3.1-Flash/M3 image input](https://platform.minimax.io/docs/api-reference/text-anthropic-api); M2.x is text-only and excluded |
| xAI | [Grok 4.7](https://docs.x.ai/developers/models/grok-4.7), [Grok 4.3](https://docs.x.ai/developers/models/grok-4.3) |

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

The v1 continuation protocol accepts optional per-message
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
