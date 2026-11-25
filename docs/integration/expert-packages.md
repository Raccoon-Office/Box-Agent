# 专家与专家团云端接入

本文说明宿主通过 ACP 向 Box-Agent 提供专家包下载引用的接口。宿主负责专家目录、用户选择和下载票据；Box-Agent 负责下载、校验、缓存、装配规则和检查当前运行环境中的依赖。专家目录不随 runtime 发布，`initialize` 不返回可选专家清单。

技能来源、身份约束与权限的实现说明见 [Skill 引擎设计](../design/skill-engine.md#专家与专家团技能)。ACP 恢复及历史回放契约见 [ACP SDK 对接](acp-sdk-0.12.md)。

## 1. 能力协商与兼容

支持下载引用的运行进程在 `initialize` 响应中声明以下扩展。示例仅展示响应片段：

```json
{
  "result": {
    "_meta": {
      "expert_package_download": 1
    }
  }
}
```

宿主确认该值为整数 `1` 后，才能使用本文的 `packageDownload` 格式。当前不需要在 `host_capabilities` 中回传同名开关；该声明也不表示市场技能、工具或连接器已经可用。

若字段缺失或版本不受支持，宿主应沿用已验证的完整专家对象协议，或提示更新 runtime。不能直接把下载引用或仅含 `id` 的对象发给旧 runtime，也不能只根据客户端版本推断服务端支持情况。

新 runtime 继续接受完整专家对象。完整对象中的规则、`skillBindings`、`packageSnapshot` 等仍按原协议处理；其中资源路径必须在运行 Box-Agent 的机器上可用，桌面本地路径不能直接作为云端资源路径。没有专家选择的普通会话保持原行为。

## 2. 新建会话

以下为 JSON-RPC 请求。示例中的 URL、摘要、大小、ID 和工作目录均为占位数据，接入时必须替换。`digest` 和 `size` 必须来自同一份实际 ZIP；示例不包含真实下载签名。

### 单专家

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "session/new",
  "params": {
    "cwd": "/workspace/review",
    "mcpServers": [],
    "_meta": {
      "session_id": "chat-review-001",
      "expert": {
        "id": "catalog-reviewer",
        "name": "代码审查专家",
        "source": "builtin",
        "packageDownload": {
          "downloadUrl": "https://desktop-skills.tos-s3-cn-shanghai.volces.com/examples/reviewer.zip",
          "digest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
          "size": 4096
        }
      }
    }
  }
}
```

### 专家团

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "session/new",
  "params": {
    "cwd": "/workspace/team-review",
    "mcpServers": [],
    "_meta": {
      "session_id": "chat-team-001",
      "expert_team": {
        "id": "catalog-review-team",
        "name": "代码审查专家团",
        "source": "builtin",
        "packageDownload": {
          "downloadUrl": "https://desktop-skills.tos-s3-cn-shanghai.volces.com/examples/review-team.zip",
          "digest": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
          "size": 8192
        }
      }
    }
  }
}
```

推荐使用 `expert_team`；兼容别名为 `expertTeam`，一个请求选择一种写法。团长、成员及工作流由团队包解析，宿主不需要在下载引用里重复传入。

### 下载引用字段

下表中的路径相对于 `expert` 或 `expert_team` 对象：

| 字段 | 要求与含义 |
| --- | --- |
| `id` | 必填，宿主目录中的专家或专家团 ID；1–80 个 ASCII 字母、数字、`.`、`_`、`-`，首字符必须为字母或数字。它与包内 `packageId`、Agent ID、Skill ID 不是同一个概念。 |
| `name` | 必填，非空显示名称。 |
| `source` | 下载引用仅支持 `builtin`；省略时按 `builtin` 处理。完整对象的旧来源规则不受影响。 |
| `description`、`starterPrompt`、`tags` | 可选展示元数据；前两项为字符串，`tags` 为字符串数组。 |
| `packageDownload.downloadUrl` | 必填，官方允许来源的 HTTPS URL，可包含服务端签发的查询参数；不得含用户名、密码或 fragment。 |
| `packageDownload.digest` | 必填，ZIP 原始字节的 SHA256，64 位小写十六进制。 |
| `packageDownload.size` | 必填，ZIP 字节数，正整数且不超过 256 MiB。 |
| `packageDownload.id` | 可选；提供时必须等于外层 `id`。通常省略即可。 |

下载来源目前仅允许以下主机，端口须为默认 HTTPS 或 `443`；重定向目标也执行相同检查：

- `sta-volc.xiaohuanxiong.com`
- `desktop-skills.tos-s3-cn-shanghai.volces.com`
- `desktop-skills-pre.tos-s3-cn-shanghai.volces.com`

运行端直接访问下载 URL，不从客户端取得 Cookie 或额外鉴权头，也不继承环境代理配置。票据必须可由目标云端访问；需要签名时，由宿主业务接口取得可用的签名 URL。

同一对象不能混用 `packageDownload` 与完整配置字段，例如 `instructions`、`visibleRules`、`leader`、`members`、`defaultSkills`、`optionalSkills`、`skillBindings` 或 `packageSnapshot`。仅传 `{"id": "catalog-reviewer"}` 也不受支持；运行端不会根据 ID 查询业务目录。

## 3. 包准备、依赖与成功回执

ZIP 必须包含原始专家包结构：`.box-agent-plugin/plugin.json`、指令资源及 `files.json`。manifest 的 `schemaVersion` 为 `1.0`；单专家的 `expertType` 为 `agent`，团队为 `team`。支持包文件直接位于 ZIP 根目录，或位于一层外包目录中；必须能唯一定位包 manifest。

包校验包括 ZIP 大小和摘要、文件清单中的摘要及声明大小、资源路径和包类型。解压后总大小上限为 1 GiB、单文件上限为 256 MiB、ZIP 条目上限为 20,000；符号链接及路径越界会被拒绝。BUNDLED 技能随包提供；HUB 技能通过 `dependencies.lock.json` 对齐 `key`、`skillId` 和 `slug`，转换为运行时 MARKET 绑定。

必需 HUB 绑定缺少锁定记录时拒绝准备；可选 HUB 绑定没有锁定记录时会被略过，不根据名称猜测身份。已有锁定记录与定义不一致时仍视为包错误。

缓存位于运行端 profile 的 `experts/packages/<digest>` 下，默认 profile 为 `~/.box-agent`，可由 `BOX_AGENT_HOME` 指定。同一 profile 中相同 digest 可复用校验后的缓存；缓存命中仍会校验现有内容，发现损坏时返回错误，不自动删除或重新下载。任务工具不能修改该共享缓存，任务产物应写入工作区。缓存共享不等于会话状态共享。

每个下载引用的准备阶段配置了 120 秒超时。宿主的请求超时应为下载、解压校验、依赖检查及失败清理预留余量，不把 120 秒当作整个 ACP 请求的严格完成上限。

准备完成后再检查实际会话能力：

- 必需/default 技能、其传递依赖及请求的工具必须在当前会话可用。工具注册、执行模式和连接器选择等限制仍然生效。
- 包中 `required: false` 的技能进入所属专家的 `optionalSkills`，缺少它本身不阻止启动；一旦成为必需技能的依赖，仍需通过启动预检。
- 专家包下载不会自动安装 HUB/市场技能。宿主需通过已有安装或资源同步流程，将技能和匹配的 `.skill-installation.json` 安装凭据放入云端实际技能来源；不能只复制同名目录或伪造标记。
- MARKET 的 `skillId` 在读取、依赖解析及子 Agent 委派时继续校验。可选入口的依赖身份不符时，拒绝该入口，不拿其他专家的同名技能正文替代；目录或安装标记变化后重新判断。

成功响应的 `_meta.expert_context` 是本次生效选择的摘要，可含 `expert`、`expert_team`，以及冲突可选项的 `unavailable_optional_skills` 诊断。它不是完整专家配置或下载票据，不能直接拿来恢复配置。宿主只在请求成功后更新当前专家选择；包准备、团队声明或必需依赖预检失败时，已有活动会话保留。

## 4. 恢复与重新选择

宿主应保存业务专家 ID、所选包 digest 等选择信息，并在 `session/load`、`session/resume` 中重新提供下载引用或完整对象。需要时为同一 digest 获取新票据。两种恢复入口复用新建时的准备和依赖校验；省略专家元数据会按无专家选择装配，不会从历史自动恢复原选择。

以下 `sessionId` 必须替换为此前 `session/new` 返回的值，按不透明 ID 原样保存和回传；`cwd` 保持原会话目录：

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "session/resume",
  "params": {
    "sessionId": "acp-v1-Y2hhdC1yZXZpZXctMDAx",
    "cwd": "/workspace/review",
    "mcpServers": [],
    "_meta": {
      "expert": {
        "id": "catalog-reviewer",
        "name": "代码审查专家",
        "packageDownload": {
          "downloadUrl": "https://desktop-skills.tos-s3-cn-shanghai.volces.com/examples/reviewer.zip",
          "digest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
          "size": 4096
        }
      }
    }
  }
}
```

需要回放历史时，将方法改为 `session/load`，其他选择字段相同。`load` 会回放历史；`resume` 默认不回放，显式请求增量回放的规则沿用 ACP SDK 对接文档。恢复请求可省略 `_meta.session_id`；提供时必须与返回 ID 对应的原业务会话一致。

`session/prompt` 不重新准备专家资源，也不能通过临时传入专家元数据切换选择。切换应通过会话重新绑定流程进行，并等待成功回执；有正在执行的回合时按已有会话忙碌规则处理。

## 5. 错误与宿主处理

以下专家准备错误通过 JSON-RPC `error` 返回，协议错误码为 `-32602`。宿主按 `error.data.code` 分支处理，`message`、`detail` 和 `reason` 用于诊断，不作为稳定枚举。

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "error": {
    "code": -32602,
    "message": "Invalid expert selection",
    "data": {
      "code": "EXPERT_PACKAGE_HASH_MISMATCH",
      "detail": "Expert package SHA256 or size does not match its ticket"
    }
  }
}
```

| `error.data.code` | 含义 | 宿主处理建议 |
| --- | --- | --- |
| `EXPERT_PACKAGE_INVALID_REQUEST` | 引用字段、下载来源或混合配置不合法 | 修正请求，不能原样循环重试。 |
| `EXPERT_PACKAGE_REFERENCE_REQUIRED` | 只提供了专家 ID | 获取下载引用，或发送完整配置。 |
| `EXPERT_PACKAGE_DOWNLOAD_FAILED` | 网络/HTTP 失败或重定向不满足下载要求 | 检查云端可达性；票据失效时获取同一 digest 的新票据，有限重试。 |
| `EXPERT_PACKAGE_HASH_MISMATCH` | 实际下载大小或 SHA256 与票据不符 | 核对票据与 ZIP 是否对应，重新取得一致的分发信息，不绕过校验。 |
| `EXPERT_PACKAGE_INVALID` | 包格式、类型、文件完整性、路径或资源准备失败 | 检查分发包与运行端诊断，修复后再试。 |
| `EXPERT_PACKAGE_CACHE_INVALID` | 已有缓存被修改、缺失或路径非法 | 通过运维流程处理受影响 digest 的缓存后重试；更换 URL 本身不会修复缓存。 |
| `EXPERT_PACKAGE_PREPARATION_TIMEOUT` | 包准备超时 | 检查网络、磁盘与包体积，排除原因后有限重试。 |
| `SKILL_VALIDATION_TIMEOUT` | 会话技能目录刷新超时 | 稍后重试并检查技能存储；不能视为依赖就绪。 |
| `EXPERT_DEPENDENCIES_NOT_READY` | 必需技能、工具或传递依赖不可用 | 使用 `dependencies` 中的来源、名称、状态和身份信息补齐资源或调整会话能力后重试。 |
| `EXPERT_TEAM_INVALID_WORKSTREAM` | 必需分工缺少有效 `memberId` 或 `title` | 修复团队声明。 |
| `EXPERT_TEAM_INVALID_MEMBER_REFERENCE` | 必需分工引用了未定义成员 | 根据 `missingMemberIds` 修复团队声明。 |

依赖预检错误的数据结构不同于包错误。例如，下列错误表示当前专家要求的市场技能身份不匹配：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "error": {
    "code": -32602,
    "message": "Expert dependencies are not ready",
    "data": {
      "code": "EXPERT_DEPENDENCIES_NOT_READY",
      "dependencies": [
        {
          "expertId": "catalog-reviewer",
          "expertName": "代码审查专家",
          "name": "review-method",
          "source": "MARKET",
          "skillId": "market-review-method-id",
          "required": true,
          "state": "unavailable",
          "reason": "The selected Skill has no matching market installation identity."
        }
      ]
    }
  }
}
```

传递依赖错误还可能包含 `dependencyName`、`dependencyCode`，运行时身份冲突使用 `SKILL_DEPENDENCY_IDENTITY_MISMATCH`。会话创建后的读取/委派失败通过对应工具的错误结果返回，不重新发送一次会话准备错误。宿主应兼容未知业务错误码，保留原选择并展示失败状态，避免无限自动重试。普通 ACP 的会话忙碌、历史不存在等错误仍遵守原协议。

诊断材料记录业务 ID、digest、业务错误码与 runtime 标识即可；不要复制带签名的下载 URL。宿主展示的错误内容不应包含票据或访问凭据。

## 6. 验证与 runtime 发布

以下是接入和发布要求，不代表某个发布已经完成验收。PR 的 Proof/Risk 应填写实际结果、最终 Head SHA 与尚未完成的步骤。

源码回归入口：

```bash
uv run pytest tests/test_expert_packages.py tests/test_expert_package_format.py tests/test_expert_packages_acp.py tests/test_expert_dependencies.py tests/test_expert_preparation_freshness.py -q
```

这些测试覆盖 mock 下载、缓存、依赖身份和 ACP 生命周期，不能证明生产下载服务、Linux 分发包或前端集成已经可用。准备发布时按 [生产指南](../PRODUCTION_GUIDE_CN.md) 构建对应目标平台的 runtime；仓库构建入口为 `uv run box-agent-build-runtime`。本机 macOS 构建结果不能作为 Linux runtime 证据。

| 阶段 | 应记录的验证证据 |
| --- | --- |
| 最终源码 | Head SHA、所运行检查及结果；代码发生变化后重新评估受影响检查。 |
| Linux runtime 构建 | 基于最终 Head 的构建日志、目标架构、runtime 版本、产物位置与 SHA256。 |
| 云端安装与重启 | 实际安装目录、进程使用的 runtime 标识、重启完成；旧进程不会热加载新包。 |
| 已安装 runtime 探针 | 从实际新进程执行 `initialize`，确认能力声明和进程版本；再走真实下载与会话准备。 |
| 云端冒烟 | 单专家/专家团首次下载与缓存复用；必需/可选市场依赖；`new/load/resume`；一次准备失败后原会话及同进程另一会话仍可继续。 |
| 前端端到端 | 用户真实选择专家、发起任务、使用技能/委派、恢复会话及错误展示；记录客户端、服务端 runtime 和验证任务。 |

失败路径至少包含一项摘要不符或无效票据，以及一项缺少必需依赖；可选依赖不可用应能启动，实际使用时给出错误且不替代正文。还需验证旧 runtime 缺少能力声明时，客户端会回退或提示更新。

发布记录分别报告 `源码测试 → runtime 构建 → 安装 → 重启 → 探针 → 真实任务` 的完成状态。更新开发 checkout 不等于更新已安装客户端；验证停在哪一步就记录到哪一步，云端冒烟和前端验收分别留证。

## 实现与维护入口

- [ACP 适配](../../box_agent/acp/__init__.py)：能力声明、会话准备和结构化错误映射。
- [包准备](../../box_agent/expert_packages.py)：下载来源、票据、缓存及资源校验。
- [包格式转换](../../box_agent/expert_package_format.py)：原始包到专家/团队完整对象的转换。
- [专家依赖](../../box_agent/expert_dependencies.py)：有效选择、必需依赖及市场身份约束。
- [技能加载器](../../box_agent/tools/skill_loader.py)：会话目录、刷新和运行时依赖身份检查。

协议字段或错误码变化时同步更新本文示例和对应回归用例；技能选择与加载细节继续维护在 Skill 引擎设计中。
