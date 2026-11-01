# G2 投票审查（仓库内工具）

本目录实现可选的 G2 投票模式：多个视角并行找问题，再由独立的验证代理逐条投票，
最后用代码确定性计票。它只服务于本仓库的 PR 审查：

- 不在 `box_agent/` 下，不进入 wheel、sdist 包数据或 standalone runtime；
- 只通过公开 SDK（`AgentSession` / `AgentClient`）调用 Box-Agent，产品代码不依赖它；
- 所有代理只有只读工具（`read_file`、`search_files`，可用时加 `grep` / `glob`），
  读取范围限制在仓库内，不写 memory，不启用 MCP、skills、bash 或网络工具。

合并门禁、严重级别、输出格式和 Verdict 规则仍以
[`docs/PR_REVIEW_STANDARD_CN.md`](../../docs/PR_REVIEW_STANDARD_CN.md) 为准。

## 用法

在被审查的 head 已检出、且没有未提交改动的仓库中运行（CI 中即 preflight 使用的临时 worktree）：

```bash
uv run python general_review/vote/review.py --base main --preflight success --tpr-file pr_tpr.md
```

开发者在提 PR 前本地试跑（不要求 preflight，结论里会标注）：

```bash
uv run python general_review/vote/review.py --base main --local
```

第 2、3 轮复审只看增量 diff，并跳过已发过的 finding：

```bash
uv run python general_review/vote/review.py --base main --preflight success --since <上一轮 head> --previous <上一轮 votes.json> --round 2
```

模型配置读取 Box-Agent 的 `config.yaml`（默认搜索路径，或 `--config`）。
`--verifier-model` 可让验证者使用另一个模型，降低同一模型一起出错的风险。

输出写到 `workspace/review-vote/<head 前 12 位>/`（已被 `.gitignore` 忽略）：

| 文件 | 内容 |
| --- | --- |
| `report.md` | 第 8 节格式的审查结论，附投票记录 |
| `votes.json` | 全部 finding、每一票及其证据、过滤与失败记录，供审计和下一轮 `--previous` |
| `diff.patch`、`files.txt`、`history.txt`、`tpr.md` | 交给代理读取的上下文包 |

退出码：`0` 表示审查流程跑完（结论看 `Verdict`），`2` 表示输入或仓库状态不合法、审查未启动。

## 流程

1. **门禁**：preflight 不是 `success`（且未加 `--local`）、超过 3 轮、或没有改动文件时，不启动任何代理。
2. **分级**：按 [`tiers.json`](tiers.json) 的路径规则取改动文件中最高的风险档位，映射到审查深度。
3. **找问题**：按深度启动不同视角的代理（[`prompts/lenses.json`](prompts/lenses.json)），每个输出 JSON finding。

   | 深度 | 视角 | 每条 finding 的验证者 | 通过阈值 |
   | --- | --- | --- | --- |
   | quick | correctness、security | 3 | 2 |
   | standard | + architecture、cross-platform | 3 | 2 |
   | deep | + proof、history | 5 | 4 |

4. **过滤与归并**（`tally.py`）：丢弃 style / naming / formatting / cleanup 类；
   丢弃上一轮已发过的（指纹为文件 + 类别 + 标题）；同文件、同类别、相距 3 行以内的合并，保留最严重的一条。
5. **投票**：每 8 条为一批，每批启动 K 个全新上下文的验证代理。验证者看不到视角、
   原始严重级别、证据和建议，必须自己读源码并引用 `file:line`，否则该票记为 UNSURE。
6. **计票**（`tally.py`）：
   - 确认票 ≥ ⌈2K/3⌉ → 发出；严重级别取确认者给出级别的中位数，平局取更严重的一级；
   - 未达阈值但有确认票、且严重级别为 P0/P1 → “存疑·需人工复核”，不阻塞；
   - 其余丢弃，只记录在 `votes.json`；
   - 缺失或格式错误的票按 UNSURE 计，阈值按请求的 K 计算。
7. **标签与 Verdict**：不在 merge-base diff 改动行内的 finding 标为 Pre-existing，只有 P0 阻塞；
   P0–P2 为 Important，P3 为 Nit。Verdict 按第 9 节规则计算，另加两条：
   任一代理失败或输出无法解析时不会是 `APPROVE`；存在存疑项时为 `COMMENT`。

## 验证

```bash
uv run pytest general_review/vote/tests -q
```

这些测试不在 `pytest tests/`（G1 preflight）范围内，需单独运行。端到端测试用脚本化的假 LLM
走真实 SDK 路径，覆盖只读工具的仓库边界、计票、Pre-existing 判定和报告格式；
它不评估真实模型的找问题和验证质量，阈值与视角需要用历史 PR 评测后再调整。
