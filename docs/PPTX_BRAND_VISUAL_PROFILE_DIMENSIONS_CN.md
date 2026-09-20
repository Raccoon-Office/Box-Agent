# PPTX 品牌视觉维度契约

品牌视觉扩展用于把一个具体产品、文化场景或活动主题映射到可组合的视觉语汇。它补充基础主题和逐页布局，不替代主题、配色契约或内容大纲。

## 设计边界

```text
内容与受众 → 视觉 profile → 基础主题 → 配色契约 → 逐页布局 → 素材与装饰 → 编译校验
```

- 主模型负责内容、事实、受众和交付流程。
- 设计角色负责 profile、基础主题、完整配色和逐页布局。
- 程序负责字段合法性、对比度、素材绑定、渲染和 QA。
- HTML 编辑器中的人工修改拥有最高优先级。

## Profile 维度

每个 profile 只描述视觉意图和可组合语汇，不写具体页面坐标或 CSS。

| 维度 | 作用 | 示例值 |
| --- | --- | --- |
| `id` | 稳定标识 | `brazil-football` |
| `semantic_tags` | 主题、文化、受众和情绪标签 | `巴西`、`足球`、`热情` |
| `preferred_themes` | 适合承载的基础主题 | `bold-poster`、`stadium-score` |
| `direction` | 明暗、动静和正式度方向 | `dark`、`energetic`、`informal` |
| `palette_preset` | 推荐的高级感或品牌色板 | `tropical-green-yellow` |
| `color_roles` | 对主色、强调色和辅助色的语义要求 | `green-field`、`solar-yellow` |
| `motifs` | 可重复使用的图形母题 | `pitch-lines`、`scoreboard` |
| `geometry` | 形状、边框、角度和节奏 | `diagonal`、`hard-frame` |
| `typography` | 标题、正文和标签字体方向 | `poster-sans`、`condensed` |
| `composition` | 页面层级和构图倾向 | `dynamic-diagonal`、`stats-board` |
| `asset_style` | 图片、插画或图标的表现方式 | `action-sports` |
| `decoration_density` | 装饰出现比例 | `sparse`、`balanced`、`dominant` |
| `hard_constraints` | 不允许被主题静默覆盖的要求 | `keep-user-colors` |
| `provenance` | 官方品牌标准或创意解释的来源 | `creative-interpretation` |

## 推荐结构

```json
{
  "visual_profile": {
    "id": "brazil-football",
    "semantic_tags": ["巴西", "足球", "运动", "热情"],
    "preferred_themes": ["bold-poster", "stadium-score"],
    "direction": {"canvas": "solid", "mood": "energetic", "formality": "medium"},
    "palette_preset": "tropical-green-yellow",
    "color_roles": {
      "primary": "field-green",
      "accent": "solar-yellow",
      "secondary": "signal-blue",
      "usage": "balanced"
    },
    "motifs": ["pitch-lines", "scoreboard", "ball-circles"],
    "geometry": ["dynamic-diagonal", "bold-frame"],
    "typography": {"heading": "poster-sans", "body": "sans-serif", "label": "mono"},
    "composition": ["hero-action", "stats-board", "match-timeline"],
    "asset_style": "action-sports",
    "decoration_density": "balanced",
    "hard_constraints": ["preserve-user-palette"],
    "provenance": "creative-interpretation"
  }
}
```

## 三个组合例子

### 巴西足球

```text
运动海报主题
+ 热带绿黄配色
+ 球场线、比分牌、圆形足球母题
+ 动态斜线和比赛时间线
+ 运动动作图片
```

### 西班牙舞娘

```text
编辑或舞台主题
+ 红黑、陶土或深酒红配色
+ 扇形、褶皱、舞台光带母题
+ 纵向舞台构图和节奏型时间线
+ 舞者姿态或剪影素材
```

### 乐高搭建火车

```text
模块化积木主题
+ 红黄蓝配色
+ 凸点网格、硬投影、方角卡片
+ 一块卡片对应一个积木模块
+ 火车拼搭和亲子协作插画
+ 车头 → 车厢 → 连接 → 测试的步骤构图
```

## 选择规则

1. 先从内容、受众和文化语义匹配 profile。
2. 再从 `preferred_themes` 中选择能承载该 profile 的基础主题。
3. 用户明确颜色进入锁定的配色契约；profile 只能补足未指定角色。
4. profile 的图形母题和素材风格可以跨布局复用，不能修改内容顺序和事实。
5. 没有 exact profile 时，组合通用语汇；不能因此阻塞或切换成无关风格。
6. 只有反复出现且需要稳定复用的视觉语言，才提升为新的注册 profile。
7. 官方品牌色和创意解释必须记录不同的 `provenance`，不能把创意配色说成官方标准。

## 与主题、布局和配色的关系

```text
基础主题：决定整体视觉骨架
品牌 profile：决定产品或文化识别语汇
配色契约：决定最终色值和使用比例
布局：决定每一页的信息结构
素材：决定真实场景和情绪证据
```

profile 不绑定固定布局。一个 profile 可以同时使用封面、卡片、时间线、对比和收尾布局，只要它们共享相同的视觉语汇和配色契约。

## 可选能力模块

核心 profile 负责视觉身份；特殊 PPT 类型通过可选模块补充表达约束。模块是能力声明，不是新的主题或布局系统。

### 数据模块 `data_module`

用于 KPI、经营复盘、财务分析和数据故事。

```json
{
  "data_module": {
    "chart_language": "minimal-editorial",
    "data_density": "medium",
    "metric_emphasis": "primary-value",
    "axis_style": "quiet",
    "annotation_style": "direct-label",
    "number_format": "preserve-source-units",
    "missing_value_policy": "show-gap"
  }
}
```

程序仍负责数值完整性、单位一致性和图表字段校验。设计角色只决定图表视觉语言和信息层级，不能补造数据。

### 证据模块 `evidence_module`

用于研究、医学、法律、政府和需要引用来源的 PPT。

```json
{
  "evidence_module": {
    "evidence_mode": "public-authoritative",
    "citation_style": "compact-footnote",
    "source_provenance": "visible",
    "uncertainty_display": "neutral-note",
    "unsupported_claim_policy": "omit-or-placeholder",
    "compliance_level": "strict"
  }
}
```

事实、来源和不确定性由内容与研究流程负责；profile 只规定引用如何呈现，不能把未验证内容包装成已证实结论。

### 媒体模块 `media_module`

用于产品发布、生活方式、体育、旅游和图片主导的 PPT。

```json
{
  "media_module": {
    "media_policy": "web-first-then-ai-labelled",
    "image_direction": "documentary-or-conceptual",
    "subject_position": "right-safe-for-copy",
    "crop_behavior": "preserve-subject",
    "caption_style": "small-source-rail",
    "required_media_ratio": "one-hero-plus-supporting"
  }
}
```

素材获取、授权状态、文件存在性和页面绑定由程序与素材流程校验。没有可用图片时必须保留可编辑版式，不能伪造素材来源。

### 无障碍模块 `accessibility_module`

用于公共发布、教育、企业内训和需要广泛阅读的 PPT。

```json
{
  "accessibility_module": {
    "minimum_contrast": "4.5:1",
    "body_text_floor": "readable",
    "color_only_encoding": "forbidden",
    "alt_text_policy": "required-for-meaningful-media",
    "motion_preference": "respect-reduced-motion",
    "language_script": "zh-CN"
  }
}
```

配色契约负责实际对比度检查；布局和内容仍需提供文字、标签或形状等非颜色线索。

### 动效模块 `motion_module`

用于演讲型、互动型和分步揭示的 PPT。它是可选的，静态 HTML 交付可以省略。

```json
{
  "motion_module": {
    "motion_language": "subtle-reveal",
    "transition_style": "cut-or-fade",
    "interaction_level": "low",
    "reveal_order": "narrative-order"
  }
}
```

动效不能改变页面事实、顺序或用户编辑后的内容。关闭动效后，页面仍应完整可读。

### 多品牌模块 `co_brand_module`

用于联合品牌、合作伙伴和渠道方案。

```json
{
  "co_brand_module": {
    "brand_priority": ["primary-brand", "partner-brand"],
    "shared_palette": "neutral-base-with-locked-accents",
    "logo_rules": "preserve-supplied-assets",
    "partner_visibility": "footer-and-closing"
  }
}
```

合作方颜色、Logo 和露出位置属于用户约束或 supplied assets，不能由主题默认值覆盖。

## 模块选择规则

```text
普通介绍 / 品牌展示       → 只用 core profile
数据与指标                 → core + data_module
研究与合规内容             → core + evidence_module
图片与场景叙事             → core + media_module
公共发布与教育             → core + accessibility_module
演讲互动                   → 按需增加 motion_module
联合品牌                   → 按需增加 co_brand_module
```

模块缺失时不应阻塞普通 PPT 生成；只有用户明确要求该能力，或内容本身依赖该能力时，才把模块字段作为硬约束。
