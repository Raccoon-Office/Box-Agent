# T13：P0-Q0 最小任务基线与现状清单

后续状态：T13 已提交为 `a8b9f49`。登录恢复后的入口修复与首轮七例真实结果见 [T13a](T13a-profile-trace-capture.md)；下方保留首次登录失败的历史记录。

## 修改方案

基于 `2f6501b`，在 `feat/shared-agent-runtime` 上实施。

- 固定边界：桌面独立运行、第三方模型、ACP、权限、预算、取消、Session Log 保持。
- 保持行为：复用 `test_workspace/run_acp_eval.py` 的 ACP 执行与证据目录；增加离线验收器、固定合成任务和来源清单。使用已有 trace 观察最终模型请求，不增加运行时注入。
- 功能变化：仅评估工具增加任务级通过/失败/未验证结果，以及版本、环境、请求指纹、延迟和用量记录。回合正常结束不等于任务通过；缺失证据不计通过，未知价格和用量保持空值。
- 最小任务集：文件修改与保留约束、给定资料检索、Skill 产物、较长输入约束、缺失输入等待、工具失败后恢复、明确完成条件；区分开发与保留验收样本。权限等待与取消沿用已有确定性 ACP fixtures，单独报告。
- 兼容影响：不改公开接口、Prompt、工具定义、调度、权限策略、依赖或锁文件；不把评估模块加入产品运行依赖。
- 验收：验收器直接覆盖成功、伪完成、错误内容、缺失或损坏证据；运行已有 Prompt/工具/ACP 契约回归。真实模型首轮每例一次、串行、每例最多 180 秒和单次输出 4096 tokens；先运行一个开发样本确认可用性，再决定其余有界样本。该限制不是费用硬上限。没有受支持的可用配置时记录未验证，不更改用户 profile。
- 对照门禁：后续 S1/T1 同模型、配置、任务、环境分别运行开发集和保留集各三次；关键约束不得回退，任务通过数不得降低，中位总延迟和每成功任务 token 增幅超过 20% 需解释后另行决策。首轮一次只作为可用性与失败定位证据，不推断统计改善。
- 回退：撤销本项提交即可移除评估入口、合成样本、测试和文档；本地评估输出保留在忽略目录，不提交原始 trace 或用户配置。

## 使用入口

现状与失败样本依据见 [System／Tool 清单](T13-system-tool-inventory.md)。固定输入和独立判定条件位于 `test_workspace/inputs/quality_baseline/`，入口为 `test_workspace/quality_baseline.py`。

先准备一个有效、隔离的 `BOX_AGENT_HOME`，使用受支持的模型配置和登录；保持该 profile、任务与模型绑定固定。不要把凭据放进命令参数。Windows 仓库环境：

```powershell
$env:PYTHONUTF8='1'
$env:UV_CACHE_DIR="$PWD/workspace/uv-cache"
$env:PATH="$PWD/.venv/Scripts;$env:PATH"
$env:BOX_AGENT_HOME="$PWD/workspace/t13-profile"
$env:PLAYWRIGHT_BROWSERS_PATH="$PWD/workspace/t13-profile/browsers"
$env:BOX_AGENT_SESSION_TRACE_ENABLED='1'
.venv/Scripts/uv.exe run --no-sync python -m test_workspace.quality_baseline run --catalog-model raccoonwork-auto --case-id q0-file
```

上述命令不会创建 profile 或修复登录；`run` 只调用现有 `run_acp_eval.main` 执行 ACP。成功的首个样本之后，开发集使用 `q0-source`、`q0-skill`、`q0-recover`，保留集使用 `q0-long`、`q0-wait`、`q0-complete`，每个 ID 前分别加 `--case-id`。固定模型可用 `--model` 替代目录模型选择，沿用标准评估器绑定限制。

每次 `run` 创建独立 `yymmdd-hhmm-q0-<随机后缀>` 目录；每例 180 秒、串行、4096 输出上限绑定。截断恢复可能提升单次输出额度，因此该值不是严格 token／费用硬预算；profile 应限制 max_steps。原有评估器捕获完整协议、trace、文件快照、产物和回合结果。登录阶段失败也保留三项根清单及未验证报告。除实际选择的样本外，其余样本不计为执行。

复核已有输出（只读原始证据，新增质量报告）：

```powershell
.venv/Scripts/uv.exe run --no-sync python -m test_workspace.quality_baseline check test_workspace/outputs/260928-1259-q0-b6e93055
```

报告按所有 immutable attempts 记录；`passed` 才是独立校验通过，`failed` 为已取得证据但条件不满足，`unverified` 为证据缺失／损坏或输入不匹配。退出码为 0 表示选中样本全部通过；1 表示有失败／未验证；非法入口或配置错误可抛错。原始证据只保留本地，提交记录不得包含用户 profile 或 trace 正文。

## 必要评估入口修复

`acp_eval.batch_runner._python_executable` 按运行平台选择仓库虚拟环境：Windows 使用 `.venv/Scripts/python.exe`，其余平台使用 `.venv/bin/python`；不存在时仍回退当前解释器。增加两个平台及缺失环境回归。只影响评估子进程，不改变产品启动、依赖或模型行为；随本项提交回退。

首轮启动发现评估登录刷新调用 Windows 不提供的 `os.fchmod`。在支持该接口的平台保留原权限设置；Windows 临时文件继承所在目录 ACL，继续原子替换和现有 `chmod` 可写属性设置，不把 POSIX mode 当作 Windows ACL 证明。测试分别验证刷新、原子替换失败保留旧文件、临时文件清理及平台分支。真实评估使用 workspace 内独立 profile，原用户配置不覆盖。

## 首轮实际验证：2026-09-28

### 确定性证据

环境：Windows、仓库 `.venv`，`PYTHONUTF8=1`、本地 uv cache；评估测试另外设置 `PYTHONPATH` 为 `test_workspace/acp_eval/src` 与仓库根。最终执行：

```text
uv run --no-sync python -m pytest test_workspace/test_quality_baseline.py test_workspace/test_refresh_box_agent_auth.py test_workspace/test_run_acp_eval.py test_workspace/acp_eval/tests/test_batch_runner.py -q -rs --basetemp=workspace/t13-pytest-complete
99 passed, 2 skipped in 5.75s

uv run --no-sync python -m pytest tests/test_system_prompt_contract.py tests/test_skill_prompt_layout.py tests/test_context_engine_contract.py tests/test_local_tool_exposure.py tests/test_tool_engine_preparation_integration.py tests/test_desktop_run_delivery.py tests/test_desktop_runtime_fixture.py -q --basetemp=workspace/t13-pytest-contracts
63 passed in 20.26s
```

两项跳过均为已有 Windows 符号链接语义用例，见 `test_batch_runner.py` 的对应 skip。新增校验器覆盖缺失、损坏、伪完成、错误值、错误 Skill、不可变多次尝试、路径逃逸、输入字节不符、登录失败记录。桌面直接回归包含权限回复／取消、交付背压和六项真实 ACP stdio 确定性场景；不代表真实模型通过七个任务。

最终源码另经 `git diff --check`、Python 编译检查及本文档相对链接检查。`check` 复核首轮目录仍得到 passed=0、failed=0、unverified=1；新增报告保留原始质量记录。未运行完整产品测试，因为改动仅涉及离线评估、合成数据和入口兼容。

### 真实模型入口

选中 `q0-file` 一例，目录模型 `raccoonwork-auto`（现有内置候选路由）；固定 4096 输出绑定、180 秒、串行、seed 13。临时 profile 复用既有连接配置和登录副本，关闭 Memory、提取／维护、MCP、sub_agent，max_steps=12、重试=1、skills_dir=skills；这是一组受限评估能力，不等同当前桌面完整配置。未覆盖原 profile、未重启客户端。

第一次登录刷新在服务响应后因 Windows 无 fchmod 而写入失败；修复后服务返回 HTTP 401。刷新可能已轮换远端 refresh token，原文件未改不代表远端登录状态未变化；后续需从客户端恢复有效登录再运行，不在聊天提供密钥。为落盘首轮状态，最终再次执行入口，输出：

```text
test_workspace/outputs/260928-1259-q0-b6e93055/
selection.json / manifest.json / summary.json / baseline-context.json
quality-4b949671.json: passed=0, failed=0, unverified=1
```

这是登录预检失败记录，未创建 ACP 任务、未调用任务模型，无可用真实通过率、请求清单、延迟或 token／成本对照。其余六例没有运行；真实模型对照、跨模型重复、联网检索、长历史压缩、办公产物视觉质量尚未验证。保留失败记录，不以历史 GLM 联调或 fixture 结果补数。

### 交付边界与后续

source → 本项源码／评估测试通过 → 确定性 ACP stdio probe 通过。未做 runtime build、install、host restart 或 fresh live task；本项不改产品运行代码，没有机械重跑全量及打包。真实模型基线受登录阻断，下一轮 S1/T1 进行效果对照前必须先取得有效的同配置基线；本项不提前启动它们。
