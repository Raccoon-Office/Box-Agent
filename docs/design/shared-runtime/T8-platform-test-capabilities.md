# T8：权限属性与符号链接测试的平台契约

## 修改方案

- 固定边界：认证刷新、文件替换、会话 cwd 身份检查和操作系统权限策略不变。只修改测试。
- 保持行为：刷新测试在 Unix 保留精确 0600 断言；Windows 验证 chmod 支持的可读写属性，继续验证 token、附加字段和请求参数，并检查无临时文件残留。
- 保持行为：符号链接测试先尝试真实创建；仅对明确的平台不支持或权限不足跳过，其他错误继续失败。实际创建成功时仍要求拒绝别名目录且日志未改变。
- 功能变化：无。测试不将 Windows 的 stat 权限位视为 ACL，也不宣称 chmod 实现了仅当前用户访问。
- 验收：复跑两个用例和完整 test_auth.py / test_session_log.py；报告实际跳过原因。无需修改系统开发者模式或提升测试权限。
- 回退：回退本提交，无数据迁移。

## 实施与验证

实现与方案一致，仅修改 tests/test_auth.py 和 tests/test_session_log.py。

验证命令：`.venv/Scripts/python.exe -m pytest tests/test_auth.py tests/test_session_log.py -q -rs --tb=short`：**88 passed、1 skipped**，没有失败。

剩余跳过：`test_session_log_rejects_symlink_alias_for_same_workspace` 实际创建目录符号链接时返回 Windows `WinError 1314`（进程缺少所需权限）。因此该环境的真实符号链接场景仍未验证；不通过 mock 或替换为普通目录宣称通过。具备符号链接权限的 Windows 环境会执行此用例，其他错误不会被静默跳过。

最初复现结果为 1 failed、1 skipped；失败点仅在 POSIX mode 位断言，刷新请求、token 及附加状态写入均已通过。不能从 stat 位推断 Windows ACL 是否只允许当前用户访问，ACL 安全验收仍在本项范围之外。

`git diff --check` 通过。产品源码未变更；未构建、安装、重启或推送。未在 Unix 实机复跑，原精确 0600 断言保留。

参考：[Python 3.12 chmod](https://docs.python.org/3.12/library/os.html#os.chmod)、[symlink](https://docs.python.org/3.12/library/os.html#os.symlink)。Windows 下的访问控制由 ACL 决定，本项不实现或验证 ACL 收敛。
