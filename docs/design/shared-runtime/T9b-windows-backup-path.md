# T9b Windows 备份与产物路径

## 修改方案

- 固定边界：文件权限、工具执行、回收策略、备份失败的返回方式不变。
- 保持行为的改动：测试使用独立回收目录，不读取或写入真实用户目录。
- 功能变化：Windows 绝对路径拼接时不再覆盖备份根目录。盘符或 UNC 根映射为安全相对目录，其余路径保留，避免覆盖原文件或跨盘同名碰撞。POSIX 备份路径保持原样。
- 功能变化补充：工具 JSON 输出中的双反斜杠路径可匹配 Windows 产物。仍要求文件在本次执行中改变、位于 workspace 内、输出精确引用路径；不由目录或单独 diff 推断产物。
- 兼容影响：此前 Windows 备份会因源目标相同失败；修复后在回收目录生成副本。不迁移历史文件。
- 验收：备份位于指定根下、内容一致、源文件保留；不存在文件及目录仍返回 None；检查不同路径的备份位置。
- 产物验收：JSON 转义路径可发现本次产生的文件；未修改文件、嵌套同名文件和目录引用继续拒绝。
- 回退：回退本项函数、回归测试和方案提交；已有副本无需迁移。

## 实施与验证

实现与方案一致。修改 `box_agent/tools/safety.py` 与 `box_agent/tools/engine/artifact_results.py`；直接回归位于 `tests/test_safety.py::TestBackupFile` 和 `tests/test_artifact_delivery.py::test_json_output_requires_changed_file_and_exact_path`。

验证：设置 `PYTHONUTF8=1`，执行 `.venv/Scripts/python.exe -m pytest tests/test_safety.py::TestBackupFile tests/test_artifact_delivery.py::test_json_output_requires_changed_file_and_exact_path -q --tb=short --basetemp workspace/t9b-direct-final`，**10 passed**。同一时间戳的不同目录同名文件分别保留独立副本；六种产物组合仅接受本次改变且被精确引用的文件。

先前在系统 Temp 目录执行时，测试本身通过，但 pytest 退出清理遇到已有目录访问限制；上述仓库临时目录重跑无该问题。完整门禁结果统一见 [T9](T9-test-gate.md)。未修改前端或打包脚本；用户文件无需迁移，产品采用此修复仍需重新构建和升级 runtime。
