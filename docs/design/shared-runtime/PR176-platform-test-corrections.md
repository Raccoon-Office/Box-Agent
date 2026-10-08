# PR #176：跨平台测试与浏览器环境修正

## 范围与行为

本项修正 PR #176 首轮 GitHub Actions 暴露的测试前提与环境配置，产品分类、权限、超时处理和用户目录隔离保持不变。

- CSV 分类测试固定 MIME 输入，分别验证 `text/csv -> data` 和 `application/vnd.ms-excel -> spreadsheet`；保留实际产物路径检测。
- 符号链接测试显式设置用户目录边界：用户目录内拒绝访问并提供授权请求；用户目录外的 POSIX 路径直接拒绝，Windows 显式盘符路径按现有宿主契约提供目录授权请求。所有场景均不允许链接直接绕过权限。
- ACP 连接主动报错与等待超时分开验证。主动报错必须保留原异常实例或因果链，并停止运行；Python 3.10 的内置 TimeoutError 与 asyncio.TimeoutError 不同，3.11 起为同一类，按现有包装规则精确断言。等待超时用真实阻塞连接触发，验证固定 ACP 文案、发送协程取消和运行结束。
- 渲染 CI 在 job 级设置绝对 `PLAYWRIGHT_BROWSERS_PATH`，安装 Chromium 与测试子进程共用路径。隔离 HOME 后仍能发现浏览器，继续实际渲染与 doctor 验证。

## 验收与边界

先运行 `uv run --no-sync python -m pytest tests/test_core.py tests/test_permissions.py tests/test_desktop_run_delivery.py -q --tb=short --basetemp=workspace/pr176-ci-fix`，并检查 `git diff --check`。随后推送同一 PR，验证 GitHub Python 3.10/3.11/3.12 全量测试与 Windows/Linux/macOS 渲染矩阵。

本项不修改运行时源码，不需要重新生成 Skill manifest。当前 Windows 本地结果不能替代 Linux/macOS 和 Python 3.10 的 CI 结果；具体提交与检查结果记入 PR。此前上游 macOS 构建测试的两项 Windows 路径断言问题独立跟踪，不并入本项。

本地最终验证：上述命令使用最终修正版本和 `--basetemp=workspace/pr176-ci-fix-final`，331 passed in 15.75s；`git diff --check` 通过。GitHub 矩阵在推送后独立验证。
