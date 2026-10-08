# T9e Windows 验证 Unix runtime 包的目录发布

## 修改方案

- 固定边界：下载地址、校验和、tar 安全解压、manifest、现有缓存判定和安装失败清理不变；Windows zip 安装路径不变。
- 复现：Windows 执行 Linux tar 安装验证时，已解压目录的 rename 偶发 WinError 5；现有 Windows zip 安装已处理过同类占用，tar 发布直接 rename。
- 功能变化：macOS/Linux tar 的目录发布只在实际 Windows 宿主遇到 5/32/33 三类拒绝访问或共享占用错误时进行有限重试，最多 4 次，等待总计 350ms。保持原子 rename，不退化成复制后删除。
- 保持行为：非 Windows、其他错误和重试耗尽继续抛错，不绕过 hash、可执行文件检查或 manifest 写入顺序。
- 验收：暂时占用后成功；持续占用、其他错误立即/最终失败，源目录内容保留；原 runtime 安装测试。
- 回退：独立回退本项；无配置或数据迁移。

## 实施与验证

新增 `_publish_extracted_directory`，仅替换 macOS/Linux tar 安装路径的最终 rename；Windows zip 的原有处理保持不变。真实权限失败最终仍进入原安装异常处理，不会提前写成功 manifest。

`tests/test_runtime_directory_publication.py` 覆盖三种可重试错误、持续失败、其他错误、非 Windows 行为；用受控错误注入验证有界重试，成功路径实际移动测试目录，失败路径保留源文件。未使用真实 sleep 延长测试。

`PYTHONUTF8=1 python -m pytest tests/test_runtime_directory_publication.py tests/test_skill_runtime.py -q --tb=short --basetemp workspace/t9e-direct`：**52 passed、2 warnings**。警告是 Python tarfile 后续版本的默认过滤策略提示；未改锁文件或解压策略。完整门禁见 [T9](T9-test-gate.md)。
