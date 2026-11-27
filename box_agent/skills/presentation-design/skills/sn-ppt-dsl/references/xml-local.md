# 本地 SML XML 合同

使用豆包归档中的 SML 2.0 XML 语法和坐标布局。画布固定 `960×540`；输出为 `13⅓×7½` 英寸，坐标单位和字号均按一单位等于一磅转换。XML 命名空间只用于识别格式，不会联网访问该地址。

## 最小页面

```xml
<slide xmlns="https://www.larkoffice.com/sml/2.0">
  <style><fill><fillColor color="rgb(247,248,245)"/></fill></style>
  <data>
    <shape type="text" topLeftX="48" topLeftY="48" width="864" height="68">
      <content fontFamily="Noto Sans CJK SC" fontSize="32" color="rgb(28,42,48)" bold="true" lineSpacing="multiple:1.25" verticalAlign="top">
        <p>让标题表达明确的判断</p>
      </content>
    </shape>
    <shape type="text" topLeftX="48" topLeftY="155" width="820" height="130">
      <content fontFamily="Noto Sans CJK SC" fontSize="22" color="rgb(28,42,48)" lineSpacing="multiple:1.3" verticalAlign="top">
        <p>说明结论，用 <strong>加粗</strong> 建立重点。</p>
        <p>保留真实来源和必要条件。</p>
      </content>
    </shape>
  </data>
  <note><content><p>本页的讲解要点和证据说明。</p></content></note>
</slide>
```

目录模式按 `slides/slide_*.xml` 的自然顺序构建；文件模式接受完整 `<presentation width="960" height="540">` 或单页 `<slide>`。可选 `theme.xml` 的根为 `<theme>`。页数与现有大纲不同会报错。

## 当前输出能力

| 元素 | 本地输出 |
|---|---|
| 文本和富文本 | 原生文本；p、br、span、strong、em、u、del、a，以及单层列表 |
| 形状 | 原生矩形、圆角矩形、椭圆、三角形、菱形、多边形、箭头及常用流程形状 |
| 直线/连接线 | 原生线条、折线、曲线连接与实心箭头 |
| 图片 | 本地素材、居中/四侧裁剪、圆形与圆角裁剪；保留图片对象 |
| IconPark | 本地图标包解析成图片对象；使用上游图标检索器得到真实名称 |
| 表格 | 原生可编辑表格；固定行列尺寸，不自动拆页 |
| 图表 | 柱、条、折线、面积、饼、环、雷达及单轴组合图；内嵌数据工作簿 |
| SVG | 静态内嵌 SVG 渲染为图片对象；不能把其中的字当作可编辑正文 |
| 颜色与渐变 | rgb/rgba；线性渐变保留为原生 OOXML 渐变 |
| 备注/链接 | 原生讲稿备注和文本超链接 |

图表详细结构参考 `../vendor/doubao/references/xml/slides_chart_demo.xml`（路径相对技能根为 `vendor/doubao/...`）。范例展示上游完整能力，并非每个高级属性已映射；编译器明确拒绝未支持项。

目前不支持原生公式、合并单元格、双纵轴、交互提示、动画和完整上游形状全集。遇到这些需求，使用可表达同样信息的原生元素或明确告知局限；不要静默改成整页图片。所有未支持标签/属性都会报出源 XML 路径。

## 排版注意

- 中文标题建议 28–40，正文 18–24；保持稳定字阶，留出实际换行余量。
- 给“万辆”等短单位保留至少额外一个汉字的宽度，避免文本内边距让两个字在实际PPTX中折成两行；静态检查通过后仍检查数值与单位的实渲染。
- `dsl.py preview` 已用 `pdftotext -raw` 按页核对原生段落与渲染 PDF 的中文、英文、拼音、数字和单位。它容忍正常换行；抽取顺序造成的差异记为 `text_flow` 风险，回看原尺寸确认。不要临时改用整册字符计数或 `-layout` 代替入口检查。
- 地图和证据图的XML显示框保持原素材宽高比，防止居中裁剪截掉地理边界或表格脚注；地图着色不代表数值时，在图旁明确写“仅作区域定位”。
- 默认字体为随技能提供的 `Noto Sans CJK SC`。改用其他字体前，确认最终渲染环境可用；不要只依赖本机字体名称列表。
- 默认 `autoFit="no-auto-fit"`；优先调整文本框和内容分配。自动缩字或扩高并不能解决所有遮挡。
- `shape` 与其他元素都是平级坐标对象；文字没有因为画在矩形内就成为矩形的子元素。
- 本地导出圆角形状须使用 `type="round-rect"` 配合正数 `presetHandlers`；不要照上游示例对 `type="rect"` 设置正数圆角，本地编译器会拒绝。背景分区内的文本框边界也应完整落在分区内，即使实际字形没有越界。
- 数据条与轴线刚好共边可能被当前静态检查器识别为线穿过形状；给轴线留出少量真实间隙，不关闭校验。
- 原生折线图在 LibreOffice 预览中可能让数据标签贴在线上，即使静态校验通过；发现穿字时取消图内标签，在图框外设置按月份对齐的数值带，保留可编辑图表并重新渲染验收。
- 图片路径相对 XML 文件所在目录。裁剪中心是省略 `anchor` 的默认值；需要偏向一侧才写 `top/bottom/left/right`，上游协议不接受 `anchor="center"`。
- 表格的行高、列宽之和必须与声明的整体尺寸一致。保留文字内边距；内容太长要增加行高或缩短内容。
- 不要在 `<table>/<tr>/<td>` 下放通用 `<border>`：静态 schema 会以 `sxsd_unexpected_child` 拒绝它，即使渲染代码含对应分支。原生表格可先省略单元格边框；若要使用方向边框或完全去除网格，先核实本地编译器是否支持，不能只依据渲染代码猜语法。
- 图表使用明确、相同长度的分类和数值数组，不用格式化百分号代替数值。
- `chartLegend` 不写 `fontFamily`：虽然本地映射层列出了该属性，上游 XML 静态校验不接受；保留 `position/fontSize/color` 即可，字体沿用本地默认。
- XML 的 `&`、`<`、`>` 需要正确转义。几何布局由校验器辅助检查，美学与事实由 Agent 负责。
- 内嵌 SVG 的 `<marker>` 当前会被静态校验拒绝；箭头尖用受支持的 `<path>` 三角形直接绘制，或者改用原生 `<line><endArrow type="solid-triangle"/></line>`。不要靠移除校验或保留失效的 `marker-end` 绕过。
- 首次检查含图片的页面之前就写好 `assets/sources.json`；逐页 `check` 会校验来源清单，不能等全册完成后再补。
- 田字格的中线穿过示范字会被当前校验器判为 `bbox_overlap`。优先把示范字放在无中线的独立方框，把空田字格放在旁边用于书写；课堂指令应是“看示范→写一个”，没有浅色范字时不要要求在空格里“描红”。不能为此跳过校验。
- 儿童语文课件的课文引句、词语与朗读谱应按语义手动使用 `<br/>` 分行；检查器允许自动换行并不代表适合识读。实际预览重点排查把“小莲蓬”“雪白”“飘动”等词拆开或留下单字尾行的情况，优先改断行而非缩字号。
- 含 `<span>` 等混合内容的 `<p>` 不要用 `ElementTree.indent()` 或通用 pretty-printer 缩进：段首内联节点前新增的换行/空格会被转换器当作真实文字，造成额外空段、超大行距和成品裁切。保持段落内紧凑串行；修复时只删除确认由格式化新增的空白，保留有语义的空格，再重建并实际渲染。
- 教学对照图须按文字要求验收可观察特征，而非只检查图片是否显示：如初开与全开的花瓣数量区别、莲蓬是否露出、花苞是否饱满。概念相近但辨识度不够时制作局部近景，替换素材后重新检查对应页。

## 原生图表例子

```xml
<chart topLeftX="48" topLeftY="160" width="550" height="285">
  <chartPlotArea><chartPlot type="column"/></chartPlotArea>
  <chartData>
    <dim1><chartField name="阶段">阶段一,阶段二,阶段三</chartField></dim1>
    <dim2><chartField name="示例值">48,72,91</chartField></dim2>
  </chartData>
  <chartStyle><chartColorTheme><color value="rgb(39,106,115)"/></chartColorTheme></chartStyle>
</chart>
```

只有明确作为演示/测试时才使用示例数据；正式数据须来自任务材料。
