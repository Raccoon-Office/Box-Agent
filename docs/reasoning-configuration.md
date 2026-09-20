# Thinking and endpoint compatibility

The session decides whether the Agent should request thinking. The provider
adapter translates that choice into the model's API dialect. A child Agent
inherits its parent's current choice in both ordinary delegation and batch file
synthesis. Changing the parent setting between turns also changes subsequent
children; standalone `SubAgentTool` callers can pass `thinking_enabled` explicitly.

Utility calls, including image inspection, long-page summaries and the
continuation judge, continue to request `thinking_enabled=False`. They use the
same provider adapter, so endpoint compatibility applies to them too.

## Endpoints that reject `none`

Some OpenAI-compatible endpoints accept only `low`, `medium` and `high` for
`reasoning_effort`. For a model dialect that normally sends `none` when thinking
is disabled, set this optional **root-level** key in that endpoint's
`config/config.yaml`:

```yaml
reasoning_effort_when_disabled: low
```

Keep the existing endpoint, model and credentials in that file. For an isolated
runtime, edit the configuration under its own `BOX_AGENT_HOME`.

| Value | Effect when the model dialect normally sends `none` |
| --- | --- |
| omitted or `null` | Use the provider default: `low` for the exact deployed model noted below, otherwise `none`. |
| `none` | Explicitly retain `none`. |
| `low` | Send `low` instead. This reduces reasoning; it does **not** guarantee reasoning is disabled. |

Other values fail configuration validation. This option affects the existing
SenseNova and eligible Gemini `none` mappings. It does not force a reasoning
field onto other models or alter their native thinking controls. Requests with
thinking enabled retain the existing mapping, including `high` for SenseNova.
Anthropic requests are unchanged.

`SenseNova-Flash-Lite-20260727-v39-fp8-step4k-dpov2-mtp` rejects `none`, so its
default when thinking is disabled is `low`. This also applies to summaries and
other auxiliary requests through the same adapter; it does not enable session
thinking or make auxiliary calls inherit `high`. Explicit endpoint overrides
still take precedence: configuring `none` for this deployment will still be
rejected by its server. Other SenseNova models keep their existing defaults.

The CLI can update and inspect the same setting:

```bash
box-agent config --set reasoning_effort_when_disabled low
box-agent config --get llm.reasoning_effort_when_disabled
```

Remove the key or set it to `null` to restore the previous behavior. No automatic
parameter negotiation is performed. A deterministic HTTP 422 is returned as an
error without repeating the same invalid request, including in the streaming
path. Retryable transport/service failures retain their existing retry policy.

## Configuration ownership and process boundaries

| Client or process | Source of this option |
| --- | --- |
| CLI, API verification and ACP startup client | Main `config.yaml` root key. |
| A `.for_model()` clone | The original client's endpoint option; selecting a different model keeps the same endpoint. |
| A session bound to an immutable model profile | That profile's optional `reasoningEffortWhenDisabled` field. It does not inherit the fallback client's option. |
| Bundled web-extraction MCP server | Its own `Config.load()`. Managed MCP configuration forwards `BOX_AGENT_HOME` so the subprocess reads the intended profile directory. |
| Legacy `lite_llm` configuration block | Its own optional `reasoning_effort_when_disabled`, defaulting to `null` independently of the main endpoint. Current CLI/ACP utility routing uses the main client; adding a `lite_llm` block does not enable a separate utility client. |

For hosts that generate model-profile revisions, include the option in the
new revision for the affected endpoint, for example:

```json
{
  "profileId": "compatible-service",
  "profileRevision": "compatible-service-r2",
  "provider": "openai",
  "apiBase": "https://inference.example/v1",
  "defaultModel": "SenseNova-Flash-example",
  "reasoningEffortWhenDisabled": "low"
}
```

This is one profile entry in the existing registry, with credentials supplied
through the existing profile mechanism. The runtime supports this field; a host
that writes registry entries must include it explicitly. Do not mutate an old
immutable revision to change its meaning.

After changing configuration, recreate the Agent client and restart the MCP
subprocess. The web-extraction server caches its client; editing a file does not
change an already running process. If an MCP server uses another
`BOX_AGENT_HOME`, configure that profile separately.

## Verification boundary

The regression tests exercise both SDK request paths, parent-to-child inheritance
across turns, utilities, profile isolation, deterministic 422 handling, and a
real MCP stdio subprocess with an offline HTTP transport. These prove source
behavior and configuration propagation. They do not prove a deployed runtime was
rebuilt or restarted, endpoint behavior on a live service, or end-to-end PPT
delivery quality.
