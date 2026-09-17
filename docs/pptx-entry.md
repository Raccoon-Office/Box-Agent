# PPT 统一入口

公共 Skill `pptx`（PPT 制作）负责选择，生成仍由独立的后端 Skill 执行。

| 用户模式 | 内部 Skill | 默认交付 |
|---|---|---|
| 快速模式 | `ppt-fast` | 沿用原主题、版式和编辑流程；请求 PPTX 时导出文件 |
| 设计模式：静态 | `sn-ppt-entry` → `sn-ppt-story` → `sn-ppt-standard` | 静态 HTML 和 PPTX |
| 设计模式：动态 | `sn-ppt-entry` → `sn-ppt-story` → `sn-ppt-dazzle` | 带动效的 HTML，不是原生 PowerPoint 动画 |

Tools 和 Doctor 为设计模式提供共用工具和检查。Standard 自带静态页面与 PPTX exporter，
保留源代码模块结构及字体许可。
设计模式的 PPTX 是文件导出，不宣传或承诺编辑能力。
静态字段为 `static_html` / `standard` / `static_postprocess`；默认要求 HTML 和 PPTX
同时交付，仅用户明确只要 HTML 时省略 PPTX。续作时保留绝对任务目录、原材料、大纲、
页面及已确认的输出选择。

静态整册入口固定为同一任务目录的 `present.html`。父级先执行 Standard 的 `deck.py build`
与 `deck.py audit`，核对页面覆盖、资源和播放器，再完成最终像素检查及所需 PPTX 导出；
登记 `state.artifacts.present_html` 并在最终回复提供真实 HTML 链接。仅逐页文件或 PPTX
成功不足以宣告完成；播放器缺失时复用已有页面补齐，失败保留产物并记录 `partial`。
动态出口继续使用 `deck.html`。这是 Skill 收尾要求，未增加内核强制门或另一套汇总脚本。

## 选择与续跑

OfficeV3 直接发送用户需求，不做 PPT 正则分类或发送前的模式拦截。Box-Agent 原有通用
Skill 发现将匹配的入口放入目录，模型需调用 `get_skill` 读取完整指引；显式选择则由宿主
直接提供 Skill reference。当前 ACP 不按 PPT 关键词自动预载正文。用户在 prompt 中明确选择
“快速模式”或“设计模式”作为制作方式，或同一任务已有用户亲自作出的模式选择时，直接路由。
“帮我设计一下 PPT”或“介绍软件设计模式的 PPT”不构成选择；对模式的提问、比较、引用和
否定同样不算。明确要求制作动态 PPT、动态演示或带动效幻灯片时，也直接进入设计模式，设置
`dynamic_html` / `dazzle`，仍通过 Entry → Story → Dazzle，不再弹模式选择卡。
动态必须描述要制作的呈现形式；否定、引用、比较、提问或“行业动态”等内容主题不算。
本轮明确改做动态演示优先于历史模式；本轮同时指定快速模式与动态演示时先澄清冲突。其余
请求均调用 `request_user_decision`，使用 `presentation_mode` 分类和 `fast`/`design`
选项。套模板、自由设计、静态、文件格式或模型已写入的默认模式不能代替用户选择。
普通模式卡由模型推荐 `fast` 或 `design`，传入默认项、30 秒及低风险可逆声明；
宿主收到点击或超时后提交选择。名称含糊或要求冲突时仍手动澄清，不设倒计时。
模式名称含糊或要求冲突时同样通过选择卡澄清，说明待确认的点，选项只包含当前可执行路线。
这仍是 Skill 的执行指引，不增加前端正则判断。

system 的通用 Skill 指引要求匹配任务先读取 Skill，且 Skill 要求人工选择时必须调用
决策工具并等待。`pptx` 强调 JSON 示例只是工具参数，普通正文不会触发选择卡。
同一任务同时匹配入口与旧后端时，用户未指定 Skill 则先读取入口，由入口路由；不删除
或禁用用户安装的旧 Skill，用户明确选择其他 Skill 时仍尊重其选择。
这些指引改善模型遵循，不能当作运行时对每次路由的强制保证；验收需检查实际
`get_skill`/显式 reference 和 `request_user_decision` 事件，不能仅看模型口头说明。

若模型用正文请求用户选择却没有调用工具，现有通用结束检查会结合本步真实提供的工具，
识别未完成的结构化交互并要求补调。仅解释选项差异、用户明确只要文字选项、工具不可用
且正文已给出完整选择时，不因此补卡。检查沿用当前主请求的思考开关及绑定 client 的
参数映射，不强制关闭思考。恢复保留人工选择要求，不擅自设置默认值或倒计时；仍受
原有两次恢复上限、剩余轮数和取消机制约束。判断依赖模型，不保证每次都能纠正漏调用；
先前已流式显示的正文可能仍可见，实际卡片随后出现。

工具新增手动等待用法：省略 `default_option_id`、`requested_auto_submit_seconds` 及
相应安全声明。返回既有 `user_decision_request`，`autoSubmit.allowed=false`。原有请求
超时默认选项的调用仍必须提供完整声明，安全策略不变。

工具成功后现有 `ends_turn_on_success` 机制停止当前轮；OfficeV3 通过现有
`UserDecisionCard` 展示按钮，并以 `_meta.user_decision` 续跑同一个任务。ACP 将选择写入
既有 `[HOST_USER_DECISION_RESPONSE]` 用户消息；不增加 PPT 专用 RPC、状态文件或内核
策略。重复加载入口、补充材料和恢复会话沿用已有选择。取消卡片不自动选择任何模式。

## 名称和资源

- 制作模式的选项 ID 为 `fast` / `design`。未明确选择模式时通过选择卡确认，名称含糊时澄清。
- `html-templates` 只提供视觉样式；新建 PPT/PPTX 或 HTML 幻灯片先读 `pptx`，
  确定模式后再应用模板。自动选择视觉模板不能代替制作模式选择。
- 原 `pptx` 注册名改为 `ppt-fast`；物理目录 `document-skills/pptx/` 保留，避免移动原有
  导出器、受信同步脚本和打包资源路径。新入口位于 `skills/pptx/SKILL.md`。
- 公共入口与后端设置 `metadata.allow_override=false`，避免旧用户安装覆盖该套件。
  后端另设 `metadata.user_visible=false`；它们不参与普通目录推荐，但允许精确读取。
  显式禁用、任务作用域和 Connector 授权限制仍生效。

## 视觉检查

`inspect_images` 的代理请求继承主会话当前的 thinking 开关，包括首轮附件预处理和
后续轮次切换；不再因子请求漏传而落入默认关闭值。同一主/视觉 client 使用同一套
provider 参数映射；单独配置的视觉 client 保留自身 provider 和关闭思考时的档位策略。
原生图片路径继续直接随主请求发送。主会话本身关闭思考时的服务兼容配置仍由 provider
负责，此改动不把所有模型的 `none` 全局替换为 `low`。

## 更新设计模块

上传字体的 `User::<id>` 只作内部注册标识，`Deck-*` 继续用于 HTML 字体子集；PPTX 从原始
字体文件的 name table 读取真实字体族名（优先 name 16，再取 name 1），不使用配置显示名
或文件名。字体许可确认、路径边界和原文件 hash 校验保持原有要求。PPTX 保留字体名，
不因此嵌入字体或保证其他电脑已安装该字体。

旧任务若在字体 manifest 中写入了 `User::`、`Deck-*` 或无效的 `source_family`，需从
原字体重新运行 `font_bundle.py` 的 bundle 流程再导出；不靠改写 HTML 别名修复。
缺失映射、已存在但损坏的 manifest 同样给出重建错误，普通无字体 bundle 的导出仍可用。
合法名称里的逗号、引号、反斜杠和 `&` 会被正确解析并编码为 XML，不改写浏览器 IR。

Standard 的单页、批量截图和播放器审计共用同步 Playwright 生命周期。每次调用由独立
监督进程管理总期限和并发槽，worker 持有 driver 和 browser，每页使用独立 context；
批量任务内复用 browser。Chromium 启动守卫在启动浏览器前登记专属进程组，父进程消失
时自行终止该组；守卫提前退出时，监督进程按已登记组及进程身份清理后代。不会按进程
名称、年龄或孤儿状态清理其他任务。导入模块和 `--help` 不启动或清理浏览器。

`RENDER_JOB_TIMEOUT` 默认 600 秒，包含排队、启动、页面处理及预留的关闭/强制清理时间。
`deck.py audit` 的调用预算为 180 秒；字体处理后的整册重渲染为 600 秒。各页另有有限
阶段期限，播放器逐页更新阶段期限但不延长任务总预算。超时或取消后先完成本任务清理，
再释放槽位；主错误和清理错误均保留。只有明确的 `TargetClosedError` 可在确认清理后
重试（单页最多三次、批量最多两次、审计不重试），内容、环境和清理错误不因此重试。

`RENDER_GLOBAL_LIMIT=0` 默认不设全局槽限制；配置正整数后，同一 `RENDER_LOCK_DIR`
内共用该上限。排队受 `RENDER_SLOT_TIMEOUT`（默认 900 秒）和任务剩余预算共同约束，
超时失败，不绕过上限。该实现针对 macOS/Linux POSIX，依赖 Playwright 和 `psutil>=5.9`；
Skill 安装脚本和 requirements 同步声明依赖。Windows 需要单独的进程所有权实现。

源库保持独立开发。`scripts/sync_presentation_suite.py` 从指定 Git 提交读取六模块及所需
资源，用可检查的替换适配两个出口和 Box-Agent 工具名称。源文本变化不满足适配条件时
同步失败，要求维护者重新检查，不静默套用旧修改。生命周期适配输入位于
`scripts/presentation_suite_overlays/`，不要直接修改打包副本。`source.json` 记录来源、
本地 helper 输入与集成后文件哈希。

```bash
uv run python scripts/sync_presentation_suite.py --source-checkout /path/to/sensenova-presentation-int
uv run python scripts/generate_skills_manifest.py
uv run pytest -q tests/test_pptx_entry.py tests/test_presentation_suite_bundle.py tests/test_request_user_decision_tool.py
```

跨仓库交付需要重建 Box-Agent runtime 并将其装入 OfficeV3 包。连接正式服务的未签名
macOS 包使用 `publish:electron-mac-unsigned`；`:test` 会编入测试服务地址。用户在新包内
登录正式环境后，客户端自动同步 Box-Agent 登录态。源测试、包内协议探针、真实模型
生成和视觉质量分别验收，不能互相替代。
