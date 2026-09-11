# 测试工作区规范

本规范适用于 `test_workspace/` 下的离线评测、评测输出和可视化工具。

## Agent 测试入口

- 所有会实际调用 Box-Agent 的测试必须且只能使用 ACP 入口。
- 统一使用 `test_workspace/run_acp_eval.py` 启动离线评测。
- 该脚本调用 `test_workspace/acp_eval` 的 `acp-eval` 命令，最终启动 `box_agent.acp.server`。
- 禁止使用交互式 Box-Agent CLI 进行评测。
- 禁止在评测脚本中直接实例化或运行 `Agent`。
- 单元测试可以 mock ACP 子进程，但不得改用 CLI 或直接 Agent 调用作为替代路径。

## 标准测试方法

运行仓库自带的 3 条纯文本 smoke-test：

```bash
uv run python test_workspace/run_acp_eval.py --count 3
```

运行完整评测集并指定输出目录标题：

```bash
uv run python test_workspace/run_acp_eval.py \
  --dataset test_workspace/inputs/hermes_antilia_v2/dataset.jsonl \
  --count 69 \
  --title first
```

指定随机种子以便复现：

```bash
uv run python test_workspace/run_acp_eval.py --count 5 --seed 2839328477913252832
```

运行指定样本：

```bash
uv run python test_workspace/run_acp_eval.py \
  --case-id Q36 \
  --case-id Q58
```

默认评测集为 `test_workspace/inputs/smoke_test/dataset.jsonl`，包含 3 条纯文本、无附件的 Case。
`test_workspace/inputs/hermes_antilia_v2/` 下的完整评测集及输入文件仅供本地使用，不得提交到 Git。

## 输出保存规范

- 所有离线评测结果保存到 `test_workspace/outputs/`。
- 每次运行只创建一个一级目录，目录名必须为 `yymmdd-hhmm-<title>`，例如 `260821-2008-first`。
- `--title` 只控制输出目录后缀，不会修改 ACP 会话元数据；默认值为 `smoke-test`。
- `--title` 必须是单个非空目录名称，不得包含 `/` 或 `\\`。
- 同一分钟内已经存在同名目录时必须停止并报错，不得覆盖或混入既有结果。
- 根目录必须包含 `selection.json`、`manifest.json` 和 `summary.json`。
- `selection.json` 记录抽样模式、随机种子、样本 ID 和评测集路径。
- 每个 case 使用不可变 attempt 目录；重试必须创建新 attempt，不得覆盖旧 attempt。
- 必须保留 ACP 原始输入/输出、规范化协议、Agent 内部轨迹、stderr、进程事件、文件快照、artifact 清单、最终回答、run 状态和 completeness 状态。
- 不兼容旧评测目录；需要时清空旧目录并重新运行。
- 评测输出可能包含敏感信息，只能在可信网络和本地可信环境中查看。

## 诊断结果

- 离线评测完成后，由执行被诊断任务之外的其他 Agent 阅读该 Case 的采集证据并生成诊断。
- 每个 Case 的诊断固定保存为 `cases/<case_id>/diagnosis.md`，不得放入 attempt 目录。
- `diagnosis.md` 使用 UTF-8 编码。
- 不限制或假设 Markdown 的内容、结构、标题、字段或元数据。
- Trace Viewer 将文件作为完整 Markdown 内容展示；文件不存在时显示未生成状态。

## Trace Viewer

`test_workspace/trace_viewer` 只读取 `test_workspace/outputs/` 的一级目录。启动方式：

```bash
uv run --project test_workspace/trace_viewer trace-viewer \
  --repo-root "$PWD" \
  --host 127.0.0.1 \
  --port 8000
```

Viewer 默认仅允许本机启动评测。远程查看可显式绑定网络地址；远程启动必须额外设置
`--allow-remote-evaluations` 并由部署方限定可信访问。启动前对实际附件重新核对披露确认，
不能仅使用较早的选项统计。Markdown 保留完整内容并将原始 HTML 作为文本显示，避免诊断或模型输出变成执行控件。
