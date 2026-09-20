# CUA 插件使用指南

本文说明如何在 macOS 13+ Apple Silicon 上让 Box-Agent 通过 CuaDriver 的 MCP 服务使用桌面操作，并让支持图片输入的模型看到 CUA 截图。

## 1. 运行前提

CUA 插件只负责 Box-Agent 内的 MCP 结果适配、图片注入和 sidecar 保存。它不包含 `cua-driver` 二进制，也不会替操作系统授予辅助功能或屏幕录制权限。

需要先准备以下运行时：

1. macOS 上已安装并能运行 `cua-driver`。
2. CuaDriver.app 已获得辅助功能和屏幕录制权限。
3. Box-Agent 的 MCP 配置能启动 CuaDriver 的 MCP 子命令。

下面是一份可以直接复制到 macOS Apple Silicon 上执行的最小安装脚本。它下载并校验固定版本的 CuaDriver，安装 CLI 和 `CuaDriver.app`；它不授予系统权限，也不会自动绕过 TCC。Box-Agent PR 不把这个 macOS 原生驱动打进 Python 包。

```bash
cat > /tmp/install-cua-driver.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail

[[ "$(/usr/bin/uname -s)" == "Darwin" && "$(/usr/bin/uname -m)" == "arm64" ]] || {
  echo "This installer requires Apple Silicon macOS." >&2
  exit 2
}
[[ "$(/usr/bin/id -u)" == "0" ]] || {
  echo "Run with: sudo bash /tmp/install-cua-driver.sh" >&2
  exit 2
}

version="0.25.0"
url="https://github.com/trycua/cua/releases/download/cua-driver-rs-v${version}/cua-driver-rs-${version}-darwin-arm64.tar.gz"
sha256="48fb4c329987f66ea9b76d47e6fc19bf8618309afb96d53755e1a6a937025d5c"
prefix="/usr/local/lib/cua-driver-${version}"
cache="/usr/local/lib/cua-driver-downloads"
archive="${cache}/cua-driver-rs-${version}-darwin-arm64.tar.gz"

/bin/mkdir -p "${cache}" /usr/local/bin
if [[ ! -f "${archive}" ]]; then
  /usr/bin/curl --fail --location --retry 5 --output "${archive}.partial" "${url}"
  /bin/mv "${archive}.partial" "${archive}"
fi
printf '%s  %s\n' "${sha256}" "${archive}" | /usr/bin/shasum -a 256 -c -

if [[ ! -f "${prefix}/.installed" ]]; then
  /bin/mkdir -p "${prefix}"
  /usr/bin/tar -xzf "${archive}" -C "${prefix}" --strip-components 1
  [[ -x "${prefix}/cua-driver" && -d "${prefix}/CuaDriver.app" ]] || {
    echo "Unexpected CuaDriver package layout." >&2
    exit 5
  }
  /usr/bin/codesign --verify --deep --strict "${prefix}/CuaDriver.app"
  /usr/bin/touch "${prefix}/.installed"
fi

/bin/ln -sfn "${prefix}/cua-driver" /usr/local/bin/cua-driver
if [[ -d /Applications/CuaDriver.app ]]; then
  current=$(/usr/bin/shasum -a 256 /Applications/CuaDriver.app/Contents/MacOS/cua-driver | /usr/bin/cut -d ' ' -f 1)
  expected=$(/usr/bin/shasum -a 256 "${prefix}/CuaDriver.app/Contents/MacOS/cua-driver" | /usr/bin/cut -d ' ' -f 1)
  [[ "${current}" == "${expected}" ]] || {
    echo "An existing CuaDriver.app has a different binary; remove it explicitly before rerunning." >&2
    exit 6
  }
else
  /usr/bin/ditto "${prefix}/CuaDriver.app" /Applications/CuaDriver.app
fi
/usr/bin/codesign --verify --deep --strict /Applications/CuaDriver.app
/usr/local/bin/cua-driver --version
echo "Installed CuaDriver ${version}. Grant Accessibility and Screen Recording permissions, then start CuaDriver.app."
SH
sudo bash /tmp/install-cua-driver.sh
```

安装后，执行权限引导并检查状态：

```bash
/usr/local/bin/cua-driver permissions grant
/usr/local/bin/cua-driver permissions status --json
```

然后打开 `CuaDriver.app`。需要 MCP 服务时，可以用 `open -g -a /Applications/CuaDriver.app --args serve` 启动驱动；配置了下面的 MCP server 后，Box-Agent 也可以通过 `cua-driver mcp` 代理自动拉起它。只有本机已经准备好驱动后，下面的 Box-Agent 配置才有意义。

安装后可以先验证：

```bash
/usr/local/bin/cua-driver --version
```

如果使用本地开发目录中的二进制，也可以直接在 MCP 配置中填写绝对路径，例如：

```text
/Users/lixiaowan/Documents/CUA/.cua-probe/cua/cua-driver
```

## 2. 配置 CuaDriver MCP 服务

Box-Agent 默认读取 `~/.box-agent/config/mcp.json` 和 `~/.box-agent/config/config.yaml`。开发目录中的配置可能优先；可以用 `box-agent config --json` 查看实际配置文件路径。在 `mcpServers` 下加入一个 stdio server；下面的 `cua` 是服务名，后面插件配置中的 `server_name` 必须与它一致：

```json
{
  "mcpServers": {
    "cua": {
      "description": "Local CuaDriver computer-use MCP",
      "type": "stdio",
      "command": "/usr/local/bin/cua-driver",
      "args": ["mcp"],
      "alwaysLoad": true,
      "disabled": false,
      "connect_timeout": 60,
      "execute_timeout": 120
    }
  }
}
```

如果驱动安装在其他位置，只替换 `command`，不要把 `cua-driver` 的路径写进 `config.yaml`。`mcp.json` 中的 `disabled` 必须为 `false`；修改后重新启动 Box-Agent，让 MCP loader 重新建立连接。

## 3. 开启 Box-Agent CUA 插件

在 Box-Agent 的 `~/.box-agent/config/config.yaml` 中加入：

```yaml
plugins:
  cua:
    server_name: "cua"
    feed_screenshots: true
```

这里的 `server_name` 必须匹配 `mcp.json` 的键名。如果 MCP server 使用的是 `computer-use`，就把两处都改成 `computer-use`。

CUA 插件是显式可选的：

- 没有 `plugins.cua` 命名空间时，插件不会激活。
- `feed_screenshots: false` 时只保留工具文本，不注入截图。
- 设置 `BOX_AGENT_CUA_VISION=false` 可以临时关闭图片注入，不改配置文件。
- 关闭插件不会卸载或停止已经安装的 CuaDriver；要停止 MCP 服务，应把 `mcp.json` 中对应 entry 设为 `disabled: true` 或移除它。

## 4. 模型识别和图片注入

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
- 不写该字段：能力保持未知，默认按文本模型处理，不发送截图。

该字段会同时作用于 CLI 和 ACP 入口，并随模型 client 的 `for_model` 绑定保留。它只是 Box-Agent 对模型能力的声明，不会探测或修改服务端模型能力；填写 `true` 前应先确认该模型的接口确实接受 OpenAI 风格的图片消息。API 的 `/models` 返回能力元数据时，也需要由操作者核对具体模型是否支持图片输入，Box-Agent 不会自动把任意 `image` 字段转换成该声明。

TokenHub 当前的模型目录可能只返回模型 ID 和 endpoint 类型，不一定带 `image` 字段。对实际模型发一条带 `image_url` 的最小 `chat/completions` 请求并得到成功响应，才是登记 `image_input: true` 的依据；不能因为模型能生成图片，就推断它能接收图片。

### 最小端到端验收

重启 Box-Agent 后先检查配置和 MCP：

```bash
box-agent config --json | jq '{config_file, model: .llm.model, image_input: .llm.image_input}'
box-agent doctor
```

然后运行 `box-agent`，发送“使用 CUA MCP 读取当前屏幕并描述截图内容”。模型应先调用 CuaDriver 工具；若 `image_input: true` 且模型确实接受图片，后续模型请求会携带最新截图。若只看到工具文本而没有图片，先检查 `image_input`、`server_name` 和 MCP 权限。

## 5. 图片保存和请求行为

一次 CUA 工具调用返回图片后，插件会：

1. 把规范化后的图片写入当前 session 目录的 `images/<sha256>.<ext>` sidecar。
2. 通过已有的 `user/message` Surface 写入一个 `source: runtime` 的图片消息。
3. 在 provider 请求边界只 hydrate 最新一张未解析的图片引用。
4. 把旧图片保留为引用，但不把旧图片重新以内联 base64 发送给模型。

SessionLog 和工具 trace 不保存图片 base64，只保存 `contentRef`、SHA-256、尺寸和字节数等元数据。`~/.box-agent/log/sessions/` 下的文件是诊断 SessionTrace，主要记录 LLM/tool 请求；要检查 Surface 中的 runtime `user/message`，应查看对应的 `~/.box-agent/sessions/<session>/session.jsonl`。

## 6. 常见问题

### MCP 工具没有出现

检查 `tools.enable_mcp` 是否开启、`mcp.json` 的 `disabled` 是否为 `false`、`command` 是否可执行，并确认 `server_name` 与插件配置一致。

### 工具成功但模型没有看到截图

依次检查：

1. `plugins.cua.feed_screenshots` 是否为 `true`。
2. 是否设置了 `BOX_AGENT_CUA_VISION=false`。
3. 当前 LLM 是否声明 `image_input: true`；未知能力会按文本模型处理。
4. CuaDriver 返回的图片是否是 MCP image block。

### 普通模型收到了图片错误

这是模型能力声明不准确造成的。为该 LLM adapter 明确返回 `supports("image_input") == false`，或将其 `capabilities["image_input"]` 设置为 `false`。插件随后只保留工具文本。

### 如何完全关闭 CUA

同时执行以下任一项即可停止图片注入：移除 `plugins.cua`，设置 `feed_screenshots: false`，或设置 `BOX_AGENT_CUA_VISION=false`。如果还要停止 CuaDriver MCP 连接，将 `mcp.json` 中的 `cua` entry 禁用并重启 Box-Agent。这个操作不会保证独立的 CuaDriver app daemon 退出；要完全停止驱动，再执行：

```bash
/usr/local/bin/cua-driver stop
```
