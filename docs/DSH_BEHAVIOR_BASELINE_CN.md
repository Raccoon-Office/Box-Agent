# 第一批行为集成测试：Box-Agent 基线

日期：2026-11-16。状态：**已实现测试并运行源码 ACP 基线；未改业务代码，未接入 DSH，未验证桌面安装版。**

## 结果与复现

源码基线 `1298dc225d95d37499ec754b564c8acf0d31d558`；macOS ARM64、Python 3.12.13。模型为 loopback OpenAI 协议服务，工具、权限协商、Agent 进程与落盘均为真实执行。工具接口沿用默认 v2。

- 新增行为测试：**12 passed**，无 skip、无 xfail。
- 与原有 `tests/acp_host` 合跑：**22 passed，4 skipped，26.68 秒**。
- 4 个 skip 均来自原有真实模型 opt-in 用例；未启用外部模型访问，不是新增合同被跳过。
- 所有启动的 15 个 Agent 进程均有已等待完成的退出记录（含强杀与重启）；原始协议、进程日志和隔离会话文件均保留。

```bash
uv run --no-sync pytest tests/behavior_contracts tests/acp_host -q \
  --contract-evidence-root=/private/tmp/box-behavior-baseline-NEW \
  --junitxml=/private/tmp/box-behavior-baseline-NEW.xml
```

必须使用全新输出路径，已有证据不覆盖。测试代码及详细说明见 [测试入口](../tests/behavior_contracts/README.md)。失败仍返回 pytest 非零退出码，未将失败转换为通过或预期失败。

## 本批合同与判定

| 范围 | 已执行的合同 | 独立观察依据 |
| --- | --- | --- |
| 工具审批 | 授权前无外部写入；拒绝不写；允许后写入真实文件 | 模拟 HOME 内、工作区外的文件和真实权限 RPC |
| 计划审批 | 强制门控阻止抢先写入；计划卡进入待批；显式批准后可写 | 真实文件、计划快照、执行结果 |
| 取消 | 模型返回工具前取消、审批等待取消、晚到批准不复活、单会话取消不阻断另一会话 | 同步屏障、cancelled 终态、后续真实请求和文件 |
| 恢复 | 完成轮次后重启读回；工具结果已提交、最终模型回复前强杀再恢复；错误 cwd 拒绝 | 新 PID、session/load、真实 read_file、文件内容与 mtime |
| 隔离 | 同进程并发会话同名文件不串用；跨工作区读拒绝后不泄露内容；原所有者仍能读 | 分会话通知、模型实际收到的工具内容、两份磁盘文件 |
| 产物交付 | 写入并发布主交付物，事件归属与磁盘内容一致 | primary artifact 的路径、会话、大小、SHA256 与真实文件匹配 |

这些合同对应迁移清单 C01–C05/C07/C10 的部分基础分支。不是全部 C01–C12 已完成，也不等于多用户后端或真实 UI 已验收。

## 失败、调整与证据保留

每轮独立目录保留，不覆盖首轮失败。所有证据归档到本地忽略目录 [基线目录](../workspace/behavior-baselines/20261116/)，不进入 Git。总览为 [baseline.json](../workspace/behavior-baselines/20261116/baseline.json)，最终合同结果为 [summary.json](../workspace/behavior-baselines/20261116/final/summary.json)，合跑结果为 [JUnit](../workspace/behavior-baselines/20261116/final.xml)。`SHA256.json` 记录归档文件校验值。

| 轮次 | 结果 | 诊断与处理 |
| --- | --- | --- |
| 01 | 5 passed、3 failed | 夹具把权限目标放在模拟 HOME 外。现有政策直接拒绝，测试等待审批超时；调整模拟 HOME 边界，使目标满足现有可申请授权的条件，未改生产权限策略 |
| 02 | 8 passed | 初始合同通过 |
| 03 | 11 passed、1 failed | 测试模型驱动没有识别初始 v2 目录已提供的 tool_ref，反复搜索；搜索又被计划门控阻止。修正驱动读取真实目录引用，未改业务门控 |
| 04 | 12 passed | 完整合同通过 |
| final | 22 passed、4 skipped | 增加版本/测试哈希/文件快照和汇总采集后，新增合同与既有 ACP 探针合跑通过 |

本批没有确认需修复的业务缺陷；上述失败归类依据为原始协议、模型请求与权限诊断，不因最终通过而删除失败记录。归档为逐字节复制，协议和测试配置中的原始绝对路径仍指向 `/private/tmp`；归档是证据，不作为可重定位运行配置。后续重新执行使用新目录。

最终 `manifest.json` 记录源码 commit、Python/平台、依赖版本、测试文件 SHA256 和当时已有 tracked 改动路径。本轮保留了原有图谱/文档改动；`box_agent/`、旧 `tests/acp_host/`、依赖和锁文件无本轮修改。

## 跨引擎复用与剩余范围

共享用例及真实副作用判定；BoxEngine 实现当前进程操作，Engine 协议描述未来驱动边界。DSH 仍需增加原生会话/审批/控制适配、工具 schema 映射和语义观察接口；当前部分断言仍读取 ACP 通知与 stopReason，不能声称仅换启动命令即可跑 DSH。驱动不得替引擎生成文件或补做持久化。

尚未覆盖：真实安装版与界面、真实模型质量、账号/tenant/云记忆隔离、Jupyter/浏览器/子 Agent 的取消和清理、未知副作用窗口、磁盘失败、身份切换/撤销、过期/错误请求审批、计划拒绝/版本冲突、Office 打开预览、多平台安装。

其中“副作用已发生但结果尚未落盘”的恢复窗口需要在外部服务边界设置可控副作用屏障，或找到现有可靠观察点；本批只测结果已提交后的中断，不通过修改业务代码制造这一状态。
