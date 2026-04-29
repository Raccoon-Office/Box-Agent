# Box-Agent ACP 对接索引

本目录下与 ACP 宿主对接相关的协议文档总索引。所有协议都是 **Box-Agent 0.8.26+ 的 ACP 通道扩展**，CLI 通道不受影响。

> ACP 入口：`box-agent-acp` 走 stdio JSON-RPC。
> 公共扩展点：`session/new._meta`（一次性配置）、`session/prompt._meta`（每轮可变）、`update_tool_call.rawOutput`（结构化产物）。

---

## 协议清单

| 协议 | 方向 | 入口 | 文档 | 用途 |
|---|---|---|---|---|
| **Action Hint** | 后端 → 前端 | 模型 markdown 围栏块 | [ACTION_HINT_PROTOCOL.md](./ACTION_HINT_PROTOCOL.md) | 模型在合适场景输出 ```action_hint``` 块，前端解析为可点击设置入口 |
| **Env Context** | 前端 → 后端 | `session/new._meta.env_context` | [ENV_CONTEXT_PROTOCOL.md](./ENV_CONTEXT_PROTOCOL.md) | 宿主把 CLI 路径 / 平台 / 浏览器工具状态等已知事实喂给模型，避免它否认已可用的工具 |

> 已经存在但本次未变更的扩展点：`_meta.session_mode`（会话模式）、`_meta.deep_think`（深度思考开关）、`_meta.officev3_permissions_override`（已废弃）。

---

## 典型对接场景

### 场景 A：用户首次打开应用

1. 宿主侧检测：`MEMORY.md` 是否已存在、各个 CLI 是否已安装、Chromium 是否已通过 `box-agent install-browser` 装好。
2. 创建会话时，把这些事实通过 `_meta.env_context` 一次性喂给后端。
3. 用户问"你好/你是谁"时，后端发现 MEMORY.md 稀缺 → system prompt 注入 onboarding hint 规则 → 模型在回复末尾输出 `action_hint{tab:"onboarding"}` → 前端渲染为"点击完善个人记忆"链接，点击打开设置页 onboarding tab。

### 场景 B：用户问"帮我打开 example.com"

1. 后端发现 mcp.json 中 playwright 缺失或 `disabled=true` → system prompt 注入 browser-tools hint 规则。
2. 模型回复："我目前还没有可用的浏览器工具" + `action_hint{tab:"browser-tools"}`。
3. 前端剥离 hint 块，原位渲染为可点击链接，点击打开设置页 browser-tools tab。

### 场景 C：用户问"用飞书 CLI 给我发条消息"

1. 宿主在 `env_context.cli["lark-cli"]` 里传了 bundled 路径。
2. 后端 sanitize（绝对路径校验、长度限制等）后写入 system prompt 的"可用 CLI"清单。
3. 模型不再说"你机器上没装"，而是直接通过 bash 工具调用绝对路径。

---

## 安全约束（最低限度）

- **`_meta.env_context.extras`** 不会进入 system prompt（仅入后端日志），但仍然在 ACP 入站记录中可见。**不要传 token / API key / 用户隐私字段。**
- **CLI value 必须是绝对路径，不能含控制字符 / 反引号。** 后端会单条丢弃违规条目并 WARNING 日志。
- **action_hint 块由模型生成，前端必须做白名单校验**（`tab` 只接受协议定义的值），未知 tab 应当忽略不报错。

详细规则见各协议文档第 2 节。

---

## 实现位置（后端）

```
box_agent/acp/
├── __init__.py            # newSession / _build_session_prompt 主流程
├── action_hints.py        # MEMORY 稀缺检测 + playwright disabled 检测 + prompt 段
└── env_context.py         # 宿主环境注入 schema + sanitize + markdown 渲染

tests/
├── test_action_hints.py   # 18 个用例
└── test_env_context.py    # 23 个用例（含 11 条恶意输入回归）
```

---

## 版本与变更

| 版本 | 变更 |
|---|---|
| 0.8.26 | 首次引入 `action_hint` 协议、`_meta.env_context`、`enable_mcp` 防护；env_context 包含输入校验与 extras 不进 prompt |
| 0.8.25 | `context_window` / `max_output_tokens` 配置化（与 ACP 无关） |
| 0.8.24 | `max_tokens` 截断防护（与 ACP 无关） |

后续协议变更会在本表追加，并在对应协议文档第 1 节注明适用版本。
