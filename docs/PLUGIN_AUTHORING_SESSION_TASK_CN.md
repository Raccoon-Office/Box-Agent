# 新 session 任务：独立开发并验收一个 Skill 插件

## 1. 任务目标与交付标准

从本次推送的两个开发分支建立独立环境，仅根据插件开发文档，开发一个新的业务 Skill 插件，验证它能被 Box-Agent 和桌面客户端加载、配置、启停，并在真实模型任务中产生符合预期的效果。

本任务验证“另一个团队或 Agent 能否依靠文档交付插件”。不要把直接安装已有 PPT 插件、仅创建一个普通 Skill 目录，或静态检查通过当成完成。先开发插件；若发现宿主或文档问题，记录最小复现，不为通过验收自行修改核心代码。

完成后交付：

1. 可分发插件包、用途和参数 README、资源许可说明。
2. 作者自检报告，含正常配置、参数变化、非法配置拒绝和双实例检查。
3. 隔离客户端的安装、HTML 保存、应用、使用开关、实例停用/恢复及回退记录。
4. 固定输入的真实模型结果与判定，不以模型自述代替检查。
5. 填写完成的验收表，以及开发文档的缺失/歧义清单。

任何一层缺少环境或授权时继续完成其他独立工作，明确标记停在哪一层。不得伪造启动确认、登录结果或模型输出。

## 2. 固定代码与工作目录

| 组件 | 仓库 | 开发分支 |
| --- | --- | --- |
| Box-Agent | `git@github.com:wangbo0727/Box-Agent.git` | `codex/plugin-skill-html-20261028` |
| 桌面前端 | `git@gitlab.bj.sensetime.com:sensecode/raccoon-lab.git` | `codex/plugin-config-ui-20261028` |

以任务交付消息列出的两个 commit 为验收基准；本指南的永久链接也能确定所属的 Box-Agent commit。若远端分支继续变化，checkout 指定 commit，不自动切换最新 main 或下载最新 release runtime。

这两条分支保留原验收基线。提交前拉取远端时，Box-Agent 基线落后 upstream/main 59 个提交，前端基线落后 origin/main 5 个提交；这是独立开发分支验收，不代表已完成最新 main 的合并回归。

创建一个全新任务目录。下面以 `$PLUGIN_TASK_ROOT` 表示其绝对路径，替换示例路径后执行；不要复用正式客户端 checkout 或旧测试 profile。

```sh
export PLUGIN_TASK_ROOT=/absolute/path/plugin-authoring-session
mkdir -p "$PLUGIN_TASK_ROOT"
cd "$PLUGIN_TASK_ROOT"

git clone --branch codex/plugin-skill-html-20261028 \
  git@github.com:wangbo0727/Box-Agent.git Box-Agent
git clone --branch codex/plugin-config-ui-20261028 \
  git@gitlab.bj.sensetime.com:sensecode/raccoon-lab.git raccoon-lab

git -C Box-Agent rev-parse HEAD
git -C raccoon-lab rev-parse HEAD
```

将实际 SHA 与交付记录核对；如不一致，在各 clone 中执行 `git checkout <交付的commit>`。不要合并别的分支来消除差异。测试产物放在任务根目录的 `artifacts/`、`reports/`、`plugins/`，不提交凭据、用户数据或运行日志。

优先读取：

- 两个仓库的 `AGENTS.md`，以及前端 `apps/office-desktop/AGENTS.md`。
- [插件开发指南](RESOURCE_PLUGIN_DEVELOPMENT_CN.md)。
- [最小示例](../examples/resource-skill-plugin/README.md)。
- [验收记录模板](RESOURCE_PLUGIN_ACCEPTANCE_TEMPLATE.md)。
- 需要了解加载机制时阅读 [Resource Skill plugins](RESOURCE_PLUGINS.md)。

开发插件阶段先不阅读宿主实现来猜契约。文档不足时先记录具体缺口；排查阻塞时如必须看源码，记录阅读位置与原因，不能把获得的内部知识算成文档已经说明。

## 3. 环境前置与验证边界

本轮优先使用 macOS arm64。需要 GitHub/GitLab 仓库访问权限、uv、Node 22.x、pnpm 10.18.2；macOS 原生构建还需要 Xcode Command Line Tools 和 `xcrun swiftc`。Python 按仓库锁定环境准备，源码要求 Python >=3.10。

Git 不包含已生成的运行时、浏览器和正式用户配置。构建可能需要下载资源，网络及权限不通时记录失败命令。当前记录中的 V5/V9 是历史测试构建标签，不是可从版本号 `1.0.0` 自动推断出的兼容能力。

以下构建和原始 Electron 隔离启动命令已按源码核对，但提交本轮没有从全新 clone 完整执行。新 session 的工作包括实际验证这些步骤，不得把源码核对写成实机通过。

登录和模型是独立前置条件：

- 作者自检和真实管理 CLI 不需要模型或登录凭据。
- 登录后 HTML/模型验收需要平台提供可使用的隔离测试账号或明确授权的隔离认证环境。
- 当前默认 macOS 登录页仅提供网页登录；下文原始 Electron 隔离启动不会注册正式系统协议，其网页登录会被拒绝。不能写成“直接点密码登录”或绕过鉴权。
- 平台可提供完整隔离、可正常登录的测试 App；也可在独立 OS 用户/专用机器准备正常登录环境。若提供专用认证文件，应只写入本任务 profile，按授权范围使用并清理。
- 不自行读取、复制或修改正式客户端凭据，不把旧会话的本机临时登录状态当成本次 clone 的内置能力。不得把 token 写入文档、配置示例或报告。

如果尚无登录条件，完成插件开发、自检、构建和无登录检查，将真实 UI/模型层标为待环境；有独立模型配置时可另做隔离 CLI 模型验证，但它不能替代桌面 UI 验收。

## 4. 开发一个可观察效果的新插件

建议业务场景为“需求评审摘要”。也可以选择同样无外部业务依赖的文本场景，但保持下面的验收覆盖，不使用已有 PPT 包替代开发。

建议目录：

```text
plugins/review-brief/
├── plugin.yaml
├── config.schema.json
├── ui/config.html
├── skills/review_brief/SKILL.md
├── skills/extract_actions/SKILL.md
├── LICENSE
└── README.md
```

要求：

- 使用团队自有包 ID；公开贡献 `review_brief`，隐藏辅助贡献 `extract_actions`。
- 辅助方法设置 `metadata.user_visible: false`，Manifest 用 `enabled_by: [review_brief]` 关联；正文通过 `{{skill:extract_actions}}` 引用同实例方法。
- 至少提供 `detail_level`（简洁/详细）、`max_actions`（默认 3，合法范围 1～10）、`include_open_questions`（默认 true）三个参数，并定义可观察的输出差异。
- Schema 的默认值使 `{}` 能规范化为合法初始配置；桌面新安装先提交空对象。
- 复用通用 HTML 模板，可编辑、校验和保存配置；不要求重新做漂亮的表单。
- 只整理用户提供的材料：行动项、负责人、期限、未决问题；没有提供的信息标“未提供”，不得编造或假装外部核实。
- 用户明确要求优先于插件默认偏好。输出采用对话文本即可，不要求网络、额外工具、包安装或文件导出。
- 包资源只读，报告放在包外；不新增 Tool/Hook/Workflow/Worker 声明，也不修改宿主或前端才能接入。

在 Box-Agent 根目录准备环境和模板：

```sh
cd "$PLUGIN_TASK_ROOT/Box-Agent"
uv sync
mkdir -p "$PLUGIN_TASK_ROOT/plugins" "$PLUGIN_TASK_ROOT/reports"
cp -R examples/resource-skill-plugin "$PLUGIN_TASK_ROOT/plugins/review-brief"
```

按开发指南修改 Manifest、Schema、Skill 和 README，保留模板许可证。不要把包 ID、实例 ID、贡献 ID、模型调用名混为一谈；不硬编码 `p__...` 名称。

## 5. 作者自检：先通过再进入客户端

```sh
cd "$PLUGIN_TASK_ROOT/Box-Agent"
uv run python scripts/check_resource_plugin.py \
  "$PLUGIN_TASK_ROOT/plugins/review-brief" --json \
  > "$PLUGIN_TASK_ROOT/reports/default.json"
```

在包外创建合法配置，例如 `reports/detailed-config.json`：

```json
{"detail_level":"detailed","max_actions":5,"include_open_questions":false}
```

枚举值需与自己的 Schema 一致，再执行：

```sh
uv run python scripts/check_resource_plugin.py \
  "$PLUGIN_TASK_ROOT/plugins/review-brief" \
  --config "$PLUGIN_TASK_ROOT/reports/detailed-config.json" \
  --instance-id review_team_a --alias review_summary=review_brief --json \
  > "$PLUGIN_TASK_ROOT/reports/detailed.json"
```

预期两次退出码为 0；每项 Skill 的配置投影与引用核验通过，两个实例名称独立。再创建 `max_actions: 0` 的非法配置，预期退出码 1，报告明确字段错误。

记录每条命令与退出码。不要在脚本后用 `|| true` 淹没失败。报告中的 `runtime_verified:false`、`activation_confirmed:false` 是准确边界，不是需要用内部方法“修复”的错误。

## 6. 构建并接入本分支运行时

在 Box-Agent 仓库构建本机架构运行时，并安装到刚 clone 的前端资源目录：

```sh
cd "$PLUGIN_TASK_ROOT/Box-Agent"
uv run box-agent-build-runtime \
  --version 1.0.0 \
  --external-python-sandbox \
  --output "$PLUGIN_TASK_ROOT/artifacts/box-agent-runtime" \
  --install-lab "$PLUGIN_TASK_ROOT/raccoon-lab"
```

`--install-lab` 会替换目标 checkout 的开发运行时，因此只能指向本次新 clone。产物安装到 `apps/office-desktop/build-resources/box-agent-runtime`。前端的 `desktop:build:runtime` 只编译 TypeScript，不能代替这一步。保存实际运行时构建记录及摘要。

在前端根目录安装依赖。使用已有 pnpm 10.18.2；若没有，按团队 Node 环境规范安装该版本，不必修改正式客户端的环境。

```sh
cd "$PLUGIN_TASK_ROOT/raccoon-lab"
pnpm --version
pnpm install --frozen-lockfile
cd apps/office-desktop
pnpm exec electron --version
pnpm desktop:build
```

`desktop:build` 包含资源准备、原生模块、客户端和静态 renderer 构建；可能联网下载资源。不要把“前端页面能编译”写成 Box-Agent 已正确接入，需核对实际使用的运行时来自本次构建。

## 7. macOS 隔离启动与认证准备

不要直接使用默认 `desktop:dev:live` 或 `desktop:start:client` 做这次独立验收。默认 profile 可能复用 `office-raccoon` 及旧 Box-Agent 数据；macOS 开发 `.app` 包装还可能注册正式 URL scheme。`RACCOON_PLUGIN_DEVELOPER_TOOLS=1` 只控制功能入口，不隔离数据。

静态构建完成后，在 `apps/office-desktop` 执行：

```sh
PLUGIN_CLIENT_PROFILE=$(node -e 'const {openClientProfile}=require("./build/electron/main/client/bootstrap/clientProfile.js"); process.stdout.write(openClientProfile().root)')
export PLUGIN_CLIENT_PROFILE
printf '%s\n' "$PLUGIN_CLIENT_PROFILE" > "$PLUGIN_TASK_ROOT/reports/client-profile-path.txt"

env -u ELECTRON_RUN_AS_NODE \
  RACCOON_PLUGIN_DEVELOPER_TOOLS=1 \
  pnpm exec electron . --client --client-profile "$PLUGIN_CLIENT_PROFILE"
```

`openClientProfile()` 使用现有宿主函数创建专用临时 profile，`--client-profile` 绑定其 user-data、agent、workspace 和 logs。原始 Electron 在 macOS 的 defaultApp 模式不注册 connector 系统协议，也不使用开发 `.app` 的注册脚本。该边界不能扩写为 Windows/Linux 已验证。

首次启动前清点继承的 `BOX_AGENT_*`、插件状态及 Electron 开发环境覆盖；不允许它们把任务指回正式路径。启动后核对实际 profile 与 `agent` 路径，发现复用正式数据就停止本测试进程并记录，不迁移或清空正式数据。

同一轮重启、开关持久化和普通/开发模式切换，继续使用相同 `PLUGIN_CLIENT_PROFILE`，不要再次调用创建函数。普通模式启动时先正常退出开发进程，再执行：

```sh
env -u ELECTRON_RUN_AS_NODE -u RACCOON_PLUGIN_DEVELOPER_TOOLS \
  pnpm exec electron . --client --client-profile "$PLUGIN_CLIENT_PROFILE"
```

两个模式不能同时运行。这里没有自动取得账号认证；第 3 节认证前置必须单独满足。平台提供的专用隔离认证若采用文件方式，其位置属于该 profile 的 `agent/config/`，不能在报告中展示内容。缺认证时不要修改客户端鉴权代码。

## 8. 真实客户端和模型验收

先安装自己的插件，不必安装 PPT。新 profile 没有 PPT 是预期，因为 PPT 已作为独立插件分发；若另做 PPT 回归，按 `plugins/ppt/README.md` 准备完整依赖。

| 编号 | 操作 | 必须观察到的结果 |
| --- | --- | --- |
| U1 | 开发模式安装新实例 | 默认停用；能打开本插件 HTML，看到合法默认配置 |
| U2 | 编辑、校验、保存 | 非法值被拒；合法值保存；显示待应用，不能立即声称运行时已变更 |
| U3 | 启用并应用 | 等待空闲、真实重启确认；active 修订和 generation 对应实际候选 |
| U4 | 技能列表及详情 | 公开入口可见可选；辅助方法不作为公开技能展示 |
| U5 | 用户关闭公开 Skill | 条目保留、使用禁用、辅助方法不可绕过；重启保持关闭；再开启恢复 |
| U6 | 开发页停用整个实例并应用 | 插件能力退出可用目录；检查已打开页面和重新进入页面；再次启用并应用恢复 |
| U7 | HTML 语言/主题、配置冲突 | 中英日及亮暗变化不丢草稿；冲突保留草稿，显式重读后继续 |
| U8 | 失败与回退 | 失败候选不能冒充 active；回退经过真实重启；普通能力可用 |
| U9 | 普通模式启动 | 管理入口和管理请求受限；已启用公开 Skill 仍能使用和控制个人开关 |

准备一份固定材料：至少 6 项行动、若干已明确负责人/期限、至少一项未明确负责人/期限、至少一个未决问题。使用虚构测试数据，写入 `reports/input.md` 并保留原文。

| 编号 | 模型任务 | 判定标准 |
| --- | --- | --- |
| M1 | 默认配置整理材料 | 调用正确实例；最多 3 项主要行动；未提供的信息不编造；按默认值展示未决问题 |
| M2 | 保存 detailed/5/false 但未应用 | 通过运行状态确认仍是旧配置；不能因 HTML 显示新草稿就判已生效 |
| M3 | 应用后用新会话输入相同材料 | 读取新修订；最多 5 项；不再额外展示未决问题；详细内容仍来自材料 |
| M4 | 默认最多 3 项时明确要求 2 项 | 遵循用户明确要求 |
| M5 | 没有提供材料 | 请求补充，不编造摘要 |
| M6 | 禁用入口及受控方法后再调用 | 真实读取/使用被拒，重新开启后恢复；不以模型“我不能”自述代替调用结果 |

测试至少记录：模型标识、输入、实例与配置修订、Skill 读取/选择证据、工具结果、最终输出、预期与实际差异。对文字效果预先定义标准，不能只看输出长度。一次成功不足以承诺所有模型兼容；失败和重试也保留。

## 9. 基线测试与已知未验证项

本次提交前实际重跑的结果：

- Box-Agent：插件相关 122 项、语言/Prompt/SDK 187 项、原有 Skill/ACP 接入 409 项，共 718 项不重复测试通过。
- 后端 ACP 组存在本机环境问题：禁用 capture 会缺少 `capsys`；启用时本机 `readline` 初始化崩溃。最终仅在测试进程跳过 pytest 的 readline 初始化钩子后通过，产品实现及真实捕获 fixture 未修改。这不是新环境应默认复制的绕过方式；先运行正常测试，遇到相同崩溃再记录和处理。
- 前端：插件/协议 Node 67 项、原生适配 36 项通过；native/renderer TypeScript 和 5 文件适配来源校验通过。Node 组包含真实管理 CLI/HTML 桥，但属于测试 harness。
- 作者自检工具已用最小模板、PPT 包及独立开发的新插件验证。生成内置清单与 PPT inventory 已与生成器核对。

最终提交说明是对应代码验证范围的记录。上述结果不是全库、全平台或新的真实 App/模型验收。本轮尚未重做完整实例停用/恢复 UI、登录后 HTML 保存/应用、真实 PPT 产物和 Windows/Linux 实机；这些不能从历史 V8/V9 测试自动继承。

当前平台还没有通用的一键隔离登录启动器，没有完整同实例 UI 升级/卸载流程，外部贡献类型只有 Skill。不要绕过边界来制造测试通过。

## 10. 最终交付、清理与反馈

将 [验收模板](RESOURCE_PLUGIN_ACCEPTANCE_TEMPLATE.md) 复制到 `reports/ACCEPTANCE.md`，分别填写作者自检、真实宿主、真实模型和最终结果。另提供 `reports/DOCUMENTATION_FINDINGS.md`，逐条记录仅靠文档无法完成的步骤、原文位置、实际现象和建议。

最终报告开头直接写：

```text
插件：包 ID / 版本 / 摘要
代码：Box-Agent commit / 前端 commit / 实际运行时构建摘要
作者自检：通过/失败
真实客户端：通过/部分通过/未执行
真实模型效果：通过/部分通过/未执行
是否满足接入条件：结论与尚缺项目
```

正常退出本次进程，按授权约定删除本任务创建的临时凭据副本，确认正式配置和系统协议没有被改动；不要退出、撤销或清理正式账号。保留脱敏测试报告和必要产物。新增插件可提交到独立业务仓库或专用分支，未获进一步指示不推送、合并平台代码，也不发布应用。
