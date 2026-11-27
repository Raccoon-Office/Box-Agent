---
name: computer-use
description: "通过独立的 CUA 工具组操作 macOS 原生应用；用 cua_repl_js 编写 JS，使用 AX、截图、键盘和鼠标完成交互。网页任务使用 browser-use。"
keywords:
  - 桌面控制
  - 打开计算器
  - 计算器
  - 原生应用
  - 操作飞书
  - computer-use
  - desktop application
---

# macOS 桌面控制

## 桌面控制入口

当前描述已有 `cua_repl_js` 的有效 `tool_ref` 时，直接用 `call_tool`。外层只传 `tool_ref` 和 `arguments_json`，后者是包含 `code` 的 JSON 字符串。缺少引用或引用过期时，用 `tool_search` 搜索 `{"tool_names":["cua_repl_js"]}`，等搜索结果返回，在下一模型步骤调用。后续代码格复用有效引用。仅当旧协议没有提供 `call_tool` 且按名激活时，使用当前步骤实际提供的工具名。

搜索仍无入口或 Driver 未就绪时报告具体原因。原生 GUI 操作仅用 CUA REPL。任务要求通过桌面版 Word、Excel、PowerPoint 或 WPS 编辑文件时，同时读取 `office-gui-basics`，按实际应用核对选区、文档和保存位置。

例如，首次绑定 Excel 时，把下面的占位符换成本次返回的引用，将整个外层对象传给 `call_tool`。

```json
{"tool_ref":"<returned tool_ref>","arguments_json":"{\"code\":\"var targetApp = 'Microsoft Excel'; var app = await cua.getApp(targetApp);\",\"title\":\"绑定 Excel\"}"}
```

`unexpected outer fields` 或 `Invalid arguments_json` 表示这一格 JS 尚未执行。保留有效引用和已有变量。把 `code` 和可选的 `title` 一起序列化到 `arguments_json`，再发这一格。

## 绑定应用与复用 app

每个代码块是一格 REPL。新会话的第一格只绑定用户指定的应用，例如文本编辑：

```js
var targetApp = "TextEdit";
var app = await cua.getApp(targetApp);
```

这次调用会显示操作手册和应用的初始辅助功能树（AX）。读回窗口标题和控件后，下一格直接使用已有的 `app`。若需先辨认应用，第一格可单独运行 `await cua.listApps()`，再按返回的名称或 Bundle ID 绑定。

如果 AX 显示 `Window: none`，先看它是否列出了应用菜单。例如文本编辑没有打开文稿，但 AX 中有“文件”菜单时，点击该菜单的当前编号，执行 `await app.getAXState()`，再从返回的菜单项中选择“新建”。新窗口出现后读回标题和正文。只有菜单时按 AX 操作，此时没有窗口截图是预期结果。

**同一应用内换窗口、点控件、写文字，都可以复用 `app`。** 切换目标应用时，先把 `targetApp` 更新为本次指定的名称或 Bundle ID；原进程重启时保留该值。用 `app = await cua.getApp(targetApp)` 重新绑定，再核对窗口标题。控件编号必须取自当前目标窗口的最新 AX。操作后若要再点下一个控件，先观察结果，再选取编号。

## 示例：在文本编辑中新建文稿

需要辨认多个窗口时，在已绑定的 `app` 上列出窗口，再读当前窗口的完整 AX：

```js
await app.listWindows();
await app.getAXState({disableDiffing: true});
```

窗口列表提供标题和窗口 ID；点击控件用 AX 树中的控件编号。发送新建快捷键前，检查查找栏、浮动面板和其他附加窗口。若需要聚焦正文，从最新 AX 选正文控件，点击后重新观察焦点。确认目标后执行：

```js
await app.pressKey("super+n");
await app.getAXState({disableDiffing: true});
```

核对新文稿标题和正文焦点。焦点不在正文时，用最新 AX 的正文编号点击，再读回焦点。看到正文已获得焦点后，将示例文字换成用户原文，执行下一格：

```js
await app.typeText("周五前确认参会人数。");
await app.getAXState({disableDiffing: true});
```

核对同一文稿的正文是否出现完整原文。`typeText(text)` 和 `paste(text)` 写入当前焦点；对 AX 已确认可写的字段，可用 `app.setValue(id, text)` 指定控件。

`app.pressKey` 只接收一个非空字符串。Command+A 写作 `await app.pressKey("super+a")`，
Shift+左箭头写作 `await app.pressKey("shift+Left")`。传第二个参数（例如
`app.pressKey("a", {modifiers: 4})`）或未知键会触发 `InvalidArguments`，该次按键尚未发送。
确认同一格里先前动作的结果，再单独执行修正后的调用。输入整段原文用 `typeText` 或 `paste`。

## 观察和恢复

需要坐标点击或拖放时，先调用 `await app.getAXStateAndScreenshot()`，确认模型实际收到图像。普通窗口使用截图局部坐标，附加窗口点击按返回的根窗口 bounds，菜单表面用自己的 bounds。附加窗口不能拖放或按坐标滚动。菜单图若混入父对话框或大片空白，且无法把菜单项对应到 bounds，就停止坐标点击，改用可核实的控件或菜单高亮。

按键（包括 `Escape`）后需要新图才能使用坐标。例如退出幻灯片的文字编辑再切页，第一格执行 `await app.pressKey('Escape'); await app.getScreenshot();`。看完新图，下一格按新图坐标点击缩略图，再观察页码。

Finder 行内改名时，确认选中的是目标文件，改名框仍在编辑且默认选区覆盖文件名主体。保留焦点，直接用 `await app.typeText(newStem)` 输入所需主体，单独读完整 AX 核对编辑值和扩展名。确认后按 Return，再从文件列表核对新名称。若编辑框消失、文件变了或收到窗口校验拒绝，先停下读回 Finder，确认目标后再进入编辑。重新点击主窗口会打断临时编辑框。

滚动正文优先把最新 AX 中的正文编号作为 `app.scroll` 的第一个参数。普通窗口需要按位置滚动时，把当前截图上的点写成二元数组 `[x, y]`，例如 `await app.scroll([x, y], 'up', 4)`，其中 x、y 要换成实际观察到的数值。

| 遇到的情况 | 下一步 |
| --- | --- |
| `WindowChanged`，且初始化失败 | 下一格只用 `app = await cua.getApp(targetApp)` 重新绑定原任务应用。核对返回的窗口标题或菜单，再发输入。 |
| 动作阶段出现 `WindowChanged` 或 `AttachmentUnverified` | 保留已有 `app`，下一格只执行 `await app.getAXStateAndScreenshot({disableDiffing: true})`，核对窗口、面板和焦点。若目标控件可验证，按需聚焦并读回，再继续。Finder“前往文件夹”若有可写路径字段，用最新编号填入并读回；若仍只看到浮层、AX 没有可验证的输入目标，停止向该浮层发键并报告卡点。换用粘贴、Escape 或 reset 都不能证明焦点。 |
| `StaleElement` 或差量里找不到目标 | 单独取 `app.getAXState({disableDiffing: true})`，从结果重选编号。差量显示无变化时也不能只在该差量里查找控件。 |
| `TargetUnverified`，控件归属未通过校验 | 这次输入未发送。先取完整 AX，核对目标窗口和控件。若刷新后同一菜单项仍被拒绝，停止点该项。仅当确认同一菜单仍打开且能观察高亮时，按当前高亮用 `await app.pressKey("Down")` 或 `"Up"` 移动，逐步读回。看到目标项高亮才按 Return，回到父对话框核对字段值。无法确认菜单或高亮时停止输入。 |
| `Coordinates require a current screenshot of this window` | 点击尚未发送，排在失败点击后面的截图也未执行。下一格只执行 `await app.getScreenshot()` 或 `await app.getAXStateAndScreenshot()`；看图后下一格直接按新图选点。中间若又单独读 AX、输入或换窗，须重新取图。 |
| 单击 AX 控件报 `AXPress` 返回 `-25204` | 动作可能已生效。下一格只执行 `await app.getAXState({disableDiffing: true})`；需要图像时用 `await app.getAXStateAndScreenshot()`。例如点 Excel“排序”后若已看到排序对话框，从对话框最新编号继续。只有读回明确显示原操作未生效，才考虑再次点击；仍无法判断时先补充观察。 |
| 点击返回 `AXOpen` 警告，或动作结果不确定 | 点击可能已经生效。下一格先用 `await app.getAXState()` 检查菜单或窗口，再决定是否继续点击。 |
| 超时报错明确写了 `Session reset` | 新一格重新执行本次目标应用的初始化格（重新设置 `targetApp` 并绑定 `app`），从初始 AX 确认上一格已产生的效果。 |
| REPL 断线且需要显式重置 | 按入口规则调用 `cua_repl_reset`，目标参数为 `{}`。然后用 `cua_repl_js` 重新绑定应用，读回窗口或菜单再继续。 |

失败格也可能占用 `let` / `const` 变量名。复用已有变量，可重算的临时数据用 `var` 在 GUI 动作前单独准备。使用顶层 `await`，不写顶层 `return`。观察方法自动输出，额外结果用 `nodeRepl.write(value)`。reset 清除变量和观察记录，应用和文档状态保留。
网页任务使用 `browser-use`。CUA 仅由主代理操作。
