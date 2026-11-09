# Thinking and endpoint compatibility

The session decides whether the Agent should request thinking. The provider
adapter translates that choice into the model's API dialect. A child Agent
inherits its parent's current choice in both ordinary delegation and batch file
synthesis. Changing the parent setting between turns also changes subsequent
children; standalone `SubAgentTool` callers can pass `thinking_enabled` explicitly.

Utility calls, including image inspection, long-page summaries and the
continuation judge, continue to request `thinking_enabled=False`. They use the
same provider adapter, so endpoint compatibility applies to them too.

## Five-level host selection

ACP `session/new` and `session/load` accept optional `_meta.reasoning_effort`:
`fast`, `medium`, `high`, `xhigh`, or `max`. Invalid values fail validation.
When present, this field takes precedence over `deep_think`; omission keeps the
legacy boolean behavior. It is independent of the three-level `execution_profile`.
Hosts must recreate/load the ACP session to change effort between turns.

The session owns an isolated provider view. Model rebinding and child model
clones preserve the choice without modifying process defaults. Calls that
explicitly disable thinking (such as summaries) retain their existing policy.

| Provider dialect | Translation |
| --- | --- |
| OpenAI-compatible, including SenseNova | Forward the selected effort exactly; `fast` uses the existing disabled-thinking policy. GPT-6 Astra / GPT-6.1 Sol/Terra fast calls use `low`. |
| Kimi K3, GLM-5.2/5.3 | Medium/high use `high`; xhigh/max use `max`; fast keeps the existing dialect. |
| DeepSeek V4 family | Medium/high/xhigh use `high`; max uses `max`; fast disables native thinking. |
| Gemini | Medium uses `medium`; higher settings use `high`; fast retains its model-specific policy. |
| Qwen, Doubao, older DeepSeek | Retain their native on/off controls. |
| Anthropic | Native thinking budgets: 4,000 / 8,000 / 16,000 / 32,000 tokens, capped to leave at least 25% of `max_output_tokens` (minimum 1,024 tokens) for the response; fast omits thinking. |

Anthropic thinking requires `max_output_tokens >= 2048` so both thinking and
the response have at least 1,024 tokens available. Smaller caps fail locally
when thinking is requested; fast and utility calls retain the full output cap
without a thinking budget. At an 8,192-token cap, xhigh/max request 6,144 thinking
tokens; at 12,288, they request 9,216. The default 64,000-token cap retains all
four native budgets. This headroom reduces truncation risk, but cannot guarantee
a complete response for every task. See the [Anthropic budget rules](https://platform.claude.com/docs/en/build-with-claude/extended-thinking#budget-rules-and-tuning).

GLM-5.2 fast and utility requests retain the legacy disabled-thinking dialect:
no effort field by default, or the existing SenseNova policy when the model is
explicitly opted into that dialect through `BOX_AGENT_SENSENOVA_MODEL_PREFIXES`.
In the latter case, `reasoning_effort_when_disabled: low` remains effective even
when the session has an explicit effort.

Five host choices do not imply five distinct native levels on every model.
OpenAI-compatible gateways that reject an explicit effort return the provider
error; there is no hidden retry at another level. On 2026-11-09, a rebuilt
1.0.0 darwin-arm64 runtime installed in the raccoon-lab development client
completed live requests at all five levels using `sn-sensenova-6-8-flash-lite-p`.
Switching the same conversation to `sn-sensenova-6-8-flash-p` at `max` also
completed, with the runtime reporting the new actual model. A separate local
HTTP fixture verified the packaged runtime's exact outbound effort values.
These checks establish request acceptance, not distinct server-side compute
budgets or task-quality differences. Other model gateways were not live-tested.
Existing GPT-6 Sol/Luna Chat Completions function-tool restrictions remain in place.

References: [OpenAI reasoning](https://developers.openai.com/api/docs/guides/reasoning),
[Kimi K3](https://platform.kimi.ai/docs/guide/kimi-k3-quickstart),
[GLM thinking](https://docs.z.ai/guides/capabilities/thinking),
[DeepSeek thinking](https://api-docs.deepseek.com/guides/thinking_mode/).

## Legacy enabled thinking effort


For OpenAI-compatible requests, enabling thinking selects `reasoning_effort: "max"`
only for these documented model families:

| Models | Official reference |
| --- | --- |
| Kimi K3, Kimi K3.1 Pro (including Highspeed) | [K3](https://platform.kimi.ai/docs/guide/kimi-k3-quickstart), [K3.1 Pro](https://platform.kimi.ai/docs/guide/kimi-k3-1-pro-quickstart) |
| GPT-5.6 / GPT-5.6 Sol, Terra, Luna | [Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol), [Terra](https://developers.openai.com/api/docs/models/gpt-5.6-terra), [Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) |
| GPT-6 Astra, Sol, Luna; GPT-6.1 Sol, Terra | [GPT-6 guide](https://developers.openai.com/api/docs/guides/latest-model) |
| GLM-5.2, GLM-5.3 (including Flash) | [Z.ai deep thinking](https://docs.z.ai/guides/capabilities/thinking) |
| DeepSeek V4 Pro, V4 Flash, V4.1 Flash, `deepseek-flash` | [DeepSeek thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/) |
| SenseNova 6.8 Flash Lite | [SenseNova API documentation](https://platform.sensenova.cn/docs) |

DeepSeek retains its existing gateway `extra_body.thinking` control and adds the
top-level effort only when thinking is enabled. Disabling thinking retains each
model's existing behavior. In particular, Kimi K3 and GLM-5.3 still use `low`,
and GPT-6 Sol/Luna requests with function tools still force `none` on both paths.

Gemini, older models and unknown model names retain their existing mappings.
Qwen and Doubao retain their existing thinking switches. Anthropic requests
still use their native thinking budget; `output_config.effort` is a separate API
contract and is not changed here.

`max` is intended for demanding tasks and can increase latency and token usage.
[OpenAI recommends evaluating the tradeoff](https://developers.openai.com/api/docs/guides/deployment-checklist)
before adopting it for a workload. These model declarations do not prove that a
third-party gateway accepts or forwards the value. This change does not add
Responses API support: official GPT-6 Astra and GPT-6.1 Sol/Terra require
Responses for tool calling; GPT-6 Sol/Luna require it for reasoning with tools.

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
thinking enabled use the model-specific mapping above, including `max` for
SenseNova 6.8 Flash Lite and `high` for other SenseNova model names.
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
