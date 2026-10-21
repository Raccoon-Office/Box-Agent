# CUA screenshot audit references

CUA tool replies persist valid PNG/JPEG screenshots next to the durable
SessionLog, under `images/<sha256>.<extension>`. The tool's text includes the
absolute file path so image inspection can reload it from any workspace.
`raw_output.mcp_image_references` includes the absolute `path` and portable `contentRef`,
`sha256`, dimensions, media type and byte count. Resolve `contentRef` relative
to `SessionLog.path.parent`, not relative to the exported execution-trace file.
Keep the images with the SessionLog when archiving a session. Absolute paths
refer to the producing host; after moving an archive, resolve `contentRef`
against its new session directory. Image inspection still enforces its normal
file-read permissions.

Screenshot persistence and model input are separate:

- `plugins.cua.persist_images` defaults to `true`. Persistence does not require
  model vision support or `feed_screenshots`; error results containing a valid
  screenshot can also retain an audit reference.
- `plugins.cua.feed_screenshots` controls transient model input, which requires
  explicitly known image support. The next request receives the image; later
  requests do not replay it.
- Profile-bound models can declare `imageInput`. When it is absent, an explicit
  default client's `image_input` is inherited only for the same provider,
  endpoint and model. Other model bindings remain unknown.

The execution trace (`box-agent-session-trace/v1`) and durable SessionLog are
different files. An `mcp_inline_images` entry with only `encoded_chars` proves
that the tool returned image data, not that the model received it. Inspect
`llm.request.messages` for `input_image` blocks and compare their hashes with
the persisted references.

The current persistence adapter still requires a SessionLog path. Direct ACP
clients should supply `_meta.session_id` when creating a session to enable
the durable session store. Unsupported/corrupt images or file-system failures
can prevent persistence; a screenshot is auditable only when its reference
resolves to a file with the recorded hash.
