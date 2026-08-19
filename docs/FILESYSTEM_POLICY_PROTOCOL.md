# Filesystem Policy 协议对接文档

> 适用版本：Box-Agent ≥ 0.8.27（0.8.29 起 `~/.box-agent/` 默认全 scope 放行）
> 仅 ACP 通道生效；`session/new` 一次性注入，整会话有效。

## 1. 背景

Box-Agent 的权限引擎是**能力（capability）模型**：每个文件读/写都会被 `PermissionEngine.check()` 拦下，比对当前 session 的 `filesystem_scope` 与允许目录集合，落在外面就拒绝并触发 `permission/request` 协商。

`filesystem_scope` 默认值是 `session_workspace`，匹配的根目录由两个字段共同决定：

- `session_workspace_root` — session 的"主工作区"
- `allowed_directories` — 额外白名单（spec 把 `session_workspace` 与 `custom` 视为同义）

这两个字段以前**只能从配置文件取**（`config.yaml` 的 `officev3.paths.session_workspace_root` / `officev3.permissions.filesystem.allowed_directories`），宿主无法按 session 注入。

宿主（如 office-raccoon）通常知道：

- 用户当前打开的项目目录
- 应用本身存放素材的目录（如 `~/Library/Application Support/office-raccoon/...`）
- 用户的常用资源目录（Documents / Downloads / Desktop 等）

如果这些不能动态注入，agent 就会反复触发 `permission/request` 协商，污染体验。本协议补齐"宿主声明 session 工作范围"的能力。

> 与已经 deprecated 的 `_meta.officev3_permissions_override` 的区别：那个是用来**运行时升级权限范围**（user_home 之类），现在改由 in-band `permission/request` 协商。
> 而 `filesystem_policy` 是**声明 session 起手在哪儿**，是 *context*，不是 escalation。两者正交、可以共存。

---

## 2. 协议契约

### 2.1 注入位置

`session/new._meta.filesystem_policy`，对象类型。`session/prompt` 阶段**不再接受**——session 内工作区视为不变，需要变更请重开 session。

```jsonc
{
  "method": "session/new",
  "params": {
    "cwd": "/Users/me/work/my-project",
    "_meta": {
      "session_mode": "data_analysis",
      "permission_mode": "default",
      "filesystem_policy": {
        "session_workspace_root": "/Users/me/work/my-project",
        "allowed_directories": [
          "/Users/me/Documents/raccoon-assets",
          "/Users/me/Library/Application Support/office-raccoon"
        ],
        "filesystem_scope": "session_workspace"
      }
    }
  }
}
```

`session/new._meta.permission_mode` 定义会话级权限档位：

| 值 | 文件系统权限 | 危险命令审批 |
|---|---|---|
| `default` | 按 `filesystem_policy` 拦截越权访问 | 保留 |
| `unrestricted_filesystem` | 不限制目录访问 | 保留 |
| `full_access` | 不限制目录访问 | 跳过应用层审批 |

未知值或非字符串值按 `default` 处理并记录警告，避免意外放宽权限。

### 2.2 字段定义

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `session_workspace_root` | string | 否 | session 的主工作区绝对路径。覆盖 `config.yaml` 中的同名字段。空字符串等价于不传。 |
| `allowed_directories` | string[] | 否 | 额外白名单。显式 `permission_mode: "default"` 时替换全局目录；旧宿主未传 `permission_mode` 时仍与全局目录合并去重。 |
| `filesystem_scope` | string | 否 | 覆盖默认 scope，取值 `session_workspace` / `user_home` / `custom`。默认 `session_workspace`。 |

### 2.3 合并语义

| 字段 | 来源 | 合并方式 |
|---|---|---|
| `session_workspace_root` | config.yaml `officev3.paths.session_workspace_root` | 宿主提供则**覆盖**；否则继承配置 |
| `allowed_directories` | config.yaml `officev3.permissions.filesystem.allowed_directories` | 显式 `default` 时以宿主列表**替换**；未传 `permission_mode` 时追加合并去重 |
| `filesystem_scope` | config.yaml `officev3.permissions.filesystem.scope` | 宿主提供则**覆盖**；否则继承配置 |

### 2.4 输入校验

后端把 `filesystem_policy` 当作"可信但可能含有宿主开发者失误"的输入：

- 顶层非 dict 整段丢弃 + 不报错（保持向前兼容）。
- `session_workspace_root` 必须是非空字符串，否则忽略该字段。
- `allowed_directories` 必须是字符串数组；非字符串元素或空字符串单条丢弃，整体不丢弃。
- `filesystem_scope` 必须是字符串；不在已知 scope 列表中的值会被引擎在请求时按 "未知 scope，fail closed" 处理（与 config.yaml 同等待遇）。
- 路径不会 `expanduser()` / `resolve()`，由 `PermissionEngine` 在 check 时延迟解析（与 config.yaml 行为一致）。

### 2.5 不需要传的字段

请**不要**通过 `_meta.filesystem_policy` 传：

- 临时升级请求（用 `permission/request` 协商）
- 单文件白名单（同上，针对单个写入路径让用户在 UI 上点"允许"）
- 用户家目录上层（`/Users`、`/`）—— 后端会接受，但与 `filesystem_scope: "user_home"` 等价的展开权应该走显式 scope 而不是塞一个超大 allowed_dir

---

## 3. 后端处理流程

```
session/new
    ↓
解析 _meta.permission_mode 与 _meta.filesystem_policy（类型校验）
    ↓
存在全局 officev3 配置：base_policy = CapabilityPolicy.from_config(config)
否则：base_policy = 当前 workspace 的限制性策略
    ↓
base_policy.with_filesystem_overrides(
    session_workspace_root=..., allowed_directories=..., filesystem_scope=...,
    replace_allowed_directories=(permission_mode == "default")
)
    ↓
PermissionEngine(effective_policy, workspace, grant_store)
    ↓
日志:
  session/permissions session_id=... filesystem_policy applied: ...
  session/permissions session_id=... PermissionEngine created: scope=..., swr=..., allowed_dirs=[...]
```

`with_filesystem_overrides` 是**不可变变换**——返回新 `CapabilityPolicy` 实例，原对象不变。显式 `default` 会以本次 session 的目录替换持久化目录，确保会话权限不继承无关业务目录；旧宿主未传 `permission_mode` 时保留原有的合并去重行为。

---

## 4. 与权限引擎的交互

注入后的最终生效逻辑（来自 `permissions.py`）：

```python
def _path_allowed_by_scope(self, resolved: Path, scope: str) -> bool:
    if self._is_inside(resolved, self._workspace_dir):
        return True
    if scope == "user_home":
        return self._is_under_home(resolved) or any(
            self._is_inside(resolved, allowed) for allowed in self._allowed_dirs
        )
    if scope in ("session_workspace", "custom"):
        if self._is_inside(resolved, self._session_workspace_root):
            return True
        return any(self._is_inside(resolved, allowed) for allowed in self._allowed_dirs)
```

`workspace_dir`（ACP `cwd`）始终允许。`session_workspace_root` 与 `allowed_directories` 是叠加白名单。落在外面会触发 `permission_request` 协商（escalation 到 `user_home` 或 `custom`），而不是直接拒绝。

**0.8.29 起：`~/.box-agent/` 在所有 scope 下都被引擎硬编码放行，不需要进入 `allowed_directories`。**这是 Box-Agent 自有的运行数据目录（skills 内嵌资源、runtime-packages、browsers cache、log、trash 等），属于框架内部，不是用户业务数据。宿主无需为这个目录单独申请权限或在 `_meta.filesystem_policy` 里追加它。

---

## 5. 路径提取的副作用修复（0.8.27）

同版本附带一个 bash 工具的相关修复：`extract_absolute_paths` 现在会丢弃**裸系统根 token**（`/`、`/etc`、`/usr`、`/opt`、`/var`、`/bin`、`/sbin`、`/lib`、`/proc`、`/sys`、`/Library`、`/System`、`/Applications`、`/tmp` 等），避免 `cd /; ls` 这类命令里的 `/;` 被 rstrip 后变成裸 `/`，错误触发"write to / is outside all allowed scopes"。

带子路径的写入（`/etc/hosts`、`/usr/local/bin/foo`）依然会被检查，没有放松实际安全语义。

权限拒绝时的诊断日志也增强了：

- `permissions.py` 现在打印 `path / resolved / scope / op / escalation`
- `bash_tool.py` 现在打印 `bash/perm/denied path=... cap=... extracted=[...] cmd=...`
- 错误消息附带 `Extracted paths from command: [...]`，方便 LLM 与用户定位是哪一段被错抽

---

## 6. 端到端示例

### 6.1 office-raccoon 启动新 session

```ts
const filesystemPolicy = {
  session_workspace_root: projectRoot,           // 用户当前打开的项目
  allowed_directories: [
    path.join(os.homedir(), 'Library/Application Support/office-raccoon/agent-cache'),
    path.join(os.homedir(), 'Documents/raccoon-assets'),
  ],
}

await client.sessionNew({
  cwd: projectRoot,
  _meta: {
    session_mode: 'data_analysis',
    deep_think: false,
    env_context: { /* ... */ },
    filesystem_policy: filesystemPolicy,
  },
})
```

### 6.2 后端日志（stderr）

```
INFO session/permissions session_id=sess-1 filesystem_policy applied: session_workspace_root='/Users/me/work/proj', extra_dirs=('/Users/me/Library/Application Support/office-raccoon/agent-cache', '/Users/me/Documents/raccoon-assets'), scope=None
INFO session/permissions session_id=sess-1 PermissionEngine created: scope=session_workspace, openclaw=True, swr='/Users/me/work/proj', allowed_dirs=['/Users/me/Library/Application Support/office-raccoon/agent-cache', '/Users/me/Documents/raccoon-assets']
```

### 6.3 模型写文件命中 allowed_directory

```
Tool: write_file path=/Users/me/Documents/raccoon-assets/output.csv
PermissionEngine.check FILESYSTEM_WRITE → allowed=True (matched allowed_directories)
→ 直接执行，无需 permission/request 协商
```

### 6.4 模型写到外面 → 协商

```
Tool: write_file path=/Users/me/Desktop/foo.csv
PermissionEngine.check FILESYSTEM_WRITE → allowed=False, escalation=user_home
→ session/request_permission 反向 RPC，宿主 UI 弹窗
→ 用户允许 → 重试 → 通过
```

---

## 7. 测试

`tests/test_permissions.py`：

- `TestFilesystemOverrides`：3 个用例，覆盖 `session_workspace_root` 覆盖、`allowed_directories` 合并去重、空参数返回 self。
- `TestExtractAbsolutePaths::test_bare_root_dropped` / `test_bare_system_root_dropped` / `test_subpath_under_system_root_kept`：3 个用例，回归裸系统根丢弃。

---

## 8. 兼容性

| 场景 | 行为 |
|---|---|
| 宿主既不传 `permission_mode`，也不传 `filesystem_policy` | 有全局 `officev3` 配置时沿用配置；无全局配置时沿用旧版 `tools.allow_full_access` 行为 |
| 无全局 `officev3` 配置，但宿主传 `filesystem_policy` | 从当前 workspace 的限制性策略起步，并应用本次 session 的目录与 scope |
| 显式 `permission_mode: "default"`，但不传 `filesystem_policy` | 仅允许当前 workspace，不继承全局业务目录 |
| 显式 `permission_mode: "default"` 并传 `allowed_directories` | 以本次 session 的目录替换全局目录 |
| 旧宿主不传 `permission_mode`，只传 `allowed_directories` | 与全局目录追加合并并去重，保持兼容 |
| 宿主同时还传 deprecated `officev3_permissions_override` | 旧字段会被 WARNING 但忽略；新字段照常生效 |

无破坏性变更。
