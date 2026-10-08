# T7：Windows 路径测试兼容

## 修改方案

- 固定边界：Agent 执行、产物字段、会话目录身份、认证与权限策略不变；本项只修改测试。
- 保持行为：本地绝对路径用 Path 比较完整目标；会话目录期望按操作系统 normcase 规范化。相对产物路径仍严格要求 `/` 格式。
- 保持行为：Skill 引用直接读取消息文本块，不对 Python 容器的 repr 手工反转义；继续验证完整 Skill 内容、历史和当前 hash。
- 保持行为：YAML 配置中的绝对路径用正确的字符串序列化；MCP 测试使用 pytest 临时文件，在加载前关闭写入句柄。
- 功能变化：无。兼容影响仅为 Windows 下原本误失败的测试可以验证真实契约；不跳过或放松业务断言。
- 验收：直接复跑原失败用例，再跑对应 Session Log、持久化、认证测试文件；独立记录其他失败。
- 回退：回退本提交，无运行时或数据迁移。

## 实施与验证

实现与方案一致，仅修改四个测试文件，未修改产品源码或放宽权限断言。

- 直接验证：7 个路径/文件用例全部通过：`test_core.py::test_artifact_detect_in_nested_task_dir`、`test_core.py::test_artifact_envelope_shape`、`test_session_log.py::test_session_log_accepts_only_equivalent_cwd_syntax`、`test_auth.py::test_config_accepts_custom_auth_file` 及三个 `test_mcp_loader_*auth*` 用例（adds_dynamic_auth_for_hosted_url_servers、skips_auth_header_for_non_xiaohuanxiong_servers、does_not_override_configured_auth_header）。命令为 `.venv/Scripts/python.exe -m pytest <以上 node IDs> -q --tb=short`。
- Skill 引用的四个参数组合：`python -m pytest tests/test_agent_session_persistence.py::test_agent_restore_uses_current_skill_and_reports_known_changes_without_rewriting_log -q --tb=short`：4 passed。设置 BOX_AGENT_HOME 为 `E:/git/Box-Agent/workspace/windows-path-tests`；未设置时先遇到用户目录日志写权限，未计作路径断言失败。
- 完整持久化文件：`python -m pytest tests/test_agent_session_persistence.py --basetemp=workspace/windows-path-tests/persistence -q --tb=short`：35 passed，使用同一隔离 profile。
- 完整 Session Log 与认证：`python -m pytest tests/test_session_log.py tests/test_auth.py -q --tb=short`：87 passed、1 skipped、1 failed。未设置 BOX_AGENT_HOME；剩余 `test_refresh_hosted_auth_token_rotates_and_atomically_persists` 要求 POSIX 0600 权限位，Windows 实际模式不同，属于权限语义，不在路径修复范围。保留原断言。
- `git diff --check` 通过。

本次共修复 11 个原失败参数化 case。T6 的历史验证记录保留；不能据此宣称全量门禁通过。未重跑全量、未在 Linux/macOS 实机验证。仅测试和文档变更，无需重建产品包；未安装、重启、推送或合并。
