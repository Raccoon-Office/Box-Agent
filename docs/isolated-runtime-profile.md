# 独立运行时 profile：BOX_AGENT_HOME

`BOX_AGENT_HOME` 是显式、进程级的 Box-Agent 自有状态根。宿主需要在启动 Python/独立 runtime **之前**传入一个独立的绝对目录；它不修改系统 `HOME`，也不迁移、复制或读取旧 profile 的数据。

不设置该变量时，原配置搜索顺序、`~/.box-agent` 默认路径和功能默认值保持不变。空值、相对路径、系统根或用户 home 及其祖先目录作为显式值时直接报错，不按“未设置”处理。路径按宿主平台的 `pathlib.Path` 解析；不能在已导入运行时的同一个进程中切换 profile，一些既有工具常量在 import 时确定。

## 配置入口

显式 profile 只从 `<root>/config/config.yaml` 加载主配置。缺文件即失败，不自动生成、不从 cwd 开发配置、旧用户目录或打包的 config.yaml 兜底。

仅 `system_prompt.md`、`analysis_prompt.md`、`code_prompt.md` 三种不可变提示资源可以从安装包目录补充；MCP、auth、模型档案及自定义配置没有这个回退。显式 config/auth/memory/default-workspace/MCP 路径必须解析在 profile 内；相对覆盖路径以 profile 为根，已存在的越界符号链接会被拒绝。Config 校验异常的文字不会附带整份配置输入，避免错误信息夹带凭据；调用方仍不应主动 dump 配置对象或完整 Pydantic errors 输入。

开发环境可参考以下步骤（示例文件无真实凭据）：

```sh
BOX_PREVIEW_ROOT="$(mktemp -d)"
mkdir "$BOX_PREVIEW_ROOT/config"
cp box_agent/config/isolated-profile-example.yaml "$BOX_PREVIEW_ROOT/config/config.yaml"
BOX_AGENT_HOME="$BOX_PREVIEW_ROOT" uv run --no-sync --offline python -m box_agent.acp.server
```

以上命令在本源码仓库中执行，使用当前 checkout，不从 PATH 猜测可能较旧的 box-agent-acp。宿主程序应自行创建并保护 profile 目录（例如 macOS/Linux 的私有临时目录），再设置子进程环境。Windows 同样传入一个本机绝对目录，不套用 POSIX 路径或更改 USERPROFILE。

打包宿主必须先核验 runtime 的构建来源确实包含本改动；不能通过试启动旧 binary 来检测它是否支持这个变量，因为旧 binary 可能忽略它。改源码不会更新已安装客户端，版本号相同也不是支持证明。

## 路径归属

| 自有内容 | 显式 profile 默认位置 |
| --- | --- |
| 配置、默认 auth、MCP、模型/工作区/Skill/Obsidian 设置 | `config/` |
| Agent 上下文持久化与长工具结果 | `sessions/` |
| Agent 日志与诊断 trace | `log/`、`log/sessions/` |
| 记忆 | `memory/` |
| 默认工作区 | `workspace/` |
| 用户 Skills / hooks | `skills/`、`hooks/` |
| Python sandbox 与运行时安装包 | `sandbox/`、`runtime-packages/` |
| 自管理 Node、Office 稳定运行时回退位置 | `runtimes/node/`、`box-agent-runtime/` |
| Skill 子进程缓存 / 浏览器缓存 / 删除备份 | `skill-tools/`、`browsers/`、`trash/` |
| CLI 历史与 Goal 元数据 | `.history`、`goals/` |

统一解析由 `box_agent/user_paths.py` 提供。原权限引擎的“引擎内部数据”例外只指向当前 profile，不同时授予旧 `~/.box-agent` 的访问例外。用户显式指定的 session 工作区、项目内 `.box-agent` task registry/scratch、普通文件路径的 `~` 语义不被改写，仍由原工作区/权限逻辑控制。

日志、trace、模型档案、auth、Obsidian 设置和 Skill 缓存的已有路径覆盖在显式 profile 下也须位于该根内；不设置 profile 时原覆盖行为保持。MCP 配置工具继续跟随 loader 的实际配置文件，但会拒绝指向 profile 外的旧路径。用户 Skill 根位于 profile，builtin 相对目录只从安装包查找，不再自动扫描 cwd。

Box-Agent 管理的 stdio MCP（包括内置 Web Extract）会在启动配置中显式传入当前 `BOX_AGENT_HOME`，覆盖该管理项中残留的旧 profile 值，并保留其他环境配置。自定义 MCP 和远端 HTTP 服务配置不受此规则影响；未启用 profile 时保留原行为。

## 凭据与背景能力

- 显式 profile 不回退读取 `BOX_AGENT_AUTH_TOKEN`、Office/Raccoon 等旧登录 token 环境变量；只接受显式调用参数或 profile 内 auth 文件。LLM/config 中显式提供的 key 仍按原协议处理。
- 不自动导入全局 `~/.openclaw`，也不因此写“已导入/无内容”标记。
- 根目录设置本身**不会关闭模型、MCP、Skill、hook 或记忆维护**。`isolated-profile-example.yaml` 另外关闭记忆、提取/维护、MCP、Skill 和 hooks 等后台配置；它只是无后台启动的样例，不代表所有内置工具都不再注册。
- 上述启动示例使用 ACP 入口。普通 CLI 的 API 验证和显式任务调用仍保留原行为，不能把设置 profile 当作“禁止网络/模型调用”的开关。
- 宿主仍应使用环境白名单，显式提供必要的 executable/只读 runtime 依赖，不继承不相关凭据、Python 搜索路径、MCP/第三方 CLI 配置或连接器。已有显式二进制路径不在此机制中自动搬动。
- 这不是 OS sandbox、网络防火墙或用户文件访问授权。第三方 SDK/Skills/CLI 自己使用的缓存、系统 Jupyter 配置等不能靠改 Box-Agent 的根目录来统一约束。正式接入还需独立验证启动配置、工具权限、输出策略和子进程树清理；不应仅凭该环境变量宣称生产隔离完成。

## 验证与回退

新测试覆盖默认路径兼容、严格配置搜索、缺失配置、路径越界/符号链接、凭据与 OpenClaw 边界、权限内部目录例外、MCP 实际路径一致性和 Node 缓存选择。新进程测试在导入前设置 profile，并用 Python audit hook 拦截旧用户状态访问；使用假 LLM 完成 ACP 单轮与 SessionLog 恢复。启动测试使用假连接/假 LLM 和禁用后台的样例配置完成 initialize，禁止网络 connect。

2026-09-08 源码验收：436 项相关测试通过，旧用户状态访问审计为 0；9 个纯路径替换模块反向还原后的非 import AST 与改动前一致。没有运行全仓真实网络/模型测试；两条 tar 解包弃用提示来自既有测试路径，未在本批夹带修改。

所有这些都属于源码/受控测试证据，不是已打包或已安装 runtime 的真实模型验证。本分支不改版本、不安装、不迁移生产数据。回退宿主的 opt-in 只需停止该测试进程并移除子进程 `BOX_AGENT_HOME` 配置；旧 profile 未被改写。不要把测试目录直接当成生产数据迁移结果。

本轮未刷新 Understand Anything 图谱；新路径模块及导入关系以当前源码为准。


## 托管 LLM 登录检查

小浣熊托管地址使用占位 API key 时，会在发送模型请求前检查登录态。普通配置仍支持显式 token、auth.json 和既有登录环境变量；设置 BOX_AGENT_HOME 后仍禁止环境变量 token 回退。文件中的 token 刷新机制保持不变。

缺少可用登录信息时立即提示「未登录，请通过客户端登录后再试」；刷新接口返回 401 时提示「登录态已过期，请重新登录」。这两种需要用户登录的错误不会自动重试，也不会发出裸模型请求；临时网络或服务端刷新错误仍使用既有重试策略。显式真实 API key 和非托管地址不受此检查影响。
