# CUA 插件配置与启动边界

完整的安装、MCP 配置、模型能力识别和排错步骤见
[CUA 插件使用指南](CUA_PLUGIN_GUIDE_CN.md)。本文保留插件边界和实现约束，供开发者阅读。

CUA 是可选插件。Box-Agent 核心只读取并保留通用的 `plugins` 命名空间，具体字段由 CUA 插件自己校验；核心 `tools` 配置不包含 `cua` 字段。

当前托管安装器固定使用 CuaDriver **0.28.2**。CLI 通过 `box-agent install-cua` 安装驱动，由 `ensure_cua_ready` 按需启动自持 daemon 并连接 MCP；桌面客户端负责安装、权限和 embedded daemon，Box-Agent 以 external 模式连接宿主提供的私有 endpoint。驱动不随 Python 包携带，两端共享 `~/.box-agent/resources/cua-driver/` 下的已验证资源，但不共享权限和 daemon 所有权。

历史 0.32.0 独立 App / 手工 MCP 配置及验收记录不代表当前托管模式。不要直接复用旧的 `args: ["mcp"]` 配置；迁移方法见下文。

## 配置示例

```yaml
api_key: "sk-..."
api_base: "https://api.openai.com/v1"
model: "gpt-4o"
provider: "openai"
image_input: true

plugins:
  cua:
    server_name: "computer-use"
    feed_screenshots: true
    persist_images: true
    runtime:
      enabled: true
      mode: standalone
```

`Config.from_yaml()` 会把每个 `plugins.<name>` 保留为一个映射，不解释未知插件的字段。CUA 插件激活时再校验自己的命名空间：`server_name` 默认是 `computer-use`，`feed_screenshots` 和 `persist_images` 默认是 `true`，未知字段会被拒绝。`persist_images` 控制会话图片 sidecar，与模型是否支持图片及视觉输入开关独立。

示例使用默认服务名，无需手写 CUA 的 `mcp.json`。`image_input` 是顶层模型能力声明，只对确认支持图片的模型开启。

启用 `tools.enable_mcp` 时，即使没有 `plugins.cua` 命名空间，也会注册默认 CUA 管理模块及就绪工具；驱动未安装时返回安装提示，不自动下载或授权。CLI 默认 standalone，设置 `runtime.enabled: false` 可关闭。宿主通过可信环境变量 `BOX_AGENT_CUA_RUNTIME_CONFIG` 提供运行配置；用户显式禁用优先于宿主启用。省略 `runtime` 或填写 `null` 均接受宿主配置；没有宿主时采用 CLI 默认值。不要在模型参数或普通配置中复制桌面宿主的临时 endpoint。

用户显式的 `runtime.enabled` 先按配置模型的布尔规则解析，再与宿主配置合并。同进程会话按服务名和 runtime 配置复用 CUA 服务，`feed_screenshots` 与 `persist_images` 仍由各会话独立决定。

## 启动链路

启动时由显式的插件装配目录注册内置可选插件：

```text
Config.from_yaml()
  -> Config.plugins（核心只做通用 YAML 透传）
  -> initialize_base_tools（启用 MCP 时注册 CUA service / ensure_cua_ready）
  -> create_cua_service（叠加可信宿主配置，写回规范化 plugins.cua）
  -> plugins/catalog.py（显式登记 CUA）
  -> PluginRuntime（默认包含 bundled plugins）
  -> PluginSession.open_run()
  -> CUA 插件读取 plugins.cua 并校验
```

需要完全排除可选内置插件的宿主可以构造 `PluginRuntime(include_bundled_plugins=False)`；这不会影响核心内置插件。

模型先读取 computer-use Skill，再调用 `ensure_cua_ready` 准备 daemon 和连接 MCP，之后使用 `tool_search` 发现动作工具。MCP 连接仍由既有 loader 负责，插件不创建第二条连接。external 模式只检查并连接宿主，不启动备用 daemon；关闭时也不停止宿主实例。

### 从手工 MCP 配置迁移

1. 结束旧桌面任务，退出 CLI / ACP 宿主；由原 owner 停止旧 daemon。
2. 移除与 `plugins.cua.server_name` 同名的旧手工 MCP entry，改用托管配置；若要从 `cua-computer-use` 改回默认名，同时清除旧服务，避免重复连接。保留其他 MCP 服务。
3. CLI 运行 `box-agent install-cua` 并按提示授权；桌面端使用客户端安装、授权及开关流程，再重启消费它的宿主。

同名 entry 与托管定义不一致时，该 CUA 服务会因冲突被禁用，其他 MCP 不受影响；显式 `disabled: true` 始终优先，恢复能力需先解除禁用。不要同时叠加旧手工模式与新托管模式。

## MCP 结构化结果

MCP loader 保留 `structuredContent` 为 `raw_output.mcp_structured_content`，成功和错误返回均适用。CUA 插件将配置服务的实际工具名、参数和结构化结果追加到工具文本；窗口、capture、snapshot、坐标空间及截图错误等字段按照 Driver 原始返回保留。其他 MCP 服务的模型文本投影保持原有路径。

针对 Driver 0.32 的窗口观察，插件补充动作依据说明：使用最新元素 token；`target` 与旧的 `pid/window_id/scope` 参数不能混用；`screenshot_frame` 是截图像素，`frame` 是屏幕坐标，动作遵循各工具实际声明的坐标合同。像素动作消费 capture ID，后续动作重新观测；工具已派发或 `effect: unverifiable` 都需要新状态确认任务结果。

## 图片注入条件

截图通过 MCP 工具结果进入运行时 Surface，但只有模型明确声明支持图片输入时才会写入图片消息：

| 模型图片输入能力 | 下一次请求 |
| --- | --- |
| `True` | 写入规范化图片块，并保留工具文本结果 |
| `False` | 不写入图片，只保留文本结果 |
| `None`（能力未知） | 不写入图片，只保留文本结果 |

普通模型或能力未知的模型只接收文本工具结果。启用 `persist_images` 且会话有 SessionLog 时，工具文本可以包含已保存的图片路径，路径本身不表示模型已经看到图片。图片能力判断、MCP 服务名匹配以及截图编码都属于 CUA 插件边界，核心 MCP 加载器不硬编码 CUA 逻辑。

图片块由插件写入会话目录的 `images/<sha256>.<ext>` sidecar，同时把路径追加到 MCP 工具结果文本。`raw_output.mcp_image_references` 保留图片引用、哈希、字节数与尺寸；已经由引用覆盖的图片字节不再重复写入 raw_output。未成功保存的图片仍使用原有 raw_output 路径。

图片输入沿用当前轮的 transient `user` 通道，只发送到工具调用后的下一次模型请求，不进入 durable Surface，也不在后续请求自动重放。插件保留 Driver 图片的原始字节、方向和尺寸，避免二次缩放改变像素依据；附带说明将图片与服务、工具、参数及实际返回的捕获身份关联。Driver 明确返回 `screenshot_frame_valid: false` 或 `screenshot_error` 时不注入该批图片，工具结构化状态仍然保留。

图片预算继续由已有 ContextEngine / transient 校验控制。需要减小输入时，通过运行中 Driver 工具实际提供的截图尺寸参数重新观测，使用该次返回的坐标信息。文件路径或 capture ID 本身不会触发自动读取文件、恢复旧截图或重放动作。
