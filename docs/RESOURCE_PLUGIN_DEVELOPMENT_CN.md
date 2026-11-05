# Box-Agent Skill 插件开发指南

本文面向独立业务团队和负责开发的 Agent。目标是：只修改自己的插件目录，完成一个可以安装、配置、加载、使用、停用和验证的插件。平台维护者仍负责提供兼容宿主、依赖环境和隔离测试客户端。

适用范围是本仓库的资源型插件：`schema_version: 1`、`plugin_api: 1`、HTML `bridge_version: 1`。当前支持 Skill、包内资源和 HTML 配置页；不支持外部 Tool、Hook、Worker 入口、常驻任务或持久 Workflow。业务流程可以写成 Skill，借助宿主已有工具执行；这不增加新的工具注册或任务调度接口。

## 1. 开发完成的标准

开发文档、静态检查和业务效果分别解决不同问题，交付时必须同时给出相应证据。

| 层次 | 要证明的事 | 通过标准 |
| --- | --- | --- |
| 作者自检 | 包格式、配置、Skill 结构、实例隔离是否符合实际加载契约 | 本文自检命令返回 0；保存 JSON 报告；报告中的未验证项仍未完成 |
| 宿主集成 | 真实应用能否承载页面、应用配置、加载和停用能力 | 在隔离开发 App 中逐项完成第 7 节；读取实际生效状态，不以点击成功代替生效 |
| 业务效果 | 模型是否使用正确实例与配置，产物是否满足需求 | 完成至少一个正常任务、一个配置变化任务、一个失败或禁用场景；核对调用记录和最终产物 |

只有第一层通过时，状态应写“作者自检通过，待宿主与业务验收”。不能写“插件已适配可用”。平台维护者接收的交付物清单见[验收记录模板](RESOURCE_PLUGIN_ACCEPTANCE_TEMPLATE.md)。

建议阅读顺序：本指南 → [可复制示例](../examples/resource-skill-plugin/README.md) → 按需查[宿主机制说明](RESOURCE_PLUGINS.md)。PPT 是复杂参考包，首次开发从最小示例开始。

## 2. 插件与宿主分别负责什么

```text
业务团队维护的插件目录
  plugin.yaml           声明身份、Skill、依赖、配置页
  config.schema.json    定义配置类型、默认值和约束
  ui/config.html        编辑本实例的配置
  skills/*/SKILL.md      定义触发条件、方法、输入输出、失败处理
  skills/*/resources    模板、脚本、字体等只读资源
          │ 安装：校验、复制、计算摘要
          ▼
宿主包存储 + 实例配置
  保存：生成待应用修订，不改变运行中的任务
          │ 宿主应用：等待空闲、重启、验证
          ▼
已生效快照 → 原有 SkillLoader / SkillRuntime → Agent 使用已有工具
          │
          └─ 普通用户的 Skill 使用开关（独立于插件配置）
```

- **插件作者**负责包内容、参数含义、Skill 方法、产物校验和自有脚本错误处理。
- **平台维护者**负责宿主接口、已有工具与权限、依赖环境、安装发布、重启回退和客户端适配。
- **业务使用者**决定是否使用公开 Skill。关闭使用开关不卸载插件、不修改插件配置。
- HTML 只能读、校验、保存当前实例的配置。安装、启停、应用和回退由页面外的宿主管理界面负责。

插件资源包不在加载时执行 Python 入口，也不启动 worker。Skill 需要运行脚本时，通过宿主已有且获准使用的工具执行，继承其权限检查。资源包是可信内部交付物；只读文件和 iframe 不构成对恶意代码的操作系统级沙箱。

## 3. 从最小示例开发第一个插件

### 3.1 准备环境并复制

获取包含本指南、自检脚本和示例的 Box-Agent 源码，使用仓库规定的 `uv` 环境。下面命令都在仓库根目录执行，输出放在 Git 忽略的 `workspace/` 中；不要修改已安装的只读包。

```sh
uv sync
mkdir -p workspace/plugin-authoring
cp -R examples/resource-skill-plugin workspace/plugin-authoring/team-brief
uv run python scripts/check_resource_plugin.py workspace/plugin-authoring/team-brief --json
```

复制的示例是**无参数**简报 Skill，可以原样通过作者自检。此命令不会把它安装到正在使用的 App；自检在临时目录完成，退出后删除临时安装状态，不需要模型凭据。

已有可用依赖环境时可在 `uv run` 后加 `--no-sync`，避免本次检查同步依赖；新环境应先完成 `uv sync`。模板附带 [MIT LICENSE](../examples/resource-skill-plugin/LICENSE)，复制和分发时保留；新增资源分别记录其许可。对外交付前将示例 README 改为本插件说明，修正指向原仓库的相对链接。

接下来把它改成有参数插件。示例团队 ID `com.example.team-brief` 仅用于演示，交付时换成团队自己的 ID。

### 3.2 修改包声明

将复制目录中的 `plugin.yaml` 替换为：

```yaml
schema_version: 1
id: com.example.team-brief
version: 0.1.0
display_name: 团队简报
description: 根据用户提供的材料整理简报，不进行外部检索。
requires:
  box_agent: ">=1.0.0 <2.0.0"
  plugin_api: 1
runtime:
  kind: resources
config_schema: config.schema.json
ui:
  config:
    entry: ui/config.html
    bridge_version: 1
contributes:
  skills:
    - id: brief
      path: skills/brief
      config_keys: [tone, max_items]
```

宿主开发版本目前可能同为 `1.0.0`，但具备不同功能。因此这个版本范围只是加载约束，不能证明任意名为 1.0.0 的运行时均兼容；交付时还要记录实际宿主构建与包摘要。使用 `enabled_by` 的包尤其需要支持该字段的宿主。

### 3.3 定义参数

将 `config.schema.json` 替换为：

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "properties": {
    "tone": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"},
    "max_items": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3}
  },
  "required": ["tone", "max_items"],
  "additionalProperties": false
}
```

宿主校验配置并按 Schema 补充支持的属性默认值。建议使用这里展示的简单属性 Schema；不要依赖条件分支自动填入默认值。配置总量上限 64 KiB。`config_keys` 必须对应根 `properties` 中的字段，只有选中的字段进入该 Skill 的上下文。

当前桌面新安装以空对象 `{}` 作为初始配置。希望用户直接从桌面安装时，必填属性应具有合法默认值，使空对象可被规范化；否则安装会在打开 HTML 前失败。确实不能提供默认值的必填参数，应先通过 CLI 的 `plugins.install.config` 提供完整初始配置，再到开发页面编辑。

**配置不用于存储凭据。** 当前是本地配置存储，HTML 可读取整个实例配置；`config_keys` 只是上下文投影限制，不是秘密存储或权限边界。不要填写 access token、API key、个人用户数据或生产账号密码。需要凭据的能力应使用平台已有凭据通道，先与平台维护者确认。

### 3.4 定义 Skill 的行为

将 `skills/brief/SKILL.md` 替换为：

```markdown
---
name: brief
description: 将用户已经提供的材料整理为团队简报，区分事实、推测和待确认事项。
---

# 团队简报

适用于用户要求整理已给材料的简报。没有材料时先索取材料，不编造事实。

宿主会在本 Skill 前提供 Plugin instance settings JSON：
- values.tone 是默认表达方式：concise 简洁表达，detailed 补充材料中已有的依据。
- values.max_items 是默认最多列出的主要事项数量，不是最低数量。
- 用户本次明确要求及本任务已确认的选择优先于这些默认值。

1. 确认材料、受众和明确的输出要求。
2. 用一段话概括材料，再按参数列出主要事项；不得补造输入中没有的事实。
3. 如存在推测或待确认事项，明确标注。
4. 检查事项数量和事实来源。默认直接在对话中回答，不创建文件、不访问网络。

没有执行外部检索，不得声称已经外部核实。
```

`name` 和 `description` 必须是非空字符串。建议 `name` 与贡献 ID 相同，同一包内不可重复。实际模型调用名由宿主按实例产生，不由这个 `name` 全局占位。

`description` 要写清触发条件；正文要写清输入不足、参数作用、工具需求、输出和验收标准。“知道有某个配置字段”不代表模型会正确使用，参数与行为的关系必须在正文中定义。

### 3.5 保留 HTML 模板并自检

原样保留 `ui/config.html`，它会根据新 Schema 展示 JSON 编辑器，无需另外开发表单。用下面两组配置检查默认值和非默认值：

```sh
uv run python scripts/check_resource_plugin.py workspace/plugin-authoring/team-brief --json
```

在**包目录之外**创建 `workspace/plugin-authoring/brief-detailed.json`：

```json
{"tone":"detailed","max_items":5}
```

```sh
uv run python scripts/check_resource_plugin.py workspace/plugin-authoring/team-brief \
  --config workspace/plugin-authoring/brief-detailed.json --json
```

预期退出码为 0。再将 `max_items` 改为 0，预期退出码为 1 并报告配置错误。将值恢复为 5，供后续真实任务验收使用。配置、检查报告及产物不要写进包目录，否则会改变包摘要并可能被一起分发。

## 4. Manifest 字段与组合规则

### 4.1 声明字段

未知字段会被拒绝；不要把后续阶段的设计字段先放进去。

| 字段 | 必填 | 含义与约束 |
| --- | --- | --- |
| `schema_version` | 是 | 固定为 `1`，描述 Manifest 格式 |
| `id` | 是 | 包 ID；小写字母开头，用点、下划线或连字符分段，如 `com.team.brief`；不是实例名 |
| `version` | 是 | 三段版本，可带预发布或构建后缀；内容变更时更新版本，实际安装同时锁定内容摘要 |
| `display_name` / `description` | 否 | 列表中的名称与用途；建议始终填写 |
| `requires.box_agent` | 是 | 空格连接的版本比较，如 `>=1.0.0 <2.0.0`；不支持 `^`、`~`、`*` |
| `requires.plugin_api` | 是 | 固定为 `1`；不是 HTML 桥版本 |
| `requires.runtime_profile` | 否 | 引用管理员配置的依赖检查方案；不能在插件内声明安装命令 |
| `requires.plugins` | 否 | 当前仅允许空对象；尚不支持跨插件依赖声明 |
| `runtime.kind` | 是 | 固定为 `resources` |
| `config_schema` | 是 | 包内 JSON Schema 文件路径，根必须是 `type: object` 且 `additionalProperties: false` |
| `ui.config.entry` / `bridge_version` | 是 | 包内 HTML 路径；桥版本为 `1`；无参数插件也要提供说明页 |
| `contributes.skills` | 是 | 1～128 项；每项至少包含唯一 `id` 与唯一 `path`，路径指向含 `SKILL.md` 的目录 |
| Skill 的 `config_keys` | 否 | 本 Skill 可见的顶层参数列表，默认没有参数投影 |
| Skill 的 `requires_host_tools` | 否 | 需要的**已有**宿主工具名；不会注册代码或提升权限 |
| Skill 的 `enabled_by` | 否 | 同实例公开入口贡献 ID 的列表；多个入口按 OR 控制此方法可用性 |

实例名和贡献 ID 使用 `^[a-z][a-z0-9_]{0,63}$`。路径使用包内相对路径和 `/`，不得包含 `..`、绝对路径或符号链接。主要大小限制：Manifest 256 KiB、单个 Schema 512 KiB、HTML 和单个 Skill 正文各 2 MiB、任一包文件 64 MiB、整包 512 MiB / 20,000 文件。

Schema 可以使用包内相对 `$ref`，不能请求远程 Schema，也不能自定义 `$id`。Schema、HTML 和正文都使用 UTF-8。

### 4.2 包、实例、贡献、别名与展示名称

以团队简报为例：

| 名称 | 示例 | 谁决定 |
| --- | --- | --- |
| 包 ID | `com.example.team-brief` | 插件作者 |
| 实例 ID | `sales_brief` / `research_brief` | 部署者；两个实例可以使用不同参数 |
| 贡献 ID | `brief` | 插件作者；实例内唯一 |
| 贡献展示名 | `SKILL.md` frontmatter 的 `displayName: 团队简报` | 插件作者；不参与调用或权限判断 |
| 模型调用名 | 宿主生成的 `p__...` 名称 | 宿主；不要自行拼接 |
| 可选公开别名 | `sales_summary` → `brief` | 部署者通过 `aliases` 绑定 |
| 用户使用偏好身份 | 包 ID + 实例 ID + 贡献 ID | 宿主；显示名或别名变更不应改变偏好身份 |

部署默认暴露全部声明的 Skill。`expose` 决定贡献是否进入运行时，不等于是否在用户技能页展示。两个实例不能占用相同别名，一个贡献也只能对应一个别名；系统保留名称不能覆盖。首次开发不设置别名即可。

`plugin.yaml` 的 `display_name` 是**包的管理标题**；每个公开 Skill 的用户展示名写在其 `SKILL.md` 顶层 `displayName`，二者独立。`displayName` 是去掉首尾空格后不超过256个字符的非空单行字符串；缺失或非法时回退到该文件原始 `name`，不会回退到插件命名空间调用名。ACP目录以 `displayName` 传递这个字段，`name` 仍是实际调用名，`plugin` 中的包/实例/贡献身份保持不变。客户端展示作者名，并用实例 ID 区分多个安装；选择、使用开关和权限校验仍使用宿主身份，不可用展示名发起调用。

作者展示名与描述按原文显示，不要求宿主自动翻译。宿主已提供的PPT标题/简介翻译仅匹配明确支持的 `com.box.ppt` 包及其 `pptx` 贡献；任意第三方贡献使用相同ID，不因此获得内置身份或翻译。不要通过配置额外alias来修饰普通用户看到的名称。

### 4.3 一个公开入口与多个内部方法

在已有包中增加 `skills/format/SKILL.md`，并在 `contributes.skills` 中增加：

```yaml
    - id: format
      path: skills/format
      enabled_by: [brief]
      config_keys: [tone]
```

内部方法的 frontmatter：

```yaml
---
name: format
description: 为团队简报整理内部版式。
metadata:
  user_visible: false
---
```

此时 `brief` 仍是普通用户可见入口，`format` 不显示在用户技能列表，但仍需包含在 `expose` 中，才能由模型读取。`enabled_by: [brief]` 表示用户关闭入口后，内部方法也不能继续调用。它不是“入口自动加载辅助方法”的声明。

如入口一定依赖该方法，在入口 frontmatter 写 `required_skills: [format]`；如果只是可选方法，可写 `related_skills: [format]`。这两个列表引用原 frontmatter 名称，宿主会进行实例内映射。将必需的 Skill 方法一起放在包内，不要依赖部署环境中碰巧存在的内置或其他插件 Skill。正文中需要指定调用名称时使用 `{{skill:format}}`，宿主将其替换为同一实例的实际名称。

`enabled_by` 引用必须存在、公开可见且已暴露，不允许自引用或循环。多个控制入口按 OR 生效。`metadata.user_visible: false` 仅隐藏入口，不能替代使用权限或保密控制。

`{{skill:...}}` 只在 `SKILL.md` 正文中展开；其他 Markdown、脚本和模板文件通过文件工具读取时不会替换。它们需要使用宿主提供的 `skill_names` 映射，不能硬编码另一个团队或实例的别名。

### 4.4 工具、脚本、上下文与日志

Skill 使用宿主已有工具时，在 `requires_host_tools` 声明必需工具，如 `read_file`、`bash`，并在目标宿主确认实际名称及授权。声明只做可用性要求，不能安装工具；frontmatter 的工具描述也不能代替该检查。子 Agent 的工具范围可能更小，主 Agent 可用不代表子 Agent 可用。

资源路径以已加载 Skill 的目录为基准，采用相对路径；不要写开发机器绝对路径。脚本、模板和字体位于只读安装包，输出、缓存及中间文件必须写任务工作目录。包安装会忽略 `.git`、`.venv`、`node_modules` 等目录，不能靠夹带这些目录分发运行环境。依赖由平台预装，通过 `requires.runtime_profile` 检查；不要让模型执行临时 pip/npm/浏览器安装。

Skill 上下文包含包与实例身份、配置修订、generation、选中的 `values` 和 `skill_names`。配置是默认数据，用户明确要求和本任务已确认选择优先；不得把配置拼接成 shell/Python 代码或额外授权指令。普通资源文件不会自动进入上下文，按需使用文件工具读取。

宿主管理审计记录操作、身份、修订和摘要，不记录配置值；本地保留最近 256 条，它不是插件业务日志服务。业务脚本日志放在任务输出中，附退出码、产物路径和必要错误信息，不输出凭据。平台 SessionLog 保留会话及工具记录；当前没有供资源插件注册独立日志后端或后台上下文服务的接口。

### 4.5 将已有脚本 Skill 改造成插件

只增加 Manifest 不足以完成迁移。先复制业务源码和资源到新插件目录，保留原 Skill 不变；核对下表后，再从**安装后的只读包**运行脚本。

保留原 Skill 也包括保留现有用户默认可发现、可调用的入口，不能只留下源码却先从宿主发现清单中删除。平台完成替代包分发、依赖准备和实际激活之前，单独升级宿主必须继续提供原有能力。作者自检通过不等于用户已经迁移；发布验收还须覆盖第 8 节的升级矩阵。

| 原 Skill 常见行为 | 插件迁移要求 |
| --- | --- |
| 在 Skill 内运行 `npm install`、创建 `.venv` | 由平台管理员在包外准备依赖；删除模型任务中的自动安装步骤，缺依赖时报告准确错误。安装器排除这些目录，不会把已安装依赖带入包。 |
| 使用 `__file__` / `import.meta.url` 附近目录写缓存 | 这些位置只能读资源。字体下载、临时文件、日志、浏览器缓存和恢复状态使用任务输出目录或管理员明确提供的可写目录；不得假设进程 cwd 可写。 |
| Python 导入在包内生成 `__pycache__` | 平台启动或脚本子进程设置 `PYTHONDONTWRITEBYTECODE=1`，或使用 Python 的 `-B`。 |
| ESM 脚本直接 `import ... from '依赖名'` | 核对实际解析路径；ESM bare import 不使用 `NODE_PATH`。支持 CommonJS 的依赖可使用下文 `createRequire` 示例；不要在包中创建 `node_modules` 符号链接。 |
| 从当前目录或其他应用自动寻找 `.env`、账号配置 | 使用平台明确提供的当前宿主凭据通道；测试限定隔离 profile。缺少授权或配置时停止，不搜索其他客户端的登录数据，不把凭据放进 HTML 配置或产物。 |
| 支持断点恢复或保存运行参数 | 新配置默认只影响新任务。恢复以原任务保存的参数、输入和源码版本为准；源码有指纹清单时同步更新，并记录升级兼容性，避免重做已成功的付费调用。 |

目录名 `runtime` 本身不是应排除项；已有 Skill 可能把完整业务源码放在其中。根据文件用途迁移，保留源文件、模板和相对路径，排除依赖、缓存、个人配置及输出。不要把示例的 MIT 许可扩展到没有该许可的业务源码、字体或模板；分别记录原作者、来源、摘要、修改和许可范围。

**参数不会自动传给程序。** `values` 和 `skill_names` 是宿主提供给模型的上下文数据，不是自动创建的文件、环境变量或脚本参数。Skill 正文应明确哪些字段对应哪些命令行选项，模型读取生效配置并结合用户要求后调用脚本；脚本仍需校验类型和范围。需要跨进程传递时，可把非秘密设置写入任务目录的 JSON 文件，由插件自己的启动脚本读取。这个文件是插件约定，不是新增宿主接口。

例如图片任务将 `max_corrections` 映射为 `--max-corrections`。下面仅演示预算参数的构造；具体插件还需补齐模型等业务必需参数，不能把片段当作完整图片任务入口。`values` 是已读取的非秘密配置，`explicit` 是用户本任务明确选择，`entry`、`input`、`output` 由调用方提供：

```javascript
import {spawnSync} from 'node:child_process';

export function runTask({values, explicit = {}, entry, input, output}) {
  const corrections = explicit.max_corrections ?? values.max_corrections;
  if (!Number.isInteger(corrections) || corrections < 0 || corrections > 10) {
    throw new Error('max_corrections must be an integer from 0 to 10');
  }
  return spawnSync(process.execPath, [entry, '--input', input, '--output', output,
    '--max-corrections', String(corrections)], {stdio: 'inherit', shell: false});
}
```

路径作为独立 argv 传入；调用方需检查子进程退出码。不要将 JSON 值拼入 shell 命令，不允许设置字段替换可执行文件、脚本路径或额外权限。恢复入口应单独使用已保存参数，不能直接套用这个新任务示例。

**可安装默认值与业务运行前置条件可以分开。** 例如外部图片分层测试插件的 `recognition_model`、`image_model` 是非秘密模型 ID，Schema 默认均为 `""`，允许 `{}` 安装；新任务和 `--check-auth` 必须显式取得两个非空 ID，均最长 200 字符、不以 `-` 开头且无空白/控制字符。`--settings` 可用于这两种入口，`--model` / `--image-model` 优先于 settings；纯离线 `--check` 不需要模型。配置页应以文本字段和能力提示说明识别模型需图像输入、结构化 JSON 与坐标，生图模型需图像编辑、参考图、mask/遮罩和透明 PNG 等能力；字段合法不证明远端模型可用。endpoint 和鉴权继续来自当前宿主，不加入 URL 或密钥配置。恢复沿用原任务模型和指纹，拒绝新 settings 或模型覆盖；这些是业务脚本的明确契约，不由宿主自动完成。该业务仅用于独立迁移测试，不随宿主正式插件源码或客户端分发。

### 4.6 为脚本插件准备宿主依赖

Manifest 仅引用 profile 名称；profile 的内容属于管理员维护的**宿主配置**，不放在插件业务配置里。以下是没有浏览器需求的图片脚本示例，需按真实依赖修改；完整宿主配置示例见 [config-example.yaml](../box_agent/config/config-example.yaml)。

桌面验收先通过正常客户端流程初始化模型配置并完成一次最小普通任务，再向现有配置合并 runtime profile。不要先创建只有 `plugin_platform` 的 `config.yaml`：文件已经存在可能使客户端跳过模型默认配置初始化，导致应用候选因缺少模型凭据而启动失败。下列片段不是可直接覆盖宿主配置的完整文件。

```yaml
# plugin.yaml 的 requires 中增加：
# runtime_profile: image-layer-host-v1

# 以下写入隔离宿主的配置，由管理员与已有 runtime_profiles 合并：
plugin_platform:
  runtime_profiles:
    image-layer-host-v1:
      python_modules: [PIL, fontTools, numpy, requests]
      node_modules: [sharp, yaml]
      playwright_chromium: false
      timeout_seconds: 20
```

`python_modules` 填导入名（例如 Pillow 对应 `PIL`）；`node_modules` 填模块名。`timeout_seconds` 为每个探测进程的超时，范围 1～60 秒。当前 Python 探测使用 `find_spec` 检查模块可发现，Node 探测使用 CommonJS 解析模块路径；不验证这些模块的版本、原生二进制实际加载或全部业务行为。设置 `playwright_chromium: true` 才会额外启动 Chromium 并检查页面截图。通过 profile 不能代替脚本自己的依赖加载和最小功能测试。

平台在启动宿主及其工具环境中提供 `BOX_AGENT_NODE` 和 `BOX_AGENT_PYTHON`（或 `BOX_AGENT_PYTHON3`）绝对路径。非冻结源码宿主未指定 Python 时使用自身解释器；冻结运行时不能依赖该回退。Node profile 必须有 `BOX_AGENT_NODE`。外部 Node 依赖目录可通过 `NODE_PATH` 提供；脚本若使用 `TEXT_PYTHON`、`python3` 或其他解释器，管理员必须核对它与 profile 探测的环境一致，宿主不会自动解释插件自定义的环境变量。

**PPT 隔离环境还需明确 Node 前缀。** 当前 PPT 包的一些环境检查/导出脚本先向模块搜索路径加入 `BOX_AGENT_NODE_PREFIX/node_modules`，次选 `BOX_AGENT_RUNTIME_PREFIX/node_modules`；两者都没有时会使用正式 `office-raccoon` 用户目录。它们不会根据 `BOX_AGENT_HOME` 改这个默认前缀。因此做 PPT 回归时，应把 `BOX_AGENT_NODE_PREFIX` 设为本轮管理员准备的独立 Node 依赖根目录（其下含 `node_modules`），同时核对 `NODE_PATH` 和探测输出中的 managed node_modules 路径。仅设置 `BOX_AGENT_HOME` 或 `NODE_PATH` 不能证明 PPT 脚本的依赖查找已经隔离。这是当前 PPT 资源的兼容行为，不是新插件应仿照的默认路径。

对同时支持 CommonJS 的依赖，ESM 源文件可以这样使用管理员准备的 `NODE_PATH`：

```javascript
import {createRequire} from 'node:module';
const requireHost = createRequire(import.meta.url);
const sharp = requireHost('sharp');
```

多个脚本共用一个依赖适配模块，子进程继承同一环境。仅能以 ESM 形式加载的依赖需由平台另行明确入口解析方式；不要假定这个示例覆盖所有包。管理员在独立依赖目录按插件锁文件准备环境，记录版本与安装来源；包内只交付依赖声明、锁文件及管理员说明，不携带依赖树。

**实际工具环境必须单独核对。** 桌面启动进程继承的 `NODE_PATH` 不一定原样进入 Bash；宿主可以按当前会话重新组装工具环境。管理阶段只校验包完整性、Skill 和 runtime profile 声明，不使用父进程环境判定依赖是否可用。CLI 与 ACP 在真实启动确认时共用同一规则：从实际工具集合中的原生 Bash 取得只读环境快照，再执行依赖探针；显式快照不补回工具已移除的父进程变量。有 runtime profile 但没有真实 Bash 环境时，候选失败并保留旧 active；普通启动发生同类失败时只降级外部 Skill、标记运行状态异常。没有 runtime profile 的纯文本插件不要求 Bash。后续 session env_context 或 shell 仍可能改变执行环境，因此应用通过不能代替业务脚本依赖检查。先在实际会话使用宿主已有的 `runtime_environment` 检查 `NODE_PATH`、`NPM_CONFIG_PREFIX`、Python 路径及可写工具目录，再由管理员向该环境可见的包外位置准备依赖，或使用宿主支持的正式配置。不要为通过预检改变业务脚本去搜索正式客户端目录，也不要把本机绝对路径写入插件。

上线前至少验证：依赖缺失时不自动安装；安装后脚本可读完整资源；执行后包摘要不变；字体等缓存和业务输出均在包外；两个实例参数独立且不会共享可变业务状态。认证检查与纯离线依赖检查应分开，避免 `--help`、本地导出等离线操作要求模型登录。

业务测试插件应放在宿主仓库之外的独立工程，随测试交付物附上源码、构建脚本、迁移实录和验收证据；不要因接入验证而纳入宿主正式 `plugins/` 目录。图片分层采用这一边界，宿主正式插件目录只保留 PPT。通用作者流程仍从[最小资源插件示例](../examples/resource-skill-plugin/README.md)开始；独立业务项目的源码、自检、离线输出与真实客户端效果分别记录，不能互相替代。

## 5. HTML 配置页与 CLI 对接

### 5.1 优先复用配置页模板

通用模板已包含 JSON 编辑、校验、保存、冲突保留草稿、显式重读、中英日及主题切换。无参数插件使用示例的闭合空对象 Schema，页面显示说明；不能把 `{}` 当成无参数 Schema。

如确需专用表单，保留[示例 README 的配置桥契约](../examples/resource-skill-plugin/README.md#配置桥契约)：

1. 接收父窗口 `box-plugin-config:init`，检查来源、桥版本、nonce 和唯一 MessagePort。
2. 在私有端口回复带相同 nonce 的 `box-plugin-config:ready`。
3. 只调用 `config.get`、`config.validate`、`config.save`。
4. 响应 `context.changed` 更新文字和主题，保留草稿。
5. 保存冲突不覆盖草稿，不自动重试覆盖服务器值；由用户显式重读。

页面不拥有实例身份、安装路径或通用管理权限。配置桥由宿主绑定当前实例和摘要；不得自行传入另一实例 ID。不得读取登录凭据、调用 Agent、访问 Node/Electron 或加载远程 CDN。保存用 `type="button"` 的点击事件；iframe 不允许原生表单提交。不要为此放宽 sandbox 或 CSP。

页面的附属资源放在 HTML 同目录或子目录，不引用包内其他目录。宿主支持 CSS、JavaScript、JSON、常用图片及字体等静态资源，最多 256 个附属资源，HTML 加附属资源合计最多 8 MiB。自检读取宿主实际可提供的资产，但不执行页面，也不会完整检查 HTML/CSS 中的引用；资源能否在页面正确加载仍需真实 UI 验收。

直接双击 HTML 文件只能检查静态外观，没有配置桥；它会显示未连接，不能算配置功能已通过。

### 5.2 CLI 与 HTML 请求不是同一种调用层

独立 CLI 使用真实管理协议：

```sh
BOX_AGENT_HOME=/absolute/isolated-plugin-test uv run python -m box_agent.plugin_management <<'JSON'
{"method":"plugins.list","params":{}}
JSON
```

`BOX_AGENT_HOME` 必须是专用测试目录。还应检查当前终端是否继承了 `BOX_AGENT_PLUGIN_STATE_DIR`、`BOX_AGENT_PLUGIN_TARGET_ID`、`BOX_AGENT_PLUGIN_PROFILE_ID`、`BOX_AGENT_PLUGIN_ACTIVATION_ID` 等覆盖项；使用它们时必须与隔离宿主一致，不能误指正式客户端。作者自检脚本不使用这份实际部署状态。

以下为请求结构示例，绝对路径、修订、摘要及请求 ID 均需从实际操作中取得：

```json
{"method":"plugins.install","params":{"instance_id":"sales_brief","package_path":"/absolute/team-brief","enabled":false,"config":{"tone":"concise","max_items":3},"request_id":"install-sales-brief-v1"}}
```

先 `config.get` 读取 `saved_revision`、`package_digest` 和 `schema_digest`，再校验、保存：

```json
{"method":"config.get","params":{"instance_id":"sales_brief"}}
{"method":"config.validate","params":{"instance_id":"sales_brief","config":{"tone":"detailed","max_items":5},"package_digest":"<config.get 返回值>","schema_digest":"<config.get 返回值>"}}
{"method":"config.save","params":{"instance_id":"sales_brief","config":{"tone":"detailed","max_items":5},"expected_revision":1,"package_digest":"<config.get 返回值>","schema_digest":"<config.get 返回值>","request_id":"save-sales-brief-edit-1"}}
```

每次 CLI 进程只接受一条请求；上面三行分别执行，不能一并输入。`expected_revision: 1` 仅演示格式，必须替换为实际读取值。**HTML 桥会代为绑定摘要，独立 CLI 不会，不能直接复制 HTML 的 params。** 成功响应为 `{"ok":true,"result":...}`；失败为 `{"ok":false,"error":{"code":...,"message":...,"details":...}}`，退出码非零。`config.validate` 还需检查 `result.valid`，请求成功不代表配置通过校验。

修改启用状态同样使用当前 `saved_revision`：

```json
{"method":"instance.set_enabled","params":{"instance_id":"sales_brief","enabled":true,"expected_revision":2,"request_id":"enable-sales-brief-1"}}
```

这只是保存期望状态。由隔离开发 App 执行“应用”，等待空闲、重启并确认；`plugins.list` 可核对 `desired_enabled`、`active_enabled`、`saved_revision`、`active_revision` 和 `restart_required`。

纯 CLI 宿主的受控启动应由平台适配器完成：读取最新 profile 修订 → `profile.prepare_apply` → 将返回的 `result.activation_id` 赋给真实宿主进程的 `BOX_AGENT_PLUGIN_ACTIVATION_ID` 环境变量，在同一 profile 下启动 → `activation.status` 读取结果。核对 `activation.status` 为 `active`、`runtime.healthy` 为 `true`，并确认 runtime 与 activation 的 generation 一致。`profile.prepare_apply` 会做 Skill 与运行环境声明的静态校验，成功仅表示准备好候选；依赖可用性由真实启动的工具环境探针确认。公共 CLI 不允许调用 `profile.confirm_apply`；不得通过内部 Python 方法伪造真实启动成功。

## 6. 作者自检命令的能力与边界

```sh
uv run python scripts/check_resource_plugin.py /absolute/plugin \
  --config /absolute/nonsecret-config.json \
  --instance-id team_brief \
  --alias team_summary=brief \
  --json
```

只有包路径必填。默认使用空配置并应用 Schema 默认值；别名可不填。`--host-version` 只用于检查版本约束，不会切换执行代码，也不证明该版本的其他构建具有相同能力。

脚本复用实际包安装、配置服务和 Skill 加载计划，在自动清理的临时目录中完成：

1. 校验包、Schema、HTML 文件和不可变安装内容。
2. 使用实际修订和摘要校验、保存配置。
3. 安装第二个无别名实例，检查模型名称及引用隔离。
4. 冻结候选并校验包完整性，构建真实 Skill 加载计划；解析 frontmatter、依赖、配置投影及 `enabled_by` 结构。
5. 读取页面资产，列出需要的宿主工具和 runtime profile。

报告标记 `scope: authoring_static_preflight`。退出码 0 表示这些检查通过，1 表示检查失败，2 表示命令参数错误。它**不执行 HTML JavaScript、不执行插件脚本、不启动 Agent、不调用模型、不检查真实目标的工具权限或运行环境，也不激活或回退正在使用的客户端**。宿主依赖与功能效果出现在未验证列表中。

每项 Skill 的 `projection_verified` 表示实际加载内容中的参数和身份与冻结配置一致，`reference_binding_verified` 表示正文引用绑定到同一实例，`reference_count` 是实际替换的引用数量。检查失败不会标为通过；报告不回显参数值或完整正文。这些字段证明宿主准备的输入正确，不证明模型已遵循输入完成任务。

不要把 `profile.preflight` 的包与配置检查误认为完整启动验证。自检工具另外构建 Skill 计划，但仍不是实际客户端启动。自检输出应保存到包目录外并随验收记录交付。

## 7. 隔离开发 App 与业务验收

平台维护者应提供明确版本的隔离开发 App/启动脚本、测试 profile 和宿主依赖。`RACCOON_PLUGIN_DEVELOPER_TOOLS=1` 控制桌面管理入口；**这个开关本身不会隔离数据目录，也不是员工身份认证**。普通版本找不到管理入口是预期行为，不要为了测试修改正式客户端数据。

按以下顺序验收，失败时保留观察结果，不把“按钮点击成功”当作目标已完成：

| 操作 | 预期结果 |
| --- | --- |
| 安装新实例，保持停用 | 有实例与配置页；Skill 尚不可用 |
| 修改参数、校验、保存 | Schema 错误可见；成功后显示待应用，当前任务不变 |
| 启用并应用 | 等待在途任务；真实重启成功后 active 与 saved 修订对应，目录中出现公开 Skill |
| 打开详情并选择 Skill | 详情可读，选中的是正确插件实例，辅助方法不伪装成公开入口 |
| 用户关闭公开入口 | 公开条目仍可管理，但不可使用；`enabled_by` 内部方法也不可调用；重启保持偏好 |
| 用户重新开启 | 可选择并使用，不要求修改插件参数或 generation |
| 开发页停用实例并应用 | 插件能力退出可用目录；已打开的技能页、重新进入页面均检查；不能回落到残留同名旧 Skill |
| 再启用并应用 | 恢复正确实例和参数；其他正常能力可用 |
| 模拟错误配置或失败候选 | 不能冒充已生效；确认上一可用状态及宿主正常能力仍可恢复 |
| 回退上一已确认快照 | 经过再次受控重启；核对实际版本、generation、配置和可用性 |
| 中英日／亮暗主题切换、配置冲突 | 页面更新且保留草稿；冲突后显式重读，不默默覆盖 |
| 普通模式再启动 | 管理页与接口不可用，已启用的公开 Skill 仍能正常管理使用偏好 |

团队简报至少做以下真实模型任务：

- **默认参数**：提供含 6 条事实的固定材料，要求“整理团队简报”，观察是否使用本实例、读取 `max_items=3` 并最多列 3 项。
- **参数生效**：改为 `detailed/5`，保存但未应用时核对旧生效状态；应用后用新会话执行同一输入，核对配置修订和最多 5 项。详细程度用事先写明的业务标准评估，不能只看模型自述。
- **用户优先**：在默认最多 3 项时明确要求 2 项，预期遵循用户要求。
- **失败边界**：未提供材料时不编造；关闭入口后不能通过同实例辅助 Skill 绕开；再开启恢复。

记录 Skill 读取/选择证据、实例 ID、配置修订、工具调用和最终输出。无模型凭据时停在集成前或调用前，将业务效果标为未验证；不得生成一个模拟回答来冒充模型验收。PPT、表格等插件需要实际打开或渲染产物，检查可编辑对象与内容，文件存在不足以验收。

脚本自行调用模型时，还要区分 HTTP 成功与业务产物通过。请求模型 ID 与服务响应标签应分别记录；标签差异本身不证明业务响应不可用，也不证明两个 ID 是等价别名。外部图片分层测试案例将标签不同记为 warning，继续校验结构化响应和图像结果，不仅因标签差异失败、重发或自动换模型。标签相同也不证明底层身份，服务未报告时保留 `unreported`。

以下是该业务脚本的诊断格式示例，不是宿主自动提供的插件接口：单次响应的 `modelDiagnostics` 包含 `model`、`returnedModel`、`modelIdentity` 和可选 `modelWarning`；根 `result.json.modelDiagnostics` 汇总平铺的 `requestCount`、`matched`、`mismatch`、`unreported` 以及 `warnings`、`requests`，没有 `counts` 子字段，不保存认证 headers。作者应在交付说明中提供与实际脚本一致的字段和脱敏示例。

仍须保留 HTTP 状态、结构化响应校验、图层数量、视觉复核与导出检查，不能因文件存在就判成功。一个图片样本即便完成文字和几何重建，如果图像生成 API 调用为 0，仍不能据此声称线上生图能力通过。每个业务分支、配置变化和失败场景都应记录实际覆盖范围。修正预算需说明按图、按轮还是按任务计量；手动恢复可再次尝试失败项时，应与自动恢复预算和已完成请求的复用规则分别说明，不能把某个配置值当作整个任务生命周期的请求总上限。新版本的源码、运行包、客户端和真实模型验证需单独记录，不能继承旧版本通过或失败的结论。

## 8. 升级、失败与恢复

### 8.1 升级约定

内容修改后提升插件版本，重新自检与验收，再安装固定的新包。安装后修改源目录不会更新已安装包；不要直接改包存储。Schema 变更必须说明旧配置如何迁移，当前平台没有插件自定义迁移脚本。

对同一实例执行 `plugins.install` 更新时，必须带 `expected_revision`，并显式带上要保留的 **config、enabled、aliases、expose**。更新是整实例替换，省略字段会采用安装默认值：`enabled=false`、空配置经默认值规范化、空 aliases、暴露全部贡献，**不会自动继承原实例**。可先用 `config.get` / `profile.export` 备份待保存配置与绑定；导出内容也应按配置敏感性保护。当前桌面安装表单不应被当作完整升级/卸载工具。

升级成功安装后仍需应用与真实验证。回退恢复的是上一已确认快照，不会自动把较新的待保存编辑抹掉；应分别核对 active 与 saved。停用保留别名占位，避免同名旧能力重新出现。

#### 从内置能力迁移到独立插件

宿主 wheel 和独立运行时保留 PPT 的内置兼容资源；`plugins/ppt/` 是另行分发的插件包，升级 core 不会自动安装或启用它。当前兼容入口共八个：`pptx`、`ppt-fast`、`sn-ppt-entry`、`sn-ppt-story`、`sn-ppt-standard`、`sn-ppt-dazzle`、`sn-ppt-tools`、`sn-ppt-doctor`。

只有在固定插件包和宿主依赖已经交付、八个贡献全部暴露、按 [PPT README](../plugins/ppt/README.md) 完整绑定上述八个名称，并经真实宿主重启确认激活后，才能称为完成 PPT 迁移。仅绑定部分名称或不绑定别名属于并存部署：已绑定名称由插件接管，未绑定的内置入口继续存在，插件也可通过实例命名空间提供入口。不能把“安装成功”或“插件目录可见”当作迁移完成。

| 部署或启动场景 | 必须保留或禁止的行为 |
| --- | --- |
| 新装 core，或仅升级 core；插件存储为空 | 八个内置 PPT 兼容入口仍可发现、可调用；原有用户开关和权限检查继续生效。 |
| 包已安装、配置已保存或候选已准备，但未激活 | 继续使用上一 active 状态；此前只有内置能力时继续走内置路径。 |
| 完整八别名迁移并确认激活 | 八个公开名称全部指向插件；隐藏对应内置副本。 |
| 部分别名或无别名部署并确认激活 | 只接管实际绑定的名称；未绑定内置入口保留，记录为并存部署。 |
| 候选校验或真实启动失败 | 保留上一 active 快照；此前未启用插件的用户仍保有内置能力。 |
| 实例停用并应用 | 该实例别名继续占位且不可用，不重新暴露同名内置或用户 Skill。 |
| active 快照可读，但包校验或运行时加载失败 | 保留快照中已知别名的屏蔽并报告错误；未绑定的内置能力继续可用。 |
| 插件状态无法可信读取 | 保守屏蔽宿主声明的兼容入口并报告错误；不能当作空存储而让旧 PPT 自动复活。 |
| 显式设置 `BOX_AGENT_DISABLE_PLUGINS=1` 进入安全模式 | 绕过外部状态和包，明确恢复内置兼容路径；仍受原有用户开关和权限限制，不改写插件状态，不代表插件迁移通过。 |

损坏状态的保护名单来自生成的宿主内置 Manifest 的 `plugin_compatibility_names`，当前是上述八个 PPT 名称；它不是插件作者可在 `plugin.yaml` 添加的字段。只有无法可信读取状态时才使用该名单，快照可读时使用其具体别名；宿主兼容名单自身无法验证时应修复运行时，不能用空名单继续回退。

**发布门槛：默认升级路径必须保有既有能力。** 平台维护者负责替代包、依赖和客户端的分发组合，并分别验证源码、构建产物、安装后冷启动以及实际业务调用。至少要测“仅升级 core + 空 active 状态”，不能只测已导入开发配置的环境。无自动迁移时，保留原有兼容入口及资源，并记录维护责任；不能靠在文档列为已知问题来替代兼容保障。只有支持范围内的升级路径都能交付并激活替代能力后，才另行评估移除兼容资源。

### 8.2 常见错误

| 现象或错误 | 定位与处理 |
| --- | --- |
| `unsupported_feature` / `invalid_manifest` | 检查是否写入 Tool/Hook/入口代码字段、拼错字段或使用不支持的新字段；不要修改宿主校验绕过 |
| 安装成功，准备应用失败 | 安装不等于 Skill 正文可用；检查 frontmatter、重复名称、依赖、引用、暴露列表和别名 |
| `invalid_config` | 根据错误路径修正字段、类型、范围或必填值；保留草稿 |
| `stale_package` | 包或 Schema 摘要不匹配；重新获取配置，页面关闭重开；不要沿用旧编辑器身份 |
| 修订冲突 | 重新读取最新配置/修订并处理差异；新的内容使用新的 request_id；同一提交重试保持同一 ID |
| `alias_conflict` 或准备时别名错误 | 去掉重复/保留名称；一个贡献只绑定一个公开别名 |
| 修改源文件后没有变化 | 重新打包、安装并应用；运行时读取已安装冻结内容 |
| 保存“停用”后仍可见 | 先检查是否仍待应用，再区分“用户使用开关”和“实例停用”；不可只看按钮文字 |
| 目录里有 Skill，但读取被拒绝 | 检查用户开关、入口联动、会话工具范围和权限；目录可见不授予调用权限 |
| 缺少 runtime profile 或依赖 | 平台维护者修复宿主环境，再应用；插件不自行安装环境 |
| 包被改动或状态损坏 | 保留错误和摘要，交由平台恢复已知备份；不要手改状态文件伪造修订 |

候选以整个 profile 为单位校验，失败不发布部分新快照；保留上一 active 状态。运行时发现包损坏时，当前实现可能关闭该 profile 的整组外部 Skill，不能承诺只隔离单个坏插件。宿主正常能力与外部 Skill 状态要分别检查，并遵守上表对已知别名及状态损坏时兼容入口的屏蔽规则。

必要时由平台以 `BOX_AGENT_DISABLE_PLUGINS=1` 启动安全模式，再使用独立管理通道从已知备份恢复。安全模式是显式恢复内置兼容路径的操作，区别于普通模式下禁止因状态损坏而自动恢复旧能力；它不会改写插件状态或重放中断的业务操作。插件有外部业务副作用时，业务团队要另行定义重试、幂等与补偿，资源插件的配置回退不会撤销那些副作用。

## 9. 交付与文档维护

交付：固定插件目录或压缩包（安装前解压）、README、参数说明、依赖与兼容信息、变更和迁移说明、许可证/资源来源，以及填写完成的[验收记录](RESOURCE_PLUGIN_ACCEPTANCE_TEMPLATE.md)。README 必须说明用途、触发方式、输入输出、普通用户开关和开发配置的区别。

不提交真实配置、凭据、用户会话、缓存、日志和模型产物到插件包。交付包的摘要必须与验收包一致；改动后重新检查，不复用旧摘要的通过记录。

### 9.1 压缩包摘要与安装包摘要

交付记录分别保存两个值，不能互相替代：

- **归档 SHA-256**：整个 ZIP 文件字节的摘要，用于确认下载/传输的文件；压缩方式或归档元数据变化就可能改变它。
- **`package_digest`**：宿主对解压后可安装文件的相对路径、是否可执行、长度及内容摘要计算的值，不是 ZIP SHA-256，也不是单个文件的 SHA-256。以作者自检或真实安装返回的值为准。

打包及解压必须保留文件的可执行标志。只检查文件字节相同仍不够：例如把可执行脚本从 `0755` 解压成 `0644` 会改变 `package_digest`。PPT 构建器在 ZIP 的 Unix 属性中记录权限，但不同解压程序未必恢复；Python `zipfile.extractall()` 不会自动恢复这些权限。使用能保留归档权限的工具或经过验证的提取流程，不要通过修改只读包存储来修补。

分发验收步骤：构建归档并记录 SHA-256 → 解压到包存储之外的全新目录 → 对**解压目录**重新运行 `scripts/check_resource_plugin.py ... --json` → 将返回的 `package_digest` 与原始待交付目录自检报告比较 → 从该解压目录安装并核对安装摘要。差异需在打包/提取环节修复，重新验收后再分发。解压也要拒绝路径逃逸、符号链接等非法条目；不要仅为保留权限而信任任意归档路径。源目录自检通过不等于已验证发给同事的压缩包。

### 9.2 开发 Agent 与维护约定

给开发 Agent 的任务可以使用下面的完整约束：

```text
请按 docs/RESOURCE_PLUGIN_DEVELOPMENT_CN.md 开发资源型 Skill 插件。
从 examples/resource-skill-plugin 复制，只修改指定插件目录。
先说明业务输入、输出、公开入口、参数和验收任务，再编写文件。
不修改 Box-Agent 或前端源码，不添加本阶段不支持的 Tool/Hook/Worker 声明。
复用 HTML 模板，不把凭据放进配置，不硬编码宿主机器路径或实例调用名。
运行 scripts/check_resource_plugin.py，修复到自检通过，并验证一条非法配置。
提供插件包和填写后的 RESOURCE_PLUGIN_ACCEPTANCE_TEMPLATE.md。
将作者自检、真实宿主、真实模型、最终产物的验证状态分别列出。
不能访问测试 App/模型时明确标未验证，不伪造 activation 确认或业务效果。
若业务要求超出资源 Skill 能力，列出所缺宿主接口，不通过修改核心绕过。
```

平台维护者变更 Manifest、加载器或 HTML 桥时，应在同一变更中更新示例与指南，运行作者自检回归，并安排一个未参与实现的开发者/Agent 仅依本文完成新插件。已知内部实现知识不能代替文档中的步骤。

实现依据：[Manifest 校验](../box_agent/plugins/manifest.py)、[包安装](../box_agent/plugins/package_store.py)、[配置服务](../box_agent/plugins/settings_service.py)、[Skill 投影](../box_agent/plugins/skill_sources.py)、[公开管理入口](../box_agent/plugin_management.py)。字段以这些实现为准；若指南与实现不一致，应修复契约及文档，而不是要求每个业务团队自行猜测。
