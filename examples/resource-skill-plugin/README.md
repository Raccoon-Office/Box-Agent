# 外部 Skill 插件最小示例与配置页模板

完整的开发、部署与交付步骤见[插件开发指南](../../docs/RESOURCE_PLUGIN_DEVELOPMENT_CN.md)，
发布时填写[验收记录](../../docs/RESOURCE_PLUGIN_ACCEPTANCE_TEMPLATE.md)。本页聚焦可复制模板与 HTML 桥。

本目录本身就是一个可安装的**无参数插件**。它贡献一个 `brief` Skill，把用户已提供的材料整理成简报；没有额外工具、模型凭据、网络依赖或安装脚本。`ui/config.html` 同时是可复制的通用配置页：闭合的空对象 Schema 显示说明页，有参数的 Schema 显示 JSON 对象编辑器。

配置页不自己解释 JSON Schema，也不加载第三方表单库。合法性、默认值、版本冲突和持久化均由宿主的管理服务处理。

本示例沿用仓库的 MIT 许可，副本见本目录 [LICENSE](LICENSE)。复制和分发模板（包括 HTML）时保留该版权及许可文本；新增业务资源的来源与许可证需由插件作者另外记录。

## 文件与修改位置

```text
resource-skill-plugin/
├── plugin.yaml
├── config.schema.json
├── ui/config.html
├── skills/brief/SKILL.md
├── LICENSE
└── README.md
```

| 文件 / 字段 | 作者应做什么 | 作用 |
| --- | --- | --- |
| `plugin.yaml` 的 `id` | 换成团队自己的包 ID，例如 `com.example.brief` | 标识插件包；不同团队不共用示例 ID。 |
| `version` | 修改资源、Schema 或页面时更新版本 | 帮助识别安装内容；实际存储还绑定包摘要。 |
| `display_name`、`description` | 写用户能理解的名称和用途 | 用于插件列表展示。 |
| `requires.box_agent`、`plugin_api` | 声明宿主版本范围和插件接口版本 | 不兼容声明在加载前被拒绝；HTML 桥使用单独的 `bridge_version`。 |
| `runtime.kind` | 首期保持 `resources` | 本示例是资源包，不运行插件 worker 或入口代码。 |
| `config_schema` | 指向包内 Schema，按需增加字段 | 宿主校验配置并补充字段默认值。 |
| `ui.config` | 保留相对路径与 `bridge_version: 1` | 宿主加载隔离页面并建立配置桥。 |
| `contributes.skills[].id`、`path` | 每个 Skill 使用唯一贡献 ID 和包内目录 | 宿主按实例建立运行时 Skill 身份，避免不同团队冲突。 |
| `config_keys` | 只列当前 Skill 真正需要的顶层配置键 | 仅选定字段进入该 Skill 的运行上下文；无参数时为 `[]`。 |
| `skills/brief/SKILL.md` | 替换 frontmatter 与方法说明 | Skill 的实际能力由这里定义。 |
| `ui/config.html` | 可以原样复制 | 通用 JSON 编辑器和无参数说明页，不含示例包 ID 或宿主路径。 |

实例名与贡献 ID须以小写字母开头，仅包含小写字母、数字、下划线，最多 64 字符。例如 `brief_team_a`。包 ID 是另一个命名空间，本例使用 `org.example.resource-skill`；复制后应替换。

## 安装无参数示例

在平台提供的**隔离开发客户端**选择“设置 → 插件”，填写实例名 `brief_example`，通过原生目录选择器选择**本目录**。管理入口由 `RACCOON_PLUGIN_DEVELOPER_TOOLS=1` 开启，普通模式不可见；该开关本身不隔离数据目录。新实例默认停用。配置页应显示“此插件没有可配置参数”，并显示已保存修订与生效修订。启用并应用仍从宿主插件管理页操作，页面不自行重启 Agent。

也可使用独立管理 CLI，在 Box-Agent 仓库和已安装依赖的环境中执行。下面的两个绝对路径必须替换为自己的目录；测试状态目录不能指向生产配置。

```sh
BOX_AGENT_HOME=/absolute/plugin-test-state uv run python -m box_agent.plugin_management <<'JSON'
{"method":"plugins.install","params":{"instance_id":"brief_example","package_path":"/absolute/box-agent/examples/resource-skill-plugin","enabled":false,"request_id":"install-brief-example-1"}}
JSON

BOX_AGENT_HOME=/absolute/plugin-test-state uv run python -m box_agent.plugin_management <<'JSON'
{"method":"config.get","params":{"instance_id":"brief_example"}}
JSON
```

成功时 stdout 是一个 `{ "ok": true, "result": ... }` JSON 响应。`config` 应为 `{}`，安装和保存不等于激活。HTML 不获取安装目录、不调用此 CLI，也不能选择其他实例。

## 复制为有参数的插件

1. 复制整个目录到自己的新包目录，修改包 ID、名称、版本与 Skill 内容。
2. 将 `config.schema.json` 替换为下面的 Schema。
3. 将 `plugin.yaml` 中该 Skill 的 `config_keys: []` 改为 `config_keys: [tone]`。
4. 在 Skill 的方法说明里定义如何使用宿主注入的 `tone` 配置，例如 `concise` 生成简短版本、`detailed` 生成详细版本。配置值是数据，不应被拼成可执行脚本或模板指令。
5. 原样保留 `ui/config.html`。安装这个新包后，页面自动显示 JSON 编辑器。

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "tone": {
      "type": "string",
      "enum": ["concise", "detailed"],
      "default": "concise"
    }
  },
  "required": ["tone"],
  "additionalProperties": false
}
```

用户可编辑 `{"tone":"detailed"}`。校验按钮只校验；保存按钮先校验再保存，使用当前 `saved_revision` 做并发检查。成功后显示“已保存，需从宿主应用并重启”，不会宣称已进入正在运行的 Agent。

无参数说明页采用明确的闭合空对象 Schema：`type: object`、没有 `properties`、`additionalProperties: false`，且不含组合 Schema、`$ref` 或 `patternProperties`。`{}` 本身允许任意 JSON，**不是**无参数声明；复杂 Schema 保守使用 JSON 编辑器并交给宿主校验。

## 配置桥契约

页面只接受来自父窗口的一次 `box-plugin-config:init` 消息：`bridge_version: 1`、随机 `nonce`、恰好一条 `MessagePort`、初始 `locale` / `theme`。页面通过这条私有端口回复 `box-plugin-config:ready`，随后请求配置。直接在浏览器中打开文件没有宿主配置服务，会显示未连接。

请求与响应结构：

```text
{type:"request", id:"config-1", method:"config.get", params:{}}
{type:"request", id:"config-2", method:"config.validate", params:{config:{...}}}
{type:"request", id:"config-3", method:"config.save",
 params:{config:{...}, expected_revision:1, request_id:"一次保存的稳定ID"}}

{type:"response", id:"config-1", ok:true, result:{config,schema,saved_revision,active_revision,...}}
{type:"response", id:"config-3", ok:false, error:{code,message,details}}
```

保存按钮必须使用 `type="button"` 并在 `click` 事件中调用配置桥；不要依赖原生表单提交或只监听 `submit` 来保存。宿主 iframe 只有 `allow-scripts`，浏览器会在触发 `submit` 事件前阻止表单提交。保留表单时只用 `preventDefault()` 禁止导航；不要为保存配置增加 `allow-forms` 或放宽 CSP。键盘聚焦保存按钮后按 Enter/空格同样触发按钮点击。

页面不会提交 `target_id`、`profile_id`、`instance_id`、包摘要、Schema 摘要、文件路径或通用管理方法。它只能读、校验、保存宿主绑定的当前实例。包和实例身份由父页面与主进程持有，并在管理请求中校验。

- `context.changed` 在同一端口传入新的 `locale`（zh/en/ja）和 `theme`（light/dark）。只更新文字和样式，不重新读取配置，不重载页面、不丢草稿。
- 版本冲突保留草稿与原修订，不自动覆盖服务器配置。用户点击“重新读取已保存配置（放弃草稿）”后，才更新本地配置与修订。
- 同一草稿的保存重试沿用 `request_id`，便于恢复超时请求的保存结果；编辑内容或重新读取后建立新的保存身份。
- 包或 Schema 失效时提示关闭并重新打开。连接异常、JSON 错误或服务端校验失败均不清空输入。
- HTML 自包含，无 CDN、网络请求、动态代码求值、Node/Electron API。Schema 和错误信息只以文本显示。安全隔离仍必须由宿主的 sandbox、CSP 和绑定配置桥执行；这个 HTML 不能代替宿主权限校验。

## 验证边界

客户端仓库的 `tests/settings/resource-plugin-template.test.mjs` 执行本文件内的真实 JavaScript，通过保存按钮的 `click()`（不直接伪造 `submit`）经既有 MessageChannel 桥验证无参数显示、语言/主题切换保留草稿、版本冲突与显式重读，以及无越权字段。设置 `PLUGIN_TEST_ROOT` 和 `PLUGIN_TEST_PYTHON` 后，还会通过真实管理 CLI 安装本示例与测试参数副本，校验并保存配置。

这些是 HTML/桥测试和真实 CLI 验证，不是已登录客户端页面、模型任务或最终运行效果的验收。该示例不要求修改 Box-Agent 核心代码，也不需要重新打包宿主；由插件安装/应用机制加载。
