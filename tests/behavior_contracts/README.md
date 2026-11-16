# 首批跨引擎行为基线

真实运行 `python -m box_agent.acp.server` 子进程，经 ACP 调用真实工具、权限协商与会话落盘。模型是本地 loopback OpenAI 协议服务，不调用付费模型，不导入或 monkeypatch 生产 Agent/Tool。默认工具接口保持 v2，按真实模型请求里的 schema/tool_ref 发起调用。

从仓库根目录运行（使用全新证据目录，已有目录会报错，防止覆盖）：

```bash
uv run --no-sync pytest tests/behavior_contracts -q \
  --contract-evidence-root=/private/tmp/box-behavior-baseline-NEW \
  --junitxml=/private/tmp/box-behavior-baseline-NEW.xml
```

也可不传证据目录，使用 pytest 临时目录。CI 应上传指定目录与 JUnit XML。这里只运行测试，不调用 `uv sync`、不初始化用户真实配置。模型服务只监听 loopback；运行时环境明确构造，HOME、BOX_AGENT_HOME、浏览器缓存指向测试目录，不继承真实模型凭据、代理或 Box 配置。仅合成夹具，不使用客户数据。

## 覆盖范围

| 测试 | 合同与证据 |
| --- | --- |
| permission_controls_real_external_write × allow/deny | 文件位于模拟 HOME 内、会话工作区外；收到真实权限 RPC 后才回答；批准后真实写，拒绝不写 |
| cancel_waiting_for_permission_does_not_write | 审批等待中取消，收到 cancelled 终态后晚到批准不能复活；下一轮仍可运行 |
| cancel_before_model_tool_delivery_has_no_side_effect | 模型请求到达同步屏障后取消，未返回工具调用不产生写入 |
| published_artifact_is_real_and_delivered | 真实 write_file + publish_artifact；检查主交付事件、会话归属、路径、文件大小及 SHA256 |
| completed_session_survives_process_restart | 写入完成后 kill 真实进程，启动新 PID 并 session/load，再用真实 read_file 读回 |
| two_concurrent_sessions_keep_workspaces_and_results_separate | 同进程两会话并发写同名文件；磁盘内容和各会话通知不串用 |
| restore_rejects_wrong_workspace_without_mutating_files | 重启后用错误 cwd 恢复必须拒绝，再使用正确 cwd 恢复；不在错误目录创建业务文件 |
| plan_gate_blocks_write_until_explicit_approval | 强制计划审批时先尝试写入；检查文件未写、真实计划卡待批；批准后允许写入 |
| cross_workspace_read_is_denied_without_leaking_content | A 读 B 工作区文件，拒绝真实权限请求后模型看不到内容；B 自己仍可读 |
| cancelling_one_session_does_not_stop_another | A 请求阻塞时 B 真正完成写入，再取消 A，验证 B 文件保留且 A 未写 |
| interrupted_turn_restores_without_replaying_committed_write | 工具写入已返回并进入下一次模型请求时强杀；新进程恢复后读回，文件 mtime 不变 |

共 12 个 pytest case（11 个函数，其中授权用例参数化两次）。最后一项只覆盖**已提交工具结果后的中途退出**，不代表已覆盖“副作用发生但工具结果尚未提交”的未知窗口。这里没有修改业务代码来制造故障点。

同步使用模型请求屏障或真实权限 RPC，不靠固定 sleep 猜取消时机。超时使测试失败；失败原样保留，不标 xfail、不跳过、不修业务代码。测试驱动可修正夹具错误，但每轮证据目录独立保留。

## 证据与失败记录

- `manifest.json`：源码 commit、Python、测试文件哈希与证据范围。
- `results.jsonl` / `summary.json`：setup/call/teardown 的成功、失败、跳过和不完整状态；fixture/清理失败也计失败。
- 每个 case 的 `model.jsonl`：模型接收的请求；`model-errors.jsonl` 表示夹具错误，不算产品缺陷。
- `box/protocol.jsonl`、`stdout-*.raw`、`stderr-*.log`、`process.jsonl`：双向 ACP、原始输出、诊断、PID 与退出。
- `box/profile/`：真实隔离配置、会话 JSONL 与相关状态；`workspace*`：真实业务文件。每次重启原始输出独立分代。
- JUnit：供 CI 读取失败；本地详细证据目录不入 Git。

## 以后接 DSH

`harness.Engine` 描述 open/prompt/cancel/restore/stop 操作边界；`BoxEngine` 是当前唯一实现。后续增加 DSH 驱动和 factory 选择，保留业务输入、外部副作用及文件判定；驱动负责原生协议、结果/审批/交付事件投影，模型夹具负责按目标引擎提供的 schema 映射同一逻辑操作。当前部分断言仍读取 Box ACP 通知与 stopReason，接 DSH 时需移到驱动的语义观察接口；此处只建立可复用用例，没有宣称 DSH 已接入或零适配。

适配器不得代替引擎创建产物、执行工具、批准授权或补写持久化。保留各引擎原始协议证据，无法对应的合同报不支持/失败，不伪造成功事件。DSH 原生前端仍需真实安装版端到端测试，不能由此 ACP 源码基线代替。

## 尚未覆盖

真实桌面/UI 和安装包、真实模型质量、OS 强隔离、跨账号/tenant/云记忆隔离、Jupyter/浏览器/子 Agent 取消、未知副作用窗口、磁盘损坏/写失败、身份切换及凭据撤销、过期/错请求审批、计划审批拒绝/版本冲突、Office 预览与多平台安装，仍需后续补齐。

模拟 HOME 是权限夹具边界，不是操作系统沙箱。测试不会把“mock 模型”称作真实模型验收，也不会将一次源码进程通过描述为客户安装版通过。
