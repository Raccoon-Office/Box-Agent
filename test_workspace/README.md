# 离线 ACP 评测与 Trace Viewer

`test_workspace/` 用于通过 ACP 运行离线评测，并在本地或可信局域网中查看完整执行过程。

## 自动路由评测基线（2026-10-31）

先检查确定性选模规则，再用真实任务结果比较固定模型和自动模型。两种证据分别报告：

```bash
# 无模型调用：合成候选池，检查任务能力、容量边界及独立子任务选模。
uv run pytest tests/test_model_routing_baseline.py -q -rx

# 无模型调用：验证真实证据的判定、路由摘要和对照条件校验。
uv run pytest test_workspace/test_quality_baseline.py -q
```

规则基线覆盖中文聊天、翻译、润色、摘要、办公邮件、PPT、数据、代码、推理、检索、图片、附件、显式工具／Skill 和长上下文。模型名 `light`、`office`、`advanced` 是合成能力档位，不代表线上模型排名。桌面仓库对应的 `localAgentModelRoutingBaseline.spec.ts` 另覆盖宿主模式、思考开关，以及复杂任务后的简短续作保持模型。两端职责不同：主模型续作保持绑定，独立子任务分别选模。

第五批已修复用例 `translation-keyword-contamination`，两端均转为普通通过测试：`把这句话翻译成英文：这个错误已经修复。` 按能力 1、`rewrite` 标签分类，不再因为待译内容添加 `debug` 标签。识别范围是以中英文翻译指令开头、有成对引号或冒号后单行待译内容的明确请求；混合动作、引号不闭合和边界不清时保留原分类。原始正文、长度阈值及容量估算保持完整，图片、工具、思考等显式信号仍参与分类。新增对照用例覆盖真实排查、追加分析／修复动作、长输入、英文及多种引号。此修复不调整模型池、评分权重或会话保持策略，也不证明所有表达的语义分类已准确。

真实交付质量继续使用 `quality_baseline run` → ACP 采集 → `quality_baseline check` 的既有入口和任务判定，不直接运行 Agent。每次必须保留所有 attempt，包括失败和证据不足的 attempt。拿到固定模型和自动模型在同一组任务上的多次运行目录后，才执行：

```bash
# fixed-run、auto-run 需替换成实际采集目录，可各列出多个重复运行目录。
uv run python -m test_workspace.quality_baseline compare \
  --baseline test_workspace/outputs/fixed-run \
  --candidate test_workspace/outputs/auto-run \
  --compare-models
```

默认比较仍要求模型配置相同，可用于比较源码策略改动。`--compare-models` 仅允许两组之间的模型配置不同；源码内容哈希、运行器、判定器、任务包、Python、平台、配置和环境必须一致。每组内部模型配置必须相同，每个 attempt 的模型配置哈希必须匹配所属运行清单。缺少源码哈希的旧记录不能用于跨模型对照，需要用当前入口重新采集。

对照报告逐任务给出通过／已验证次数、未验证次数、token、工具失败次数、平均耗时、实际请求模型及调用次数、路由决策次数和有路由证据的 attempt 数。实际模型取自 `llm.request`，包含辅助调用；路由理由取自第三批的 `model.routing`，不从模型名猜测。旧 trace 没有路由记录时保留“无路由证据”，不会推断其使用自动模式。耗时和资源汇总只包含已验证 attempt，未验证数单列；因此它们不是包括超时和服务故障在内的全链路 SLA 或实际花费。

真实任务是否通过由产物、协议终态和原始证据决定，不由路由日志或模型自述决定。没有已核验的价格／账单来源时，金额仍未知，不能把 token 下降等同费用下降。退出码沿用 `0` 无观察到的回归、`1` 存在回归、`2` 条件不可比或证据不足；小样本“无回归”不代表统计上等效，也不构成上线阈值。本批只运行上述离线测试，未产生真实模型对照结论。

## 评测集

仓库自带 3 条纯文本、无附件的 smoke-test：

```text
test_workspace/inputs/smoke_test/dataset.jsonl
```

评测集使用 JSONL 格式，每个非空行是一个 Case，至少包含：

- `id`：Case 的唯一标识。
- `query`：发送给 Agent 的任务。
- `input_files`：输入文件路径列表；路径相对于评测集所在目录。

## 运行评测

从仓库根目录准备依赖：

```bash
uv sync
uv sync --project test_workspace/acp_eval
```

运行默认 smoke-test：

```bash
uv run python test_workspace/run_acp_eval.py --count 3 --title smoke-test
```

运行本地完整评测集：

```bash
uv run python test_workspace/run_acp_eval.py \
  --dataset test_workspace/inputs/hermes_antilia_v2/dataset.jsonl \
  --count 69 \
  --title first
```

运行指定 Case：

```bash
uv run python test_workspace/run_acp_eval.py \
  --case-id Q36 \
  --case-id Q58 \
  --title selected
```

评测只使用 ACP 入口。结果写入 `test_workspace/outputs/yymmdd-hhmm-<title>/`。
完整评测集及其输入文件只保存在本地，不提交到 Git。

选择 `source=builtin` 的办公小浣熊托管模型时，初始化会读取
`~/.box-agent/config/auth.json`。若 access token 已过期或将在 5 分钟内过期，
会先通过 `test_workspace/refresh_box_agent_auth.py` 使用本地 refresh token 调用
官方 HTTPS 刷新接口，原子回写并重新校验令牌，然后才创建评测输出和启动 ACP。
刷新失败不会生成残缺的评测目录；需要重新登录时会明确报错。该工具不会输出任何
access token 或 refresh token。

可单独检查并按需刷新登录状态：

```bash
uv run python test_workspace/refresh_box_agent_auth.py
```

测试或隔离环境可用 `BOX_AGENT_EVAL_AUTH_FILE` 指定认证文件。只有
`xiaohuanxiong.com` 官方域名（及现有内网认证主机）的固定 refresh 路径被允许；
`BOX_AGENT_AUTH_REFRESH_URL` 不能用于向任意地址转发令牌。

更多参数可运行：

```bash
uv run python test_workspace/run_acp_eval.py --help
```

### 接入 agents-eval 效果评估

先在 `agents-eval` 仓库启动只监听本机的效果评估服务，再通过环境变量为新 Attempt 开启同步评估：

```bash
export BOX_AGENT_EFFECT_EVAL_URL=http://127.0.0.1:8766
export BOX_AGENT_EFFECT_EVAL_TIMEOUT_SECONDS=180

uv run python test_workspace/run_acp_eval.py \
  --count 3 \
  --title smoke-with-effect
```

每个 Case 会先按原有逻辑完成 ACP 采集和终态落盘，再调用独立服务，并把原样返回保存到 Attempt 的 `effect_evaluation.json`。服务不可达或响应错误会写入 `service_error`（能落盘时），不会改变原来的 ACP、完整性和批次成功状态。DeepSeek 等 Judge 密钥只配置在 `agents-eval` 服务进程，不传入 Box-Agent。

dataset 记录可选增加 `benchmark_case_id`（如 `case-05`）以使用 agents-eval 官方指标；未提供时使用通用 40 分过程指标和 60 分结果指标。

Trace Viewer 首页提供“执行评估”入口。它通过 `BOX_AGENT_OPS_URL` 从
RaccoonOps 获取数据集和已激活 ACP Case 中的被测模型绑定，随后仍调用
`test_workspace/run_acp_eval.py`，不会绕开既有 ACP 采集、Attempt 落盘或
agents-eval 效果评估逻辑。包含附件的数据集必须在弹窗中明确确认后才能启动。

## 基础任务验收口径

默认质量评测不再固定使用 180 秒；它与标准 ACP 入口共用总时限策略。
运行器从被测源码、解释器、配置及环境读取有效 `provider_stale_seconds` 和恢复次数，
默认总时限取 `max(2700, (恢复次数 + 2) × 模型等待秒数 + 60)` 秒。
这覆盖一次主请求及其恢复、一次结束检查，并为启动和协议通信预留 60 秒；
多步工具任务仍可能需要更长时间。不会降低生产模型等待设置。
时限覆盖 ACP 初始化到 prompt 终态；退出清理、快照和诊断写盘另计。

显式 `--timeout-seconds 180` 仍可用于短时限/SLO或故障测试；若不足上述预算，
stderr 提示 `constrained`，预算随 manifest、summary 和每个 attempt 的 run.json 保存。
读取运行时策略失败时明确标记 `unavailable`，不会猜测模型预算。
预算变化时重新创建 attempt；跨模型对照要求各运行的初始预算和退避策略一致。
重试 attempt 记录实际剩余预算，另以 `evaluation_timeout_budget` 保留原总预算，
不会把不同等待条件下的成功率、耗时混合比较。

### 429 退避与重试

默认 `--rate-limit-retries 2 --rate-limit-backoff-seconds 120`：只重试明确可重试的
LLM provider HTTP 429，且要求采集完整、没有工具调用、权限请求或已展示的回答。
每次使用新工作区、会话和不可变 attempt。不会重放已有工具副作用的任务，也不因
普通错误、超时或验收失败而自动重试。`--rate-limit-retries 0` 可关闭重试。

同一批次的 worker 共用冷却门，指数等待从 120 秒起、单次最多 600 秒；等待和
所有 attempt 共用该 case 的原总时限，不重新获得一份完整预算。持续 429 达到重试
上限或无法安全重放时停止启动后续排队 case，记录 `deferred_rate_limit`；已在执行的
任务继续采集。这个门只在当前批次内共享，不跨进程协调，也不能解除服务端配额。
底层 SDK 的短重试仍属于各 attempt；当前 ACP 错误未提供 Retry-After 时使用上述退避。

`cases/<id>/retry-history.json` 和 summary 的 `attempts` 保留本次调度的完整尝试链、
等待及总耗时，`initial_attempt_counts` 与 `attempt_counts` 分别列出本次调度的首试
和全部尝试计数。恢复后 case 最终状态可成功，但不能抹掉首试失败；质量验收始终遍历
所有历史 attempt，计费也应逐个独立会话汇总。`latest.json` 仅用于恢复索引。
显式重新运行必须创建新 attempt；旧证据不覆盖。

外层截止记录 `timeout_details.source=evaluation_deadline` 和 initialize/session_new/prompt 阶段；
模型持续无响应和结束检查超时分别使用 `LLM_PROVIDER_STALE_TIMEOUT`、
`TURN_COMPLETION_CHECK_TIMEOUT`。三者均保留为未验证交付。截止后收到的成功/取消响应
不能覆盖外层 timeout；缺少最终响应时 turn ID 使用已发送请求身份，
`turn_id_source=request` 明确其来源，不补造模型响应。

`quality_baseline.py` 按实际文件值、类型、查询范围和原样复制字节验收。
数字文件的读回可通过 `read_file` 或有成功执行证据的 Python（`execute_code`、
`bash` 中的 `python -c`）完成；不会因为工具不同直接判失败。
静态分析只识别直接执行的文件读取，不执行捕获的代码。无法确认的其他实现标记
`unverified`，交由独立诊断，而不是猜测通过。JSONL 约束检查返回的记录，
不会把工具内部为定位记录而扫描文件误判为整体读入模型上下文。

`q0-long` 还检查 `no_full_file_read`：成功执行的 Python 整读（包括 `read().splitlines()`、
`readlines()[n]`、Path 读取和文件句柄转 list）、shell 中的直接 cat、Python `-c` 或
here-document 都不能绕过约束。识别 `&&` 顺序链，并区分目标源文件与交付文件；
限定范围的 `read_file(offset, limit)`、工具内部扫描及校验和/计数不因此失败。
条件执行、动态代码、未知语言或不完整执行证据仍标记 `unverified`，这是有限静态验收，
不是任意程序的完备分析；仍需独立诊断。

任务总超时或 `TURN_COMPLETION_CHECK_TIMEOUT` 均表示尚未验证完整交付；
保留已收集的用量、耗时和可用产物检查，不能从分母中删掉这些尝试。
重新评分必须输出新的报告并保留其 oracle 哈希，不修改原始 attempt 和旧分数。

## 启动 Trace Viewer

安装 Viewer 依赖：

```bash
uv sync --project test_workspace/trace_viewer
```

启动服务：

```bash
uv run --project test_workspace/trace_viewer trace-viewer \
  --repo-root "$PWD" \
  --host 127.0.0.1 \
  --port 8000
```

本机访问 `http://127.0.0.1:8000/`。网页可启动限定数据集和模型的 ACP 评测，实际附件复制前必须确认披露。
显式绑定 `--host 0.0.0.0` 可供可信内网查看；远程启动还需 `--allow-remote-evaluations`，并由部署方配置访问控制。Viewer 不提供登录认证，不应公开到不受信任网络。
打开 Case 后的“效果指标”标签可查看评分与关键证据、性能耗时、成本，以及当前数据无法评估的指标。

详细规范见 [AGENTS.md](AGENTS.md)，组件说明见 [acp_eval/README.md](acp_eval/README.md) 和 [trace_viewer/README.md](trace_viewer/README.md)。
