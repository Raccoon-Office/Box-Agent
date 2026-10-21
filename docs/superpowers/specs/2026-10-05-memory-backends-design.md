# Memory 后端接入设计

## 已确认范围

扩展现有 memory 插件和能力模块，不新增插件包，不修改 MemSense。默认
`memory_backend_type: local`，继续使用现有 `MemoryManager`、提取、维护和工具。
第一期接通 `local`、`memsense`；`generic` 提供可配置 HTTP 适配，`mem0`、
`memu` 暂时复用 generic，不能据此声称原生兼容这两个服务。

MemSense 每个真实用户轮次开始时通过 `/v1/memory/files/read` 读取
`memory://user.md` 和 `memory://memory.md`，更新提示中的记忆块；显式历史
检索参照 xhx_agent_v3，通过 `/v1/memory/resource_search` 检索会话标题与 QA。
每轮结束后，通过 `/v1/memory/save` 保存真实用户文本及最终有效回复。
第一期不注册远端文件 write/edit 工具，核心文件由 MemSense 自身维护。

真实联调确认 MemSense 会话文件读取只接受 UUID。外接后端保留已有 UUID，
其他宿主 session_id 结合 tenant/user 稳定映射成 UUID，本地 Session Log 和
日志不变；不改动 MemSense 路径协议，不迁移既有远端数据。当前服务会将
`qa_chunk` 检索类型映射到事实记忆，适配层保留实际返回的资源类型。

## 插件边界

后端实现、HTTP 请求、能力声明、身份映射、上下文刷新及后台保存任务都归
`box_agent/memory.py` 所属能力。现有 session.memory 初始化器负责装配和资源
释放；memory 工具按能力注册。稳定内核不添加 MemSense 路径或行为分支。

AgentService 使用共享的 memory 运行包装器；CLI 和 ACP 为可能包含多次
内部运行的真实用户轮次添加外层边界。嵌套运行只更新最终运行句柄，不重复
刷新、保存，不把自动续跑指令写为用户输入。Python 服务调用默认一次运行
对应一轮，也可显式使用相同外层轮次边界。

读取与保存分别是可选能力。generic 不要求 Markdown 文件、租户、用户或
MemSense 的响应格式。每项操作独立配置 HTTP 方法、路径、请求模板、响应
字段路径和可选成功标记；仅显式引用的变量才发送给外部服务。

## 配置与身份

沿用顶层 AgentConfig 配置方式，增加 backend type、tenant/user 和外部连接
配置。tenant/user 缺省或空值分别解析为 `default`。会话可显式覆盖身份，
不会改变进程默认配置。ACP 在 session/new 的 `_meta.memory` 中接受
`tenant_id`、`user_id`，未提供时继承配置。

local 的 `default/default` 使用旧目录；其他身份在旧目录下按不可逆摘要
划分独立目录，避免路径穿越和身份字符串碰撞。远端模式下纠错记忆仍使用
同身份的本地存储。普通本地提取、维护、经验晋升不作用于远端后端。

## 保存与故障边界

保存数据包含 user、assistant、session/turn 标识和后端所需元数据；不包含
系统提示、工具输出、思考过程、进度消息或历史包装。使用运行句柄结算后的
结果，取消、异常或没有完整回答时跳过。后台任务由 session.memory 持有，
关闭会话时有限等待；超时或队列满时记录明确放弃日志。

HTTP 使用有限超时和有限重试。失败不会修改主任务结果，日志记录后端、
操作、身份、session/turn、状态及尝试次数，不输出 QA、密钥或响应正文。
CLI 将 memory 日志单独写入 Box-Agent 用户目录的 `log/memory_<进程号>.log`，不向
终端输出重试、最终失败或关闭时的提示。文件按大小轮转，进程间分开存储；
日志路由覆盖会话初始化至后台任务清理结束，退出后恢复宿主原有日志设置。
该显示策略属于 CLI，不改变 memory 插件的日志接口或 ACP 的日志路由。
重试不能保证 exactly-once：MemSense 没有客户端幂等键，本期只保证应用侧
每轮调度一次；传输不确定性下以服务端现有去重为准。

## 验证与部署边界

覆盖默认兼容、身份隔离、协议映射、工具能力开关、每轮刷新、单次 QA 保存、
自动续跑、失败和取消、后台关闭、generic 无身份字段及占位类型。
先运行直接回归，再运行相关 CLI/ACP/plugin 测试。本任务交付源码及测试；
没有请求安装到外部宿主或修改 MemSense，部署状态单独报告。
