---
name: data-dashboard
description: 适用于把数据、表格或分析结果整理成数据看板、仪表盘及 HTML 分析报告，支持指标浏览、筛选和对比。
keywords: [dashboard, 数据看板, 看板, 仪表盘, 数据分析报告, 数据可视化]
---

# Data Dashboard Skill

## 0. 何时触发

需求主体是数据、表格或分析结果，且目标是数据看板、仪表盘，或以指标浏览、筛选和
对比为主的 HTML 分析报告时，使用本规范。

仅要求 HTML、网页、图表、可视化、动画，或面向管理层展示，都不足以判定为数据看板。
教学课件、课堂演示和幻灯片制作使用 `pptx` 入口；独立网页模拟器、游戏或其他应用按其
实际任务处理，不套用看板流程。已有页面的局部文字、颜色或交互修改沿用原任务和范围，
不因出现图表或 HTML 就重建为整份看板。

## 1. 非协商铁律（违反即重做）

1. **单文件自包含**：CSS / JS / 数据全部 inline。禁止外链 PNG/JPG。
2. **图表 = ECharts**。所有图表用 ECharts 5.x 渲染，CDN 引入。**禁止** `matplotlib` / `pandas.plot` 出 PNG，**禁止** `pandas.DataFrame.to_html()` 原样输出（识别特征：`class="dataframe"`）。
3. **不要复杂图就硬上 ECharts，但简单图也不要回退到 Chart.js**——统一栈，便于主题一致。
4. **无后端**。数据 inline 为 JS 常量，离线可双击打开。
5. **响应式**：桌面 / 平板 / 手机三档断点都不能裂。
6. 数字格式化必须本地化：千分位、百分比 1 位小数、货币缩写（K/M/B）、时长人类化。

## 2. 技术栈

| 用途 | 选型 |
|---|---|
| 图表 | ECharts 5.5+ via `cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js` |
| 框架 | 无，vanilla JS |
| 字体 | 系统栈优先：`-apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif` |
| 图标 | inline SVG 或 Unicode；不引图标字体库 |
| 数据 | 全量 inline `const D = {...}`，按页 key 组织 |

### 2.1 大文件落盘契约

- 若预计最终 HTML / CSS / JS 正文无法容纳在一次模型输出中，从一开始就对同一路径
  使用 `write_file` 有序分块：首块 `chunk_index=0, final=false`，后续递增索引，
  最后一块 `final=true`；每个生成块建议不超过 5,500 字符。
- `execute_code` 只用于数据计算、短小的文件修正和交付校验；禁止把整份 HTML、
  CSS、JS 或模板正文放进 `execute_code`。同样禁止用 `bash` heredoc、base64 或超长
  命令写页面正文。
- `bash` 可以执行短小的目录准备、合并脚本和校验命令，但页面正文必须来自分块文件
  写入，或来自已经落盘的 fragments / template 文件。

## 3. 架构骨架（每次都用这个）

> 📦 **完整可运行骨架见 `assets/template.html`**——200 行左右的脚手架，已包含 ECharts CDN、主题注册占位、路由懒渲染、`fmt/ui` 工具完整版、`D = {}` / `renderers = {}` 空对象。新任务复制该模板，填数据 + 写 renderer 即可。

```
┌─ <head>  meta + 主题 CSS 变量 + ECharts CDN
├─ <body>
│   ├─ .sidebar    侧边栏导航（多页时）
│   ├─ .main
│   │   ├─ .page#page-P1.active   首屏：洞察总结
│   │   ├─ .page#page-P2          ...
│   │   └─ .page#page-PN
│   └─ .footnote   数据口径 / 模拟数据声明
└─ <script>
    const D = { P1:{...}, P2:{...} };       // 数据
    const theme = {...};                    // ECharts 全局主题
    const fmt = {...};                      // 格式化工具
    const ui  = { mkKPI, mkTable, mkFunnel };// 通用 DOM 组件
    const renderers = { P1:()=>{}, ... };   // 每页渲染器
    showPage('P1');                         // 路由 + 懒渲染
```

**关键模式**：

- **ECharts 实例缓存**：`charts[id] = echarts.init(dom)`，在 `window.resize` 时统一 `charts[id].resize()`。
- **renderer 是纯函数**：只读 `D[id]`，只写 `#page-{id}` 子树，不互相依赖。
- **首页强制是"洞察总结"**：3-6 条 bullet + 核心 KPI + 一张总览图。让人 5 秒看完结论。

### 3.1 大看板分片模式（5+ 页时启用）

默认仍由一个 Agent 生成小看板。只有满足任一条件时，才使用子 Agent 并行分片：

- 看板有 **5 页或更多**；
- 单页已明确划分，且预计完整 HTML 会很长；
- 每页的图表、数据和 DOM 都能以 `P1`、`P2` 等页面 ID 隔离。

不要把多个子 Agent 指向同一个 `dashboard.html`。父 Agent 先串行确定页面、数据口径、主题 token、导航和共享交互；再让 **2–3 个**子 Agent 并行写各自独占的 `drafts/P*.json`。每个委派必须设置 `read_only: false` 和仅包含该 fragment 的 `write_scope`。父 Agent 不得把 fragment 正文读回后用模型拼接，而应运行 `scripts/merge_dashboard_fragments.js` 生成唯一的最终 HTML。

父 Agent 先写 `drafts/dashboard-contract.json`：

```json
{
  "title": "经营总览",
  "brand": "Northstar",
  "initialPage": "P1",
  "note": "数据截至 2026-07-14",
  "pages": [
    {"id": "P1", "label": "总览", "group": "概览"},
    {"id": "P2", "label": "趋势", "group": "经营"}
  ]
}
```

每个子 Agent 只写一个 fragment，字段如下：

```json
{
  "pageId": "P1",
  "data": {"kpis": [], "trend": []},
  "html": "<section class=\"page\" id=\"page-P1\">...</section>",
  "renderer": "() => { const d = D.P1; /* only render #page-P1 */ }",
  "css": "#page-P1 .local-panel { display: grid; }"
}
```

硬约束：`pageId` 必须在 contract 中且每页恰好一个 fragment；HTML 只能是一段对应 `#page-Pn` 的 `.page`，不得含 document-level 标签或 `<script>`；内部 `id` 必须以 `Pn-` 开头；renderer 只能是函数表达式，不得声明 `D`、`renderers` 或共享状态；局部 CSS 必须以 `#page-Pn` 开头，不能修改 `body`、`:root`、导航或主布局。页面排序永远以 contract 为准，不以文件传入顺序为准。

合并命令（先解析当前 skill 目录；不要假定当前工作目录）：

```bash
DATA_DASHBOARD_SKILL_DIR="${BOX_AGENT_DATA_DASHBOARD_SKILL_DIR:-${DATA_DASHBOARD_SKILL_DIR:-{skill_dir}}}"
${BOX_AGENT_NODE:-node} "$DATA_DASHBOARD_SKILL_DIR/scripts/merge_dashboard_fragments.js" \
  --template "$DATA_DASHBOARD_SKILL_DIR/assets/template.html" \
  --contract drafts/dashboard-contract.json \
  --out dashboard.html \
  drafts/P1.json drafts/P2.json drafts/P3.json
```

合并后父 Agent 负责唯一的最终检查：确认所有 contract 页面都存在、浏览器打开无 JS 错误、375/768/1440px 三档不裂、切页和 resize 后图表正常。若子 Agent 失败，仅重试对应 fragment；不要重做已通过的页面。

## 4. ECharts 用法约定

- 注册主题：`echarts.registerTheme('app', themeObj)`，所有 `echarts.init(dom, 'app')`。主题统一了背景、坐标轴、tooltip、palette。
- **每张图必给**：`title.text`（图名）、`tooltip`（hover 详情）、`grid` 留白（top/bottom/left/right ≥ 40）。
- **palette 不超过 8 色**，按数据语义映射（正向 / 负向 / 中性 / 高亮）。
- **复杂图保留给 ECharts 独家能力**：桑基（sankey）、关系（graph）、地图（map）、漏斗（funnel）、热力（heatmap）、雷达（radar）、treemap、parallel。
- 漏斗：优先用 `series.type='funnel'`；如果想要"宽度按值缩放 + 步骤转化率标注"的横向漏斗，可以用 `series.type='bar'` + `yAxis.type='category'` + 自定义 label。
- 图表容器：固定高度 `height: 320px`（KPI 行）/ `420px`（主图）。不要用 `height: auto` 否则 ECharts 报 0 size 警告。

## 5. 通用工具（每次都内置）

```js
// 数字
const fmt = {
  num: n => n.toLocaleString('zh-CN'),
  pct: (n, d=1) => (n*100).toFixed(d)+'%',
  money: (n, cur='¥') => cur + n.toLocaleString('zh-CN'),
  short: n => n>=1e8 ? (n/1e8).toFixed(1)+'亿'
            : n>=1e4 ? (n/1e4).toFixed(1)+'万'
            : n.toLocaleString('zh-CN'),
  sec: s => `${Math.floor(s/60)}分${s%60}秒`,
};

// DOM 组件
const ui = {
  kpi: (label, value, delta) => `<div class="kpi">...</div>`,
  table: (headers, rows) => `<table class="t">...</table>`,
  insight: (title, items) => `<div class="insight">...</div>`,
};
```

## 6. 视觉与设计规范

> ⚠️ **设计规范在 `references/design-spec.md` 中维护。开始渲染前必须完整读取它**，并把里面的 token 翻译为 `:root { --token: value }` CSS 变量与组件样式；ECharts palette 注册为主题 color 字段。全文件只用变量，不写硬编码色值。
> 如果 design-spec.md 已填写某条目，**完全以它为准**，不要叠加下方兜底规则——避免与设计语言冲突（例如 spec 明确"不用阴影"时，兜底里的"软阴影"必须丢弃）。

**兜底规则（仅当 design-spec.md 对应段落为空白时启用）：**

- 用 8-12 个 CSS 变量收口色彩与间距，禁止散落的 `#xxx` 硬编码。
- 信息密度：metric grid 用 `repeat(auto-fill, minmax(200px, 1fr))`。
- 数字字符等宽：`font-variant-numeric: tabular-nums`，表格数字列右对齐。
- 留白比对比更重要：section 间距 ≥ 24px，卡内 padding ≥ 16px。

## 7. 反模式

- ❌ 浅色 + 默认字体 + Bootstrap 卡片的"通用 AI 感"
- ❌ 一页到底长滚动，无导航、无分组
- ❌ 外链 PNG / matplotlib 出图
- ❌ pandas `to_html()` 直接拼字符串
- ❌ Chart.js 与 ECharts 混用
- ❌ 主题色硬编码在 30 个地方
- ❌ 没有 tooltip / 没有图名 / 没有口径说明的"裸图"
- ❌ 移动端裂版、表格横向溢出
- ❌ 数字未格式化（`28500000` 而非 `2,850 万` / `¥28.5M`）

## 8. 交付前 checklist

- [ ] 单文件 < 2MB，双击可开
- [ ] 移动端宽度 375px 不裂
- [ ] 所有 ECharts 实例响应 `window.resize`
- [ ] 所有数字本地化
- [ ] 每张图有 title + tooltip
- [ ] 首页 5 秒能读懂核心结论
- [ ] 如为模拟数据，页面有明显标注
