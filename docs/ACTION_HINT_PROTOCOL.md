# Action Hint 协议对接文档

> 适用版本：Box-Agent ≥ 0.8.26（当前未发版，待确认版本号）
> 仅 ACP 通道生效；CLI 不会输出此协议。

## 1. 背景与目标

前端的设置弹窗存在多个 tab（如"个人记忆 / onboarding"、"浏览器工具 / browser-tools"）。在某些会话场景下，**模型需要主动引导用户去打开对应设置页**：

| 场景 | 触发条件 | 引导目标 |
|---|---|---|
| 用户问候 / 自我介绍 / "你是谁" | `MEMORY.md` 内容稀缺（< 30 字符或没有姓名标识） | 打开 `onboarding` tab |
| 用户提出浏览器/抓取/Playwright 需求 | `mcp.json` 里没有 playwright，或被 `disabled`，或全局 `enable_mcp=false` | 打开 `browser-tools` tab |

为此约定了一种由模型直接产出、前端解析的 **结构化 markdown 围栏块**，无需扩展 ACP 协议字段。

---

## 2. 协议契约

### 2.1 围栏块格式

模型在普通回复正文**末尾**追加一个语言标记为 `action_hint` 的 markdown 围栏代码块，内容为合法 JSON：

````
正常回答用户的内容...

```action_hint
{
  "action": "open_settings",
  "params": {"tab": "onboarding"},
  "display_text": "点击完善个人记忆，让我更懂你"
}
```
````

### 2.2 字段定义

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `action` | string | ✅ | 当前固定为 `"open_settings"`。未来可能扩展。前端遇到未知值应当忽略此 hint，不要报错。 |
| `params.tab` | string | ✅ | 设置页 tab 名。当前合法值见 §2.3。 |
| `display_text` | string | ✅ | 模型生成的中文/英文引导文案，用于在 UI 中作为可点击链接的文本。 |

### 2.3 当前已定义的 tab 值

| `tab` | 含义 | 后端注入条件 |
|---|---|---|
| `onboarding` | 个人记忆配置页 | `MEMORY.md` 缺失/为空，或 `strip()` 后 < 30 字符，或全文（lower 后）不包含 `name` / `姓名` / `我叫` / `我是` / `叫我` |
| `browser-tools` | 浏览器工具配置页 | `tools.enable_mcp = false`，或 `mcp.json` 缺失/JSON 损坏，或不包含 playwright 入口，或 playwright 入口 `disabled = true` |

> **重要：** 后端只在条件命中时把对应规则写进 system prompt。条件不命中时，模型连"还有这个 tab 可以用"都不知道，因此**不会**误输出。

### 2.4 输出位置约定

后端在 system prompt 里要求模型遵守以下规则：

- 一次回复**至多 1 个** `action_hint` 围栏块；
- 必须放在正文之后（不要嵌在段落中间）；
- 仅在用户当前问题真正契合场景时输出，避免无脑推销。

模型可能违反约定（出现频率低，但应做防御性解析）：见 §4。

---

## 3. 前端解析建议

### 3.1 流式增量

ACP 通道的内容以 streaming delta 形式抵达（`update_agent_message` / `update_agent_thought`）。建议策略：

1. **累积全量文本**，在每次 delta 后做 idempotent 解析；
2. 解析到 `action_hint` 块后，**从渲染文本中剥离**该块，避免把围栏源码暴露给用户；
3. 在剥离位置原地（或回复尾部）渲染为可点击链接，文案使用 `display_text`，点击触发 `open_settings({ tab })`。

### 3.2 推荐正则

```js
// JS / TS
const ACTION_HINT_RE = /```action_hint\s*\n([\s\S]*?)\n```/g;

function extractActionHints(text) {
  const hints = [];
  const cleaned = text.replace(ACTION_HINT_RE, (_, jsonStr) => {
    try {
      const obj = JSON.parse(jsonStr);
      if (obj.action === 'open_settings' && obj.params?.tab && obj.display_text) {
        hints.push(obj);
      }
    } catch {
      // 解析失败：保留原文（或丢弃，按产品要求决定）
    }
    return ''; // 从展示文本中剥离
  });
  return { cleaned, hints };
}
```

### 3.3 流式截断的处理

模型还在写一半时，文本可能停在 `` ```action_hint\n{ `` 这种状态。建议：

- 只对 **完整闭合** 的围栏块（``` `\n``` `` 已经出现）做解析；
- 半截块**不要**先渲染源码再回收——容易闪烁。最稳妥是检测到开头 ``` ```action_hint `` 就先把这一段**遮住**，等闭合后再处理。

### 3.4 白名单校验

`tab` 字段必须做白名单校验（前端持有当前已知的合法值集合）。模型如果返回未知 tab：

- 既不渲染链接，也不报错；
- 可选：上报埋点便于发现 prompt 漂移。

---

## 4. 异常 & 边界处理

| 情况 | 后端是否会发生 | 前端应对 |
|---|---|---|
| 模型输出非法 JSON | 偶发（低频） | `try/catch` 解析；失败则按普通文本展示或丢弃，不要 crash |
| 模型同一回复输出多个 hint | 偶发 | 推荐只取**第一个**有效 hint，其余忽略 |
| `tab` 不在白名单 | 偶发（prompt 注入或模型幻觉） | 忽略，不渲染 |
| `display_text` 缺失 | 偶发 | 用前端自带的兜底文案，或忽略 |
| 模型不输出任何 hint | 大多数情况 | 正常，不要主动猜测插入 |
| `action_hint` 块被截断在流中间 | 流式必现 | 见 §3.3 |

---

## 5. 后端控制开关与配置

| 开关 | 位置 | 默认 | 说明 |
|---|---|---|---|
| `tools.enable_mcp` | `~/.box-agent/config/config.yaml` | `true` | 关闭后强制视 playwright 不可用 |
| `tools.mcp_config_path` | `~/.box-agent/config/config.yaml` | `mcp.json` | 后端检测 playwright 时读的 MCP 配置文件 |
| MEMORY 文件 | `~/.box-agent/memory/MEMORY.md` | — | 内容判定见 §2.3 |

后端检测 & 注入的源码定位：

- 纯函数检测层：`box_agent/acp/action_hints.py`
- 注入挂载点：`box_agent/acp/__init__.py:_build_session_prompt → _build_action_hints_prompt`
- 单元测试：`tests/test_action_hints.py`（18 用例）

---

## 6. 端到端示例

### 6.1 触发 onboarding 的对话

**用户：** 你好，你是谁？

**后端 prompt 注入（仅当 MEMORY 稀缺时存在）：**
> 用户问候、自我介绍、问"你是谁/你能做什么"等关系建立类话题，且当前对用户了解很少时 → 使用 `"tab": "onboarding"` …

**模型回复（流式）：**

> 你好！我是 Box-Agent，一个可以帮你写代码、做数据分析、生成 PPT 的 AI 助手。
>
> ```action_hint
> {"action":"open_settings","params":{"tab":"onboarding"},"display_text":"点击完善个人记忆，让我更懂你"}
> ```

**前端渲染：**

> 你好！我是 Box-Agent，一个可以帮你写代码、做数据分析、生成 PPT 的 AI 助手。
>
> 🔗 *点击完善个人记忆，让我更懂你* （链接 → 打开 onboarding 设置页）

### 6.2 触发 browser-tools 的对话

**用户：** 帮我打开 example.com 看看页面内容

**模型回复：**

> 我目前还没有可用的浏览器工具，无法直接访问网页。你可以启用浏览器工具后再试。
>
> ```action_hint
> {"action":"open_settings","params":{"tab":"browser-tools"},"display_text":"点击启用浏览器工具"}
> ```

---

## 7. 已知局限

1. **依赖模型遵守 prompt：** 检测条件命中只是给模型"许可"，模型自行决定是否输出。极小概率会漏出。后续可考虑事后正则补齐，目前不做。
2. **MCP 运行时加载失败不会触发 hint：** 当前只检测 `mcp.json` 静态配置和 `enable_mcp` 开关，不感知运行时 MCP 启动失败。如果 playwright 进程崩了但配置写着 enabled，hint 不会触发。修复成本与收益评估后**故意未做**——前端可以在 ACP 工具调用失败时另行引导。
3. **CLI 不支持：** CLI 没有设置弹窗，不注入此 prompt 段。
4. **白名单仅含 2 个 tab：** 新增 tab 需要后端 `action_hints.py` 加规则 + 前端白名单同步更新。
