# T13a：隔离 profile 的评估日志采集

## 方案

基线 `a8b9f49`。登录恢复后，首例 ACP session/new 返回 `State path is outside BOX_AGENT_HOME`：评估器强制将 BOX_AGENT_SESSION_TRACE_DIR 指向 profile 外的 attempt/agent。

- 固定边界：保留 BOX_AGENT_HOME 的路径隔离校验，保持 ACP、模型请求、工具与权限行为。
- 改动：指定 profile 时，将本次 trace 写到 profile/log/acp-eval/独立 attempt ID；子进程及流收尾后，仅复制该次 JSONL trace 到评估 attempt/agent。无 profile 时沿用原目录。
- 兼容：不改用户 profile 配置，不复制凭据，不删除源日志；每次 attempt 使用独立目录。拒绝相对 profile 路径和越出 profile 的日志路径。
- 重复运行：ACP session_id 使用 `eval-acp-` 加 case ID／workspace 的 SHA-256 前 24 位，固定长度。旧入口只用 case ID，会在同一 profile 重跑时撞到已保存的不可变 cwd。仅评估会话身份变化，原输出不迁移、不覆盖。
- 等待任务验收：q0-wait 预期为 waiting_for_user；只有完整证据、全部工具／文件约束满足且 ACP 元数据 ok=true、runStatus=waiting_for_user 时通过。其他任务继续要求 completed。修正验收器，不改任务 Prompt 或产品行为；旧质量报告保留，复核另写报告。
- 验收：成功与失败场景均能收集 trace，复制失败不能计完整，其他 profile 日志不混入；运行现有 case runner 回归，再真实模型首例探针。
- 回退：撤销本项评估器变更；保留已有评估输出。

环境入口另设置 UV_PYTHON 为仓库 `.venv/Scripts/python.exe`、UV_PYTHON_INSTALL_DIR 为 workspace/uv-python、UV_PYTHON_DOWNLOADS=never。只使用现有解释器，不修改依赖或锁文件。

首次唯一身份实现直接拼完整 attempt ID，使 q0-complete 的任务注册表临时路径达到 261 字符，已产物回读成功但状态落盘失败；同批 q0-file 为 253 字符且落盘成功。收紧为固定短哈希身份后仅复跑 q0-complete 一次（同配置、180 秒），检验路径假设；保留原失败，不改产品持久化逻辑。

## 验证

Windows、Python 3.12.14；执行 `PYTHONUTF8=1`、仓库 uv cache，测试 `PYTHONPATH` 指向评估 src 和仓库。直接回归：

```text
uv run --no-sync python -m pytest test_workspace/acp_eval/tests/test_case_runner.py test_workspace/acp_eval/tests/test_batch_runner.py test_workspace/test_quality_baseline.py test_workspace/test_run_acp_eval.py -q -rs -k "not input_attachments_reach and not descendant_inheriting and not forced_stream_finalization and not model_metadata_and_staged" --basetemp=workspace/t13a-pytest-short-id
135 passed, 2 skipped, 4 deselected, 1 warning in 12.23s
```

两个 skip 是已有 Windows 符号链接用例；warning 为 slow-drip 超时用例的 `StreamReader.read was never awaited`。完整 case runner 初次执行有四项失败，随后将 **a8b9f49 原版** case runner、测试和 fake ACP 复制到 workspace 独立目录复现，四项仍失败：

- `test_input_attachments_reach_acp_with_original_bytes_and_session_scoped_paths`：将 JSON 中转义的 Windows 路径与原始路径字符串直接比较。
- `test_model_metadata_and_staged_attachments_reach_the_same_acp_session`：Windows 写入 CRLF，断言固定 LF 字节。
- `test_descendant_inheriting_pipes_is_killed_without_hanging_stream_finalization`：预期 Unix SIGKILL 进程组事件；Windows 无该事件。
- `test_forced_stream_finalization_records_cancelled_stream_error`：Windows 无 os.killpg。

原版复现命令为 `pytest workspace/t13a-original-eval/tests/test_case_runner.py -q -k "input_attachments_reach or descendant_inheriting or forced_stream_finalization or model_metadata_and_staged" --basetemp=workspace/t13a-pytest-original`，4 failed、36 deselected、2 warnings。未把这四项记作通过，也未随本项更改它们。新增路径、复制失败、重复身份、等待状态回归均通过；无全量产品门禁或打包验证。

## 真实模型首轮

用户恢复客户端登录后，检查 access token 仍有至少五分钟有效期，再复制到隔离评估 profile；不覆盖原登录文件。模型绑定使用既有 `raccoonwork-auto` 路由目录，12 步 profile、180 秒／例、4096 输出绑定，其余配置同 T13。实际请求分别路由到 `sn-sensenova-6-8-flash-lite`（下表 S）和 `sn-deepseek-v4-pro`（D）；是固定路由配置的混合模型样本，不是固定单模型效果对照。服务端部署版本、价格／实际费用未知。

| 样本 | 结果 | 模型 | 尝试耗时秒 | 已报告总 tokens | LLM 调用数 |
| --- | --- | --- | ---: | ---: | ---: |
| q0-file | passed | S | 25.391 | 96775 | 7 |
| q0-source | passed | S | 19.437 | 56009 | 4 |
| q0-skill | failed，无工作簿 | D | 22.327 | 56801 | 4 |
| q0-long | passed | D | 19.536 | 47317 | 4 |
| q0-wait | passed，预期等待 | D | 7.676 | 12191 | 1 |
| q0-recover | passed | S | 53.257 | 156698 | 10 |
| q0-complete | failed，文件正确但状态落盘失败 | D | 167.938 | 79098 | 7 |

首轮七例 **5 passed、2 failed**，共 504889 个已报告 tokens、37 次 LLM 调用；不知道账单，不能按零费用处理。首次环境／会话初始化失败单独留档，不计为模型任务执行。每例只执行一次，没有用重复抽样挑最好结果；下述单例重跑独立报告。

原始证据仅本地保留：

- `260928-1612-q0-472be4c3`：uv 解释器目录权限阻断，未运行模型。
- `260928-1612-q0-43d7f1b4`：ACP trace 越出 profile，未运行模型。
- `260928-1616-q0-4b34983a`：固定 session ID 的 cwd 冲突，未运行模型。
- `260928-1617-q0-a451a246/quality-8b6e25ae.json`：q0-file 通过。
- `260928-1618-q0-ee6384d3/quality-49544c34.json`：原始六例报告包含等待状态误判；修正后只读复核 `quality-6076bbff.json` 为 4 passed、2 failed，不重跑模型。
- `260928-1626-q0-0672277e/quality-ae73c1f1.json`：短会话 ID 后单独复跑 q0-complete，通过文件、回读与 ACP completed 验收；162.750 秒、79719 个已报告 tokens、7 次 LLM 调用，模型 D。此结果独立于首轮 5/7，不覆盖原失败；仍有一次工具失败，沙箱启动问题另行处理。

以上路径均位于 `test_workspace/outputs/`。每个完成的 ACP Case 已由独立诊断 Agent 阅读证据，写入该 Case 的 diagnosis.md。没有将 raw trace、profile、凭据或工作簿作为源码提交。

## 给后续优化的证据

- q0-skill 第 3/12 步出现“约剩 10 步，停止调用任何工具”的运行提示，模型按提示收尾，没有交付 report.xlsx。这是当前预算与提示交互造成的真实未完成；本项不调大预算掩盖，不改 Kernel 策略。
- q0-recover 已从缺失文件恢复，但 `report_execution_result` 第一次参数缺少 `outcome`，发生 INVALID_TOOL_ARGUMENTS，补参后成功。可作为 P0-T1 的具体参数误用样本。
- q0-complete 曾有 execute_code 沙箱初始化 120 秒超时，随后改用文件工具成功写出并回读 391；最终注册表路径过长导致 ACP error。不能只按最终文件正确便宣称端到端成功。
- q0-wait 表明预期等待与完成必须分别验收；错误地统一要求 completed 会惩罚正确行为。

现有 System／Tool 定义和调度未优化。下一步按上述失败归因另立任务；涉及预算收尾和持久化的阻断问题先与产品默认配置区分，模型对照需固定模型或固定路由及候选配置。
