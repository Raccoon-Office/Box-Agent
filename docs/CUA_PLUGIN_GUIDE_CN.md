# CUA 插件使用指南

本文说明如何在 macOS 13+ Apple Silicon 上让 Box-Agent 通过 CuaDriver 的 MCP 服务使用桌面操作，并让支持图片输入的模型看到 CUA 截图。

## 当前分支：CLI 安装与双宿主模式

独立 CLI 使用 `box-agent install-cua` 下载并安装固定的 CuaDriver 0.28.2，不需要 sudo 或模型 API 配置。安装成功会打印驱动绝对路径；macOS 按输出提示执行 `"<驱动绝对路径>" permissions grant`，完成授权后重新启动 `box-agent`。

安装命令支持 macOS arm64/x64、Windows arm64/x64、Linux arm64/x64；Windows/Linux 的真实桌面操作仍需实机验收。

下载源使用 `RACCOON_CUA_DOWNLOAD_BASE_URL`：未设置时用官方 GitHub Releases；设置时仅使用该 HTTPS CDN，不在失败后回退官方。CDN 必须镜像官方原始压缩包，安装器使用内置 SHA-256 校验；拒绝归档路径逃逸、链接和特殊文件，macOS 还检查签名与 Gatekeeper。

```bash
box-agent install-cua
# 可选 CDN，文件布局与桌面端一致：
RACCOON_CUA_DOWNLOAD_BASE_URL=https://cdn.example.com/computer-use/ box-agent install-cua
```

驱动资源默认存放于同一用户的 `~/.box-agent/resources/cua-driver/`，桌面客户端与 CLI 共用 `current.json` 安装记录。该目录从系统用户 home 解析，不跟随 `BOX_AGENT_HOME` 或客户端的隔离 profile。同版本已验证资源直接复用，无需分别下载；两边同时安装时可能各自下载，但以独立目录和原子记录发布避免覆盖运行中的二进制。可信环境可设置绝对路径 `RACCOON_CUA_RESOURCE_DIR`；要共享资源，两边必须使用同一目录。不会修改 PATH 或 `/Applications`，二进制不进入源码或 Python 安装包。

共享的只是驱动文件，不是授权或 daemon：CLI 的 `ensure_cua_ready` 管理独立 daemon；桌面客户端以 external 模式提供自己持有的 embedded daemon。关闭一方不会停止另一方。下载不自动开启能力、不授予权限、不启动 daemon。

已有安装版本或签名校验失败时，`install-cua` 会重新下载到独立目录；新驱动通过验证后才切换安装记录，下载失败保留旧记录及文件。macOS CLI 的 LaunchServices 启动取消会清理尚未监听的自持进程；清理无法确认时保留待启动句柄和私有目录，供关闭时重试，不按应用名称停止其他进程。

macOS CLI 会区分尚未确认的启动请求与已就绪的实例。已就绪 daemon 意外退出后，下一次 `ensure_cua_ready` 核对其精确进程和私有 endpoint 均不存在，再清理旧 socket 并启动替代实例；不能确认退出时仍保留清理责任，不删除活进程的 socket。未确认的 LaunchServices 请求继续等待并清理可能迟到的进程。

## 生命周期与 MCP 配置

CLI 无需手写 CUA 的 `mcp.json`。启用 MCP 工具时，Box-Agent 默认注册 CUA 专用管理模块；模型先读取 computer-use Skill，再调用 `ensure_cua_ready`。该工具检查已安装驱动、按需启动独立 daemon，并连接私有 endpoint 上的 MCP 代理。未安装驱动时返回明确错误，不自动下载或授权。

桌面客户端负责驱动安装、当前客户端的系统权限和 embedded daemon。宿主通过可信环境变量 `BOX_AGENT_CUA_RUNTIME_CONFIG` 提供 external 模式、驱动绝对路径和私有 endpoint；Box-Agent 只检查并连接，不自行启动备用 daemon。必须由 Electron 自持的原因是 macOS 的权限归属、客户端开关和退出清理都由桌面宿主负责，而 CLI 使用独立 App 身份。

托管 MCP 默认服务名为 `computer-use`。不要再添加同名手工配置（例如 `args: ["mcp"]`）：与托管定义不一致会禁用该 CUA 服务并报告冲突，其他 MCP 不受影响。已有同名 `disabled: true` 配置优先生效。

可在现有 `config.yaml` 的 `plugins` 下合并以下可选配置，保留其他插件：

```yaml
plugins:
  cua:
    server_name: computer-use
    feed_screenshots: true
    persist_images: true
    runtime:
      enabled: true
      mode: standalone
```

- 不填写 `plugins.cua` 时也会注册默认管理模块；缺少驱动仍不会启动 daemon。
- CLI 可设置 `runtime.enabled: false` 禁用 CUA；桌面端使用客户端开关，不手动设置 external endpoint。
- `feed_screenshots: false` 保留工具文本和结构化状态，但不向模型注入截图；`BOX_AGENT_CUA_VISION=false` 可临时关闭图片注入。
- `persist_images: true` 让有 SessionLog 的会话保存截图 sidecar，独立于模型图片能力；设为 `false` 关闭图片落盘。
- 报错结果仍保留结构化诊断信息，但保持失败状态、不注入图片。模型需刷新目标状态后再操作，不能把失败观察当成有效操作依据。
- 退出 CLI 清理自己启动的 daemon；external 模式不停止宿主 daemon。两种模式都不会停止其他应用的进程。

## 模型识别和图片注入

插件不会仅因为模型名称看起来像视觉模型就无条件发送图片。它按以下顺序读取模型的图片输入能力：

| 检查来源 | 结果 |
| --- | --- |
| LLM adapter 的 `supports("image_input")` | 明确返回 `true` 或 `false` 时优先使用 |
| LLM adapter 的 `capabilities["image_input"]` | 明确声明时使用 |
| 当前模型候选的 `tags` 包含 `vision` | 判定为支持图片 |
| 模型名包含 `vision` 或 `deepseek-vl` | 作为兼容性启发式判定为支持 |
| DeepSeek API 或 `deepseek-*` 文本模型 | 判定为不支持 |
| 其他情况 | 能力未知，不发送图片 |

只有最终结果为 `true` 时，CUA 截图才会进入下一次模型请求。结果为 `false` 或 `None` 时，模型仍能看到 CUA 工具返回的文本状态，但不会收到图片。这是为了避免把图片块发送给普通文本模型。

截图保留 Driver 返回的字节和像素尺寸，Box-Agent 不再进行二次缩放。模型上下文会同时包含图片所属工具调用、参数和返回的捕获身份。截图明确无效或采集失败时只保留工具状态；下一次请求消费图片后，后续请求不会自动复用旧截图。

如果接入自定义模型，推荐在 LLM adapter 或模型目录中明确声明 `image_input: true`，或给对应模型候选增加 `vision` tag；不要依赖模型名称猜测。

### 在 `config.yaml` 登记自定义模型的图片能力

对于 OpenAI 兼容的自定义网关（例如 TokenHub），可以在主模型配置中显式登记能力。当前 Box-Agent 的 `config.yaml` 使用顶层模型字段，因此这里写成 `image_input`，不是再包一层 `llm:`：

```yaml
api_base: "https://tokenhub.sensetime.com/v1"
provider: "openai"
model: "你的模型名"
api_key: "YOUR_TOKENHUB_API_KEY"
image_input: true
```

`api_key` 只应保存在本机配置或 `auth.json`，不要把真实 key 提交到仓库。配置写入后，可以先确认 Box-Agent 读取到了声明：

```bash
box-agent config --json | jq '{config_file, model: .llm.model, image_input: .llm.image_input}'
```

`image_input` 只接受布尔值：

- `true`：允许 CUA 插件把最新截图作为图片输入发送给该模型；
- `false`：明确禁止图片输入，即使模型名包含 `vision` 也不会注入截图；
- 不写该字段：按上面的 adapter、模型候选和兼容性规则判断；最终能力仍未知时不发送截图。

该字段用于 CLI 和 ACP 默认模型 client，并随 `for_model` 绑定保留。ACP 宿主使用模型 profile 时，还需满足下文的 profile 能力规则。它只是 Box-Agent 对模型能力的声明，不会探测或修改服务端模型能力；填写 `true` 前应先确认该模型的接口确实接受 OpenAI 风格的图片消息。API 的 `/models` 返回能力元数据时，也需要由操作者核对具体模型是否支持图片输入，Box-Agent 不会自动把任意 `image` 字段转换成该声明。

### ACP 会话使用模型 profile 时

ACP 的 profile 绑定会创建独立模型 client。图片能力按以下规则传递：

1. 宿主生成的 profile 可显式提供布尔字段 `imageInput`；该声明对 profile 的 `defaultModel` 生效，`false` 也会保留并优先于主模型配置。
2. profile 没有对当前模型声明能力时，只有其 **provider、API 地址和会话选中的 model** 都与 `config.yaml` 默认 client 一致，才继承该 client 显式声明的 `image_input`。API 地址末尾的 `/` 不影响匹配。
3. 切换到不同模型、provider 或接口时，不继承其他模型的声明；该模型按自身能力规则判断。profile 的 `imageInput` 不自动扩展到同一接口下的其他模型。

例如，当前 ACP 会话与 `config.yaml` 都选择相同接口上的 `gpt-5.6-sol/azure_L/sfa`，顶层设置 `image_input: true` 后，匹配的 profile client 会保留该声明。若宿主管理另一模型，需在宿主生成对应 profile 时声明其能力，例如以下 profile 字段：

```json
{
  "defaultModel": "你的视觉模型名",
  "imageInput": true
}
```

这只是 profile 字段片段，不是完整 registry。模型 profile revision 由宿主管理，不要手工覆盖生成的 registry 或修改已有 revision。使用这些规则需要包含该修复的 Box-Agent runtime；更新源码后，需重新构建、安装并重启 ACP 宿主，再新建会话验收。`box-agent config --json` 显示默认模型配置，不能单独证明宿主 profile 会话的实际能力。

### 三个边界为什么需要同时存在

这次实现把图片能力拆成三个边界，各自解决不同问题：

1. **模型配置的 `image_input` 参数**：模型名称无法可靠证明接口是否接受图片。显式能力参数让 CLI、ACP 和 CUA 插件使用同一个判断结果，避免把图片误发给文本模型，也避免在不同入口重复维护模型识别逻辑。
2. **CUA 插件**：截图只属于配置的 CUA MCP 服务，不应让通用 MCP loader 认识 CuaDriver、sidecar 目录或 CUA 开关。插件负责服务激活、截图规范化、能力门控和 sidecar 生命周期，关闭插件时普通 MCP 和默认 Agent 路径保持不变。
3. **MCP result hook**：MCP loader 只能看到远端返回的 text/image content，不能把 CUA 规则硬编码进通用工具。run-scoped hook 将匹配的 CUA 图片转换成当前请求的一次性 transient user 内容，并把 sidecar 路径追加到普通 tool 结果；这样图片不会进入 durable user history，也不会污染其他 MCP server 的结果。

TokenHub 当前的模型目录可能只返回模型 ID 和 endpoint 类型，不一定带 `image` 字段。对实际模型发一条带 `image_url` 的最小 `chat/completions` 请求并得到成功响应，才是登记 `image_input: true` 的依据；不能因为模型能生成图片，就推断它能接收图片。

### 最小端到端验收

CLI 按 `install-cua` 输出的驱动路径检查权限；桌面端通过插件卡片检查当前客户端权限。重启 Box-Agent 后检查模型配置：

```bash
box-agent config --json | jq '{config_file, model: .llm.model, image_input: .llm.image_input}'
box-agent doctor
```

`box-agent doctor` 的 MCP 检查只确认配置文件存在，不证明 Driver 连接、截图或模型视觉输入成功。然后运行 `box-agent`，发送“使用 CUA MCP 读取当前窗口并描述截图内容”。若使用 ACP，在重启后的宿主中新建会话执行同样的任务；CLI 检查不能代替 ACP 宿主验收。

验收时分别检查：

1. **工具连接与状态**：会话实际调用 Driver 工具，返回当前窗口状态；启用插件后结构化结果包含真实工具名、参数及 `structuredContent`。
2. **截图保存**：启用 `persist_images` 且会话有 SessionLog 时，工具结果的 `raw_output.mcp_image_references` 引用实际存在的图片文件，哈希和尺寸与本次返回一致。
3. **模型图片请求**：支持图片且开启输入时，工具后的下一次请求携带匹配的图片；后续请求不会自动累积旧图片。图片落盘、路径出现在文本或模型自称看到了截图，都不能单独证明请求携带了图片。以能保留图片输入信息的请求记录核对；若记录只保留文本或省略图片字段，这项仍待验证。

若只看到工具文本而没有图片，先检查 `image_input`、`server_name` 和 MCP 权限。

### 观察与动作约定

Box-Agent 使用 Driver 实际声明的工具 schema，插件补充动作依据说明：

- 元素动作使用目标窗口最新观察的 `element_token`；新观察替换旧快照后，不继续使用失效 token。
- 支持 `target` 的工具使用 `target` 或旧 `pid/window_id/scope` 字段之一，不能混用。
- `screenshot_frame` 是返回截图的像素坐标，`frame` 是桌面屏幕坐标；每个动作按其工具 schema 的坐标约定执行。
- 像素动作消费 capture ID，后续动作取得新观察。`effect: unverifiable` 或派发成功后仍需观察结果。
- `window_id_not_found` 时重新枚举窗口；`window_owner_pid_mismatch` 时按返回的真实 owner PID 核对目标，尤其是打开/保存面板。

## 图片保存和请求行为

一次 CUA 工具调用返回图片后，保存和模型输入独立控制：

1. 开启 `persist_images` 且有 SessionLog 时，把保留 Driver 原始字节和尺寸的图片写入当前 session 目录的 `images/<sha256>.<ext>` sidecar。
2. 把 `images/<sha256>.<ext>` 路径追加到 MCP 工具结果文本，让普通 tool message 进入 live Surface 和 SessionLog。
3. 开启 `feed_screenshots`、模型明确支持图片且截图有效时，把图片作为当前轮的 transient `user` 消息发送一次；它不进入 durable Surface，也不需要 provider hydrate。模型不支持图片时仍可独立保存 sidecar。

已成功保存到 sidecar 的图片不再把 base64 写入工具结果；SessionLog 保留图片路径、哈希和尺寸。未成功保存的图片保留原有 MCP raw output，避免丢失数据。`~/.box-agent/log/sessions/` 下的文件是诊断 SessionTrace，主要记录 LLM/tool 请求；要检查工具结果路径，应查看对应的 `~/.box-agent/sessions/<session>/session.jsonl`。

## 常见问题

### MCP 工具没有出现

检查 `tools.enable_mcp` 和 `plugins.cua.runtime.enabled` 是否开启、驱动是否已安装，再调用 `ensure_cua_ready`，之后用 `tool_search` 发现动作工具。桌面端还需开启插件并完成授权；external 模式必须有可用的宿主 daemon。若报告同名 MCP 配置冲突，移除旧手工 CUA entry；显式禁用的 entry 则需先解除禁用。

### 工具成功但模型没有看到截图

依次检查：

1. `plugins.cua.feed_screenshots` 是否为 `true`。
2. 是否设置了 `BOX_AGENT_CUA_VISION=false`。
3. 当前 LLM 是否声明 `image_input: true`；未知能力会按文本模型处理。
4. CuaDriver 返回的图片是否是 MCP image block。
5. 是否返回 `screenshot_frame_valid: false` 或 `screenshot_error`；插件不会注入这类无效截图。

### 升级后仍连接旧版本

检查共享目录 `current.json` 所指向的安装，以及是否设置了覆盖路径的环境变量或 `runtime.driver_path`；对实际驱动路径执行 `--version`。结束旧任务并重启 CLI / 桌面宿主，不能只替换文件。终端 PATH 中另一个 `cua-driver` 的版本不能证明 MCP 使用的版本。源码改动也不会自动进入已打包的 ACP runtime，需重新构建、安装并重启消费它的宿主后验收。

### 普通模型收到了图片错误

这是模型能力声明不准确造成的。为该 LLM adapter 明确返回 `supports("image_input") == false`，或将其 `capabilities["image_input"]` 设置为 `false`。插件随后只保留工具文本。

### 如何完全关闭 CUA

仅关闭图片输入可设置 `feed_screenshots: false` 或 `BOX_AGENT_CUA_VISION=false`。完全停用时，CLI 设置 `plugins.cua.runtime.enabled: false` 并退出当前进程、重新启动；桌面端关闭「桌面控制」开关。生命周期 owner 会清理自己启动的 daemon，不停止其他应用的实例。移除 `plugins.cua` 不会禁用默认管理模块。

## MR 说明草稿（2026-10-13）

建议标题：`feat(cua): add managed desktop runtime and CLI driver installation`

### Task

- CLI 默认注册 `ensure_cua_ready`，按需启动私有 daemon；external 模式仅连接 Electron 提供的 daemon，不启动备用实例、不停止宿主。
- `install-cua` 显式安装固定版本驱动，支持配置 CDN 或默认官方源，两种宿主共享用户级资源目录，不将驱动二进制加入源码或 wheel。
- 保留精确进程归属、启动取消及迟到进程清理保护，修复 macOS 已就绪 daemon 退出后的重新准备；补齐新增 Skill/tool 的严格兼容测试，未放宽其他工具的 schema 断言。
- 不修改共享模型循环，不自动授权、自动下载或自动更新；未知图片能力仍不注入截图。

### Proof

在本仓库执行：

```sh
uv sync --frozen --all-extras
uv run --frozen --no-sync python -m compileall -q box_agent
uv run --frozen --no-sync pytest tests/ -q --tb=short --deselect tests/test_mcp.py::test_connection_timeout_on_unreachable_server
uv build --out-dir /private/tmp/box-agent-cua-fixed-review-20261013
git diff --check
```

- 全量回归：6931 passed、293 skipped、1 deselected；耗时 342.52 秒。原 9 条 Skill/tool 基线失败与 1 条耗时断言本次均未失败。
- 恢复、取消、Skill 清单及工具兼容聚焦测试：70 passed；仍保留待启动句柄清理超时、活 PID 状态查询失败等保护。
- 有一条既有 `requests` 依赖版本兼容警告，没有测试失败。排除项来自仓库 Preflight，跳过项按测试自身条件执行，不计作实机通过。
- 本说明是未提交工作区的本地验证记录，不是远程 `teamwork/local-ci` 状态；正式提交后必须对准确 Head 重新执行远程门禁。

### Risk

- 客户端必须配套包含这些源码的 Box-Agent standalone runtime；源码包/wheel 构建不是 standalone runtime 构建、安装、宿主重启或真实任务验收。
- Windows/Linux 桌面输入、macOS 真机异常退出恢复仍需平台验收；本次新增回归使用受控 PID/endpoint，不运行用户应用。
- 同名手写 CUA MCP 与托管配置冲突会禁用该服务；使用客户端开关或 CLI 管理配置，不并行维护旧手写定义。
- 驱动固定版本且不内置，部署需提供可访问的官方源或可信 CDN；配置 CDN 后不会静默回退官方。撤销版本时应重建宿主 runtime，而非覆盖正在使用的驱动文件。
