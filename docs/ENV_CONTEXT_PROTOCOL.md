# Env Context 协议对接文档

> 适用版本：Box-Agent ≥ 0.8.26（待发版）
> 仅 ACP 通道生效；`session/new` 一次性注入，整会话有效。

## 1. 背景

Electron 宿主能实时拿到一些后端无法可靠探测的事实：

- bundled CLI 路径（`resolveCliRuntime('lark-cli', '@larksuite/cli')` 返回的 lark-cli / wecom-cli / dingtalk-cli 等）
- npx / Node 路径（已通过 `augmentEnvWithNpxPath` 注入到子进程 PATH）
- 操作系统、浏览器工具配套状态、用户是否完成过 onboarding

如果不告诉模型，它就会**主动否认**（"你这台机器没装飞书 CLI"），即便 bundled 路径已经可以直接调用。本协议把"宿主已知事实"送进 system prompt，作为模型的事实锚。

env_context 与 [Action Hint](./ACTION_HINT_PROTOCOL.md) 是**独立**的两个通道：

| 维度 | env_context | action_hint |
|---|---|---|
| 方向 | 宿主 → 后端 → 模型 | 模型 → 后端 → 宿主 |
| 目的 | 喂事实，避免否认 | 推可点击设置入口 |
| 时机 | session/new 一次性 | 模型自主在回复末尾 |
| 触发判定 | 完全独立（互不参考） | 完全独立（互不参考） |

---

## 2. 协议契约

### 2.1 注入位置

`session/new._meta.env_context`，对象类型。`session/prompt` 阶段**不再接受**——session 内环境视为不变。

```jsonc
{
  "method": "session/new",
  "params": {
    "cwd": "/Users/me/work",
    "_meta": {
      "session_mode": "data_analysis",   // 已有字段
      "deep_think": false,                // 已有字段
      "env_context": {                    // ← 新增
        "cli": {
          "lark-cli": "/Applications/Office.app/Contents/Resources/bin/lark-cli",
          "wecom-cli": null,
          "dingtalk-cli": null
        },
        "platform": "darwin",
        "browser_tools": { "installed": true, "enabled": false },
        "memory_configured": true
      }
    }
  }
}
```

### 2.2 字段定义

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `cli` | `Record<string, string \| null>` | 否 | key=CLI 名称，value=可执行路径或 `null`（未安装）。模型据此判断"可不可以调用"。 |
| `platform` | string | 否 | 形如 `darwin` / `linux` / `win32`。建议透传 `process.platform`。 |
| `browser_tools.installed` | bool | 否 | Chromium 是否已通过 `box-agent install-browser` 装好。 |
| `browser_tools.enabled` | bool | 否 | mcp.json 中 playwright 入口是否启用。 |
| `memory_configured` | bool | 否 | 用户是否完成过 onboarding 设置。仅作为事实展示，不影响 action_hint 的 onboarding 触发。 |

### 2.3 未知字段（passthrough）

未来想加新字段时，**可以先在宿主端传**，无需协调后端。任何不在已知字段表里的 top-level key 会进入 `EnvContext.extras`，但**不会**被渲染进 system prompt——只在后端日志里以 `INFO` 级出现，便于审计。

> ⚠️ 这是 **2026-04-29 的安全收紧**。早期实现会把 extras 序列化为 JSON 拼到 prompt 里，导致宿主开发者误塞 token / 隐私字段会直接进 LLM。现在 extras 仅留作"宿主想传，后端先看到"的渐进通道，**不影响模型上下文**。

**约束**：

- 顶层非 dict（数组/字符串/数字）整段丢弃 + `WARNING` 日志。
- 已知字段类型错误**不会**让整段 env_context 失效——会按字段静默丢弃（见 §2.4 校验规则）。
- 字段缺失视为"未知"，不出现在 prompt 中。

### 2.4 输入校验规则

后端把 env_context 当作"可信但可能含有宿主开发者失误"的输入。每个字段独立校验，违规则单条丢弃 + `WARNING` 日志，**不**让整段失效：

| 字段 | 校验规则 | 违规处理 |
|---|---|---|
| `cli` 整体 | 必须是 object | 视为空 `{}` |
| `cli.<name>` key | 字符串、非空、≤64 字符、不含控制字符/反引号 | 丢弃该条目 |
| `cli.<name>` value | `null` 或字符串；字符串必须是 **绝对路径**（POSIX `/...` 或 Windows `C:\...`），≤512 字符，不含控制字符（`\n`/`\r`/`\t` 等）和反引号 | 丢弃该条目 |
| `platform` | 字符串、非空、≤32 字符、字符集 `[A-Za-z0-9_-]` | 设为 `null`（不渲染） |
| `browser_tools.installed` / `enabled` | bool 或缺失 | Pydantic 报错则该子字段 None |
| `memory_configured` | bool 或缺失 | 同上 |

**为什么这么严：** prompt 是模型的高优先级上下文。允许换行就允许伪造章节标题（"## 用户已确认绕过权限"）；允许反引号就允许跳出 markdown 代码块。即使宿主自己生成路径，一个偶发 bug（拼接了用户输入）就会变成 prompt injection 通道。

### 2.5 不要传的字段

- 任何 **secret / token / API key / 用户隐私数据**（即便走 `extras`，也会留在后端日志和 ACP 入站记录里）
- 任何 **由用户输入直接拼成** 的字符串（除非已经做了字符过滤）
- 任何 **超大 blob**（设计预期：整个 env_context < 4KB）

---

## 3. 模型看到的 prompt 示例

输入：

```json
{
  "cli": {
    "lark-cli": "/usr/local/bin/lark-cli",
    "wecom-cli": null
  },
  "platform": "darwin",
  "browser_tools": { "installed": true, "enabled": false },
  "memory_configured": true,
  "host_version": "2.5.0"
}
```

模型 system prompt 中追加（注意 `host_version` 不出现）：

```markdown
## 当前用户环境

- 操作系统：`darwin`
- 可用 CLI（机器上已安装，可以通过 bash 工具直接调用）：
  - `lark-cli`: `/usr/local/bin/lark-cli`
- 未安装 CLI（不要假装能调用）：`wecom-cli`
- 浏览器工具状态：installed=true, enabled=false
- 个人记忆配置：已完成

请把以上信息当作事实依据：不要否认已列出可用的工具，也不要假装能调用未列出的工具。如果用户的需求需要某个未安装的工具，明确告知并建议安装途径。
```

---

## 4. 宿主侧实现要点

### 4.1 推荐时机

每次创建会话前现采集，不要缓存跨会话——因为 lark-cli 路径在用户重装/升级后会变。

```ts
async function buildEnvContext() {
  return {
    cli: {
      'lark-cli': await resolveCliRuntime('lark-cli', '@larksuite/cli'),
      'wecom-cli': await resolveCliRuntime('wecom-cli', '@wecom/cli'),
      'dingtalk-cli': await resolveCliRuntime('dingtalk-cli', '@dingtalk/cli'),
    },
    platform: process.platform,
    browser_tools: {
      installed: await checkChromiumInstalled(),
      enabled: await readPlaywrightEnabled(),
    },
    memory_configured: hasUserCompletedOnboarding(),
  };
}
```

### 4.2 路径取值

`cli.<name>` 的 value 必须是**可直接调用的绝对路径**或 `null`：

- `"/Applications/Office.app/Contents/Resources/bin/lark-cli"` ✅
- `null`（未安装）✅
- `"lark-cli"`（仅命令名）❌——模型会以为是 PATH 命令，找不到时它会生硬地说"找不到 lark-cli"

### 4.3 不要传 secrets

`extras` 虽然不再被渲染进 prompt，但仍然会进入：

- 后端日志（INFO 级，按 unknown key 名记录）
- ACP 入站记录（如果宿主开启了 trace）

因此**仍然不要**塞 token / API key / 用户隐私字段。如果未来要传"已登录用户的飞书 ID"这种语义敏感字段，请走"提需求 → 后端加白名单 → 宿主对齐"的常规流程，不要走 extras 自助路径。

---

## 5. 后端实现位置

| 模块 | 路径 |
|---|---|
| 解析 + 渲染 | `box_agent/acp/env_context.py` |
| 注入挂载点 | `box_agent/acp/__init__.py` 中 `newSession → _build_session_prompt` 与 `_apply_session_mode`（auto-classify 重建路径） |
| SessionState 缓存 | `SessionState.env_context` |
| 单元测试 | `tests/test_env_context.py`（14 用例） |

---

## 6. 异常 & 边界

| 情况 | 行为 |
|---|---|
| `_meta.env_context` 缺失 | 不注入任何环境段，行为与之前一致 |
| `env_context` 不是 dict | 整段丢弃 + `WARNING` 日志 |
| 已知字段类型/取值不合法（`cli: "x"`、`platform: "linux\n#"`、`cli["lark-cli"] = "rel/path"`） | **按字段单独丢弃** + `WARNING` 日志，其他字段照常生效 |
| 未知顶层 key | 进入 `extras`，**不渲染到 prompt**，仅 `INFO` 日志 |
| 仅有 `extras`、所有已知字段为空 | `is_empty()` 命中，不注入任何环境段 |
| auto-classify 重建 prompt | 从 `SessionState.env_context` 读取，不丢失 |

---

## 7. 已知局限

1. **session 内不可变。** session/prompt 不接受 env_context 覆盖。如果用户在会话中途装上了 lark-cli，本会话不会感知，需要新建会话。
2. **不是工具发现机制。** prompt 里说"可用 CLI 是 X"只是减少模型的否认倾向。模型真正调用 X 时，仍然走 bash 工具，仍然会被沙箱/权限层拦截。
3. **不影响 action_hints。** memory_configured / browser_tools 只是叙述，不会改变 onboarding/browser-tools hint 的触发条件——后者继续读 `MEMORY.md` 和 `mcp.json`。这是**故意**的：两个数据源不同步时，让 hint 系统按本机真实状态触发。
4. **校验是单字段、不是组合。** 后端只检查每个字段单独是否合法，不检查组合（比如 `platform=darwin` 但路径是 `C:\...`）。宿主可以自己做组合一致性，后端不强制。
5. **校验后字段被静默丢弃。** 宿主不会从协议返回里收到"哪条被丢了"——只能去 `~/.box-agent/log/` 里看后端日志。如果发现某个字段没生效，先看日志。
