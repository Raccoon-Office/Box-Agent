# T9d Windows 渲染退出屏障

## 修改方案

- 固定边界：独立 Job Object、禁止越界进程终止、既有渲染总期限和准入额度不变；不改变 Agent loop 或 ACP。
- 复现：真实探针观察到 Job 清理返回后，原子进程还被系统报告存活约 15ms；同一 PID 与创建时间检查也失败。因此 T9a 的身份校正不足以解决退出完成时序。
- 功能变化：终止前从本 Job 枚举进程并持有同步句柄，再次核对 Job 归属；终止后在既有清理期限内等待这些句柄进入退出状态，完成后才释放准入。失败必须返回清理错误，不能宣称成功。
- 保持行为：不扫描全机或按进程名杀进程；PID 复用不能使无关进程进入终止范围；所有获取的句柄在异常路径关闭。
- 兼容影响：Windows 清理等待真实退出，正常情况下只增加必要的内核退出等待；其他平台实现不变。
- 验收：原生超时/取消/泄漏/硬退出/重试测试；同时覆盖 venv 启动器与直接解释器子进程，无关进程存活。保留原总超时断言，不给测试额外放宽等待。
- 生成与回退：通过 runtime-input 生成器更新副本和 provenance；独立提交，可单独回退，不迁移数据。

## 依据

[TerminateJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-terminatejobobject) 对成员执行进程终止；[WaitForSingleObject](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-waitforsingleobject) 可等待进程同步句柄；[Job 进程列表](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_process_id_list) 包含嵌套 Job 的进程。这里用实际句柄的退出状态补足观察到的清理完成时序。

## 实施与验证

实现通过 JobObjectBasicProcessIdList 获取本 Job（含子 Job）的进程，打开 SYNCHRONIZE/QUERY_LIMITED_INFORMATION 句柄并用 IsProcessInJob 再确认归属。临时句柄在所有异常路径释放。Job 终止和活动计数检查后，再等待 retained handles；沿用原清理期限，无无限等待。

生成器已更新 bundled helper 与来源记录；Skill manifest 重新生成。原生超时测试分别使用 venv 和基础解释器创建后代，保留“返回时已退出、无关进程存活、总耗时 <12 秒”断言。

`PYTHONUTF8=1 python -m pytest tests/test_presentation_windows_runtime.py tests/test_presentation_runtime_inputs.py tests/test_presentation_render_receipt.py tests/test_presentation_render_lifecycle.py -q --tb=short -rs --basetemp workspace/t9d-exit-barrier`：**45 passed、7 skipped**（七项均为 POSIX 进程组/flock 契约）。独立真实探针连续三次超时，清理返回时所观察到的六个后代均已退出，不再出现原先约 15ms 的尾部存活。

T9a 的进程身份修改保留；其当时的独立回归没有捕获此清理时序问题，本项补足。最终全量验证见 [T9](T9-test-gate.md)。未验证非 Windows 实机。
