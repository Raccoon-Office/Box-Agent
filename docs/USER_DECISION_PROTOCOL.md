# User Decision Protocol

`request_user_decision` is the public, declarative decision surface shared by
built-in workflows and user-provided Skills. A Skill supplies text and stable
option IDs; the host owns rendering and the Box-Agent runtime owns timeout
policy. Skills cannot ship custom UI through this protocol.

Use the tool only when 2-6 choices materially change the user-visible
deliverable, scope, format, or content direction. The model must choose internal
implementation and recovery details itself. Missing facts use
`request_user_input`; post-turn input suggestions remain
`follow_up_suggestions`.

The optional `allow_freeform` argument defaults to `true`. When omitted, the request carries `allowFreeform: true`, allowing the host to show a custom text input. Explicit `false` still disables freeform responses. This default applies only to new requests; persisted requests are unchanged.

## Request event

The successful tool result is sent through `update_tool_call.rawOutput`:

```json
{
  "type": "user_decision_request",
  "schemaVersion": 1,
  "requestId": "decision_...",
  "status": "waiting",
  "question": "Choose the delivery scope.",
  "decisionKind": "delivery_scope",
  "options": [
    { "id": "keep_full", "label": "Keep the full version" },
    { "id": "prototype", "label": "Deliver a prototype first" }
  ],
  "allowFreeform": true,
  "defaultOptionId": "keep_full",
  "autoSubmit": {
    "allowed": true,
    "requestedSeconds": 30,
    "effectiveSeconds": 30,
    "behavior": "submit_default"
  },
  "resumeBehavior": "continue_existing_task"
}
```

The model may request a timeout, but the runtime enables it only when the call
declares low risk, reversibility, and preservation of the explicit user intent.
Authentication, authorization, deletion, payment, purchase, publishing, and
external-message decisions never auto-submit. Invalid or unsupported timeout
data degrades to manual selection.

Decisions follow a safe-progress preference. If the model can choose without
changing the user-visible outcome, it should proceed without showing a card. If
a card is necessary but one option safely continues the user's explicit request,
the model should make that option the default and request a 15-30 second timeout.
Manual-only selection remains required when there is no clearly safe default or
the decision is sensitive. Technical feasibility alone is insufficient: the
default must also be low-risk, reversible, and intent-preserving.

## Host response

Resume the same ACP session with a hidden prompt and matching
`session/prompt._meta.user_decision` (camelCase alias `userDecision` is also
accepted):

```json
{
  "request_id": "decision_...",
  "tool_call_id": "call_...",
  "decision_kind": "delivery_scope",
  "selected_option_id": "keep_full",
  "selected_option_label": "Keep the full version",
  "trigger": "user"
}
```

`trigger` is `user` or `timeout`. A free-form response uses `custom_text`
instead of `selected_option_id`. Older hosts may render the tool's text fallback
and collect a manual reply; they must not invent or silently submit a default.

Outside the Skill-declared business options, the host should always provide a
fixed **Cancel and chat instead** action. Cancellation closes the card, stops the
countdown, and returns focus to the composer. It is not a business option and
must not send a hidden resume prompt. The user's next ordinary message clears
the wait state and continues the same task.

## Skill authoring

A Skill may instruct the model to call `request_user_decision`, but it must not
duplicate the choices in Markdown after the call. Use stable ASCII IDs, explain
the user-visible tradeoff, and treat timeout fields as a request rather than a
guarantee. A Skill's omission of timeout fields does not prevent the model from
adding a safe default and timeout request under the shared policy above. No Skill
manifest declaration is required in schema version 1; hosts that do not implement
the card retain the manual fallback.
