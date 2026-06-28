# 上下文压缩

Box-Agent 如何在保证正确性、多 provider 兼容性与 prompt cache 友好性的
前提下，将 LLM 消息历史控制在模型上下文窗口之内。

## 设计目标与非目标

**目标**
- 让消息列表无限期地保持在 `token_limit` 以下，即便经过数百次工具调用。
- 严格不变性：任何压缩路径都必须**单调地减少或保持** token 数量。
  会让历史变大的压缩路径就是 bug。
- 廉价优先：开销（LLM 调用、延迟、缓存失效）只在更便宜的层无法满足预算时才升级。
- Provider 中立：在 Anthropic / OpenAI 协议 / DeepSeek / Qwen / MiniMax M2 等
  各条路径上行为完全一致。
- 对下游工具无损：事件流、logger、磁盘 artifact 始终持有原始完整数据。
  **只压缩模型看到的视图**。

**非目标**
- 可逆。一旦在某次运行中被压缩，模型对该步骤的视图就无法恢复。logger 持有
  原文以供事后排查。
- 在所有工具输出上保持语义完整。我们针对真实负载中占主导地位的形态
  （大文件内容、重复的工具调用、长执行轮次）做优化，而非任意 worst case。

## 架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                        run_agent_loop step                          │
│                                                                     │
│  ┌─────────────────┐    ┌────────────────────┐                      │
│  │  Layer 1        │    │  Layer 2           │                      │
│  │  _micro_compact │───▶│  _maybe_summarize  │────┐                 │
│  │  (每步，        │    │  (token 触发，     │    │                 │
│  │   0 LLM 调用)   │    │   1 次 LLM 调用)   │    │                 │
│  └─────────────────┘    └────────────────────┘    │                 │
│                                                    ▼                │
│                                          ┌──────────────────┐       │
│                                          │  LLM.generate_   │       │
│                                          │  stream(...)     │       │
│                                          └──────────────────┘       │
│                                                    │                │
│                                                    ▼                │
│                                          ┌──────────────────┐       │
│                                          │ append assistant │       │
│                                          │ message          │       │
│                                          └──────────────────┘       │
│                                                    │                │
│                  (abort 路径: cancel/max_tokens/error)              │
│                                                    ▼                │
│                                          ┌──────────────────┐       │
│                                          │ _cleanup_        │       │
│                                          │ incomplete_      │       │
│                                          │ messages         │       │
│                                          └──────────────────┘       │
│                                                    │                │
│                                                    ▼                │
│                                          ┌──────────────────┐       │
│                                          │ tool exec        │       │
│                                          │   │              │       │
│                                          │   ▼              │       │
│                                          │ Layer 0          │       │
│                                          │ _compact_visible │       │
│                                          │ _tool_content_   │       │
│                                          │ for_model        │       │
│                                          │ (按调用，        │       │
│                                          │  artifact 感知)  │       │
│                                          └──────────────────┘       │
│                                                    │                │
│                                                    ▼                │
│                                            (下一步)                 │
└─────────────────────────────────────────────────────────────────────┘
```

层编号反映的是每层在一个 step **代码顺序中的执行时机**，**不是优先级**：
Layer 0 在代码顺序中最后执行，但对任何单条 tool 结果来说它是最早作用的一层。

| 层 | 触发条件 | 成本 | 作用对象 | 压缩范围 |
| - | -------- | ---- | -------- | -------- |
| 0 — 模型可见内容 / 参数 | 每次工具调用 & 每次 append assistant | 0 LLM，O(content) | 单条消息（append 前） | 生成型 artifact 工具输出与内联脚本参数 |
| 1 — `_micro_compact` | 每步 | 0 LLM，O(messages) | 整个 `messages` 列表，原地修改 | 旧的 tool-role 消息 |
| 2 — `_maybe_summarize` | `_estimate_tokens > token_limit` 或 `api_total_tokens > token_limit` | 每轮 1 次 LLM 调用 | 整个 `messages` 列表，整体替换 | assistant + tool 执行序列 |
| Cleanup — `_cleanup_incomplete_messages` | abort 路径（cancel / max_tokens / 空流 / 错误） | 0 LLM | `messages` 尾部 | 仅当前正在进行的 incomplete turn |

## Layer 0 — 模型可见工具内容 & 参数压缩

在生成型 artifact 内容落入模型可见消息历史前，有选择地缩减它们。
完整内容仍然：
- 作为 `ToolCallResult` 发给事件消费者（CLI / ACP / 子 agent），
- 写入磁盘 `{workspace}/output/`，
- 在 agent 日志中原文记录。

**被压缩的工具输出**（`_compact_visible_tool_content_for_model`）
- 当前仅作用于 `read_file`，且路径匹配 `_path_needs_compact_model_context`
  的生成型 artifact 启发式规则。
- 替换格式：
  ```
  [Full tool output omitted from model history]
  Tool: read_file
  Path: output/deck.html
  Lines returned: 1240
  Characters returned: 58213
  Reason: generated/QA artifact content can bloat future LLM turns;
  call read_file again with offset/limit if exact content is needed.

  Preview first 20 lines:
  …
  ```

**被压缩的工具调用参数**（`_compact_tool_call_arguments_for_model`）
- `write_file.content` 与 `edit_file.{old_str, new_str}` 若路径匹配
  `_MODEL_CONTEXT_PATH_EXTS`，替换为带 12–20 行预览的结构化占位符。
- 兜底：任何字符串参数若长度大于 `_MODEL_CONTEXT_CONTENT_THRESHOLD`，
  一律压缩。

**白名单（目前刻意保守）：**
- 后缀：`.html`、`.htm`、`.json`、`.md`、`.txt`、`.log`、`.xml`
- 特殊文件名：`qa.json`、`html_self_check.json`、`visual_review.md`、
  `vision-review-prompt.txt`
- 路径片段启发式：路径中包含 `qa/` 的任何文件

这一层目前是白名单驱动的，扩展到纯尺寸启发式是一项已知的改进方向。

## Layer 1 — 微压缩（每步）

**每次 LLM 调用之前**都运行的、零 LLM 成本的廉价压缩。

```python
def _micro_compact(messages: list[Message]) -> int:
    """把旧的 tool 结果内容替换为短占位符。

    Token 感知：保留最近 N 条 tool 结果完整，但如果"最近 N 条本身"
    就已经超出窗口预算，则收缩 keep 窗口，避免几条超大输出绕过压缩。

    任何情况下都至少保留最近 1 条 tool 消息。
    """
```

### 算法

```
tool_indices = messages 中 role == "tool" 的所有下标

# 保守下限
keep_count = min(_KEEP_RECENT_TOOL_RESULTS, len(tool_indices))   # 3

# Token 感知收缩 —— 至少保留 1 条
while keep_count > 1:
    recent = tool_indices[-keep_count:]
    if sum(_approx_tokens(msgs[i]) for i in recent) <= _KEEP_RECENT_TOOL_TOKEN_BUDGET:
        break        # 窗口可接受
    keep_count -= 1  # 过大，收缩

# 把比 keep 窗口更老的全部压缩
for idx in tool_indices[:-keep_count]:
    if len(content) > _MIN_COMPACT_LEN:    # 200
        messages[idx] = Message(
            role="tool",
            content=f"[Previous result from {tool_name}: {first_line[:100]}...]",
            tool_call_id=...,    # 保留（协议正确性）
            name=...,            # 保留
        )
```

### 常量

| 名称 | 值 | 含义 |
| ---- | -- | ---- |
| `_KEEP_RECENT_TOOL_RESULTS` | `3` | keep 窗口的下限条数 |
| `_KEEP_RECENT_TOOL_TOKEN_BUDGET` | `12_000` | 超过此 token 数，keep 窗口开始收缩 |
| `_MIN_COMPACT_LEN` | `200` | 短于此长度的 tool 结果不值得压缩 |

### 性质

- **幂等**。被压缩过的 tool 消息长度 < `_MIN_COMPACT_LEN`，下一次调用不会
  再动它。压缩后的前缀因此是稳定的，**LLM prompt cache 不会被每步打穿**。
- **协议安全**。`tool_call_id` 与 `name` 被保留，assistant↔tool 消息配对
  在所有 provider 上仍然有效。
- **首行锚点**。原始输出的首行被保留在占位符里，这通常是工具最有用的
  摘要，可以防止模型仅仅为了回忆刚才工具返回了什么而重新调用。

### 行为对比

| 场景 | 旧行为（仅 N-recent） | 新行为（token 感知） |
| ---- | --------------------- | -------------------- |
| 10 条小 tool 结果 | 保留最近 3，压缩 7 | 完全相同 |
| 3 × 50KB `read_file` 结果 | 全 3 条保留（~38k tokens 漏过） | 窗口收缩到 1，压缩 2 条 |
| 1 × 100KB tool 结果 | 保留 1（正确） | 保留 1（正确，下限永不小于 1） |

## Layer 2 — 摘要（token 触发）

只有在 token 估算超过 `token_limit` 时才触发。每轮（每个 user turn）
1 次 LLM 调用，然后整个消息列表被替换。

### 触发条件

```python
estimated = _estimate_tokens(messages)   # tiktoken cl100k_base
if estimated <= token_limit and api_total_tokens <= token_limit:
    return None   # 不压缩
```

客户端估算和 provider 上报的 `api_total_tokens` **都**必须低于阈值，
任一超过都会触发。

`token_limit` 来源于 `AgentConfig`：
```
context_token_limit = int((context_window - max_output_tokens) * 0.9)
```
Box-Agent 默认 `context_window = 180_000`。用户自配 endpoint 省略
`max_output_tokens` 时默认 `63_999`，推导出 `context_token_limit = 104_400`
（约 **104k**）；托管的 `xiaohuanxiong.com` endpoint 省略该字段时默认
`80_000`，推导出 `context_token_limit = 90_000`（约 **90k**）。这些值都可以在
`config.yaml` 里覆盖。10% 的预留空间用来吸收 token 估算漂移与摘要请求本身的开销。

### 算法

```python
async def _maybe_summarize(llm, messages, token_limit, api_total_tokens, skip_check):
    if skip_check:
        return None, False, 0
    if estimate(messages) <= token_limit and api_total_tokens <= token_limit:
        return None, False, estimated

    user_indices = [i for i, m in enumerate(messages) if m.role == "user" and i > 0]
    new_messages = [messages[0]]   # 保留 system prompt

    for idx, user_idx in enumerate(user_indices):
        user_msg = messages[user_idx]
        exec_msgs = messages[user_idx + 1 : next_user_or_end]

        # 折叠孤立的 summary marker —— 防止陈旧摘要在多轮压缩中堆积
        if _is_summary_marker(user_msg) and not exec_msgs:
            continue

        new_messages.append(user_msg)

        if exec_msgs:
            try:
                summary = await _create_summary(llm, exec_msgs, idx + 1)
            except Exception:
                # 失败路径：丢弃 exec_msgs。token 严格递减，绝不增加。
                # 对话流保持完整（user_msg 仍在）。
                summary = ""
            if summary:
                new_messages.append(Message(
                    role="user",
                    content=f"{_SUMMARY_MARKER}\n\n{summary}",
                ))

    return new_messages, True, estimated
```

### 轮次边界

一"轮" = 一条 user 消息 + 它与下一条 user 消息之间的所有内容。
system prompt 和 user 消息原样保留；只有一轮内部的 assistant / tool
执行消息会被摘要。

### Summary marker

每个被摘要的轮次会留下一个标记：
```
[Assistant Execution Summary]

<摘要文本>
```
存为 `user` 角色的消息（这样下一次 assistant turn 能把它视作上下文）。
marker 前缀就是常量 `_SUMMARY_MARKER`，被 `_is_summary_marker` 用于在
后续压缩周期中识别孤立 marker。

### `_create_summary` 契约

```python
async def _create_summary(llm, messages, round_num) -> str:
    """单次 LLM 调用。失败抛错。"""
    response = await llm.generate(
        messages=[system_role, user_prompt],
        tools=None,                # 显式 —— 跨 provider 一致
        thinking_enabled=False,    # 显式 —— 跨 provider 一致
    )
    return response.content        # 绝不返回未摘要的输入
```

Prompt 要求：
1. 聚焦"完成了什么任务、调用了哪些工具"。
2. 保留关键执行结果。
3. 不超过约 800 tokens。
4. **使用与输入相同的语言**（避免中英文混合会话中的双向翻译开销）。
5. 不复述 user 内容，只摘要 agent 的执行过程。

### 性质

- **Token 单调**。`_maybe_summarize` 的所有路径 —— 摘要成功、摘要失败、
  孤立 marker 折叠 —— 都严格减少或保持 token 数。旧的反向膨胀 bug
  （`except` 分支 `return summary_content`）已修复：失败现在丢弃
  `exec_msgs`，而不是把它替换成一个更大的拼接占位符。
- **Marker 折叠**。前后相邻、中间没有新 exec_msgs 的 summary marker
  会折叠成一个，限制了"摘要套摘要"残留物在多轮中的增长。
- **Provider 一致**。`tools=None` + `thinking_enabled=False` 显式传递。
  Anthropic / OpenAI / DeepSeek / Qwen / MiniMax M2 各路径产生相同形状
  的输出 —— 例外：某些会无条件 emit thinking 块的 provider（例如
  MiniMax M2），线协议开关被遵守，但 provider 行为无法在 Box-Agent 这一
  层抑制。

## Cleanup — `_cleanup_incomplete_messages`（abort 路径）

在 `run_agent_loop` 的五个 abort 站点被调用：

| 站点 | 触发原因 |
| ---- | -------- |
| Cancel after stream | 用户按 Esc / ACP `cancel` 在流式输出中触发 |
| `MAX_TOKENS` | provider 以 `finish_reason="length"` 停止 |
| Cancel before tools | 流式结束后、工具执行前收到 cancel |
| Empty-args 循环熔断 | 模型连续 `EMPTY_ARGS_LIMIT` 次发出空参数 tool_calls |
| Cancel after tool | 多个串行工具调用之间收到 cancel |

### "Incomplete" 的定义

```
last_assistant       = messages 中最近的 role == "assistant"
trailing_tool_count  = last_assistant 之后的 tool 消息数
expected_tool_count  = len(last_assistant.tool_calls or [])
has_content          = bool(last_assistant.content or last_assistant.thinking)

incomplete = (
    expected_tool_count > 0 and trailing_tool_count < expected_tool_count
    or expected_tool_count == 0 and not has_content
)
```

- **Incomplete（删除这一 turn）**：assistant 声明了 tool_calls 但至少有一个
  tool 响应缺失；或 assistant 完全没有产生 content / thinking / tool_calls
  （provider 在任何输出落地之前就切断了流）。
- **Complete（保留这一 turn）**：assistant 产生了 content（或 thinking），
  并且所有声明的 tool_calls 都有对应的 tool 响应。

### 为什么改

旧版"无条件删除最后一个 assistant 起的所有内容"在 5 个 abort 站点里
有 4 个是正确的，但在 mid-stream cancel 那个站点是**错的** —— 因为那个
位置 `assistant_msg`（当前 step 的）尚未 append，"最后一个 assistant"
其实是**上一个已完成**的 turn，旧代码会把它误删，破坏消息列表，导致
任何续跑都不一致。

新的"按形状判定"对 complete turn 是 no-op（所以已完成的之前 turn 安全），
对另外 4 个站点行为与旧版完全等价（那里刚 append 的 assistant 确实
incomplete）。

## Token 估算

`_estimate_tokens` 与 `_approx_tokens_for_content` 都使用
`tiktoken.get_encoding("cl100k_base")`。在 `ImportError` 或初始化失败时
回退到 `len(text) // 4`，这是刻意保守的估算（高估英文，对 CJK 大致相符）。

同一个 encoder 服务于三处：
- Layer 1 keep 窗口的预算判定
- Layer 2 的触发检查
- LLM 调试日志中的 payload 摘要（`llm/debug_logging.py`）

这意味着预算数值（`12_000`、`context_token_limit`）是自洽的：一条消息
对其中一个估算贡献 T token，对另一个估算也贡献 T token。

## Provider 兼容矩阵

| 关注点 | Anthropic | OpenAI 协议 | DeepSeek | Qwen | MiniMax M2 |
| ------ | --------- | ----------- | -------- | ---- | ---------- |
| `tools=None` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `thinking_enabled=False` | ✓ | 忽略 | 忽略 | 支持则尊重 | 线协议尊重，provider 仍会 emit |
| `tool_call_id` 保留 | 必需 | 必需 | 必需 | 必需 | 必需 |
| Layer 1 占位符形态 | ✓ | ✓ | ✓ | ✓ | ✓ |
| Layer 2 summary marker 用 `user` 角色 | ✓ | ✓ | ✓ | ✓ | ✓ |

压缩路径中没有任何 provider 特化分支。

## 与相邻系统的交互

- **Logger（`AgentLogger`）**。压缩各层原地修改 `messages`；logger 在任何
  压缩之前就捕获了原始的 `LLMResponse` / `ToolResult` 载荷。磁盘日志是
  事后排查的唯一真相源。
- **Memory extractor**。`MemoryExtractor.maybe_extract` 在 Layer 2 替换
  `messages` **之前**用一份快照调用（`trigger="pre_summarize"`）。这保证
  memory 提取看到的是完整的执行细节，而不是摘要后的形态。
- **事件流**。压缩**永远不**影响事件流 —— 所有消费者（CLI 渲染、ACP、
  子 agent）看到完整的 `ToolCallResult`、`ContentEvent` 等。模型视图与
  用户视图刻意分离。
- **子 agent**。`SubAgentTool` 以自己的 `token_limit`（默认 `40_000`）
  运行 `run_agent_loop`；所有压缩层都独立作用于子 agent 的消息列表。

## 测试

覆盖位于 `tests/test_core.py`：

| 测试 | 层 | 锁定的不变性 |
| ---- | -- | ------------ |
| `test_micro_compact_no_op_when_few_tool_msgs` | 1 | ≤ N 条 tool 消息时不动作 |
| `test_micro_compact_replaces_old_tool_results` | 1 | 旧的被压缩、近的保留 |
| `test_micro_compact_preserves_short_content` | 1 | 短于 `_MIN_COMPACT_LEN` 跳过 |
| `test_micro_compact_preserves_tool_call_id` | 1 | 协议字段保留 |
| `test_micro_compact_first_line_hint` | 1 | 首行作为锚点保留 |
| `test_micro_compact_token_budget_shrinks_keep_window_when_recent_oversized` | 1 | Token 感知 keep 窗口 |
| `test_micro_compact_preserves_at_least_one_recent_when_single_giant` | 1 | 下限被尊重 |
| `test_create_summary_passes_thinking_disabled_and_no_tools` | 2 | 跨 provider 调用形态 |
| `test_create_summary_propagates_exceptions` | 2 | 失败抛错（无膨胀） |
| `test_maybe_summarize_drops_exec_msgs_on_llm_failure` | 2 | Token 单调的失败路径 |
| `test_maybe_summarize_inserts_summary_marker` | 2 | marker 形态 |
| `test_maybe_summarize_collapses_orphan_summary_markers` | 2 | marker 不堆积 |
| `test_maybe_summarize_skip_check_short_circuits` | 2 | `skip_check` 被尊重 |
| `test_maybe_summarize_below_threshold_noop` | 2 | 未超预算时不动作 |
| `test_cleanup_keeps_complete_assistant_turn` | cleanup | 完整 turn 不被动 |
| `test_cleanup_removes_empty_assistant_turn` | cleanup | 空 turn 被删 |
| `test_cleanup_removes_partial_tool_call_turn` | cleanup | 部分 tool_calls 被删 |
| `test_cleanup_keeps_complete_tool_call_turn` | cleanup | 所有 tool 响应齐全 → 保留 |
| `test_cleanup_keeps_thinking_only_assistant` | cleanup | 仅 thinking 也算输出 |
| `test_cleanup_noop_when_no_assistant_turn` | cleanup | 空对话上 no-op |

## 待优化项

这些项目**当前没有**在实现中，列在这里是为了让后续工作者带着上下文接手。

1. **泛化 Layer 0，去 PPT 白名单耦合**。目前 `_MODEL_CONTEXT_PATH_EXTS` /
   `_MODEL_CONTEXT_PATH_NAMES` 偏向 PPT skill 生成的 HTML/JSON/MD。一个纯
   尺寸兜底（`len(value) > N` 不看路径）就能覆盖 Python / CSV / SQL 等
   生成型文件，不再需要按 skill 调参。
2. **assistant `thinking` 块的老化**。开启 `deep_think` 后，thinking 块
   持续累积（每个 8k token 上限）。Layer 1 只动 `role == "tool"`。一个
   专门针对老 thinking 块的压缩器能帮助长 deep-think 会话避免直接升级到
   Layer 2。
3. **增量 Layer 2**。每次触发都重新摘要每一轮，会重复支付 LLM 成本；
   被标记为"已摘要"的轮次可以在后续触发时短路。

## 文件索引

- `box_agent/core.py` —— 所有压缩代码都在这里：
  - 常量：`_KEEP_RECENT_TOOL_RESULTS`、`_KEEP_RECENT_TOOL_TOKEN_BUDGET`、
    `_MIN_COMPACT_LEN`、`_SUMMARY_MARKER`、`_MODEL_CONTEXT_PATH_EXTS`、
    `_MODEL_CONTEXT_PATH_NAMES`、`_MODEL_CONTEXT_PATH_PARTS`、
    `_MODEL_CONTEXT_CONTENT_THRESHOLD`
  - Layer 0：`_compact_visible_tool_content_for_model`、
    `_compact_tool_call_arguments_for_model`、
    `_summarize_tool_argument_for_model`、
    `_path_needs_compact_model_context`、
    `_tool_argument_needs_compaction`
  - Layer 1：`_micro_compact`、`_approx_tokens_for_content`
  - Layer 2：`_maybe_summarize`、`_create_summary`、`_is_summary_marker`
  - Cleanup：`_cleanup_incomplete_messages`
  - Token 估算：`_estimate_tokens`、`_estimate_tokens_fallback`
- `box_agent/config.py` —— `AgentConfig.context_token_limit` 属性
- `box_agent/agent.py` —— `Agent.__init__(token_limit=...)` 透传
- `tests/test_core.py` —— 所有压缩测试
