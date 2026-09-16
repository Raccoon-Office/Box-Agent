# Computer Use 接入与生命周期

本文说明 Box-Agent 如何通过 Cua Driver 提供原生桌面控制，以及独立 CLI 和 Electron 宿主两种运行方式的职责边界。

## 1. 组件与调用链

- `computer-use` Skill：告诉模型先准备能力，再观察、操作并验证结果。
- `ensure_cua_ready`：按需建立 Cua 运行环境；它不下载二进制，也不扩大权限。
- MCP client：Box-Agent 持有，通过 stdin/stdout 调用 `cua-driver mcp`。
- Cua runtime/daemon：真正执行无障碍读取、截图、鼠标和键盘操作。

```text
模型 → ensure_cua_ready → Box-Agent MCP client
     → cua-driver mcp（stdio）→ Cua runtime/daemon → 操作系统
```

MCP 代理只负责协议转换。操作能力、权限、会话状态、录屏和代理光标都属于 Cua runtime。

## 2. 模式一：独立 Box-Agent CLI

用户直接运行 `box-agent`、没有 Electron 宿主时，Box-Agent 按需准备 Cua：启动时只登记 `cua-computer-use`，第一次调用 `ensure_cua_ready` 才连接。并发调用由进程内锁合并。

平台实现有一处重要差异：

- **macOS**：Box-Agent 启动裸 `cua-driver mcp`。该代理通过 Cua 的标准机制启动或连接 `CuaDriver.app` 持有的 daemon，使 Accessibility 和 Screen Recording 权限继续归属于稳定的 App bundle identity。Box-Agent 不能把自己伪装成 embedding host，也不应直接启动 `serve --embedded`。
- **Windows/Linux**：Box-Agent 启动私有 `cua-driver serve --socket <endpoint>`，再启动 `cua-driver mcp --socket <endpoint>`。Box-Agent 退出或能力被移除时，停止自己创建的 daemon。

因此，“CLI 自己启动 daemon”是 CLI 自主管理 Cua 运行环境的产品语义；在 macOS 上，具体的 daemon 启动必须交给 CuaDriver.app，以保持正确的系统权限身份。

## 3. 模式二：Electron 自持 daemon

Electron 启动 Box-Agent/ACP 时设置：

```text
BOX_AGENT_CUA_RUNTIME_MODE=embedded
```

并向 Box-Agent 提供完整、可信的 MCP 定义：二进制绝对路径、`mcp --embedded --socket <electron-endpoint>` 参数、安全环境变量和私有 endpoint。

```text
Electron（权限和生命周期 owner）
  → 启动 cua-driver serve --embedded --socket <electron-endpoint>
  → 启动/托管 Box-Agent，并注入 MCP 配置

Box-Agent
  → ensure_cua_ready
  → stdio_client 启动 cua-driver mcp --embedded --socket <electron-endpoint>
  → 只连接 Electron daemon，不启动第二个 daemon
```

连接失败时返回 `host_unavailable`，不得静默降级到 CLI daemon。Electron 退出时由 Electron停止 daemon；Box-Agent 只清理自己的 MCP stdio 连接。

## 4. 为什么正式产品由 Electron 持有 daemon

1. **权限身份稳定**：macOS TCC 授权绑定签名应用身份。由 Electron 启动 embedded daemon，权限归属和系统设置中展示的应用保持一致。
2. **安全策略统一**：宿主决定 permission mode、capability manifest、用户/管理员 policy 和审批边界。Box-Agent 不覆盖显式 Cua 配置及其环境变量。
3. **生命周期统一**：应用启动、功能开关、退出、崩溃恢复和升级都由同一个 owner 管理，避免遗留拥有桌面权限的进程。
4. **私有 endpoint 不外泄**：socket/named pipe 和 generation 只在主进程与受信子进程间传递。
5. **体验一致**：代理光标、录屏和运行时状态由长期 daemon 持有，不会随一次 Agent 会话抖动。
6. **跨平台发行可控**：Electron 选择正确的平台/架构资源，并负责签名、校验和兼容版本。

CLI 没有长期桌面宿主，只适合开发、训练和独立使用；正式桌面产品仍采用 Electron 自持模式。

## 5. 二进制供应契约

不要把 `cua-driver`、`cua-driver.exe`、发布归档或解压后的 runtime 提交到 Box-Agent 仓库，也不要打进 Box-Agent 的 Python wheel 或独立 runtime 包。

独立 CLI 按以下顺序发现由运行环境提供的二进制：

1. `BOX_AGENT_CUA_DRIVER_PATH` 指定的绝对路径；
2. `BOX_AGENT_RUNTIME_ROOT` 相邻或内部的 `cua-driver` resource 目录；
3. 开发环境的系统 `PATH`。

正式宿主需要：

- 按操作系统和 CPU 架构提供唯一一份 Cua Driver resource；
- 显式把绝对路径传给 Box-Agent；
- 固定兼容版本并校验摘要、签名和来源；
- 自己决定随宿主安装，还是用户启用功能后再下载；
- 升级前停止旧 daemon，验证新资源后再原子切换。

Box-Agent 不负责联网下载、安装或更新 Cua Driver。找不到二进制时，Computer Use 保持未配置并返回明确错误。当前设计允许 Electron 后续把 Cua Driver 做成独立可下载 resource，而无需修改 Box-Agent 协议。

## 6. 安全与配置规则

- standalone 可用的显式 `cua-computer-use` 配置属于 operator 安全边界，Box-Agent 必须原样保留，不改写 command、args、policy、permission mode、capability manifest 或 telemetry 配置。Electron 写入的 `--embedded --socket` 配置只在 app-hosted 模式有效；独立 CLI 会忽略其中的私有 socket，重新生成 standalone 配置。
- `disabled: true` 始终优先，独立 CLI 不得因模式迁移重新启用用户已经关闭的 Computer Use。
- Box-Agent 自动生成 CLI 配置时，透传宿主进程中的全部 `CUA_DRIVER_*` 环境变量。
- readiness 只表示 MCP 和 runtime 已连接，不代表高影响操作已获得业务确认。
- 发送消息、提交、删除、购买和安装等动作继续遵守 Box-Agent 原有确认策略。
- daemon stdout 不得进入 CLI/ACP 协议流；诊断信息只能走 stderr 或日志。

## 7. 开发验证

```bash
BOX_AGENT_CUA_DRIVER_PATH=/absolute/path/to/cua-driver \
uv run python -m box_agent.cli
```

至少验证：首次原生任务可按需连接、重复 readiness 幂等、权限错误可识别、MCP 失败可恢复、能力禁用和进程退出可清理。发布前还需在 macOS、Windows 和 Linux 的真实交互桌面中验证权限、截图、输入、窗口聚焦和代理光标。
