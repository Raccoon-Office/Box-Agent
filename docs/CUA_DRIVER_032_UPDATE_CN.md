# Box-Agent 与 CuaDriver 0.32 更新说明

更新日期：2026 年 10 月 8 日。本文用于说明本次桌面工具适配、ACP 读图修复与实际验收结果。

本次更新使 Box-Agent 能保留 CuaDriver 0.32.0 返回的结构化窗口状态与截图依据，并修复 ACP profile 会话丢失模型图片能力声明的问题。重新构建的 standalone ACP 和重启后的商汤小浣熊均已通过真实模型读图验证。

## 更新背景

接入路径为：ACP 宿主 → Box-Agent → CuaDriver stdio MCP → 窗口观察与截图 → 下一次模型请求。Driver 的工具及参数继续从运行中的 MCP schema 发现。

排查发现，截图文件已经保存，并不代表图片已进入模型上下文。小浣熊通过 `llm_binding.source=profile` 选择模型，ACP 会为会话重新创建 `LLMClient`。修复前，该客户端没有保留默认配置中的 `image_input: true`，导致 CUA 的模型能力检查无法确认支持图片，进而跳过截图输入。这一问题发生在模型客户端创建环节，与截图采集成功是两个独立状态。

## 本次改动

| 改动 | 更新后的行为 |
| --- | --- |
| 保留 MCP 结构化状态 | 成功和错误结果都保留 `structuredContent`。CUA 工具文本包含实际工具名、参数及结构化状态，使 capture、snapshot、窗口和截图错误等信息可用于判断下一步。 |
| 保留截图的几何依据 | CUA 输入与 sidecar 保留 Driver 原始图片字节、方向和尺寸，避免再次缩放改变像素坐标。图片与本次工具参数和捕获身份关联。 |
| 区分截图保存与模型输入 | 新增 `persist_images`，默认开启；它独立于模型能力和 `feed_screenshots`。已经保存的图片以路径、哈希和尺寸引用，未保存的图片继续保留原有 raw output。 |
| 保留 ACP 图片能力声明 | profile 支持可选布尔字段 `imageInput`；匹配的默认模型也能继承 config 的显式 `image_input`。不同模型、provider 或接口不会继承其他模型的声明。 |
| 更新安装与验收说明 | 文档以 CuaDriver 0.32.0 独立 App 与 stdio MCP 为基线，统一 binary 路径、服务名、权限检查、升级步骤及 ACP 验收方式。 |

### 小浣熊切换模型时会发生什么

**当前修复不会让读图配置自动跟随任意模型切换，也不会改写 `config.yaml`。** 它解决的是：小浣熊选的模型与 config 一致时，原来丢失的读图声明现在能够传给会话客户端。

例如，小浣熊和 config 都选择同一接口上的 `gpt-5.6-sol/azure_L/sfa`，且 config 写了 `image_input: true`，修复后该会话就会保留这个声明。刚才的小浣熊实测验证的是这种情况。

| 使用情况 | 读图配置怎么决定 |
| --- | --- |
| 小浣熊没有单独声明读图能力，选的模型、服务商和接口都与 config 相同 | 沿用 config 的 `image_input`。接口地址末尾多一个 `/` 不影响匹配。 |
| 小浣熊切换到其他模型或接口 | 不沿用原模型的声明。要明确支持读图，需要小浣熊为新模型提供对应的能力声明。 |
| 小浣熊自己的模型配置已经明确声明能读图或不能读图 | 优先使用它自己的声明。一个模型的声明不能自动用于其他模型。 |

开发对接时，小浣熊自己的模型配置称为 profile，其中的读图字段名为 `imageInput`；该字段仅适用于这份配置的默认模型 `defaultModel`。开关只能使用布尔值 `true` 或 `false`，格式错误会被拒绝。未明确声明时继续使用既有能力判断规则，不会强制开启图片输入。声明为 `true` 也不会让纯文本模型获得视觉能力。

小浣熊的内部 profile revision 由宿主管理，不建议直接编辑生成的 registry。要完整支持模型切换后的能力同步，需要宿主在生成各模型配置时携带对应声明；这项宿主侧工作不包含在当前 Box-Agent 修复中。

### 观察与输入约定

- Driver 明确返回 `screenshot_frame_valid: false` 或 `screenshot_error` 时，不将该批图片注入模型；工具状态仍然保留。
- 模型明确支持图片且开启截图输入时，当前截图通过已有 transient 通道进入工具后的下一次请求；后续请求不自动重放旧图，持久历史保留图片引用。
- 动作使用目标窗口最新观察中的元素 token。支持 `target` 的工具不能同时使用旧的 `pid/window_id/scope` 字段。
- `screenshot_frame` 表示截图像素坐标，`frame` 表示屏幕坐标；动作遵循运行中各工具的 schema。像素动作使用 capture ID 后重新观察，派发成功或 `effect: unverifiable` 都需要新状态确认结果。

## 验证结果

真实测试环境为 macOS Apple Silicon、CuaDriver 0.32.0，以及 `gpt-5.6-sol/azure_L/sfa` 模型。测试关闭无障碍树，仅使用窗口截图识别随机六位数字和三个图形，未将正确答案写进模型 query。

| 验证场景 | 结果 |
| --- | --- |
| 独立重建 ACP 第一轮 | 正确识别 `624900`；图形为三角形、圆形、正方形。 |
| 独立重建 ACP 第二轮 | 正确识别 `224333`；图形为圆形、正方形、三角形。 |
| 替换 runtime 后的小浣熊实测 | 实际调用一次 `get_window_state`，关闭无障碍树；正确识别第一轮图片。 |

独立 ACP 的图片请求序列为 `[0, 1, 0, 0, 1, 0, 0, 0]`，SDK 请求中的图片计数也一致。小浣熊实测的请求序列为 `[0, 1, 0, 0]`。每次包含图片的请求，其图片哈希与本次工具返回引用和 sidecar 文件一致。正确识别结果与请求记录共同证明当前步截图已传递并被模型使用；仅看到图片路径或模型自称看到了截图不作为充分证据。

源码聚焦回归覆盖 ACP/profile、CUA wiring、MCP 截图、sidecar、图片编码、配置和 MCP loader，结果为 **393 passed，3 skipped**。测试覆盖显式关闭、能力未知、模型或接口不匹配、错误状态与无效截图等情况。全仓 preflight 的结果以对应 PR 的 Proof 为准。

## 安装与交付状态

本地已完成源码修改、聚焦测试、standalone runtime 构建、旧 runtime 备份、新 runtime 安装、小浣熊完整重启、ACP 初始化、58 个 CuaDriver MCP 工具连接，以及新会话实际读图验收。

该验证对应本次本地构建，尚不代表官方发布的 runtime 已包含修复。其他机器和宿主需安装包含该修改的 runtime、完整重启 ACP，再新建会话验收；只更新源码或查看 CLI 配置不足以更新正在运行的客户端。

安装配置应保持一致：

```yaml
image_input: true
plugins:
  cua:
    server_name: cua-computer-use
    feed_screenshots: true
    persist_images: true
```

MCP 服务同名，command 指向 `/Applications/CuaDriver.app/Contents/MacOS/cua-driver`，args 为 `["mcp"]`。`image_input: true` 仅用于已确认接受图片的模型。具体安装、权限和备份升级步骤见 [安装指南](CUA_PLUGIN_GUIDE_CN.md)；插件行为见 [CUA 插件说明](CUA_PLUGIN.md)。

## 复测方法与适用边界

准备只包含随机数字和图形的图片，在预览中打开；先从当前 `list_windows` 结果取得正确的 PID 和窗口 ID，再在新会话中发送以下 query。不要把图片答案提前发给模型。

```text
这是一次视觉输入验证。请仅调用一次 get_window_state，参数为：
pid=<当前 PID>，window_id=<当前窗口 ID>，
include_accessibility_tree=false，include_screenshot=true，max_image_dimension=1200。
读取这次工具返回的截图，识别图片中央的六位数字和下方从左到右的三个图形。
只输出 JSON：{"number":"六位数字","shapes":["circle/triangle/square",...]}。
禁止读取文件、执行代码、使用其他工具或从文件名猜内容。
若没有收到图片，明确说没有图片，不要猜。
```

验收分别检查工具结果、图片文件与模型请求。核对请求图片和工具返回图片的哈希一致，再比对识别结果，并确认后续请求不重放旧图。

当前真实验证限于 macOS Apple Silicon、上述 Driver 和模型路由。尚未验证 Windows、Intel runtime 或全部视觉模型，也没有完成通用桌面任务成功率评估。该修复不会使纯文本模型具备视觉能力。保留 Driver 原始尺寸可能增加图片输入预算；需要较小截图时，通过 Driver 实际提供的尺寸参数重新观察。默认开启图片保存时，会话目录会保存桌面截图，应按使用环境管理其留存。

后续提交 Box-Agent PR，完成仓库检查与维护者 review，再通过正常 runtime 发布流程向其他宿主分发。
