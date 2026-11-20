# Device delegation local integration

Implementation handoffs: [2c-backend](DEVICE_DELEGATION_BACKEND_HANDOFF_CN.md) · [Raccoon Desktop](DEVICE_DELEGATION_CLIENT_HANDOFF_CN.md).

Work in this checkout. Use an environment installed from `pyproject.toml` and `uv.lock` (ACP SDK 0.12.1).

```powershell
uv run python -m examples.device_parent_demo
```

The endpoint uses standard NDJSON ACP stdio. Initialize with protocolVersion 1. Create a parent with `session/new`, `cwd` pointing to its task workspace, `mcpServers: []`, and `_meta: {"device":{"version":1}}`. Other sessions do not expose device tools.

The eight base tools and the optional artifact tool use id-bearing `_device/request` with `version: 1`, the parent ACP `sessionId`, a fresh request `operationId`, an `operation`, optional `deviceId`, and `args`.

| Model tool | Wire operation | Required model arguments | Optional arguments |
| --- | --- | --- | --- |
| `device_list` | `list` | none | `cursor`, `limit` |
| `device_session_list` | `session.list` | `deviceId` | `cursor`, `limit` |
| `device_session_read` | `session.read` | `deviceId`, `sessionId` | `cursor`, `limit` |
| `device_subagent_start` | `subagent.start` | `deviceId`, `instruction` | `artifactDelivery` when negotiated |
| `device_subagent_send` | `subagent.send` | `deviceId`, `sessionId`, `instruction` | `artifactDelivery` when negotiated |
| `device_subagent_get` | `subagent.get` | `deviceId`, `sessionId`, `turnId` | none |
| `device_subagent_wait` | `subagent.wait` | `targets: [{deviceId, sessionId, turnId}]` | `timeoutMs` |
| `device_subagent_cancel` | `subagent.cancel` | `deviceId`, `sessionId`, `turnId` | none |
| `device_artifact_upload` | `artifact.upload` | `deviceId`, `sessionId`, `turnId`, `artifactId` | enabled by `_meta.device.artifactUpload: true` |

Pagination limits are 1–100. Wait accepts 1–32 targets and timeoutMs 0–60000 (default 30000). Unknown properties and invalid argument types fail before RPC.

The desktop supplies its registered `deviceName` (its hostname) as `deviceId` to the mock router. Use `device_list` to discover the registered IDs and online state. A target for `subagent.start` must be online, have `authorization.execute: true`, and advertise the `subagent.start` capability. Use its exact `deviceId` in requests. The cloud gets this identity from the router.

For any operation, send a prompt text block containing JSON. This example reads a session after discovering its device:

```json
{"operation":"session.read","deviceId":"<deviceId from list>","args":{"sessionId":"local-9","limit":20}}
```

The deterministic model invokes the corresponding tool. Tool arguments are flat. The adapter puts `deviceId` in the envelope and other fields in `args`.

To discover and delegate in one parent turn, send a `session/prompt` text block containing:

```json
{"instruction":"write result.txt"}
```

The deterministic model follows the list pages and delegates when exactly one device can execute. If several qualify, include `"deviceName":"<registered deviceName>"` in the prompt to select one by its exact name. To exercise start directly, pass a discovered `deviceId` and `instruction`. The host receives an id-bearing `_device/request` with operation `subagent.start`. Reply with that `deviceId`, local `sessionId`, `turnId`, and `status: accepted`. Acceptance returns immediately without waiting for task completion.

Use the same endpoint for a local device session without device metadata. A normal text prompt invokes the real `write_file` tool and writes `result.txt` containing `ok` in that session workspace. An instruction containing `wait` waits for 300 seconds in the model stream and supports standard `session/cancel`. The exact instruction `ask for project code then write result.txt` invokes the real `request_user_input` tool with the question “Which project code should I use?”. Respond on the device in the same session to exercise the local write path.

For parent-stop testing, send:

```json
{"instruction":"wait","hold":true}
```

After acceptance the parent model waits. Send `session/cancel` to the parent to stop that active parent turn and its direct device delegations. A separate `subagent.cancel` operation targets the exact child sessionId and turnId. The parent acknowledges Stop after recording it locally. Delivery to the device runs separately with a bounded RPC timeout and capped exponential backoff. It keeps the original operation ID across retries and across an opted-in reopen. A definitive rejection or terminal child event stops the retry loop. Device acceptance leaves child status unchanged until a child event arrives. To invoke the explicit cancellation tool, use the target from the start/send receipt. For example, send `{"operation":"subagent.cancel","deviceId":"<deviceId from receipt>","args":{"sessionId":"local-9","turnId":"t1"}}`.

Deliver device progress through an id-bearing `_device/event` request:

```json
{"version":1,"sessionId":"<parent ACP sessionId>","event":{"operationId":"<start operation id>","deviceId":"<deviceId from receipt>","sessionId":"<local sessionId>","turnId":"<local turnId>","seq":1,"status":"succeeded","result":{"text":"ok"}}}
```

The parent durably records accepted events in Session Log before returning `{"recorded":true}`. Repeated or older sequence numbers and events after a terminal status receive the same receipt without changing state. Identity mismatches are rejected. `running`, `waiting_approval`, `waiting_input`, `succeeded`, `failed`, `cancelled`, and `interrupted` are supported. Terminal events may carry `result` or `error`. The operationId names the start/send operation that created the child turn.

Recovery restores delegation identities, parent turn associations, stop intent, latest sequence, status, and cancellation request identities from Session Log when the session is reopened with the capability enabled. Recovery keeps device start requests out of replay and can schedule pending cloud wake intents. A start acceptance that arrives after its parent stopped triggers cancellation of the original child turn; it cannot target a later child turn.

The local mock backend supplies the device directory (including view-only devices), authorization and paging, and wait aggregation over real events. The desktop supplies authorized session history and executes start/send/cancel. Send reuses the authorized session directory and returns SESSION_BUSY when occupied.

Get and cancel use the exact device, local session and local turn returned by start/send. On the wire, the target device is envelope deviceId and the local IDs are args.sessionId and args.turnId. The envelope sessionId identifies the cloud parent. Every wait target includes its own deviceId, sessionId and turnId. The router authorizes each target in the parent session. The request operationId identifies the current RPC command, and cancellation retries retain their own command ID. An online device returns current local state with `deviceOnline: true`, `source: "device"` and `observedAt`. A confirmed disconnected device may return the backend's last confirmed snapshot with `deviceOnline: false`, `source: "backend"` and its observation time, or explicit unknown state when no snapshot exists. A request timeout does not establish device disconnection; offline running snapshots are historical observations.

Wait checks the current state of each authorized target. A terminal, `waiting_approval`, or `waiting_input` result returns immediately, including on repeated calls. On timeout, the response contains timedOut: true, an empty results array and one status snapshot per target in states. Snapshots exclude tool progress. If a target becomes ready before the last snapshot, wait returns it as ready. Get and wait expose the device result fields and omit private diagnostics. Wait leaves child turns running and needs no follow-up get. The parent limits an unanswered get to 10 seconds and allows the requested wait interval plus 10 seconds for an unanswered wait. A transport timeout returns a tool error.

A complete terminal result, a full approval descriptor, or a `waiting_input` snapshot with a target identity and sequence can be consumed for automatic context delivery. Its full JSON must remain in an actual model request and the engine must accept a model response before the local consumed seq advances. A persisted/truncated preview, an event or query marked incomplete, a cancelled request, or a provider error before response does not consume it. Consumption deduplicates automatic model context and wakeups. Explicit wait can still read the same ready state. Offered hashes are logged. Consumed sequences are committed atomically with the accepted assistant message in Session Log and restored from the same record.

The mock router owns connection online state and per-hop receipts. The cloud Harness owns automatic parent continuation. The mock provides local routing and test state.

```powershell
uv run pytest tests/test_device_delegation.py -q
```

Tests cover all eight tools through real parent model dispatch, per-tool parameter validation, paging and authorization failures, send busy rejection, online/offline get metadata, wait timeout without cancellation, exact full-result consumption and recovery, provider failure/activity before response, standard stdio wire names, child file writes, parent cancellation races, and event deduplication.

## Cloud turn lifecycle and event delivery

- Open with device version 1 and a stable product session ID in `_meta.session_id`. Reopen it with the same ID and workspace or resume the same ACP session with device version 1. Resume schedules eligible work after any requested history replay. Load only replays history. If the host omits a turn ID, an opted-in parent gets a unique turn ID that is stored with that turn in Session Log.
- The shared context engine delivers current delegation state and unread ready results as internal RUNTIME_STATE injections. These messages are request-only, with the normal injected event and runtime source. The live transcript keeps the ordinary user and tool messages. A full get/wait reply already present in the request history suppresses a duplicate runtime update. The harness records consumption only after the engine accepts a model response. After a natural parent turn ends, an unread ready event can claim a durable automatic turn in the same session. The harness serializes automatic and user prompts. While the parent is waiting for its own user input, arriving child results stay pending for the user’s next prompt. After a history edit, automatic delivery follows the effective parent history. Physical operation records remain available for event acknowledgement and cancellation. It retries an uncommitted wake with the same turn ID after reopening. A committed wake input is retained as runtime history. If the process restarts after input commit but before the reply commits consumption, the harness opens a separate reconciliation turn. The prior input and recorded tool outcomes remain in history.
- Listen for ACP extension notifications `_device/turn` with `version`, `sessionId`, `turnId` and `phase` (`started` or `finished`). The caller can wait for the active turn to finish or retry a prompt that received ACP error −32010 / SESSION_BUSY. The caller can capture an automatic turn ID from the started notification.
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
