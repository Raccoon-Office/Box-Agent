# 离线 ACP 评测与 Trace Viewer

`test_workspace/` 用于通过 ACP 运行离线评测，并在本地或可信局域网中查看完整执行过程。

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

## 启动 Trace Viewer

安装 Viewer 依赖：

```bash
uv sync --project test_workspace/trace_viewer
```

启动服务：

```bash
uv run --project test_workspace/trace_viewer trace-viewer \
  --repo-root "$PWD" \
  --host 0.0.0.0 \
  --port 8000
```

本机访问 `http://127.0.0.1:8000/`，局域网同事使用 `http://<本机 IP>:8000/`。
Viewer 为只读工具，不包含认证或脱敏能力，只应在可信网络中使用。
打开 Case 后的“效果指标”标签可查看评分与关键证据、性能耗时、成本，以及当前数据无法评估的指标。

详细规范见 [AGENTS.md](AGENTS.md)，组件说明见 [acp_eval/README.md](acp_eval/README.md) 和 [trace_viewer/README.md](trace_viewer/README.md)。
