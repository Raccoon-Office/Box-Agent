# Device delegation local integration

Implementation handoffs: [2c-backend](DEVICE_DELEGATION_BACKEND_HANDOFF_CN.md) · [Raccoon Desktop](DEVICE_DELEGATION_CLIENT_HANDOFF_CN.md).

Work in this checkout. Use an environment installed from `pyproject.toml` and `uv.lock` (ACP SDK 0.12.1).

```powershell
uv run python -m examples.device_parent_demo
```

The endpoint uses standard NDJSON ACP stdio. Initialize with protocolVersion 1. Create a parent with `session/new`, `cwd` pointing to its task workspace, `mcpServers: []`, and `_meta: {"device":{"version":1}}`. Other sessions do not expose device tools.

The table defines the target model tool names and the exact values of `_device/request.params.operation`. Switch Box-Agent, desktop and router/mock together. The [migration table](DEVICE_DELEGATION_BACKEND_HANDOFF_CN.md#本轮-acp-报文差异) lists the previous development wire names for comparison.

The eight base tools and the optional artifact tool use id-bearing `_device/request` with `version: 1`, the parent ACP `sessionId`, a logical `operationId`, an `operation` and `args`. Every request except discovery carries a destination `deviceId`. A retry of an unresolved start/send retains its `operationId` and uses a new transport request ID.

| Model tool | Wire operation | Required model arguments | Optional arguments |
| --- | --- | --- | --- |
| `device_list` | `list` | none | `cursor`, `limit` |
| `session_list` | `session.list` | `deviceId` | `cursor`, `limit` |
| `session_read` | `session.read` | `deviceId`, `sessionId` | `cursor`, `limit` |
| `session_start` | `session.start` | `deviceId`, `instruction` | `artifactDelivery` when negotiated |
| `session_send` | `session.send` | `deviceId`, `sessionId`, `instruction` | `artifactDelivery` when negotiated |
| `session_get_turn` | `session.get_turn` | `deviceId`, `sessionId`, `turnId` | none |
| `session_wait` | `session.wait` | `targets: [{deviceId, sessionId, turnId}]` | `timeoutMs` |
| `session_cancel` | `session.cancel` | `deviceId`, `sessionId`, `turnId` | none |
| `device_artifact_upload` | `artifact.upload` | `deviceId`, `sessionId`, `turnId`, `artifactId` | enabled by `_meta.device.artifactUpload: true` |

Registration and device discovery use the same `capabilities` array. `session.history` covers both history operations. `session.execute` covers start, send, get, wait, cancel and turn reporting. The desktop handles wait using its journal. Cross-device wait aggregation belongs to the calling Box Harness. `artifact.upload` is independent. Ignore unknown groups and reject unknown operations. See the [desktop handoff](DEVICE_DELEGATION_CLIENT_HANDOFF_CN.md#同机会话协作) for the same-device route.

Pagination limits are 1–100. Wait accepts 1–32 targets and timeoutMs 0–60000 (default 30000). Unknown properties and invalid argument types fail before RPC.

The desktop supplies its registered `deviceName` (its hostname) as `deviceId` to the mock router. Use `device_list` to discover the registered IDs and online state. A target for `session.start` must be online, have `authorization.execute: true`, and advertise the `session.execute` capability group. Use its exact `deviceId` in requests. The cloud gets this identity from the router.

For any operation, send a prompt text block containing JSON. This example reads a session after discovering its device:

```json
{"operation":"session.read","deviceId":"<deviceId from list>","args":{"sessionId":"local-9","limit":20}}
```

The deterministic model invokes the corresponding tool. Tool arguments are flat. The adapter puts `deviceId` in the envelope and other fields in `args`.

To discover and delegate in one parent turn, send a `session/prompt` text block containing:

```json
{"instruction":"write result.txt"}
```

The deterministic model follows the list pages and delegates when exactly one device can execute. If several qualify, include `"deviceName":"<registered deviceName>"` in the prompt to select one by its exact name. To exercise start directly, pass a discovered `deviceId` and `instruction`. The host receives an id-bearing `_device/request` with operation `session.start`. Reply with that `deviceId`, local `sessionId`, `turnId`, and `status: accepted`. Acceptance returns immediately without waiting for task completion.

Use the same endpoint for a local device session without device metadata. A normal text prompt invokes the real `write_file` tool and writes `result.txt` containing `ok` in that session workspace. An instruction containing `wait` waits for 300 seconds in the model stream and supports standard `session/cancel`. The exact instruction `ask for project code then write result.txt` invokes the real `request_user_input` tool with the question “Which project code should I use?”. Respond on the device in the same session to exercise the local write path.

For parent-stop testing, send:

```json
{"instruction":"wait","hold":true}
```

After acceptance the parent model waits. Send `session/cancel` to the parent to stop that active parent turn and its direct device delegations. A separate `session.cancel` operation targets the exact child sessionId and turnId. The parent acknowledges Stop after recording it locally. Delivery to the device runs separately with a bounded RPC timeout and capped exponential backoff. It keeps the original operation ID across retries and across an opted-in reopen. A definitive rejection or terminal child event stops the retry loop. Device acceptance leaves child status unchanged until a child event arrives. To invoke the explicit cancellation tool, use the target from the start/send receipt. For example, send `{"operation":"session.cancel","deviceId":"<deviceId from receipt>","args":{"sessionId":"local-9","turnId":"t1"}}`.

Deliver device progress through an id-bearing `_device/event` request:

```json
{"version":1,"sessionId":"<parent ACP sessionId>","event":{"operationId":"<start operation id>","deviceId":"<deviceId from receipt>","sessionId":"<local sessionId>","turnId":"<local turnId>","seq":1,"status":"succeeded","result":{"text":"ok"}}}
```

The parent durably records accepted events in Session Log before returning `{"recorded":true}`. Repeated or older sequence numbers and events after a terminal status receive the same receipt without changing state. Identity mismatches are rejected. `running`, `waiting_approval`, `waiting_input`, `succeeded`, `failed`, `cancelled`, and `interrupted` are supported. Terminal events may carry `result` or `error`. The operationId names the start/send operation that created the child turn.

Recovery restores delegation identities, parent turn associations, stop intent, latest sequence, status, and cancellation request identities from Session Log when the session is reopened with the capability enabled. Recovery keeps device start requests out of replay and can schedule pending cloud wake intents. A start acceptance that arrives after its parent stopped triggers cancellation of the original child turn; it cannot target a later child turn.

### Routing in Box-Agent and the independent mock

Only cross-device discovery (`list`) produces a business result at the router. The destination desktop answers all other tool operations. The router retains authorization, routing and per-hop event receipts. A caller that already has a cloudFileRef can use it directly.

In `box_agent/device_delegation.py`, update the operation constants, dispatch branches and tool name mapping together. Use the same wire values in request construction, Stop retries, result validation and fixtures. Update tool descriptions, device selection and the demo to use the capability groups. Reuse the existing RPC timeout in `DeviceDelegations._request` and the snapshot validation in `_wait_result`. The independent mock must carry JSON-RPC `error.data` through both `protocol.mjs` serialization and `broker.mjs` forwarding.

Split a model wait by device in Box. Each ACP group uses its own RPC id and operationId, the same parent sessionId, a mandatory envelope deviceId and matching deviceId on each target. Validate the whole input before dispatch; validate every reply against its group. Run groups concurrently against one monotonic deadline. Stop waiting on first ready group or user Stop. A disconnected group cannot prevent an online group from returning. See the [wire examples and error contract](DEVICE_DELEGATION_BACKEND_HANDOFF_CN.md#acp-投递错误与发起端截止时间).

A desktop business timeout returns a successful result with timedOut true, empty results and the observed states for that device. A transport deadline yields a tool error. Preserve error.data.delivery: a proven pre-send disconnect is not_sent; after attempted delivery or a local RPC timeout it is unknown. Keep the idempotency key for unresolved mutations and accept late events. Preserve targeted pending cancellation across disconnect. Use 10 seconds for ordinary RPCs, timeoutMs + 10 seconds for the whole wait, and 130 seconds for artifact pickup.

### Event consumption and regression coverage

A complete terminal result, a full approval descriptor, or a `waiting_input` snapshot with a target identity and sequence can be consumed for automatic context delivery. Its full JSON must remain in an actual model request and the engine must accept a model response before the local consumed seq advances. A persisted/truncated preview, an event or query marked incomplete, a cancelled request, or a provider error before response does not consume it. Consumption deduplicates automatic model context and wakeups. Explicit wait can still read the same ready state. Offered hashes are logged. Consumed sequences are committed atomically with the accepted assistant message in Session Log and restored from the same record.

The mock router owns connection online state and per-hop receipts. The cloud Harness owns automatic parent continuation. The mock provides local routing and test state.

```powershell
uv run pytest tests/test_device_delegation.py -q
```

The integration suite covers the session operations. Run the independent mock repository’s `node examples/box-desktop-smoke.mjs --local` for a two-process, same-machine check with the backend offline. Exercise offline before send, lost acceptance after send, online silence, mixed-device waits under one deadline, Stop while offline, and using an already delivered file reference versus requesting a fresh pickup from an offline source. Keep ordinary ACP and replay regressions.

## Cloud turn lifecycle and event delivery

- Open with device version 1 and a stable product session ID in `_meta.session_id`. Reopen it with the same ID and workspace or resume the same ACP session with device version 1. Resume schedules eligible work after any requested history replay. Load only replays history. If the host omits a turn ID, an opted-in parent gets a unique turn ID that is stored with that turn in Session Log.
- The shared context engine delivers current delegation state and unread ready results as internal RUNTIME_STATE injections. These messages are request-only, with the normal injected event and runtime source. The live transcript keeps the ordinary user and tool messages. A full get/wait reply already present in the request history suppresses a duplicate runtime update. The harness records consumption only after the engine accepts a model response. After a natural parent turn ends, an unread ready event can claim a durable automatic turn in the same session. The harness serializes automatic and user prompts. While the parent is waiting for its own user input, arriving child results stay pending for the user’s next prompt. After a history edit, automatic delivery follows the effective parent history. Physical operation records remain available for event acknowledgement and cancellation. It retries an uncommitted wake with the same turn ID after reopening. A committed wake input is retained as runtime history. If the process restarts after input commit but before the reply commits consumption, the harness opens a separate reconciliation turn. The prior input and recorded tool outcomes remain in history.
- Listen for ACP extension notifications `_device/turn` with `version`, `sessionId`, `turnId` and `phase`. Register the new turn on `started`, even if the previous turn is still finalizing on the host. Match `finished` by turn ID. Its params contain either the ACP prompt `result` (including `stopReason` and any `_meta`) or a JSON-RPC `error`. Publish that turn's terminal outcome using the normal host event path.
- For the Stop action, capture the target cloud turn ID at click time. Send an id-bearing `_device/cancel_turn` with `{sessionId,turnId}`. `accepted:true` confirms that the harness matched and signaled that turn, while `accepted:false` indicates no match. Standard ACP `session/cancel` with only sessionId keeps its current-session behavior. A caller can also pass `_meta.turnId` in that notification when it does not need an acknowledgement. A stopped turn's later events remain in the log without starting a new turn.
- Each child turn starts its event sequence at 1. The receiver stores out-of-order events durably before responding with `{recorded:true,throughSeq,nextSeq}`. `throughSeq` is the highest contiguous applied sequence and `nextSeq` requests the first missing sequence. The relay must replay missing sequences after a gap or reconnect. When processing the next contiguous event also drains the buffer, the reply includes `throughSeq` without `nextSeq`. For ordinary contiguous events and duplicates, `{recorded:true}` remains sufficient. A terminal event buffered beyond a gap cannot wake the parent until the gap is filled. The reorder window is 1,024 sequence numbers.
- `running`, `waiting_approval`, and `waiting_input` can alternate until `succeeded`, `failed`, `cancelled`, or `interrupted`. These four terminal states are final. Both waiting states are ready for parent review and leave the child turn open. The receiver normalizes legacy `approval_required` from events, logs, get, and wait. A later sequence after terminal, such as an artifact retry, does not create another wake. Reconnection and offline reachability do not change a task into a terminal state.


## Delegated input provenance

The native device execution host opts in with initialize metadata `{"deviceDelegationReceiver":{"version":1}}`. For each delegated start or send, it sends the bound turn ID and `session/prompt._meta.deviceDelegation` containing `parentSessionId` (request envelope sessionId), the start/send `operationId`, and `parentTurnId` if supplied by the parent. Keep this metadata at the trusted host boundary. A manual turn omits it, even in a device-created session.

The Session Log records `source: runtime` and input facts `kind: device_delegation` for the accepted child input, with the validated association in `input_facts.deviceDelegation`. Cloud wake inputs use `source: runtime` and `kind: device_wake`. For device-capable sessions, standard ACP replay chunks from load and resume carry `inputKind`, `inputSource` and, for delegated inputs, the original association. A resume cursor may select either of these turn kinds. Runtime device inputs skip user memory recall and learning.

When the device session is waiting for a valid user-input or decision request, the trusted desktop can resume it with a new ACP turn ID, the original delegation identifiers, and `deviceDelegation.continuationOfTurnId` pointing to the immediately preceding ACP turn. Include the corresponding `userDecision` metadata with the persisted request and tool call identifiers. For a missing-input request, use the tool call identifier as the request ID if the tool does not expose a separate ID. Each request authorizes one continuation. Human replies retain user provenance, and an automatic default retains runtime provenance. One-time file approval from a continuation carries the original logical device turn in `deviceApprovalTurnId`.

A definite start/send rejection such as SESSION_BUSY is retained for diagnostics and excluded from active model context. Transport or receipt uncertainty is stored as unknown delivery. Retrying an identical unresolved request within the same parent turn retains its operation ID, including across a log reopen. An independent parent turn gets a separate operation. If a stopped request first learns its child identity from an event, cancellation is requested asynchronously for that exact child after the event is recorded.


## One-time device approval

A `waiting_approval` event includes an `approval` object with `approvalRequestId`, `title`, timezone-qualified ISO 8601 `expiresAt` and `decisions: ["allow_once","deny"]`. The approval descriptor is visible in get, wait and runtime device context. A subsequent state clears the previous descriptor.

The trusted user interface sends `_device/permission_decision` to the authenticated device route with `deviceId`, `cloudSessionId`, `sessionId`, `turnId`, `approvalRequestId` and `decision`. The device validates the target, expiry and decision before returning `{"applied":true}`. The device task tools expose task state. Approval decisions use the trusted user channel.

For a trusted delegated input, Box requests a remote decision only for eligible `read_file` and `write_file` escalation on a concrete file inside the user's home. Raw ACP permission input carries the canonical `path` and `operation` and an `exactFilesystem` descriptor. Box sets `deviceApprovalTurnId` from the trusted bound prompt. The approval is scoped to the authorized retry. Full commands, directory escalation and other capabilities use their local policy without a cross-device approval. Manual ACP prompts keep their local permission behavior.

To exercise approval in the deterministic demo, give the child ACP process a disposable home directory. Set `HOME`, `USERPROFILE`, and `DEVICE_DEMO_APPROVAL_HOME` to the absolute path of that directory. Use a separate child workspace and open the session with `permissionMode: "default"`. The child instruction `approval write` invokes the built-in `write_file` on `device-demo-approval.txt` in the fixture home. Allowing the request writes `ok` to that file. Denying it leaves the target untouched. The fixture instruction `ask for project code then approval write` first pauses for a project code, then uses the same file-approval flow after a trusted desktop reply.

A terminal result can carry `permissionIssue: {code, message}` alongside its status and summary. The demo instruction `approval write fallback` writes `fallback.txt` in the child workspace when approval is denied or expires.



## Waiting for user input on the device

A `waiting_input` event can include `inputRequest: {"question":"Which project code should I use?"}`. The status and optional question are available through get, wait and runtime device context. Later state changes clear the question. If the parent turn has already ended, it can wake to relay the question. The user supplies the answer in the device session.
