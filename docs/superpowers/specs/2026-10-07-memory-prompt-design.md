# Memory Prompt 注入设计

## 已确认行为

System prompt 预留通用 `{MEMORY_CONTEXT}`。现有 memory 插件负责放置和刷新，
后端负责生成内容；不新增插件包、不修改稳定内核或 MemSense 服务。
无占位符的旧模板自动在末尾补块，禁用记忆或 utility 会话移除占位符。
每个真实用户轮次刷新一次并原位替换，同轮工具调用和自动续跑复用该轮内容。
读取失败清除旧内容、保留块位置，后台记录日志。

local 保留原有内容和加载时机。generic、mem0/memu 占位类型保持通用 HTTP
映射和文本格式，不使用 MemSense 的路径、日期加载或条目转换。

## MemSense 内容

非空内容分为 `User Profile`、`Long-term Memory`、`Recent Date Memory`。
核心文件统一在读取出口把 `<mem time="…" priority="…">` 转成 `<mem>`，
保留正文、其他正文标签和条目边界；自动注入与 memory_read 使用相同转换。
日期摘要在 system prompt 的同一记忆块内，按旧到新列出日期及正文；
自动注入省略日期文件的 YAML 元数据，显式读取日期文件仍返回完整内容。
空分区省略。历史记忆作为背景，当前要求优先，实时状态重新核实；不主动
提及内部记忆引用，用户询问来源时如实说明。

`memory_external.date_memory_load_days` 默认 3、范围 0～31，0 关闭日期预载，
仅 MemSense 使用。按 MemSense 的 UTC 日期分区和本轮时间戳确定文件范围，
每轮重新计算。文件并行读取、分别降级，不增加保存前置等待。
沿用 context_max_chars 总预算，格式化后不超预算，截断保留完整 mem 边界。
已有日期摘要才会被加载；本次不补生成历史摘要。

## 验证与交付

验证占位位置、重复刷新和故障恢复、local/禁用/utility/generic 兼容、
核心条目转换、日期范围与顺序、跨日刷新、预算和部分读取故障。
运行 memory、session、CLI/ACP 相关测试，并对真实 MemSense 做只读探测。
自动提交推送源码；不重启用户现有进程或安装外部宿主运行时。
