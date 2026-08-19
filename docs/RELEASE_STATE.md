# Release State

## Unreleased — 0.8.85

- Source package metadata and the built-in Skills manifest target `0.8.85`.
- Release artifacts, tags, PyPI/GitHub publication, and officev3 runtime
  rebuild/install/probe have not been produced yet. Add artifact hashes only
  after those steps succeed.

## v0.8.79 (2026-07-13)

- **Commit:** release commit tagged `v0.8.79`
- **PyPI:** https://pypi.org/project/box-agent/0.8.79/
- **GitHub release:** https://github.com/Raccoon-Office/Box-Agent/releases/tag/v0.8.79
- **Compare:** https://github.com/Raccoon-Office/Box-Agent/compare/v0.8.71...v0.8.79

### Artifacts (SHA256)

| File | SHA256 |
| --- | --- |
| `box_agent-0.8.79-py3-none-any.whl` | `51b4b5313866cac5564491698254bda5ccf9702626e85c5ec67559045e162e31` |
| `box_agent-0.8.79.tar.gz` | `ee0ca09273ece392395fd3fd4227688b214ec32ec3b79af1fc7894b9b48389dc` |
| `box-agent-runtime-v0.8.79-darwin-arm64.tar.gz` | `7c2c1209b09eb721c10584d075794b372a9de911957588a844d8060b02e74cbb` |

### What shipped

- **Single-loop sub-agent contract:** calls declare a task plus optional top-level
  Skills, parent-tool selection, and hard step/tool-call budgets. Every child
  uses the same general-purpose loop, inherits the live parent policy boundary,
  blocks recursive delegation, and returns structured diagnostics. Omitting
  `required_tools` inherits every current parent tool; an explicit list narrows
  that boundary.
- **Safer model history compaction:** large mutation arguments remain visible
  for one subsequent model request and are compacted afterward. Internal history
  placeholders are rejected if the model tries to reuse them as file/code
  arguments, with one automatic regeneration attempt.
- **Bounded file mutations:** `write_file` and `append_file` advertise and
  enforce an 8,000-character per-call content limit so large static artifacts
  are written in reviewable chunks.
- **Skill routing metadata:** `allowed-tools` and `allowed_tools` are normalized,
  deduplicated, sorted, exposed in catalog metadata, and used during explicit
  sub-agent capability resolution. The manifest currently lists 32 built-in
  Skills.

### Proof and known gaps

- Focused regression coverage lives in `tests/test_sub_agent_capabilities.py`,
  `tests/test_sub_agent_tool.py`, `tests/test_core.py`, `tests/test_tools.py`,
  `tests/test_skill_loader.py`, `tests/test_cli_config.py`, and `tests/test_acp.py`.
- Verification: 244 focused tests and the full suite (1,250 passed, 16 skipped)
  passed; wheel/sdist passed `twine check` and contained no `__pycache__`,
  `.pyc`, `.DS_Store`, or `.omx` entries; the extracted darwin-arm64 runtime
  reached `ACP protocol ready` with protocol stdout clean.
- Existing configs remain compatible because the new synthesis timeout has a
  default. Setting it to `0` disables only the extra batch synthesis wall-clock
  cap; the provider request timeout still applies.
- **Runtime: darwin-arm64 only.** No `darwin-x64`, Linux, or Windows artifact was
  built in this release. The runtime was not installed into officev3 during the
  release; downstream packaged behavior still requires install/restart/probe.

## v0.8.71 (2026-06-21)

- **Commit:** `4ffc2a8e53e247cb031a3921545d29384dce4f82` (main, tag `v0.8.71`)
- **PyPI:** https://pypi.org/project/box-agent/0.8.71/
- **GitHub release:** https://github.com/Raccoon-Office/Box-Agent/releases/tag/v0.8.71
- **Compare:** https://github.com/Raccoon-Office/Box-Agent/compare/v0.8.70...v0.8.71

### Artifacts (SHA256)

| File | SHA256 |
|------|--------|
| `box_agent-0.8.71-py3-none-any.whl` | `2627dc70f5b26290f2dca826e29fff10c7b9798c6f94b0300694fabd069f6de0` |
| `box_agent-0.8.71.tar.gz` | `6e6ed72a7dc5b9e1ec5b44d3963042c5e981e1cca1c0a146bba8a2b3eea71c91` |
| `box-agent-runtime-v0.8.71-darwin-arm64.tar.gz` | `87dd99fcb138e3911b61b05814551a7c680d00d4e2905391ba38d05516387f53` |

### What shipped

- **Obsidian 原生 CLI 工具支持** (`3d473ec`, #4)：env_context 新增 `obsidian` 已知字段，宿主可注入 vault/CLI/app 状态，模型据此规划 Obsidian 工具调用。
- **ACP turn usage 埋点** (`117f687`)：`PromptResponse._meta.usage.totalTokens` 返回每轮 token 总量（见 `llm/token_meter.py`）。
- **Playwright 与真实浏览器连接器能力区分** (`6a0a1c3`)：env_context 拆分 `browser_tools`（Playwright MCP）与 `browser_connector`（浏览器扩展 daemon），action_hint 据此分别触发。
- **goal 实现优化** (`fcd3135`) 与 CLI standalone task 控制改进 (`18579ce`)。
- **MCP** (`d60bbe0`)：处理已取消的 streamable HTTP 连接。
- **文档刷新** (`4713062`)：project guides 与示例更新。
- 内置 skill 数：30（`_manifest.json`，`box_agent_version=0.8.71`）。

### Follow-ups / known gaps

- **Runtime: darwin-arm64 only.** 与 v0.8.70 相同，`darwin-x64` / `linux-*` / Windows runtime 未随本次发布构建。
- **后续改动状态**：tag `v0.8.71` 之后累积的改动已随
  `v0.8.79` 发布；当前开发版本见本文顶部 Unreleased 段。后续发版仍须
  以真实构建产物补齐 SHA256。

## v0.8.70 (2026-06-16)

- **Commit:** `3160ce3a1ab79d73d9814c5e4688d911810a6a81` (main)
- **PyPI:** https://pypi.org/project/box-agent/0.8.70/
- **GitHub release:** https://github.com/Raccoon-Office/Box-Agent/releases/tag/v0.8.70
- **Compare:** https://github.com/Raccoon-Office/Box-Agent/compare/v0.8.68...v0.8.70
- **Note:** 0.8.69 was bumped in code but never tagged/released; v0.8.70 is the first published release since v0.8.68.

### Artifacts (SHA256)

| File | SHA256 |
|------|--------|
| `box_agent-0.8.70-py3-none-any.whl` | `0e55faeaa5c966d634dba2635bfb53689c29e8fc29b8f91ca53d13accedf321e` |
| `box_agent-0.8.70.tar.gz` | `6be546098969d681cd3b2b958fc9b22078031edba430de8480089193a0dfee57` |
| `box-agent-runtime-v0.8.70-darwin-arm64.tar.gz` | `e0269c518ddb8880f71dc61671d392318348fbb3747a7816a213ad7e78b9db0b` |

### What shipped

Community-submitted "featured" skills (officev3 on-demand install, NOT builtin):
- Five submitted skills now ship inside the wheel/runtime as orphans (present on disk, excluded from the builtin `_manifest.json` whitelist) so officev3 surfaces them as "精选推荐" cards and installs them on demand into `~/.box-agent/skills/`:
  - `viral-topic` (suite: `wechat-/x-/bilibili-/youtube-viral-topic`) + `viral-title` — author **袋鼠帝**
  - `self-media-ad-workflow` — author **金三**
  - `worldcup-prediction` + `world-cup-briefing` — author **贝贝**
- Each `SKILL.md` carries an `author` frontmatter field; officev3 reads it to attribute the card.
- `scripts/generate_skills_manifest.py`: new `EXCLUDED_SKILL_DIRS` excludes these top-level skill dirs (and their nested sub-skills) from the builtin manifest by **path prefix** (name-only would miss the `viral-topic` suite's sub-skills). Regenerating still yields the same 28 builtin skills.

worldcup-prediction docs correction:
- The team-database section in `SKILL.md` and the comment in `references/schedule.yaml` claimed "聚焦 25 队 / 库外缺数据" (even listing already-present teams like CPV/EGY/KSA as missing). Corrected to the real **48 teams / 12 groups (A–L)**, rebuilt directly from `references/teams/*.yaml` data. No team data changed — documentation only.

Build hygiene:
- The wheel/runtime were built after removing untracked local cruft (`__pycache__/*.pyc`, `.omx/` runtime state) that `MANIFEST.in`'s `recursive-include box_agent/skills *` had been sweeping into prior artifacts from other skills (document-skills, research-synthesis, etc.). 0.8.70 wheel and runtime contain 0 such files.

### Follow-ups / known gaps

- **Runtime: darwin-arm64 only.** Built from this macOS host. `darwin-x64` / `linux-*` / Windows (`scripts/build_win_runtime.py`) runtimes are NOT built for 0.8.70; downstream apps on those platforms get the featured skills only after their runtime is rebuilt/repackaged.
- **officev3 build-resources not synced.** This release publishes the runtime artifact; it does not push it into officev3's `build-resources/box-agent-runtime`. The featured cards can only install once officev3 runs `npm run box-agent:install` (or repackages) with the 0.8.70 runtime. Until then the cards render (with author attribution) but install fails with "不是有效目录".
- **Builtin cruft is upstream local state, not git-tracked.** The `__pycache__`/`.omx` swept into earlier artifacts came from running those skills locally; consider a `MANIFEST.in` prune or a pre-build clean step so future releases don't depend on a clean checkout.

## v0.8.67 (2026-06-08)

- **Commit:** `7a59269814340a50b6b9a1e4a0a285c90f53a28c` (main)
- **PyPI:** https://pypi.org/project/box-agent/0.8.67/
- **GitHub release:** https://github.com/Raccoon-Office/Box-Agent/releases/tag/v0.8.67
- **Compare:** https://github.com/Raccoon-Office/Box-Agent/compare/v0.8.66...v0.8.67

### Artifacts (SHA256)

| File | SHA256 |
|------|--------|
| `box_agent-0.8.67-py3-none-any.whl` | `989e0961f458d7a5ab25f5daf42375fe68d4afe25de648b5309c0d35c06cc7dc` |
| `box_agent-0.8.67.tar.gz` | `f37535890a057187118f67378cd7268cbd2faa2794af8c4056d0935684dc4390` |

### What shipped

Configurable LLM client timeout:
- The provider SDK clients (`AsyncOpenAI`/`AsyncAnthropic`) were constructed **without** a `timeout`, so the SDK default (600s) always applied and there was no way to tune it. When a gateway stalls before the first token, the call hangs for the full default before an `APITimeoutError` surfaces.
- New `timeout` field on `LLMConfig` and `LiteLLMConfig` (default `600.0`, behavior unchanged), threaded end to end: `config.yaml` → `LLMConfig`/`LiteLLMConfig` → `LLMClient` → `OpenAIClient`/`AnthropicClient` → `AsyncOpenAI`/`AsyncAnthropic(timeout=)`. All 7 `LLMClient` call sites (CLI + ACP main/lite) forward `config.*.timeout`.
- Hosts can now lower it to fail fast with a clean error instead of hanging. Tests: `tests/test_llm_timeout.py` (SDK propagation, wrapper threading, defaults, YAML parsing).
- **Origin:** customer log analysis (Windows office-raccoon 0.7.37). 3 consecutive turns each died at **~72s** with "Request timed out" — a **server-side gateway cutoff** (the endpoint was healthy: a lightweight call to the same model succeeded in 8s right after). This release makes the client timeout tunable; it does not change the gateway behavior that caused the incident.

Factual & search reliability guardrails:
- Stop the model from treating a todo list or sparse search results as evidence. A "completed" todo means steps ran, not that facts were verified; conclusions must rest on retrieval/file evidence.
- New system-prompt "Factual & Search Reliability" section + a Plan-step note that `todo_write` is progress-tracking only and must not narrow the user's request or lower verification standards. `todo_tool` description tightened to match.

### Follow-ups / known gaps

- **Runtime artifacts not built this release.** PyPI wheel/sdist only. The reporting customer runs the **Windows** desktop runtime, which cannot be built from macOS (`scripts/build_win_runtime.py`); the timeout fix reaches that app only after the Windows runtime is rebuilt and repackaged.
- **Gateway-side root cause is upstream.** The ~72s timeout was the model gateway not returning a first token for a large-context request; investigate gateway first-token latency / `proxy_read_timeout` separately. Consider in-turn retry / degraded-mode messaging on timeout as a follow-up.
- **Test hygiene:** full `pytest` still shows the same ~14 env-dependent permission/symlink-scoping + MCP-timeout failures documented since v0.8.64 — confirmed pre-existing (they fail identically on the pre-change tree), unrelated to this release.

## v0.8.66 (2026-06-08)

- **Commit:** `0441b2e81be46f2bb2a668cc78d849e1d3937410` (main)
- **PyPI:** https://pypi.org/project/box-agent/0.8.66/
- **GitHub release:** https://github.com/Raccoon-Office/Box-Agent/releases/tag/v0.8.66
- **Compare:** https://github.com/Raccoon-Office/Box-Agent/compare/v0.8.65...v0.8.66

### Artifacts (SHA256)

| File | SHA256 |
|------|--------|
| `box_agent-0.8.66-py3-none-any.whl` | `571bfd72a822285dd8d485a3f3ec8ddb27b29c9ba0d3638993cd55157425d9ed` |
| `box_agent-0.8.66.tar.gz` | `fddfa0da35b526d41411ef61cfbf04de0677bb48888fe1b8b60e03a589fbeebd` |
| `box-agent-runtime-v0.8.66-darwin-arm64.tar.gz` | `97f9e2c8d8a2d017f61d820bf99b82617ddaa408e09e3b288ac74b9d288eb5fc` |

### What shipped

LLM error humanization:
- New `box_agent/llm/error_messages.py` — `classify_llm_error` / `humanize_llm_error` map opaque provider exceptions (content_filter, auth, permission, rate_limit, quota, context_length, model_not_found, server_error, timeout, connection) to short Chinese messages. Bulletproof by contract: runs inside the error path, never raises, unwraps `RetryExhaustedError`/`StreamInterrupted` to inspect the underlying provider error, SDK-agnostic attribute + substring detection.
- `core.py`: soft errors (content moderation) end the turn as a **normal assistant reply** (no `Error:` banner, no red), persisted to history; all other errors surfaced humanized.
- `lightweight.py`: new `LightweightContentFiltered` (`code=content_filter`) lets `_llm/prompt` callers (title/summary) fall back to a neutral default; provider raw JSON never leaks.
- `acp`: content_filter logged at info, returned as a stable error code.

pptx skill — content & outline gate:
- The outline gate (`references/outline.md` + `scripts/validate_outline.js`) was **orphaned** — SKILL.md's new-deck route jumped straight to image planning/HTML, so the gate never fired. Now wired in as route step 1 (§1.1), listed in §2 commands and §7 references.
- **Warm reject, not cold:** under-specified → ask 1-3 focused questions with a sensible default; under-sourced → route to `research-synthesis`, then build the slide plan from sourced findings; never fabricate. Hard `BLOCKED` stays reserved for `creative_image_mode` images; carve-out so creative short topics aren't pushed into research.
- Canvas-contract docs (html-first / html-editable): copy `references/starter/common.css` verbatim for the locked 1920×1080 `.slide`; `--canvas WxH` opt-in for nonstandard sizes.

### Follow-ups / known gaps

- **Runtime artifact:** only `darwin-arm64` built/uploaded. `darwin-x64` / Windows runtimes not built this release; the officev3 desktop app picks up these fixes only after the bundled runtime is rebuilt per platform.
- **Test hygiene:** full `pytest` still shows the same ~13 env-dependent permission/symlink-scoping failures documented since v0.8.64 — confirmed pre-existing (they fail identically on clean `v0.8.65` HEAD), unrelated to this release.
- **Upstream dependency:** the warm gate routes to `research-synthesis` for the deck's *content*; the officev3 expert layer still needs its "普通 PPT 设计师" role to add the research pre-step so the two layers interlock end-to-end.

## v0.8.65 (2026-06-07)

- **Commit:** `80908f81fd2e872e8db336a2aa79b65df597a909` (main)
- **PyPI:** https://pypi.org/project/box-agent/0.8.65/
- **GitHub release:** https://github.com/Raccoon-Office/Box-Agent/releases/tag/v0.8.65
- **Compare:** https://github.com/Raccoon-Office/Box-Agent/compare/v0.8.64...v0.8.65

### Artifacts (SHA256)

| File | SHA256 |
|------|--------|
| `box_agent-0.8.65-py3-none-any.whl` | `f6867fd22817c4fba3ae8b305baa361a0eb61e273f75879d3864c4582989cf0b` |
| `box_agent-0.8.65.tar.gz` | `f670ec1fa9efbe7ed3db4d7d3c0540675837a2c89c9ed4561aa435f0e3fc7963` |

### What shipped

ACP startup reliability:
- **Fix:** the one-time OpenClaw memory import ran a blocking `await llm.generate()` *before* the ACP stdio transport was established, so a slow/stalled first-launch LLM call tripped the host's 15 s `initialize` timeout and the process was killed before becoming ready. The import marker is written only after the call, so every restart re-hit the same stall — symptom: perpetual `box-agent ACP 初始化超时（15s）`.
- Moved OpenClaw import off the critical path, merged with the already-backgrounded memory maintainer into one fire-and-forget `memory-bootstrap` task (import → maintain kept sequential to avoid racing on `MEMORY.md`). Startup now reaches stdio readiness with only local work on the critical path.

### Follow-ups / known gaps

- **Runtime artifacts not built this release.** PyPI wheel/sdist only. The fix reaches the officev3 desktop app **only** after the bundled `box-agent-runtime` is rebuilt and repackaged — in particular the **Windows** runtime (`scripts/build_win_runtime.py`), which cannot be built from macOS. The failing office-raccoon install will not pick up this fix until that runtime ships.
- Host-side mitigations from the incident (raising the 15 s ACP timeout; pinning `BOX_AGENT_ACP_COMMAND`) are now unnecessary for this root cause and can be reverted.

## v0.8.64 (2026-06-05)

- **Commit:** `f5882f315b09668d8385cc674431300d272a436f` (main)
- **PyPI:** https://pypi.org/project/box-agent/0.8.64/
- **GitHub release:** https://github.com/Raccoon-Office/Box-Agent/releases/tag/v0.8.64
- **Compare:** https://github.com/Raccoon-Office/Box-Agent/compare/v0.8.63...v0.8.64

### Artifacts (SHA256)

| File | SHA256 |
|------|--------|
| `box_agent-0.8.64-py3-none-any.whl` | `5df9ce1523fea9779b1889e09d9547bca8b91487bbdfc8ebf9c08e2d9b4bc38c` |
| `box_agent-0.8.64.tar.gz` | `d4db2124fada877c928e46266d59a50c72d35d533e78c95c8fc14829cb3be266` |
| `box-agent-runtime-v0.8.64-darwin-arm64.tar.gz` | `bc98ce8e08d08acf9e43f93a9c73e96e05b5bd66b2160dedbe24f16376d94a52` |

### What shipped

Sub-agent reliability & safety:
- Sub-agents inherit the parent's **live** tool map (late-loaded MCP `web_search` included), not a construction-time snapshot.
- Sub-agent `max_steps` 60 → 40; new no-progress circuit breaker (`run_agent_loop(no_progress_limit=...)`, default 6 for sub-agents, off for top-level agent).
- Hard concurrency cap `AgentConfig.max_parallel_tools` (default 8) on `parallel_safe` tool calls; parallel guidance 3-5 → 3-7.

UX / orchestration:
- `sub_agent` short `title` surfaced in ACP labels + progress (`SubAgentEvent.title`).
- Orchestrated expert teams emit a visible "专家动作 / Expert actions" section.
- `skill_loader` desktop disabled-skills settings scoped to the officev3 user-skill source only (no leak into tests/standalone loaders).

### Follow-ups / known gaps

- **P0 (ops, not code):** the incident that triggered this work had MCP `web_search` unavailable for the whole session (401 `authorization_verify_error` + fallback to lite model `raccoon-chat-ml-5-5`). Investigate officev3 MCP config/auth so `web_search` actually loads. The code fix only guarantees parity with whatever the parent has.
- **Runtime artifact:** only `darwin-arm64` built/uploaded. `darwin-x64` / Windows runtimes not built this release.
- **No-progress breaker limitation:** detects failing/empty tool results; a tool that "succeeds" with useless content (e.g. anti-scraping HTML) is not caught — bounded by `max_steps=40`.
- **Test hygiene:** full `pytest` shows ~14 env-dependent failures (filesystem permission/symlink scoping + a skill_loader test-pollution case); all pass in isolation and are unrelated to this release. Worth de-flaking later.
