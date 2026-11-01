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
git fetch origin main
uv run python general_review/vote/review.py --base origin/main --preflight success --tpr-file pr_tpr.md
```

`--base` 决定代理能否看到目标分支的最新状态，所以审真实 PR 时要先 fetch，再传远端分支。
如果本地分支与 `origin/<base>` 不一致，报告的“残余风险”会提示。

开发者在提 PR 前本地试跑（不要求 preflight，结论里会标注）：

```bash
uv run python general_review/vote/review.py --base main --local
```

第 2、3 轮复审只看增量 diff，并跳过已发过的 finding：

```bash
uv run python general_review/vote/review.py --base origin/main --preflight success --since <上一轮 head> --previous <上一轮 votes.json> --round 2
```

模型配置读取 Box-Agent 的 `config.yaml`（默认搜索路径，或 `--config`）。
`--verifier-model` 可让验证者使用另一个模型，降低同一模型一起出错的风险。

端点有 RPM 限制时用 `--rpm` 限制所有代理合计的请求速度。不加也能跑：遇到 429 时所有代理一起暂停，
再重试这一次请求，不丢代理的进度（优先用 `Retry-After`，最多重试 6 次）。

代理依赖 ripgrep 提供 `grep` / `glob`。PATH 上没有 `rg` 时可设置 `BOX_AGENT_RG`；
都没有时仍能运行，但定位代码更慢、token 更多，报告的“残余风险”会提示。

输出写到 `workspace/review-vote/<head 前 12 位>/`（已被 `.gitignore` 忽略）：

| 文件 | 内容 |
| --- | --- |
| `report.md` | 第 8 节格式的审查结论，附投票记录 |
| `votes.json` | 全部 finding、每一票及其证据、过滤与失败记录，供审计和下一轮 `--previous` |
| `diff.patch`、`files.txt`、`history.txt`、`tpr.md` | 交给代理读取的上下文包 |
| `target-commits.txt`、`target.patch`、`merge-check.txt`、`merged/` | 目标分支领先 merge-base 时才有：目标分支提交、合并相对当前 head 带来的改动、冲突检查、这些文件合并后的版本 |

退出码：`0` 表示审查流程跑完（结论看 `Verdict`，完整性看 `Coverage`），`2` 表示输入或仓库状态不合法、审查未启动。

计算合并结果需要 git 2.38 以上，它会向对象库写入不可达对象，但不改 ref 和工作区。git 版本不够时，
代理只拿到目标分支自身的 diff，报告会注明。

## 流程

1. **门禁**：preflight 不是 `success`（且未加 `--local`）、超过 3 轮、或没有改动文件时，不启动任何代理。
2. **分级**：按 [`tiers.json`](tiers.json) 的路径规则取改动文件中最高的风险档位，映射到审查深度。
   同时计算这个 head 合入目标分支的结果。代理读到的是 PR head，所以目标分支的提交、合并 diff、
   合并后的文件都放进上下文包，用来发现 PR 自身看不出的语义合并冲突。
3. **找问题**：按深度启动不同视角的代理（[`prompts/lenses.json`](prompts/lenses.json)），每个输出 JSON finding。
   最后回答没有符合格式的 JSON，或步数耗尽时，同一会话再追问一轮，避免已花的 token 白费。

   | 深度 | 视角 | 每条 finding 的验证者 | 通过阈值 |
   | --- | --- | --- | --- |
   | quick | correctness、security | 3 | 2 |
   | standard | + architecture、cross-platform、history | 3 | 2 |
   | deep | + proof | 5 | 4 |

4. **过滤与归并**（`tally.py`）：丢弃 style / naming / formatting / cleanup 类；
   丢弃上一轮已发过的（指纹为文件 + 类别 + 标题）；同文件、同类别、相距 3 行以内的合并，保留最严重的一条。
5. **投票**：每 8 条为一批，每批启动 K 个全新上下文的验证代理。验证者看不到视角、
   原始严重级别、证据和建议，必须自己读源码并引用 `file:line`，否则该票记为 UNSURE。
6. **计票**（`tally.py`）：
   - 确认票 ≥ ⌈2K/3⌉ → 发出；严重级别取确认者给出级别的中位数，平局取更严重的一级；
   - 未达阈值但有确认票、且严重级别为 P0/P1 → “存疑·需人工复核”，不阻塞；
   - 其余丢弃，只记录在 `votes.json`；
   - 缺失或格式错误的票按 UNSURE 计，阈值按请求的 K 计算；
   - 确认者同时给出触发条件的可达性（`normal` / `edge` / `contrived`）。超过半数判为 `contrived` 时，
     非 P0 的 finding 降为 P3，不阻塞。
7. **标签与 Verdict**：不在 merge-base diff 改动行内的 finding 标为 Pre-existing，只有 P0 阻塞；
   类别为 `target-interaction` 的 finding 不算 Pre-existing，因为它正是这个改动与目标分支合并造成的。
   P0–P2 为 Important，P3 为 Nit。Verdict 按第 9 节规则计算，另加三条：
   任一代理失败或输出无法解析时不会是 `APPROVE`；存在合并冲突时不会是 `APPROVE`；存在存疑项时为 `COMMENT`。
8. **完整性**：任一代理失败时，报告写 `Coverage: incomplete`，并注明不能作为 G2 结论。
   `Proof` 只有在 proof 视角跑完且没有阻塞问题时才是 `sufficient`；段落已填写但没有评估时是 `unavailable`。

## 验证

```bash
uv run pytest general_review/vote/tests -q
```

这些测试不在 `pytest tests/`（G1 preflight）范围内，需单独运行。端到端测试用脚本化的假 LLM
走真实 SDK 路径，覆盖只读工具的仓库边界、合并上下文、限流重试、JSON 补救、计票、Pre-existing 判定和报告格式；
它不评估真实模型的找问题和验证质量，阈值与视角需要用历史 PR 评测后再调整。
