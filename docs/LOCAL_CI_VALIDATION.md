# Box-Agent 本地 CI 集成验收报告

## 验收结论

- **门禁与调度流程：通过。** Preflight 能在准确 Head 的隔离 worktree 中执行，
  测试失败时停止后续步骤并阻止 Review Agent；成功、失败、基础设施错误、首次 PR
  和状态补发分支的编排测试全部通过。
- **Box-Agent 当前 Head 合并资格：不通过。** `d4345a78467ad4eae12740b16b3de95236e4feb1`
  在 WSL Preflight 的测试阶段稳定出现 2 个失败，因此正确结果应为 GitHub
  `teamwork/local-ci: failure`，不应启动 Review Agent。

验收日期：2026-08-17。目标环境：Ubuntu on WSL，Python 3.11，WSL Git，
detached 临时 worktree，临时空 `HOME`。

`.understand-anything/meta.json` 的分析基线是
`e2079027544fc540d1b2c480d10c012230fd6048`，落后于本次 Head，因此只用于定位；
所有结论均由当前源码和真实运行验证。

## 实际 Preflight 配置

```yaml
preflight:
  enabled: true
  status_context: teamwork/local-ci
  timeout_seconds: 3600
  max_output_bytes: 2000000
  steps:
    - name: install
      timeout_seconds: 900
      command: [uv, sync, --frozen, --all-extras]
    - name: compile
      timeout_seconds: 300
      command: [uv, run, python, -m, compileall, -q, box_agent]
    - name: tests
      timeout_seconds: 1800
      command:
        - uv
        - run
        - pytest
        - tests/
        - -q
        - --tb=short
        - --deselect
        - tests/test_mcp.py::test_connection_timeout_on_unreachable_server
    - name: build
      timeout_seconds: 600
      command: [uv, build]
```

## 真实运行结果

| 阶段 | 结果 | 证据 |
| --- | --- | --- |
| 准确 Head / detached worktree | 通过 | WSL Git 检出 SHA 与目标 SHA 完全一致，shell 文件为 LF |
| `uv sync --frozen --all-extras` | 通过 | 冷启动安装 115 个包；复验同步通过 |
| `compileall` | 通过 | 退出码 0 |
| 全量 pytest | **失败** | 2 failed、2431 passed、60 skipped、1 deselected，1599.98 秒 |
| `uv build` | 单独 probe 通过 | 成功生成 `box_agent-0.8.87.tar.gz` 和 `box_agent-0.8.87-py3-none-any.whl` |
| Review Agent 调度 | 通过 | 5 个 orchestration/status 聚焦测试通过 |

由于测试阶段失败，真实门禁按 fail-fast 规则没有执行 build。表中的 build 是为了解构
失败范围而单独运行的 probe，不能把它描述为整条 Preflight 成功。

## 阻塞问题

### [P1] Controlled PPT finalizer 在标准 WSL Preflight 环境中不能满足测试契约

位置：

- `tests/test_pptx_controlled_deck.py:8987`
- `tests/test_pptx_controlled_deck.py:9140`
- `box_agent/skills/document-skills/pptx/scripts/finalize_controlled_deck.js`

稳定失败：

1. `test_controlled_finalizer_runs_compact_complete_chain` 期待 4 个
   `FINALIZE_PASS`，实际只有 2 个。
2. `test_controlled_finalizer_delivers_degraded_html_for_runtime_probe_failure`
   期待 `degraded_stages == ["runtime_probe"]`，实际还包含
   `"html_self_check"`。

聚焦复验结果：`2 failed in 5.48s`。正常 HOME 与临时空 HOME 下均可复现，排除
Preflight 凭据隔离导致的副作用。

生成的 `qa/html_self_check.json` 给出直接原因：

```text
Missing dependency: playwright
Repair Office Raccoon's managed runtime, then install Chromium in
Settings -> Plugins -> Web automation (Playwright).
```

影响：当前 Head 无法获得 required status `teamwork/local-ci: success`，Review
Agent 和合并流程应被阻断。

建议由 PPT/runtime owner 明确选择并实现一种契约：

1. 将 Playwright 与 Chromium 安装加入可复现的 CI/runtime 准备步骤，使
   `html_self_check` 在标准门禁环境中 PASS；或
2. 如果无浏览器环境下的 degraded HTML 是允许行为，修改测试和 finalizer contract，
   明确哪些 stage 可以 advisory，并补有/无 Playwright 两组回归测试。

修复后必须在新的 Head SHA 上重跑完整 Preflight，不能复用本报告的 build probe。

## 环境校准发现

第一次控制运行使用 Windows Git 创建 worktree。Windows Git 的系统配置
`core.autocrlf=true` 把 `box_agent/skills/zhihu/scripts/run.sh` 检出为 CRLF，导致
`sh -n` 语法检查失败；改用 WSL Git/LF 后该失败消失。

正式服务应完全在 WSL 内执行 Git 操作。建议另行增加 `.gitattributes`：

```gitattributes
*.sh text eol=lf
```

这能避免维护者通过 Windows Git 准备共享 workspace 时引入平台相关假失败。

## 编排分支验证

以下测试在 `teamwork_review_agents` 的本地 CI 分支上通过：

```bash
uv run pytest \
  tests/test_preflight_orchestration.py \
  tests/test_preflight.py::test_preflight_retries_only_final_status_delivery \
  -q
```

共 5 个场景：

- Preflight failure 完成事件但不启动 Review Agent。
- Preflight success 才允许匹配的 Review Agent 运行。
- 基础设施 error 重新入队。
- 启用 Preflight 的仓库首次发现 PR 时自动产生事件。
- GitHub 终态回写失败只补发状态，不重跑本地命令。

## 配置建议

- Box-Agent 完整测试本次耗时约 26 分 40 秒，`tests.timeout_seconds: 1800`
  余量较小。冷缓存、DrvFS 或网络波动可能造成假超时，建议调整为 2700 秒。
- 总超时建议至少 3600 秒；首次冷安装和大型 wheel 构建需要额外余量。
- 服务与 Git/worktree 操作必须在 WSL 内运行，并使用专门的服务账号。
- GitHub Ruleset 必须把 `teamwork/local-ci` 配置为 required status check。
- 当前只运行可信内部 PR；不把 worktree/environment filtering 当作不可信代码沙箱。
