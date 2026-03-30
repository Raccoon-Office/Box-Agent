# Memory System Integration Guide

Box-Agent v0.4.11+ 提供跨会话记忆系统，包含两部分：

| 类型 | 写入方 | 生命周期 | 存储位置 |
|------|--------|----------|----------|
| **长期记忆** (Manual Memory) | Agent 通过 `memory_write` tool | 永久 | `~/.box-agent/memory/MEMORY.md` |
| **会话摘要** (Session Summary) | 系统自动生成 | 按日期归档 | `~/.box-agent/memory/{date}/{session_id}.md` |

启动时系统自动召回长期记忆 + 最近 N 天的会话摘要，注入 system prompt。

---

## 1. 配置

在 `config.yaml` 中添加（均有默认值，不配也能用）：

```yaml
enable_memory: true                    # 开关，默认 true
memory_dir: "~/.box-agent/memory"      # 存储目录
memory_recall_days: 3                  # 召回最近几天的会话摘要
```

设置 `enable_memory: false` 可完全关闭记忆系统（tool 也不会注册）。

---

## 2. Tool 接口

### `memory_write` — 写入长期记忆

```json
{
  "name": "memory_write",
  "arguments": {
    "content": "- 用户偏好中文回答\n- 项目使用 React + TypeScript",
    "mode": "append"
  }
}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string | 是 | 要写入的内容，建议用 markdown bullet 格式 |
| `mode` | string | 否 | `append`（默认，追加）或 `overwrite`（覆盖全部） |

### `memory_read` — 读取长期记忆

```json
{
  "name": "memory_read",
  "arguments": {}
}
```

无参数，返回 `MEMORY.md` 全部内容。

---

## 3. CLI 接入

无需额外代码。`enable_memory: true` 时：

- **启动时**：自动召回记忆并注入 system prompt，终端显示 `✅ Loaded memory context`
- **会话中**：Agent 可调用 `memory_write` / `memory_read`
- **退出时**（`/exit` 或 Ctrl+C）：自动用 LLM 生成会话摘要并保存

如需手动编辑长期记忆：

```bash
# 直接编辑 MEMORY.md
vim ~/.box-agent/memory/MEMORY.md
```

---

## 4. ACP / Runtime 接入

memory tools 作为标准 tool 注册，ACP 客户端（如 officev3）通过正常的 tool_call 协议调用。

### 4.1 写入记忆

在 ACP prompt 中让 Agent 调用 tool，或客户端直接构造 tool_call：

```python
# officev3 构造 prompt，让 agent 记住信息
prompt_text = "请记住：用户偏好简洁的中文回答"
# Agent 会自行调用 memory_write tool
```

Agent 返回的 ACP sessionUpdate 中会包含 tool_call 事件：

```
tool/start: memory_write(content="- 用户偏好简洁的中文回答", mode="append")
tool/end:   [OK] Memory updated (append). Current memory: ...
```

### 4.2 自动召回

每次 `newSession` 时，系统自动：

1. 读取 `MEMORY.md`（长期记忆）
2. 扫描最近 `memory_recall_days` 天的会话摘要
3. 组装为 memory block 注入 system prompt

注入格式：

```
--- MEMORY START ---

[Manual Memory]
- 用户偏好中文回答
- 用户希望结果简洁

[Recent Session Memory]
- 2026-03-30 | session_id=sess-0-a1b2c3d4
  用户上传了 Excel 文件，分析男女身高体重分布，生成了图表。

--- MEMORY END ---
```

无记忆时不注入（空字符串）。

### 4.3 自动摘要

每次 `prompt()` 完成后，系统自动调用 LLM 生成会话摘要并保存到 `{date}/{session_id}.md`。这是系统行为，不需要客户端触发。

---

## 5. 存储结构

```
~/.box-agent/memory/
├── MEMORY.md                         # 长期记忆（memory_write 写入）
├── 2026-03-30/
│   ├── sess-0-a1b2c3d4.md           # ACP 会话摘要
│   └── cli-170530.md                # CLI 会话摘要
└── 2026-03-29/
    └── sess-1-b2c3d4e5.md
```

- `MEMORY.md`：纯文本，建议 bullet point 格式，Agent 通过 tool 读写
- 日期目录下的 `.md`：自动生成的会话摘要，包含 session_id、日期、任务、摘要、关键结果

---

## 6. Python API（直接调用）

```python
from box_agent.memory import MemoryManager

mgr = MemoryManager(memory_dir="~/.box-agent/memory", recall_days=3)

# 写入长期记忆
mgr.write_manual_memory("- 用户偏好中文\n- 项目使用 React")

# 读取长期记忆
print(mgr.read_manual_memory())

# 保存会话摘要
mgr.save_session_summary("sess-0-abc", "# Session...\n## Summary\n...")

# 召回全部记忆（用于注入 system prompt）
block = mgr.recall()

# 用 LLM 自动生成摘要
await mgr.generate_session_summary(llm=llm_client, messages=messages, session_id="sess-0-abc")
```
