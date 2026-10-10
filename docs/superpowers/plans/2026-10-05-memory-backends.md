# Memory 后端实施方案

1. 扩展现有配置：保持 local 默认，定义外部连接及 generic 每项操作的映射。
2. 在 memory 能力内实现身份解析、本地目录隔离、MemSense/generic 后端及
   有限超时重试；保留本地纠错存储。
3. 在现有 memory 工具模块内补充外部读取和检索，按后端能力注册工具。
4. session.memory 装配会话身份及运行资源；禁止远端后端进入本地提取、
   维护和晋升流程。
5. 在共享 AgentService 包装运行；CLI/ACP 标记包含自动续跑的外层用户轮次，
   保存结算后的最终回复。后台任务由会话关闭流程回收。
6. 更新配置示例和使用文档，覆盖 MemSense 与不含身份字段的 generic 示例。
7. 添加直接回归测试，验证协议、隔离、刷新、保存、工具开关、错误日志和
   关闭行为；运行相关现有套件并检查 diff。

验收：默认 local 行为保持；MemSense 核心读取/历史搜索/QA 保存协议与服务端
吻合；远端 write/edit 不开放；不同会话身份不会串用工具或提示；内核不包含
后端专属代码；远端故障不改变主任务结果且有日志。

## 实施与验证结果

已完成上述步骤，保持默认 local；未修改 MemSense、创建新插件包或变更稳定内核。

- 首次实现的完整套件（下述 UUID 兼容修复前）：`uv run --no-sync pytest -q tests --maxfail=5`，
  6148 passed、279 skipped、3 warnings。告警涉及现有 requests 依赖版本和
  tar 解包行为。首次完整运行发现的旧宿主借用本地 manager 兼容问题已修复，
  上述结果来自修复后的完整重跑。
- 补充回归：`uv run --no-sync pytest -q tests/test_memory_backends.py tests/test_session_adapter_assembly.py tests/test_agent_run.py tests/test_run_api.py tests/test_run_delivery.py tests/test_run_event_capacity.py`，
  103 passed，包含完整套件启动后补充的异常响应和保存队列满场景。
- `uv run --no-sync python -m compileall -q box_agent`、`git diff --check` 通过。
- 首次本地 MemSense 只读验证：`http://127.0.0.1:8787/healthz` 正常；使用本次后端
  代码读取 default/default 的 `memory://user.md`、`memory://memory.md` 成功，
  两个文件当时内容均为空。该阶段没有向真实服务写入测试 QA。

## 服务启动后的真实联调与兼容修复

使用固定模型回复，通过真实 AgentService 和 memory 插件请求本地 MemSense。
测试数据仅写入独立的 `box-agent-integration` 租户及随机测试用户，未写入
default/default。已验证：

- 整轮完成后自动保存，回读原始 QA 与发送内容一致；关闭会话等待保存结算。
- worker 处理后，搜索可返回会话资源和事实资源；核心记忆可注入新会话的提示。
- 相同租户的另一用户、另一租户的同名用户均不能搜索到测试记录。
- 发现 MemSense 会话文件读取只接受 UUID，而 CLI/ACP 会生成带前缀的标识。
  已在 MemSense 后端内稳定映射非 UUID 标识；已有 UUID、本地 Session Log、
  日志及 generic 协议不受影响。修复后通过真实服务再次验证保存、搜索及两类
  资源的 memory_read，全部成功。旧的非 UUID 远端测试记录未迁移。
- 修复后回归：`uv run --no-sync pytest -q tests/test_memory_backends.py tests/test_memory.py tests/test_memory_tool.py tests/test_session_adapter_assembly.py tests/test_cli_runtime.py tests/test_acp.py`，
  365 passed。此次改动仅涉及 MemSense 协议映射，未重复完整套件。

首次联调发现的 MemSense 侧问题（未修改其代码或配置，后续复测见下）：

- 配置的 embedding 服务 `127.0.0.1:8081` 拒绝连接；事实向量任务重试 5 次后
  失败，错误为 `fetch failed`。此次检索成功来自 BM25，向量召回未通过验证。
- 一条项目记忆的原始 QA 明确为「紫罗兰色」，事实抽取仍保留此值，但核心整理
  worker 将 memory.md 改成「月白色（原紫罗兰色已弃用）」。已确认偏差发生在
  服务端整理阶段，具体原因未定位。
- 包含事实提取的保存请求曾超过默认 5 秒；客户端记录超时后重试，服务端去重
  返回成功，原始 QA 没有重复写入。调用完成不代表后台整理和索引已全部完成。

## embedding 服务启动后的复测

用户启动 embedding 服务后，通过真实 AgentService、memory 插件和 MemSense
再次测试，仍使用固定模型回复及独立测试身份。九项检查全部通过：

- 先前失败的索引任务重试完成；新增事实的 dense 和 sparse 索引均就绪。
- Agent 完成后仅保存一组正确 QA，非 UUID 会话标识映射有效。
- 语义检索命中测试事实，BM25 为 0，dense 和 sparse 排名均为 1，确认向量
  召回有效；会话资源和事实资源均可读取。
- 本轮核心记忆正确保留颜色、表格偏好和项目名，新会话成功召回；租户与用户
  隔离验证通过。先前颜色偏差未复现，尚未证明其服务端根因已经修复。
- 保存超过默认 5 秒的请求经重试和服务端去重后成功，未重复写入 QA。配置
  示例改为 20 秒请求超时、45 秒退出等待，为服务端事实提取留出时间。

运行状态达到源码服务链路及真实 MemSense 保存、向量召回、资源回读和新会话
召回验证；未构建、安装外部宿主运行时，也未重启宿主或验证真实模型驱动的
宿主任务。

后台队列保存在进程内，崩溃或超出关闭等待时间时可能丢失未完成保存；放弃会
记录日志。

## CLI 后台记忆日志静默修复

用户要求后台重试和最终失败均不提示。CLI 接管 `box_agent.memory` 的日志，
按进程写入用户目录中的轮转文件；路由覆盖会话初始化、输入等待和退出清理，
结束后恢复原有 logger 设置。目录或文件不可写时保持静默，不影响主任务。
memory 插件和 MemSense 无须修改，ACP 继续使用已有日志路由。

- `uv run --no-sync pytest -q tests/test_cli_runtime.py tests/test_cli_session_trace.py tests/test_memory_backends.py tests/test_acp.py tests/test_user_paths.py`：318 passed。
- 回归通过真实 CLI、AgentService 和 memory 插件配合模拟 HTTP 超时，验证
  输入期间及退出时的保存重试、放弃均记录到文件，终端不输出提示；另覆盖
  日志配置恢复、profile 路径、磁盘错误和关闭 memory 时不创建记忆日志。
- `uv run --no-sync python -m compileall -q box_agent/cli.py tests/test_cli_runtime.py tests/test_cli_session_trace.py`、`git diff --check` 通过。

本次修复验证到源码及模拟故障集成测试；未重新运行全量套件、构建或安装
外部运行时，也未重启用户当前 CLI。现有进程须退出后用更新后的代码重启。
