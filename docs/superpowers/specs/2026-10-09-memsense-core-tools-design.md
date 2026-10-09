# MemSense 核心记忆工具设计

## 已确认接口

沿用现有 memory 插件，只为 MemSense 增加 `memory_write`、`memory_edit`，
并增强已有的 `memory_read`。三个工具的 `path` 必填，参数枚举和执行校验
均只允许 `user.md`、`memory.md`。工具内部转换为服务端的 `memory://` 路径。
日期、会话和搜索结果路径不通过这三个工具开放；搜索和后台 QA 保存保留。
local、generic、mem0/memu 占位类型保留原接口，不新增插件、配置或依赖。

显式 read 返回 JSON 结构：`path`、`exists`、`content`、
`memory_edit_write_rules`，以及按文件选择的 `user_profile_update_rules`
或 `long_term_memory_update_rules`。正文只包含记忆，规则放在独立字段；
整个结构进入模型的工具消息，不能只保存到 UI 的 raw_output。
system prompt 和自动召回不新增修改规则。

## 修改语义与状态

write/edit 的工具描述必须要求先 read 同一路径，执行时也强制检查。
自动预载不产生显式读取授权。read 明确确认不存在时，只允许 write 创建；
读取失败不能解锁修改。会话后端内部保存两份原始内容、revision、存在状态，
不增加持久化状态源；每个会话独立，按文件串行处理显式读写。

模型使用纯 `<mem>...</mem>` 条目。工具解析原始存储，保留未变条目的
time/priority，新条目补时间和 priority=1。edit 支持空 old_text 追加、
空 new_text 删除，非空 old_text 必须唯一命中一个连续完整条目块。
priority<1 的条目不能修改或删除；按 agent-v3 的方式，有受保护条目时
拒绝完整重写，仍可编辑其他非保护条目。

核心 edit 在客户端转换后，通过 `/v1/memory/files/write` 提交完整存储。
已有文件携带读取时的 base_revision，不使用 allow_overwrite。
发生冲突、超时、取消或无法验证的响应后清除读取记录，要求重新读取。
修改请求不自动重试，避免结果不确定时重复执行；其他操作保留现有重试。
后台日志不记录正文、规则、认证信息或服务错误正文。

## 验证与边界

验证路径枚举、先读条件、创建/追加/替换/删除/重写、完整块匹配、元数据和
保护条目、冲突与失败后的重新读取、并发和会话身份隔离、模型实际收到规则、
自动召回不带规则、local/generic 兼容与配置关闭。更新接入文档，运行工具和
memory 聚焦测试及相关会话、CLI/ACP 回归。真实服务探测只读，不改用户记忆。
不安装外部运行时或重启宿主；源码验证后自动提交并推送。
