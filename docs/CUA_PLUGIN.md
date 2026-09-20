# CUA 插件配置与启动边界

CUA 是可选插件。Box-Agent 核心只读取并保留通用的 `plugins` 命名空间，具体字段由 CUA 插件自己校验；核心 `tools` 配置不包含 `cua` 字段。

## 配置示例

```yaml
api_key: "sk-..."
api_base: "https://api.openai.com/v1"
model: "gpt-4o"
provider: "openai"

plugins:
  cua:
    server_name: "computer-use"
    feed_screenshots: true
```

`Config.from_yaml()` 会把每个 `plugins.<name>` 保留为一个映射，不解释未知插件的字段。CUA 插件激活时再校验自己的命名空间：`server_name` 默认是 `computer-use`，`feed_screenshots` 默认是 `true`，未知字段会被拒绝。

没有 `plugins.cua` 命名空间时，虽然 bundled catalog 可以发现插件，CUA 运行绑定保持关闭；只有配置该命名空间后才会在对应 run 中启用。

## 启动链路

启动时由显式的插件装配目录注册内置可选插件：

```text
Config.from_yaml()
  -> Config.plugins（核心只做通用 YAML 透传）
  -> plugins/catalog.py（显式登记 CUA）
  -> PluginRuntime（默认包含 bundled plugins）
  -> PluginSession.open_run()
  -> CUA 插件读取 plugins.cua 并校验
```

需要完全排除可选内置插件的宿主可以构造 `PluginRuntime(include_bundled_plugins=False)`；这不会影响核心内置插件。

CUA 仍然需要在 `mcp.json` 中配置并启动对应的 MCP 服务。此插件改动不安装 `cua-driver` 或其他驱动，也不负责保存轨迹。

## 图片注入条件

截图通过 MCP 工具结果进入运行时 Surface，但只有模型明确声明支持图片输入时才会写入图片消息：

| 模型图片输入能力 | 下一次请求 |
| --- | --- |
| `True` | 写入规范化图片块，并保留工具文本结果 |
| `False` | 不写入图片，只保留文本结果 |
| `None`（能力未知） | 不写入图片，只保留文本结果 |

因此普通模型或能力未知的模型不会得到所谓的“图片路径回退”；它们只接收文本工具结果。图片能力判断、MCP 服务名匹配以及截图编码都属于 CUA 插件边界，核心 MCP 加载器不硬编码 CUA 逻辑。

图片块先由插件写入会话目录的 `images/<sha256>.<ext>` sidecar，再以不含 `data` 的 `input_image` 引用写入现有 `user/message` Surface。核心只复用 `append_unlogged_messages()` 和已有的 `replace_surface()`，SessionLog 不解析或保存 base64。模型请求只在 provider 边界临时 hydrate 最新一张图片，旧图片保留为 sidecar 引用，不会把历史图片重新作为 inline base64 发送。

原 MCP 工具结果的 `raw_output` 仍按 Box-Agent 原有路径记录；SessionTrace 对 inline 图片只记录摘要，避免把 base64 写入轨迹。
