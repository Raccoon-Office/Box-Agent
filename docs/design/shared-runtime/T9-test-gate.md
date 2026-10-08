# T9 完整测试门禁收敛

## 修改方案

- 固定边界：生产运行、会话身份、权限策略、模型和工具行为不变。
- 保持行为的改动：为每个测试提供独立默认用户目录，清除外部传入的 BOX_AGENT_HOME；测试仍可显式设置自己的 profile 或 home。修复 Windows 文件生命周期及结构化内容断言，保持原有业务断言强度。
- 功能变化：当前不计划修改产品行为。复现产品缺陷时在实施前补充方案及兼容影响。
- 兼容影响：测试不再依赖真实用户配置或前一用例的持久状态。需要真实凭据的集成测试继续按既有条件运行/跳过。
- 验收：先运行受影响测试，再执行 preflight 同范围的锁定依赖、编译、完整测试及构建；逐项记录失败和跳过，不把未执行标为通过。
- 回退：回退本项测试及文档提交，无用户数据迁移。

## 实施与验证

旧架构索引仅作导航，实际职责以当前源码为准。实现分为测试收敛与独立产品修复，后者在实施前分别补充方案：

- [T9a](T9a-windows-render-worker.md)：Windows venv 渲染工作进程注册与生成资源同步；提交 `c430f8b`。
- [T9b](T9b-windows-backup-path.md)：Windows 备份根目录与 JSON 产物路径识别；提交 `89e6855`。
- [T9c](T9c-log-file-identity.md)：运行日志独占创建防覆盖；提交 `97e4890`。
- [T9d](T9d-windows-exit-barrier.md)：等待 Windows Job 进程实际退出再释放准入；提交 `1bdb475`。
- [T9e](T9e-runtime-directory-publication.md)：Windows 上 tar runtime 目录发布遇到占用时有限重试；提交 `4be00a2`。

### 本提交中的测试变更

| 范围 | 实际变化 | 保持的验收语义 |
| --- | --- | --- |
| conftest、ACP、CLI、runtime_entry | 每例独立 HOME/USERPROFILE，清除外部 BOX_AGENT_HOME；显式 home 测试同时设置 Windows 变量 | 会话与工作区状态不依赖真实用户配置 |
| build_runtime、skill_runtime、workspace_registry、context/Skill 引用 | 原生路径、路径分隔符、Windows 可执行文件布局、JSON 路径转义；读取实际文本块而非容器 repr | 仍比较完整目标、字段和消息内容 |
| Bash、权限、Skill scratch、只读 Skill | 原生 PowerShell 命令；确需 POSIX 语法时使用已安装 Git Bash；实际权限批准后再验证执行/超时 | 不放宽生产权限策略；孙进程超时测试必须先观察到实际运行 |
| Jupyter、MCP、隔离 profile | Python 子进程替代不可执行的 shell 夹具；临时文件关闭后删除；事件循环建好后再阻止网络 | 保留真实进程启动、缺包安装失败、文件生命周期和网络隔离断言 |
| presentation、PPTX、tools | 区分 CRLF 字节和规范文本，按 Windows 路径/编码检查脚本结果 | 保留完整内容、哈希、资源 provenance 和安全边界 |
| HookBus、verified_corrections | 注入受控时钟，替代 20ms 调度竞争与相同时间戳假设 | 仍验证链总预算及失败→修改→成功的先后关系 |
| kernel_state | 无账本的自定义 sub_agent 在硬额度下应零执行并返回 UNSUPPORTED | 对齐 T5 已明确批准的功能变化，不恢复原来事后统计的软限制 |
| core CSV 产物 | 对齐已有 spreadsheet 分类，MIME 使用本机注册表/标准库结果 | 不修改产品类型映射，继续要求正确路径及产物字段 |

当前测试修复没有更改 Kernel、模型请求、权限策略、续跑或客户端协议。T9a–T9e 的产品行为变化独立提交和回退。

### 验证环境与中间失败

Windows / Python 3.12.14，`.venv/Scripts/uv.exe` 0.12.18；设置 `PYTHONUTF8=1`。按 `general_review/ci/preflight.sh` 范围执行，未修改 pyproject.toml 或 uv.lock。真实进程树和符号链接测试在宿主权限环境运行，测试状态放在仓库 workspace 内。

- 沙箱中后台 Bash 清理曾停在进程树终止操作；同一用例在宿主权限环境通过。未据此修改 Bash 产品取消逻辑。
- 系统 Temp 目录出现链接遍历/pytest 退出清理访问限制；最终门禁明确使用 `--basetemp workspace/...`，没有删除或修正既有 Temp 目录权限。
- 第一轮收敛后完整结果：5694 passed、320 skipped、2 failed；两项是同一 Windows 路径预期未统一分隔符，已修复。
- 下一轮完整结果：5703 passed、318 skipped、1 failed；新增构建后两项包内容检查不再跳过，剩余 Windows 子进程身份检查见 T9a。该轮不能描述为门禁通过。
- 再次完整结果：5702 passed、318 skipped、3 failed。确认同时间戳日志覆盖、真实进程退出屏障不足，以及 Windows tar 安装目录的临时占用；分别通过 T9c/T9d/T9e 修复。此轮推翻了“仅用进程身份即可修复退出失败”的假设，未保留该假设作为最终结论。
- 路径/产物/备份直接组合：57 passed、2 skipped；T9b 新增同名副本用例后直接范围 10 passed；Jupyter 子进程夹具 14 passed；桌面确定性 stdio 回归 6 passed。

### 最终门禁

- 锁定依赖：`.venv/Scripts/uv.exe --cache-dir workspace/uv-cache sync --frozen --all-extras` 成功，未更改依赖声明或锁文件。
- 完整测试：`PYTHONUTF8=1 .venv/Scripts/python.exe -u -m pytest tests/ -q --tb=short -rs --deselect tests/test_mcp.py::test_connection_timeout_on_unreachable_server --basetemp workspace/t9-complete-full`：**5713 passed、318 skipped、1 deselected、3 warnings，586.17 秒，退出码 0**。包括架构边界、Core、Session、Tool Engine、ACP、SDK、原生 Windows 清理及桌面 stdio 六场景。
- 警告：1 项 requests 对现有依赖版本组合的提示，2 项 tarfile 在 Python 3.14 的默认过滤策略变更提示。未通过修改锁文件隐藏警告。
- 最终源码编译：`.venv/Scripts/python.exe -m compileall -q box_agent` 通过。
- 最终包构建：`.venv/Scripts/uv.exe --cache-dir workspace/uv-cache build` 成功，生成 `dist/box_agent-0.9.8-py3-none-any.whl` 和 `dist/box_agent-0.9.8.tar.gz`。构建提示缓存目录位于源码内；实际核查 wheel 1099 项、sdist 1617 项，均未包含 workspace、pip、.venv、.box-agent 或字节码缓存，日志、runtime 安装器和 bundled Windows 渲染实现与最终源码逐字节一致。
- 构建后重新运行 `tests/test_trace_viewer.py::test_built_wheel_contains_viewer_assets_without_bytecode_cache` 和 `tests/test_trace_viewer.py::test_built_sdist_contains_viewer_assets_without_bytecode_cache`：**2 passed**。包检查只证明 Python 分发包，不代表 standalone runtime 或桌面安装包已构建。

### 未验证范围

现有跳过条件分类共 318 项：真实浏览器 215、Canvas 3、字体及锁定导出器依赖 27、真实模型/MCP 配置或显式联网探针 19、POSIX/平台权限/可用 shell 53、当前 bundle 已应用 overlay 的分支 1。保留这些显式条件，不通过注入真实凭据、修改锁文件或伪造平台支持来消除跳过。另有 1 项无法连接的 MCP 超时用例按既有 preflight 明确 deselect。

最终跳过总数与上述分类一致。源码包构建、开发客户端源码联调、standalone runtime、正式安装版分别报告，不混为发布成功。
