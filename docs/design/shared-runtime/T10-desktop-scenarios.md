# T10 桌面开发版异常场景联调

## 修改方案

- 固定边界：ACP 线协议、客户端源码、用户模型配置、正式应用安装保持不变。使用已经授权的开发版客户端。
- 保持行为的改动：增加仅测试使用的 ACP 进程入口，运行真实 BoxACPAgent 与共享服务，通过确定性模型/工具夹具触发边界，不调用外部模型或消耗真实额度。
- 功能变化：无计划中的产品功能变化。测试入口仅由专用开发启动器选择，测试状态写入 workspace 内独立目录；不进入发行入口或默认配置。
- 兼容影响：区分真实 GLM 正常回合证据与确定性异常注入证据。慢发送用可控发送延迟注入，不把它描述为真实 UI 负载基准。
- 验收：通过客户端实际 IPC/ACP 链路接收权限并响应或取消；超大事件及消费超时显示失败，不能伪装正常结束；小容量慢发送不丢序；父子并行与祖先预算验证实际执行数。保留原生循环/账本单元测试，明确嵌套委派仍受既有能力限制。
- 回退：移除测试入口及记录，开发版重新连接普通本地 Box-Agent；不修改正式客户端安装。
- 交付记录：源码、自动化测试、构建、开发版启动/连接、实际场景分别记录；不把模拟提供方称为真实第三方模型验证。

## 实施与验证

### 实现和复现入口

`tests/desktop_runtime_fixture.py` 提供显式启用的 ACP 测试进程；`tests/test_desktop_runtime_fixture.py` 经真实 stdio 自动验证六种场景。设置 `BOX_AGENT_DESKTOP_TEST_ROOT` 为仓库 workspace 内独立绝对目录，并在启动 Python 前将 `BOX_AGENT_HOME` 设为其 `profile` 子目录；运行 `python -m tests.desktop_runtime_fixture`。缺少测试根或根超出 workspace 时拒绝启动。

该进程使用真实 ACP、Session、AgentService、Kernel、内置 sub_agent 和工具执行账本。替代层仅包含固定输出的模型、无外部副作用的测试工具、小容量交付选项和可控发送延迟。宿主的模型绑定保留在会话中，但提供方解析固定到测试模型，不加载实际 profile 凭据。

Windows 桌面通过本地临时启动器设置 `BOX_AGENT_ACP_CMD`，逐行转发 stdin/stdout/stderr 到上述 Python 入口。启动器和运行证据留在 workspace，不进入产品包。客户端源码、模型设置和打包脚本均未修改。

### 实际开发客户端结果

2026-09-24，officev3 `client-v2` / `aac3c92422f691b905540e941efbc2d272513fd9`，Electron 1.0.36 开发版，经页面 preload IPC → 主进程 BoxAgentManager → ACP stdio → 共享核心验证。测试调用真实客户端 API，未逐项人工点击 UI；不将其描述为完整视觉验收。

| 场景 | 客户端观察 | 核心结果 |
| --- | --- | --- |
| 正常回合 | `done/end_turn` | completed |
| 慢发送 | 40 个片段无遗漏且顺序一致，done | completed；每片延迟 10ms |
| 单条超限 | error，未收到 done | `RUN_EVENT_TOO_LARGE`；70173 字节超过测试额度 65536 |
| 持续拥塞 | error，未收到 done | `RUN_EVENT_CONSUMER_TIMEOUT`；测试等待上限 250ms、发送延迟 1s |
| 两个并行子任务 | done，合计执行 3 次 | 共享上限 3；未执行请求被预算拒绝 |
| 祖先与内层预算 | done，两个外层调用加一个内层调用 | 总执行 3 次，内层上限 1；通过合成嵌套工具验证祖先账本，非递归内置 sub_agent 能力扩展 |
| 权限批准 | 收到 1 次权限请求，批准后 done | 工具执行恰好 1 次 |
| 权限等待中取消 | 收到 1 次权限请求、1 次 dismiss、cancelled | 工具执行 0 次，最终 cancelled |

这组测试仅将交付容量缩小为 2 条/64KiB，生产默认保持 1024 条/4MiB/30 秒。自动回归 `.venv/Scripts/python.exe -m pytest tests/test_desktop_runtime_fixture.py -q --tb=short`：**6 passed**；完整门禁见 [T9](T9-test-gate.md)。本地证据为 `workspace/desktop-dev-validation/fixture-proof.json` 和 `workspace/desktop-scenarios-live/results.jsonl`，只在此文档保留无敏感信息的结果摘要。

### 启动失败、恢复和运行边界

测试入口启动时曾因客户端日志和浏览器目录仍指向原 profile 而触发隔离目录校验，ACP 以 code=1 退出，开发页面显示 `connectors:get-states` 未处理异常。测试入口现将两项路径固定到测试 profile；没有放宽产品路径校验。修复后 ACP 健康检查和连接器查询成功，刷新后旧错误提示消失。

联调结束已退出测试开发实例，使用原 `start-dev.ps1` 恢复普通本地源码连接，健康检查为 `ok=true`、`initialized=true`、`childRunning=true`、`launchMode=configured-dir:venv-python`。GLM 设置保留；没有新发真实模型任务。此前 GLM 正常回合及取消结果为独立的真实提供方证据，不能用于证明本表全部异常场景。

最终全量门禁和包构建后，再次重启普通开发版加载最终源码；页面完成首次编译后，ACP 健康检查和 connectors 查询均成功，GLM-5-3-Flash 选择保留，页面及 Next.js 错误浮层均未再出现 `connectors:get-states`。恢复证据保存在本地 `workspace/desktop-dev-validation/final-restored-health.json`。最终完整测试的 5713 项通过中包含本项六个 stdio 回归。

达到边界：源码回归、Python 包构建、真实开发客户端启动/连接与上述场景。未生成或安装本次 standalone runtime，没有更新已安装正式应用，没有完成正式安装包上的新任务验收。
