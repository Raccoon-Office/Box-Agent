# macOS CUA App adaptor

macOS 上启用 CUA 后，`computer-use` 提供 App 级 MCP 工具：`list_apps`、`get_app`、`list_windows`、`get_state`、`click`、`drag`、`scroll`、`set_value`、`type_text`、`paste`、`press_key`。adaptor 连接现有 CuaDriver；原生 helper 补充部分 AX 和后台输入能力，Driver 安装包不变。

## 注册和连接

`plugins.cua.tool_interface` 在 macOS 默认是 `app`，其他平台默认是 `driver`。原有 CUA 启用配置继续生效。`tool_interface: driver` 可显式使用 Driver 接口；切换后重启运行时。

`ready_tool.CuaService` 仍通过 `CuaRuntimeManager` 准备 Driver。共享 MCP loader 用该服务的配置覆盖同名旧注册，因此宿主继续提供旧的 `computer-use` Driver 定义时，不会同时暴露两套工具。显式 `disabled: true` 继续生效，其他名字的 MCP 服务保持自己的配置。

App wrapper 接收 `BOX_AGENT_CUA_RUNTIME_CONFIG` 中已解析的 `driver_path`、`endpoint` 和 `mode`。`external` 使用同一 socket 和 `--embedded`，不启动或关闭宿主 daemon。缺少宿主配置时，独立入口使用 `BOX_AGENT_CUA_DRIVER` 或默认 CuaDriver 安装路径。

Python 安装入口为 `box-agent-cua-mcp`，也可以执行 `python -m box_agent.mcp_servers.cua_server`。冻结运行时使用 `box-agent-acp --cua-mcp`，与 ACP 共用二进制但分开协议入口。服务只在连接后创建 Driver MCP 客户端。

## 代码职责

| 文件 | 职责 |
| --- | --- |
| `box_agent/mcp_servers/cua_server.py` | MCP 参数、App 引用、结构化结果和观察恢复 |
| `box_agent/mcp_servers/cua_adapter.py` | 窗口／附加表面关系、AX 编号、截图坐标、输入路由 |
| `box_agent/plugins/cua/runtime/connection.py` | readiness 与 wrapper 共用的 Driver 连接命令 |
| `box_agent/plugins/cua/runtime/coordinator.py` | 串行执行、取消后失效、在途调用与关闭的顺序 |
| `box_agent/mcp_servers/cua_panel_keyboard.py` | helper 构建缓存、进程调用、面板焦点 lease |
| `box_agent/mcp_servers/native/panel_keyboard.swift` | 原生 AX 操作、文件面板接收者和键盘事件 |

adaptor 的 App 观察和附加窗口关系逻辑移植自 mac-cua `dev/codex_v1.0`，初始基线 `fe6f3af`，关系校验对齐 `a62bc9f`。Box-Agent 保留 MCP 服务、运行时所有权、后台优先策略及截图像素坐标。

## 观察和投递

`get_app` 返回初始 AX 状态。模型使用本次观察的整数编号；adaptor 保存 Driver token 的原始来源。菜单和 Sheet 可来自父窗口 AX 树，控件来源与实际交互表面分开记录。截图坐标使用返回图片的像素，adaptor 完成窗口位置及缩放转换。

后台是默认投递方式。明确未投递的拒绝可继续既有 Driver／前台路径；已经投递、部分完成、超时或结果未知时不自动重放。显式前台请求直接使用前台路由。`set_value` 替换可写字段的值；`type_text` 在选区输入；`paste` 写入剪贴板再发送 Cmd+V，文本保留在剪贴板。

指定 `element` 的键盘输入在显式前台或拒绝回退时，继续携带原始控件 token、PID 和来源窗口，由 Driver 解析并聚焦该控件；不转换成无目标的 desktop 按键，也不把目标纠正到另一个窗口。窗口无法确认或 token 已失效时返回错误，需要重新观察。未指定元素的键盘操作保留原来的前台路由。

同一动作内复用昂贵的 `list_apps` 查询；窗口列表、几何和 AX 检查继续实时读取。进入后续动作、观察、激活或相关恢复阶段时重新获取应用信息。

## 原生 helper

helper 由捆绑 Swift 源码通过 `xcrun swiftc` 编译，按源码哈希和架构缓存在 `~/Library/Caches/Box-Agent/panel-keyboard/`。运行主机需要可用的 Swift 工具链及对应的辅助功能／输入权限。

- 文件面板：Cmd+O 通过菜单动作；Cmd+Shift+G、Return 和 Escape 解析系统面板的实际接收 PID。面板 lease 在重复调用间复用，并在目标变化、关闭或超时后释放。
- Sheet 控件：通过当前原生 AX 树重新定位控件，执行其声明的动作或修改可写文本字段。Driver token 不作为原生 AX 引用使用。
- 已聚焦编辑区：非 indexed 文本输入和编辑快捷键使用当前选区。焦点为 AXSplitGroup／AXGroup／AXScrollArea 时，从该焦点子树解析唯一正文候选；后台正文的 AXEnabled=false 不单独否决输入。保留 Cmd、Shift 等修饰键和完整 Unicode 文本，不赋值整个富文本文档的 AXValue。
- 搜索框：对已聚焦、可编辑的搜索字段投递 Escape。

原生操作返回投递证据，业务效果仍由后续观察确认。helper 使用部分私有 macOS 符号和事件字段；macOS、应用版本及权限变化需要实际桌面验收。进程级事件的投递前检查不能消除全部并发竞态。

## 取消与结果

`SerialCoordinator` 串行执行一个服务的请求。排队请求取消后撤回；在途请求取消后，worker 等待后端结束，释放 lease、使观察失效，再处理下一次请求。取消不撤销已发生的桌面操作。共享 MCP loader 继续使用主分支的连接生命周期管理。

下一项请求由事件循环在当前调用的完成／取消处理之后放行，再提交给 worker。即使底层调用先结束、取消协程稍后才恢复，已排队输入也会等待取消清理；取消调用方可以立即返回，清理完成前继续持有执行所有权。关闭服务时撤回尚在等待放行的请求。

结果保留 `request_id`、`output`、`backend_events`、`adapter_context`、`error_category`、`requires_requery`。图片通过 MCP image block 返回，继续由已有 CUA 图片插件处理。截图归档绝对路径修复单独见 [PR #220](https://github.com/Raccoon-Office/Box-Agent/pull/220)。

## 使用文档与验证

模型的工具使用说明见 [mac-cua](../box_agent/skills/mac-cua/SKILL.md)，Office 打开、保存、选择及修改文本的流程见 [office-gui-basics](../box_agent/skills/office-gui-basics/SKILL.md)。

回归测试使用 fake Driver、模拟 helper 和 MCP 内存传输，覆盖观察与 token 生命周期、附加窗口关系、后台路由、未知结果不重放、输入修饰键、同名注册覆盖、取消及资源关闭。Swift 编译和 Python 测试不能代替真实桌面结果。此次 PR 的后台编辑 C2C 由用户执行；PR 整理后的源码和注册变化需要重新构建运行包才会进入已安装客户端。
