---
name: mac-cua
description: "通过 Box-Agent 的 CUA MCP 工具操作 macOS 应用，使用 AX 元素、窗口截图、键盘和鼠标完成界面交互。"
---

# Mac-CUA

直接调用 CUA MCP 工具，参数使用 JSON 对象。adaptor 管理应用引用、当前交互窗口、AX 元素映射和 Driver 会话；调用时使用下面的工具参数。

先调用 `get_app`，例如：

```json
{"app":"TextEdit"}
```

它绑定已运行的应用，或启动应用并返回初始 AX 状态。读完返回状态后继续操作；后续调用沿用完全相同的 `app` 字符串。同一个应用的文档、菜单和弹窗共用这个引用。

## 工具与参数

下表中的 `?` 表示可选参数；实际 JSON 键名不带问号。

| 工具 | 参数 | 行为与返回 |
|---|---|---|
| `list_apps` | `{}` | 返回应用列表，包含 `id`，以及可用时的 `displayName`、`isRunning`。可将观察到的 `id` 用作 `get_app` 的 `app`。 |
| `get_app` | `app` | `app` 可为应用名称、bundle ID 或已安装 `.app` 的绝对路径。建立引用并返回当前 AX 状态；已有进程会被复用。 |
| `list_windows` | `app` | 返回窗口的 `id`、`title`、`bounds`、`layer`、`isActive`，以及可用时的 `isFocused`。`isActive` 表示 adaptor 选定的交互窗口。该调用用于查看窗口，不切换窗口；发现新的附加窗口并读取 AX 时可能使旧元素编号失效。 |
| `get_state` | `app`, `ax?`, `screenshot?`, `disable_diffing?` | 观察当前窗口、菜单或附加弹窗。默认 `ax=true`、`screenshot=false`、`disable_diffing=false`。返回 AX 文本，并按请求附带一张窗口截图。 |
| `click` | `app`, `target`, `button?`, `click_count?`, `action?`, `delivery_mode?` | `target` 是 AX 整数编号或截图像素坐标 `[x,y]`。`button` 为 `left`／`right`／`middle`，默认 `left`；`click_count` 默认为 `1`。`action` 默认 `press`，其他动作见下文。 |
| `drag` | `app`, `from`, `to` | 从截图坐标 `from:[x,y]` 拖到 `to:[x,y]`，使用前台投递。 |
| `scroll` | `app`, `target`, `direction`, `pages?` | `target` 是 AX 编号或截图坐标；`direction` 为 `up`／`down`／`left`／`right`。`pages` 为 1–50 的整数，默认 1，表示 Driver 的翻页步数，不是精确视口高度。 |
| `set_value` | `app`, `element`, `value` | 用字符串 `value` 替换可写纯文本控件的完整值。`element` 为当前 AX 编号，可用于可写的文件名或路径字段。 |
| `type_text` | `app`, `text`, `element?`, `delivery_mode?` | 输入字符串 `text`，在当前光标或选区处插入／替换。可用 `element` 指定当前 AX 控件。 |
| `paste` | `app`, `text`, `element?`, `delivery_mode?` | 将纯文本写入系统剪贴板并发送 Cmd+V。可用 `element` 指定当前 AX 控件；文本会留在剪贴板上。 |
| `press_key` | `app`, `key`, `element?`, `delivery_mode?` | 发送单键或组合键，如 `Return`、`Escape`、`cmd+s`、`cmd+shift+g`。可用 `element` 指定当前 AX 控件。 |

`click`、`scroll` 使用参数名 `target`；`set_value`、`type_text`、`paste`、`press_key` 的控件编号参数名为 `element`。编号是从 0 开始的整数，坐标是包含两个数字的数组。

## 观察状态

`get_state` 的常用形式：

| 参数 | 返回 |
|---|---|
| `{"app":"TextEdit"}` | AX 状态，使用默认差量输出。 |
| `{"app":"TextEdit","screenshot":true}` | AX 状态和窗口截图，可同时确定元素与坐标。 |
| `{"app":"TextEdit","ax":false,"screenshot":true}` | 只取截图；不会主动刷新 AX 编号，也不会让已经失效的编号重新有效。 |
| `{"app":"TextEdit","disable_diffing":true}` | 完整 AX 状态。 |

AX 与截图观察不主动激活应用。AX 文本标题标明当前窗口及其 ID；附加弹窗还可能标明 `AX source window ID`，表示控件来自另一个窗口的 AX 树。adaptor 处理这些来源关系，后续仍使用同一个 `app`。

连续观察同一界面时，AX 输出可能是全量、差量或无变化：

- `~`：用这一整行替换该编号之前的内容。
- `+`：增加这一行。
- `Removed element IDs: ...`：移除列出的编号。
- `There has been no change ...`：显示的 AX 树没有变化，但内部动作 token 已刷新。

差量应结合前一次状态阅读；需要完整上下文时使用 `disable_diffing:true`。编号是当前快照中的位置，不是控件的永久身份；同一数字在另一份快照中可能代表不同控件。

动作会使 AX 编号失效。操作后、切换应用后，或读取其他应用的 AX 后，先 `get_state` 再进行编号操作。连续的非编号键盘调用可以沿用应用引用，adaptor 会在输入前刷新焦点证据；按当前界面决定何时观察。

`AX tree incomplete` 表示树不完整，不能据此断定缺失控件不存在。AX 无法提供目标时，可通过截图确定可见目标，再选择坐标或键盘操作。截图请求若返回 `Screenshot unavailable`，本次没有可用图像。AX 文本不一定包含画布或文档正文，判断这些内容时读取截图。

## 截图坐标

`click`、`drag` 和 `scroll` 的 `[x,y]` 使用返回截图自身的像素坐标，原点是图片左上角。adaptor 负责窗口位置和显示缩放的转换。使用返回图像的像素尺寸定位，不把桌面绝对坐标或窗口 `bounds` 直接当成截图坐标。

截图对应当前选定窗口，可能包含透明区域或其他窗口背景。依据实际可见内容定位。输入、窗口移动、导航、滚动、弹窗变化或新的 AX-only 观察后，重新取图再选坐标。需要同时使用 AX 编号和坐标时，可在同一次 `get_state(screenshot=true)` 中取得二者。

## 点击与 AX 动作

`click(target=编号)` 默认执行控件点击。单次左键点击的元素若未声明 AXPress，adaptor 会尝试读取截图并根据可见控件边框定位中心点。编号点击也支持 `button="left", click_count=2` 的双击。

`action` 还可指定 `show_menu`、`pick`、`confirm`、`cancel`、`open`。这些动作使用元素编号、`button="left"` 和 `click_count=1`，要求当前元素声明相应动作。例如观察显示编号 12 的元素具有 `show menu`，可以调用 `click`：

```json
{"app":"TextEdit","target":12,"action":"show_menu"}
```

这里的 12 只是示例，实际使用观察中的编号。`confirm` 调用控件声明的确认动作；`press_key(key="Return")` 发送回车键。根据控件及操作选择，并观察后续界面是否按预期变化。

## 文本和键盘输入

按操作语义选择输入工具：

- `set_value` 替换字段的完整值，不依赖先全选文本；字段需要支持写入。赋值后观察实际值，再按任务需要提交或继续操作。
- `type_text` 在光标或选区处输入，适合插入、追加或需要键盘输入行为的控件。
- `paste` 通过剪贴板粘贴到输入目标。要替换已有内容时，根据当前选区决定是否需要先全选。粘贴失败时，剪贴板也可能已经改变。

例如，当前观察中编号 7 是要替换的可写字段，可调用 `set_value`：

```json
{"app":"TextEdit","element":7,"value":"新的字段内容"}
```

随后调用 `get_state` 检查字段及应用响应。字段值变化、提交动作和任务完成是不同的观察结果。

`type_text`、`paste`、`press_key` 不带 `element` 时使用当前输入焦点；带 `element` 时由 adaptor 解析该控件和所属窗口。键盘投递仍受实际接收窗口与焦点影响。

`press_key` 的组合键用 `+` 连接，修饰键在前、普通键在后。修饰键支持 `cmd`、`ctrl`、`shift`、`option`、`fn`；也接受 `super`→`cmd`、`control`→`ctrl`、`alt`→`option`。常用别名包括 `enter`→`return`、`esc`→`escape`、`backspace`→`delete`。

## 输入投递与窗口

adaptor 自动选择当前交互窗口、菜单或已识别的附加弹窗。需要切换文档时，使用应用自身的菜单或快捷键，再观察新的界面。

`click`、`type_text`、`paste`、`press_key` 的 `delivery_mode` 默认为 `background`。adaptor 会尝试后台路径；符合既有回退条件的明确拒绝可进入前台路径。观察确认后台输入无效或不可用时，可对相应调用指定 `delivery_mode:"foreground"`。前台输入可能激活应用或改变用户焦点。

应用是否处于系统前台，与其内部 AX 焦点窗口是不同状态。后台应用也可能有可操作的 AX 控件；是否能完成动作取决于当前控件和实际投递结果。

## 返回结果与恢复

状态文本和截图直接通过 MCP 返回。动作通常返回 `Done` 或 `Done (driver effect: ...)`，不附带新的界面状态；通过后续观察判断实际效果。`unverifiable` 表示工具未确认效果，不等于失败或未执行。

结构化结果中，`output` 是本次输出，`adapter_context` 是应用及元素上下文，`backend_events` 记录后端调用；`isError`、`error_category` 和 `requires_requery` 帮助判断后续恢复。上下文中的旧元素不能替代重新观察。

| 情况 | 后续操作 |
|---|---|
| `StaleElement`、`WindowChanged` | 用 `get_state` 读取当前界面，再选目标。 |
| `AttachmentUnverified`、窗口不明确 | 读取状态，必要时用 `list_windows` 了解窗口，再根据实际界面继续。 |
| `RequiresRequery` | 前一次调用在投递后被取消；先执行包含 AX 的 `get_state`，检查已发生的效果。 |
| `CUA connection reset; app references cleared`、应用进程已更换 | 重新 `get_app`，从返回的状态继续。 |
| 输入超时、效果未知或部分完成 | 先观察已经发生的变化，再决定下一步，避免重复输入或重复提交。 |

单次 MCP 调用的服务端预算为 120 秒，包含内部观察和投递。取消正在执行的调用不保证撤销已经发生的桌面操作。重建连接也不会回滚应用或文件变化。

同一个 CUA 服务会串行执行请求；其他客户端和用户仍共享桌面、焦点与剪贴板。操作过程中以实际返回状态为准。
