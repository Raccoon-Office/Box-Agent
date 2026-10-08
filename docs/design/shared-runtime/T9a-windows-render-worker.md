# T9a Windows 渲染工作进程兼容

## 修改方案

- 固定边界：Agent loop、ACP、模型配置、渲染输出格式不变；每次渲染仍独占 Job Object，取消和异常必须清理其所有进程。
- 保持行为的改动：测试明确区分 POSIX 进程组与 Windows Job Object；继续使用真实子进程验证，不模拟 Win32 API。
- 功能变化：允许虚拟环境启动器的直接解释器子进程完成注册。必须同时验证私有令牌、角色、存活启动进程、直接父子关系，并在发出 START 前将实际工作进程加入本次 Job。结束时只允许仍存活的已登记启动器与工作进程，其他成员仍视作泄漏。
- 兼容影响：修复 Windows venv 的合法渲染失败；直接解释器、Unix 路径保持原有行为。不得放宽为任意后代或任意带令牌进程。
- 生成资源：修改 scripts/presentation_suite_overlays 中源文件，通过仓库生成流程更新打包副本、来源记录和 Skill 清单；不手写哈希。
- 验收：实际 venv 工作进程成功、无关 PID 拒绝、超时/取消/硬退出清理、泄漏拒绝、额度释放与重试。重跑相关渲染测试。
- 回退：单独回退本项源文件、生成资源、测试与方案提交，不迁移用户数据。

## 复现证据

同一启动中观测到启动器 PID 与注册 PID 不同，注册者的直接父进程正是启动器，Job 内有两个进程；原实现报 `invalid render process registration`。此问题位于已有渲染模块，本次完整测试暴露；不是事件缓冲功能引入。

## 实施与验证

实现沿用方案中的直接父子关系和 Job Object 边界，没有允许任意后代登记。`render_runtime_windows.py` 的源文件与 bundled 副本同步更新。

生成流程补充 `scripts/sync_presentation_suite.py --refresh-runtime-inputs`：先校验固定上游版本、overlay 列表、完整文件集合及每个文件的来源哈希，再更新仓库拥有的 runtime helper。Windows 检出产生的 UTF-8 CRLF 文本按 LF 校验；实际内容不同仍拒绝，二进制不做换行转换。补充重复运行不变和篡改拒绝回归。此项是原方案生成资源步骤的实现细化，不修改模型或客户端契约。

执行过 runtime helper 刷新和 Skill manifest 生成，来源记录由生成器产生。新增测试验证私有注册通道拒绝非法 PID 和无关进程。POSIX flock/进程组用例在 Windows 明确跳过，原生 Windows Job 用例继续真实执行。

直接验证（`PYTHONUTF8=1`）：

- `python -m pytest tests/test_presentation_runtime_inputs.py -q`：7 passed。
- `python -m pytest tests/test_presentation_windows_runtime.py -q --tb=short --basetemp workspace/t9-native-final`：11 passed。
- `python -m pytest tests/test_presentation_render_receipt.py -q --tb=short --basetemp workspace/t9-receipt-check`：8 passed。

中间复跑发现两个验证边界：系统 Temp 目录中的链接目录遍历报 FileExistsError，而同一用例在仓库临时目录通过；高并发进程测试中仅凭 PID 的超时后存活断言失败，退出后未发现对应存活进程。超时用例现同时记录 PID 与创建时间，再检查相同进程是否仍活着；未增加宽限等待或降低清理要求。不能仅凭这些观察断言旧失败全部由 PID 复用导致，最终完整门禁见 [T9](T9-test-gate.md)。

未在 Linux/macOS 实机执行本项；对应原有实现和 POSIX 测试保留。此修复需要重新打包 runtime 才能进入安装版产品，开发版源码验证不代表正式应用升级。
