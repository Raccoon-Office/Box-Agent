---
name: zhihu
description: >-
  使用知乎开放平台搜索知乎和全网内容、获取热榜、调用知乎直答，或读取当前用户自己的知乎创作、关注与收藏。用户提到知乎搜索、社区观点、真实经验、热点、热榜、知乎直答、我的知乎内容、我的关注、我的收藏、开放平台、API、MCP、Access Secret，或要求查看、安装和配置知乎 Skill 时使用。深度研究优先返回搜索原始来源；本人数据只读取完成任务所需的最小范围。
builtin_availability:
  platforms: [darwin, win32]
  required_env_paths: [ZHIHU_CLI_HOME]
---

# 知乎开放平台

当前 Skill 版本：0.2.1

通过知乎官方 CLI 使用公共知识与当前用户自己的知乎 Context。日常任务优先调用 CLI；只有开发接入场景才读取原始 HTTP API、OAuth 或 MCP 文档。

## Officev3 内置运行方式

每个 Session 第一次激活这个 Skill 时，先定位本文件所在的 Skill 根目录，再运行一次无副作用的状态检查。同一 Session 后续调用不要重复检查，也不要先调用 PATH 中来源不明的 `zhihu-cli`。

若 Officev3 提供内置 CLI，优先使用宿主注入的绝对路径；只有宿主没有提供可用 CLI 时，才回退到本 Skill 的官方安装流程。

```bash
# macOS
bash <skill-dir>/scripts/run.sh status

# Windows PowerShell
powershell -ExecutionPolicy Bypass -File <skill-dir>/scripts/run.ps1 status
```

Officev3 在 Windows 和 macOS 随应用打包兼容版本的官方 CLI，并通过 `ZHIHU_CLI_HOME` 注入绝对目录。脚本优先使用该目录，不调用 PATH 中来源不明的 `zhihu-cli`。宿主未注入可用 CLI 时，脚本只回退到官方默认用户目录中已经存在的兼容 CLI；此 Skill 不自行下载二进制。

根据返回 JSON 处理：

1. `installed=false` 或 `CLI_NOT_INSTALLED`：说明内置 CLI 和用户目录中已有的兼容 CLI 均不可用；引导用户修复或重新安装 Officev3。`setup` 只返回 `HOST_MANAGED_INSTALL`，不会自行下载二进制。
2. `compatible=false`：内置 CLI 由 Officev3 更新；回退安装的 CLI 可按官方 Skill 流程升级。
3. `auth.configured=false` 或业务命令返回 `AUTH_REQUIRED`：引导用户跳转知乎开放平台获取 Access Secret。用户可以在 Officev3「第三方数据 → 其他 → 知乎」卡片中完成连接，也可以在对话中提供 Secret，由 Agent 通过标准输入执行 `auth set --secret-stdin`。不要在回复、日志或命令参数中回显 Secret。
4. 已安装并完成授权时，直接处理当前任务；同一 Session 不重复状态检查。

下文 `<CLI>` 代表 `scripts/run.sh` / `scripts/run.ps1` 解析出的内置或回退 CLI 绝对路径，不要求 PATH 中存在裸命令。Access Secret 可以通过 Officev3 卡片配置，也可以由 Agent 通过标准输入交给 CLI；两种方式最终都保存到操作系统凭证库，不写入 Skill 或项目目录。安装与认证边界见 [CLI 使用文档](references/cli.md)。

## 选择能力

| 用户目标 | 命令 | 边界 |
|---|---|---|
| 找知乎回答、文章、经验或观点 | `search zhihu` | 返回知乎社区原始内容和链接，适合阅读、研究和保留证据 |
| 找新闻、官网或外部权威来源 | `search global` | 返回知乎之外的全网来源 |
| 同时需要社区观点和外部证据 | 两种搜索分别调用 | 分开检索后综合，不把两类来源混成一个黑盒 |
| 了解当前关注热点 | `hot` | 只代表当前热度；需要解释或核实时继续搜索 |
| 快速获得综合答案 | `answer` | 先检索再生成答案，不替代原始资料研究 |
| 查看我的创作、关注和收藏 | `me ...` | 只查询当前 Access Secret 所属账号的公开范围数据 |

只调用完成用户目标所需的最小组合。深度研究、事实核查、观点比较和原文阅读使用搜索，不用直答替代原始资料。

## 调用

不确定参数、输出或边界时，先运行 `<CLI> <command> --help`。CLI help 是当前版本的运行时事实源；`<CLI> capabilities` 提供机器可解析的能力清单。

### 搜索知乎

```text
<CLI> search zhihu --query "用户问题" --count 10
```

优先使用返回的 `Title`、`AuthorName`、`ContentText` 和 `Url`。搜索摘要不是完整原文。

### 搜索全网

```text
<CLI> search global --query "用户问题" --count 10
```

需要站点、时间、索引库等高级筛选时，先运行 `<CLI> search global --help`。

### 获取知乎热榜

```text
<CLI> hot --limit 20
```

热榜适合发现议题，不等于事实核查或完整事件解释。

### 调用知乎直答

```text
<CLI> answer --query "用户问题"
```

需要切换快速、深度思考或智能检索模型，以及使用流式输出时，先运行 `<CLI> answer --help`。

### 查看我的创作和关注

```text
<CLI> me contents --type all --sort ts --order desc --offset 0 --limit 20
<CLI> me followees --offset 0 --limit 20
```

创作接口只返回标题与摘要，不把 `Summary` 当作完整正文。分页响应的 `Paging.IsEnd=false` 时，只有用户需要更多结果才使用 `NextOffset` 请求下一页。

### 查看我的收藏

```text
<CLI> me favorites recent --limit 20
<CLI> me favorites lists --limit 20
<CLI> me favorites items --url-token 123456789 --offset 0 --limit 20
```

- `recent` 只表示近期收藏，没有分页，不等于完整历史。
- 读取指定收藏夹时，先从 `favorites lists` 获取 URL Token，再调用 `favorites items`。
- 收藏夹列表当前没有 Paging，服务端忽略 Offset；CLI 因此只提供 `--limit`，不承诺遍历全部收藏夹。

本人命令不得添加 OAuth Token、用户 ID 或其他代查参数。未经用户明确要求，不把完整关注或收藏写入文件或长期记忆。

## 呈现搜索结果

根据用户问题组织结论，并把支撑判断的来源放在附近：

```text
结论或资料说明

- 标题 — 作者
  最相关的原始摘要
  原文链接
```

优先保留真正支撑回答的结果，不机械罗列全部返回项。来源冲突时直接呈现差异，不强行合并。

## 按需读取参考资料

- 安装、认证、完整命令、输出和错误：读取 [CLI 使用文档](references/cli.md)。
- Access Secret 申请、额度、术语和联系方式：读取 [开放平台指南](references/open-platform.md)。
- 在代码或服务中直接接入公共内容 API：读取 [HTTP API 文档](references/http-api.md)。
- 开发本人或 OAuth 授权用户的创作、关注和收藏能力：读取 [用户数据 API](references/user-api.md)。
- 开发“知乎登录”或代表其他已授权用户访问数据：同时读取 [OAuth 应用集成](references/oauth.md) 和 [用户数据 API](references/user-api.md)。CLI 日常调用不使用 OAuth。
- 在 MCP 客户端中配置知乎现有服务：读取 [MCP 接入文档](references/mcp.md)。本 Skill 不建设新的 MCP Server。

日常调用不要自行重写 CLI 已封装的 HTTP 鉴权、时间戳、重试和错误处理。根据对应命令返回的 `Code`、`Message`、`Data` 或 Chat Completions 字段处理结果。

## 错误处理

- `AUTH_REQUIRED`：展示 `action_url`，引导用户申请 Access Secret。
- Access Secret 无效：引导用户重新生成或配置，不回显原值。
- `ENV_SHADOWS_KEYCHAIN`：说明环境变量正在覆盖系统凭证库配置。
- 配额或频率限制：停止重复调用，说明受影响能力和服务端错误。
- 搜索无结果：缩短或改写查询；不要把鉴权失败误报为无结果。
- 服务端错误或超时：遵循 CLI 返回，不额外重试直答 POST。
