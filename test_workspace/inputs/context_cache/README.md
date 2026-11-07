# Tool / Skill / MCP 缓存评测

普通 CI 的 `tests/` 已包含离线回归，不需要登录态或模型 API。单独运行同一组检查：

```bash
bash general_review/ci/test.sh --context-cache-only
```

离线检查验证固定 system/tools、消息前缀、OpenAI/Anthropic 请求转换、usage 字段、加载后下一条主请求的归属、MCP 真实本地 stdio 协议、证据完整性、基线比较，以及压缩后的工具恢复和 Skill 保留/重读。`test_compaction_resource_retention.py` 覆盖新 turn 只标记、实际压缩才清理、完整工具 schema 恢复、当前方法页保留、旧方法重读、失败摘要路径和预算；压缩触发条件保持不变。LLM 使用脚本或 MockTransport；这些结果不证明服务端缓存命中。

## 手动真实评测

在仓库根目录运行。`run` 会真实调用配置的模型，产生耗时和费用；`check`、`compare` 只读取本地证据。使用现有模型配置和登录态，不在命令或结果中填写 token。

```bash
uv run python -m test_workspace.context_cache run --model sn-kimi-k3-p
```

可选 `--config /absolute/path/config.yaml`、`--auth-file /absolute/path/auth.json`、`--title context-cache-first` 和 `--timeout-seconds 900`。默认读取 Box-Agent profile 的配置，模型 ID 必须明确指定；自动路由不能作为固定模型对照。兼容现有 provider 配置，不引入新的模型客户端。凭证仅临时复制到私有 profile，退出时删除；不会复制到评测目录。

入口内部统一调用 `test_workspace/run_acp_eval.py` → `acp-eval` → `box_agent.acp.server`。同一任务按顺序执行两次预热读取、本地 tool_search/call_tool、Skill 目录、Skill 正文、MCP tool_search/call_tool。MCP 使用本地只读合成服务，其真实 tools/list 等到任务中的指定搜索才返回。Skill 和 MCP 使用每次运行独立的合成 receipt，分别核验文件读回和服务端执行记录。

固定配置：v2、单任务、单并发、40 步、4096 输出 token、关闭 memory、禁用 SDK 和 ACP 重试。默认任务预算 900 秒，另有 120 秒启动/退出保护；慢模型可增加预算，但比较双方预算必须一致。MCP 等待时间包含在任务耗时里，不能据此推断模型推理速度。

## 保留基线与比较

每次运行自动新建 `test_workspace/outputs/yymmdd-hhmm-context-cache-<unique>/`。指定已有 title 会失败；不会覆盖历史输出。把第一次通过的目录作为基线，后续运行显式引用它：

```bash
uv run python -m test_workspace.context_cache run --model sn-kimi-k3-p \
  --baseline /absolute/path/to/baseline-run

uv run python -m test_workspace.context_cache check /absolute/path/to/run

uv run python -m test_workspace.context_cache compare \
  --baseline /absolute/path/to/baseline-run \
  --candidate /absolute/path/to/candidate-run \
  --output /absolute/path/to/new-comparison.json
```

基线始终只读。`compare --output` 只允许两个封存目录之外的新文件；不指定则输出到终端。比较报告包含双方完成数、耗时、主/辅助调用 token、逐个加载边界的命中率及差值。模型、有效配置（排除凭证和临时路径）、固定样本、评测驱动及预算不一致时，标记不可比较，不产生缓存差值。产品源码允许变化，并分别记录提交及 `box_agent/`、`uv.lock` 文件摘要。旧的手工专项报告、历史 ACP 集和改造前源码归档继续保留；不同样本不能冒充匹配的改造前基线。

输出包含标准 selection/manifest/summary、不可变 attempt 内的 ACP 原始输入输出、协议、内部逐次请求/响应、stderr、进程事件、文件快照、产物、最终回答和 completeness；另有固定样本快照、MCP 事件和门控证明、`cache-context.json`、`evidence-index.json`、`cache-report.json`/`.md`、`cache-requests.csv`。带 `--baseline` 时增加 `comparison.json`。可用现有 Trace Viewer 查看。原始数据留在本地 ignored outputs，不能提交或自动上传。

报告按加载后**紧接着的下一条主模型请求**计算 `cached_tokens / prompt_tokens`，给出原始 token、前后 cached token 差值和 trace 行号。judge 等辅助调用单独汇总；缺失、无效值是 unknown，明确返回 0 才是零命中。汇总率按已知输入 token 加权，并披露已知调用数量。完成标志还必须有完整加载顺序、实际执行和产物证据；单有 ACP completed 不够。

`run`/`check` 成功测得所有目标返回 0；证据失败、缓存未知或无法比较返回 2。没有跨 provider 的固定命中率阈值：缓存还受服务端路由、最小前缀、TTL 等影响。一轮结果仅是一轮观测，缓存差值不会自动等同于源码回归。

压缩会改写历史；本套真实样本要求追加前缀保持不变，发生压缩/历史编辑的样本不得作为“普通加载无损缓存”的证明。压缩后的恢复行为在离线回归中单独验证。当前真实范围是固定模型、普通本地工具、短 Skill、延迟发现的 stdio MCP；HTTP/SSE、鉴权/重连、长 Skill 分页及强制压缩后的服务端命中率需要独立专项。

按测试工作区规范，真实运行结束后还需由独立 Agent 阅读证据，将诊断写入 `cases/context-loading/diagnosis.md`。程序化报告不替代这份诊断；诊断文件和派生报告不属于封存的原始证据摘要。
