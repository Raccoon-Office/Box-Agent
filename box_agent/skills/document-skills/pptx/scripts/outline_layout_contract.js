"use strict";

const {
  explicitCount,
  explicitCountContract,
} = require("./design_contract_core.js");
const {
  getLayout,
  getVisualCollectionContract,
} = require("../layouts/registry.js");

const QUADRANT_RE = /(?:四象限|象限图|2\s*[×xX*]\s*2|二乘二|优先级矩阵|影响[^\n]{0,20}紧急|quadrant|priority\s*matrix)/i;
const HEATMAP_RE = /(?:风险热力图|热力图|风险矩阵[^\n]{0,16}热力|heat\s*map|risk\s+heat\s*map)/i;
const MATRIX_RE = /(?:风险\s*[\/+与、]?依赖)?矩阵|matrix|(?:结构化)?表格|\btable\b/i;
const BAR_CHART_RE = /(?:柱状图|条形图|排名图|bar\s*chart|column\s*chart)/i;
const DATA_CHART_RE = /(?:折线图|面积图|饼图|环形图|雷达图|数据图表|line\s*chart|area\s*chart|pie\s*chart|donut\s*chart|radar\s*chart)/i;
const ARCHITECTURE_RE = /(?:分层架构|技术架构|系统架构|解决方案架构|架构图|architecture(?:\s*(?:diagram|layered))?)/i;
const INTEGRATION_RE = /(?:系统集成|集成架构|数据流(?:设计|图)?|接口关系|integration(?:\s*(?:diagram|map))?|data\s*flow)/i;
const DATA_PIPELINE_RE = /(?:数据管道|数据流水线|处理管道|ETL|ELT|data\s*pipeline|processing\s*pipeline)/i;
const DASHBOARD_RE = /(?:数据看板|管理看板|管理驾驶舱|运营驾驶舱|dashboard(?:\s*overview)?)/i;
const KPI_RE = /(?:KPI|指标卡|metrics?\s*(?:grid|board))/i;
const PROJECT_CASE_RE = /(?:精选项目|项目案例|客户案例|案例研究|case\s*study|portfolio\s+project)/i;
const PROJECT_CASE_MEDIA_RE = /(?:缩略图|项目(?:图片|视觉)|案例(?:图片|视觉)|hero|thumbnail|mockup|样机)/i;
const PROJECT_CASE_METRICS_RE = /(?:关键数字|数字指标(?:卡)?|项目指标|成果指标|metrics?)/i;
const GANTT_RE = /(?:甘特(?:图|计划)?|gantt(?:\s*(?:chart|plan|schedule))?)/i;
const TIMELINE_RE = /(?:时间轴|路线图|里程碑|节点串联|timeline|roadmap)/i;
const PROCESS_RE = /(?:三段式|四段式|能力路径|演进路径|流程路径|process\s*flow|journey)/i;
const AGENDA_RE = /(?:议程|行程|入职地图|agenda(?:[-_\s]*grid)?|编号流程|图标节点)/i;
const FACTORY_PROCESS_RE = /(?:制造产线|生产线|工位流程|工序节拍|质量控制点|factory\s+line|production\s+line|station\s+flow|shop\s+floor)/i;
const LEGAL_LOGIC_RE = /(?:IRAC|法律论证|案件逻辑|争点.{0,12}规则.{0,12}分析|issue.{0,12}rule.{0,12}analysis|legal\s+reasoning)/i;
const PROPERTY_FACTSHEET_RE = /(?:地产底卡|项目底卡|地块分区|资产底卡|用地指标|property\s+factsheet|site\s+facts|parcel\s+plan)/i;
const COMMERCE_FUNNEL_RE = /(?:零售漏斗|电商漏斗|转化漏斗|触达.{0,12}成交|commerce\s+funnel|e-?commerce\s+funnel|conversion\s+funnel)/i;
const SUPPLY_NETWORK_RE = /(?:供应链网络|物流网络|履约网络|控制塔|control\s+tower|supply\s+network|logistics\s+network|fulfillment\s+network)/i;
const PYRAMID_RE = /(?:金字塔|pyramid)/i;
const CUSTOMER_JOURNEY_RE = /(?:客户旅程(?:图|地图)?|用户旅程(?:图|地图)?|customer\s+journey(?:\s+map)?|user\s+journey(?:\s+map)?|journey\s+map)/i;
const SWIMLANE_RE = /(?:泳道(?:图|流程)?|跨部门流程|跨角色流程|role\s*[×xX*]\s*phase|swim\s*lane|swimlane)/i;
const MATURITY_RE = /(?:成熟度(?:模型|阶梯|评估)?|能力成熟度|maturity\s+(?:model|ladder|assessment))/i;
const CAUSE_TREE_RE = /(?:根因(?:树|分析)?|原因树|因果树|鱼骨图|fishbone|cause\s+tree|root\s+cause)/i;
const NUMBERED_ACTIONS_RE = /(?:行动清单|编号行动|(?<![上下第])[一二三四五六七八九十0-9]+步(?:行动|清单|流程)|numbered\s+actions?)/i;
const COMPARISON_RE = /(?:双栏对比|前后对比|方案对比|two[- ]column\s*comparison|before\s*(?:and|\/)?\s*after)/i;
const COVER_RE = /(?:封面|\bcover\b|cover[_-]|\bopening\b)/i;
const SECTION_RE = /(?:章节页|章节分隔|章节标题|章节过渡|section\s*(?:divider|marker)|chapter\s*(?:divider|marker)|\bdivider\b)/i;
const STATEMENT_RE = /(?:核心结论|关键结论|一句话结论|核心观点|关键观点|结论页|观点页|single\s+statement|key\s+takeaway|thesis\s+statement)/i;
const CLOSING_RE = /(?:行动式收尾|行动收尾|结尾页|结束页|感谢页|下一步行动|后续行动|closing|next\s+steps?|thank\s+you|call\s+to\s+action)/i;
const TAG_RE = /(?:标签|主线卡|关键词|\btags?\b|\bchips?\b)/i;
const MEDIA_RE = /(?:照片|人物|海报|主视觉|插画|概念图|界面|截图|样机|hero|photo|portrait|poster|illustration|concept\s*art|interface|screenshot|mockup)/i;

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

function outlineIntentRecord(slide) {
  return {
    title: text(slide && slide.title),
    message: text(slide && slide.message),
    layout: text(slide && slide.layout),
    visual: text(slide && slide.visual),
    ...(slide && slide.visual_item_contract
      ? { visual_item_contract: JSON.parse(JSON.stringify(slide.visual_item_contract)) }
      : {}),
  };
}

function selectionText(slide) {
  const intent = outlineIntentRecord(slide);
  return [intent.title, intent.message, intent.layout, intent.visual]
    .filter(Boolean)
    .join("\n");
}

function visualSelectionText(slide) {
  const intent = outlineIntentRecord(slide);
  return [intent.layout, intent.visual].filter(Boolean).join("\n");
}

function semanticRule(kind, preferred, allowed, reason) {
  return {
    kind,
    preferred_layout_id: preferred,
    allowed_layout_ids: [...new Set(allowed)],
    reason,
  };
}

function outlineQuantitativeEvidenceCount(slide, sourceMode = "") {
  const evidence = Array.isArray(slide && slide.evidence) ? slide.evidence : [];
  const userProvidedContent = sourceMode === "user_provided"
    ? [
      slide && slide.title,
      slide && slide.message,
      ...(Array.isArray(slide && slide.bullets) ? slide.bullets : []),
    ]
    : [];
  const content = [...evidence, ...userProvidedContent]
    .map(value => String(value || ""))
    .join(" ");
  return (content.match(
    /\d+(?:,\d{3})*(?:\.\d+)?(?:[%％])?|[$¥￥€£]|[一二三四五六七八九十百千万]+(?:次|年|届|名|个|项|座|枚|金牌|冠军)/g
  ) || []).length;
}

function outlineHasQuantitativeEvidence(slide, sourceMode = "") {
  return outlineQuantitativeEvidenceCount(slide, sourceMode) > 0;
}

function outlineHasPlottableChartEvidence(slide, sourceMode = "") {
  return outlineQuantitativeEvidenceCount(slide, sourceMode) >= 2;
}

function analyzeOutlineLayoutIntent(
  slide,
  sourceMode = "",
  { allowIllustrativeQuantitative = false } = {}
) {
  const all = selectionText(slide);
  const visual = visualSelectionText(slide);
  const layout = text(slide && slide.layout);
  const quantitativeEvidenceCount = outlineQuantitativeEvidenceCount(slide, sourceMode);
  const hasQuantitativeEvidence = quantitativeEvidenceCount > 0;

  const quantitativeRule = (kind, preferred, allowed, reason, minimumEvidence = 1) => {
    if (quantitativeEvidenceCount >= minimumEvidence || allowIllustrativeQuantitative) {
      return semanticRule(kind, preferred, allowed, reason);
    }
    return semanticRule(
      `qualitative-${kind}`,
      "cards-grid-v1",
      ["cards-grid-v1"],
      "outline names a quantitative visual without quantitative evidence or an " +
      "authorized illustrative assumption, so preserve the qualitative argument " +
      "in editable cards instead of inventing values"
    );
  };

  if (CUSTOMER_JOURNEY_RE.test(all)) {
    return semanticRule(
      "customer-journey",
      "customer-journey-map-v1",
      ["customer-journey-map-v1"],
      "outline asks for an editable customer journey with stages, touchpoints, emotion, pain points, and opportunities"
    );
  }
  if (SWIMLANE_RE.test(all)) {
    return semanticRule(
      "swimlane-process",
      "swimlane-process-v1",
      ["swimlane-process-v1"],
      "outline asks for an editable role-by-phase swimlane with explicit handoffs"
    );
  }
  if (MATURITY_RE.test(all)) {
    return semanticRule(
      "maturity-model",
      "maturity-model-v1",
      ["maturity-model-v1"],
      "outline asks for an editable maturity ladder with level criteria and current/target states"
    );
  }
  if (CAUSE_TREE_RE.test(all)) {
    return semanticRule(
      "cause-tree",
      "cause-tree-v1",
      ["cause-tree-v1"],
      "outline asks for an editable root-cause tree with cause categories and contributing factors"
    );
  }

  if (PYRAMID_RE.test(visual) || PYRAMID_RE.test(layout)) {
    return semanticRule(
      "pyramid",
      "pyramid-hierarchy-v1",
      ["pyramid-hierarchy-v1"],
      "outline explicitly asks for a top-down pyramid hierarchy"
    );
  }

  if (NUMBERED_ACTIONS_RE.test(all)) {
    return semanticRule(
      "numbered-actions",
      "cards-grid-v1",
      ["cards-grid-v1"],
      "outline explicitly asks for a complete ordered action list with editable numbered cards"
    );
  }

  if (QUADRANT_RE.test(all)) {
    return semanticRule(
      "quadrant",
      "quadrant-matrix-v1",
      ["quadrant-matrix-v1"],
      "outline explicitly asks for an editable two-by-two quadrant matrix"
    );
  }
  if (GANTT_RE.test(visual) || GANTT_RE.test(layout)) {
    return semanticRule(
      "gantt",
      "table-data-v1",
      ["table-data-v1"],
      "outline asks for an editable Gantt schedule with work packages across delivery phases"
    );
  }
  if (HEATMAP_RE.test(visual) || HEATMAP_RE.test(layout)) {
    return semanticRule(
      "heatmap",
      "heatmap-matrix-v1",
      ["heatmap-matrix-v1"],
      "outline asks for an editable heatmap matrix with semantic intensity cells"
    );
  }
  if (MATRIX_RE.test(visual)) {
    return semanticRule(
      "matrix",
      "table-data-v1",
      ["table-data-v1"],
      "outline asks for a matrix/table with explicit row and column relationships"
    );
  }
  if (BAR_CHART_RE.test(visual)) {
    return quantitativeRule(
      "bar-chart",
      "chart-bar-v1",
      ["chart-bar-v1", "chart-data-v1"],
      "outline asks for an editable bar or column chart"
    );
  }
  if (DATA_CHART_RE.test(visual)) {
    return quantitativeRule(
      "data-chart",
      "chart-data-v1",
      ["chart-data-v1"],
      "outline names a specific editable data-chart geometry",
      2
    );
  }
  if (ARCHITECTURE_RE.test(visual)) {
    return semanticRule(
      "technical-architecture",
      "technical-diagram-v1",
      ["technical-diagram-v1"],
      "outline asks for a DiagramSpec technical architecture with editable nodes and edges"
    );
  }
  if (DATA_PIPELINE_RE.test(visual)) {
    return semanticRule(
      "data-pipeline",
      "technical-diagram-v1",
      ["technical-diagram-v1"],
      "outline asks for a DiagramSpec data pipeline with editable stages and flows"
    );
  }
  if (INTEGRATION_RE.test(visual)) {
    return semanticRule(
      "system-integration",
      "technical-diagram-v1",
      ["technical-diagram-v1"],
      "outline asks for a DiagramSpec system integration or data-flow map"
    );
  }
  if (
    PROJECT_CASE_RE.test(all)
    && PROJECT_CASE_MEDIA_RE.test(visual)
    && PROJECT_CASE_METRICS_RE.test(visual)
  ) {
    return semanticRule(
      "project-case-study",
      "project-case-study-v1",
      ["project-case-study-v1"],
      "outline asks for a project case study with both media and project metrics"
    );
  }
  if (DASHBOARD_RE.test(visual)) {
    return hasQuantitativeEvidence || allowIllustrativeQuantitative
      ? semanticRule(
        "quantitative-dashboard",
        "kpi-grid-v1",
        ["kpi-grid-v1", "chart-data-v1", "chart-bar-v1"],
        "outline provides quantitative evidence for an editable KPI dashboard"
      )
      : semanticRule(
        "qualitative-dashboard",
        "dashboard-overview-v1",
        ["dashboard-overview-v1"],
        "outline asks for a dashboard concept without real values, so show editable metric domains without inventing numbers"
      );
  }
  if (KPI_RE.test(visual)) {
    return quantitativeRule(
      "kpi",
      "kpi-grid-v1",
      ["kpi-grid-v1", "chart-data-v1", "chart-bar-v1"],
      "outline asks for a KPI or metric board"
    );
  }
  if (COVER_RE.test(layout) && TAG_RE.test(visual) && !MEDIA_RE.test(visual)) {
    return semanticRule(
      "tagged-cover",
      "cover-editorial-v1",
      ["cover-editorial-v1"],
      "outline asks for a typography-led cover with tags rather than an image slot"
    );
  }
  if (COVER_RE.test(layout) && MEDIA_RE.test(visual)) {
    return semanticRule(
      "visual-cover",
      "cover-hero-v1",
      ["cover-hero-v1"],
      "outline explicitly asks for a cover image or hero visual"
    );
  }
  if (COVER_RE.test(layout)) {
    return semanticRule(
      "typography-cover",
      "cover-editorial-v1",
      ["cover-editorial-v1"],
      "outline explicitly marks this page as a typography-led cover"
    );
  }
  if (FACTORY_PROCESS_RE.test(all)) {
    return semanticRule(
      "factory-process-line",
      "factory-process-line-v1",
      ["factory-process-line-v1"],
      "outline asks for a production-line view with editable station and quality metrics"
    );
  }
  if (LEGAL_LOGIC_RE.test(all)) {
    return semanticRule(
      "legal-case-logic",
      "legal-case-logic-v1",
      ["legal-case-logic-v1"],
      "outline asks for an editable legal issue-rule-analysis-conclusion chain"
    );
  }
  if (PROPERTY_FACTSHEET_RE.test(all)) {
    return semanticRule(
      "property-factsheet",
      "property-factsheet-v1",
      ["property-factsheet-v1"],
      "outline asks for a site or asset factsheet with editable zones and metrics"
    );
  }
  if (COMMERCE_FUNNEL_RE.test(all)) {
    return semanticRule(
      "commerce-funnel",
      "commerce-funnel-v1",
      ["commerce-funnel-v1"],
      "outline asks for an editable retail conversion funnel"
    );
  }
  if (SUPPLY_NETWORK_RE.test(all)) {
    return semanticRule(
      "supply-network",
      "supply-network-v1",
      ["supply-network-v1"],
      "outline asks for an editable supply network and fulfillment status view"
    );
  }
  if (AGENDA_RE.test(all)) {
    const itemCount = explicitCount(slide);
    return semanticRule(
      "agenda",
      "cards-grid-v1",
      ["cards-grid-v1", "timeline-horizontal-v1"],
      Number.isInteger(itemCount)
        ? `outline asks for an editable agenda with ${itemCount} ordered items`
        : "outline asks for an editable agenda or numbered journey"
    );
  }
  if (TIMELINE_RE.test(visual) || /(?:timeline|roadmap)/i.test(layout)) {
    return semanticRule(
      "timeline",
      "timeline-horizontal-v1",
      ["timeline-horizontal-v1"],
      "outline asks for ordered milestones on a time or roadmap axis"
    );
  }
  if (PROCESS_RE.test(visual)) {
    const prefersCards = /(?:cards?|卡片)/i.test(layout);
    return semanticRule(
      "staged-path",
      prefersCards ? "cards-grid-v1" : "timeline-horizontal-v1",
      ["cards-grid-v1", "timeline-horizontal-v1"],
      "outline asks for a staged path; numbered cards or a controlled timeline can express it"
    );
  }
  if (COMPARISON_RE.test(visual)) {
    return semanticRule(
      "comparison",
      "comparison-two-column-v1",
      ["comparison-two-column-v1", "table-data-v1"],
      "outline asks for an explicit side-by-side comparison"
    );
  }
  const bulletCount = Array.isArray(slide && slide.bullets)
    ? slide.bullets.filter(item => String(item || "").trim()).length
    : 0;
  if (SECTION_RE.test(all)) {
    return semanticRule(
      "section-divider",
      "section-marker-v1",
      ["section-marker-v1"],
      "outline explicitly asks for a chapter or section transition"
    );
  }
  if (CLOSING_RE.test(all)) {
    return bulletCount > 4
      ? semanticRule(
        "closing-overflow",
        "cards-grid-v1",
        ["cards-grid-v1"],
        "outline closing contains more than four actions, so preserve every item in editable cards"
      )
      : semanticRule(
        "closing",
        "closing-next-steps-v1",
        ["closing-next-steps-v1"],
        "outline explicitly asks for an action-oriented closing page"
      );
  }
  if (STATEMENT_RE.test(all)) {
    return bulletCount > 3
      ? semanticRule(
        "statement-overflow",
        "cards-grid-v1",
        ["cards-grid-v1"],
        "outline conclusion contains more than three proof points, so preserve every point in editable cards"
      )
      : semanticRule(
        "statement",
        "statement-focus-v1",
        ["statement-focus-v1"],
        "outline explicitly asks for one core conclusion with a small proof set"
      );
  }
  if (/^(?:timeline|roadmap|时间轴|路线图)$/i.test(layout)) {
    return semanticRule(
      "timeline",
      "timeline-horizontal-v1",
      ["timeline-horizontal-v1"],
      "outline layout explicitly requests a timeline"
    );
  }
  if (/^(?:matrix|table|矩阵|表格)$/i.test(layout)) {
    return semanticRule(
      "matrix",
      "table-data-v1",
      ["table-data-v1"],
      "outline layout explicitly requests a matrix/table"
    );
  }
  if (COVER_RE.test(layout) && MEDIA_RE.test(all)) {
    return semanticRule(
      "visual-cover",
      "cover-hero-v1",
      ["cover-hero-v1"],
      "outline explicitly asks for a visual-led cover"
    );
  }
  return null;
}

function expectedVisualItemCount(slide) {
  return explicitCount(slide);
}

function expectedVisualItemContract(slide, layoutId = null) {
  const requested = explicitCountContract(slide);
  if (!requested || !layoutId) return requested;
  const collection = getVisualCollectionContract(layoutId, requested.dimension);
  return collection
    ? { ...requested, field: collection.path, resolved_dimension: collection.dimension }
    : requested;
}

function pathValue(value, fieldPath) {
  return String(fieldPath || "")
    .split(".")
    .filter(Boolean)
    .reduce((current, part) => (
      current && typeof current === "object" ? current[part] : undefined
    ), value);
}

function visualCollectionForSlide(slide) {
  if (!slide || !slide.props) return null;
  const requested = expectedVisualItemContract(slide.outline_intent || slide);
  const collection = getVisualCollectionContract(
    slide.layout_id,
    requested && requested.dimension
  );
  return collection
    ? { field: collection.path, value: pathValue(slide.props, collection.path) }
    : null;
}

function validateOutlineVisualCardinality(slide, outlineSlide, basePath, layout = null) {
  const requested = expectedVisualItemContract(outlineSlide, slide.layout_id);
  if (!requested) return [];
  const expected = requested.count;
  const collection = visualCollectionForSlide({
    ...slide,
    outline_intent: outlineSlide,
  });
  if (!collection || !Array.isArray(collection.value)) return [];
  if (collection.value.length === expected) return [];
  const effectiveLayout = layout || getLayout(slide.layout_id);
  const collectionContract = effectiveLayout && effectiveLayout.fields
    ? pathValue(effectiveLayout.fields, collection.field)
    : null;
  if (
    collectionContract
    && Number.isInteger(collectionContract.maxItems)
    && expected > collectionContract.maxItems
  ) {
    return [
      `${basePath}.props.${collection.field}: layout capacity mismatch; outline visual ` +
      `explicitly requests ${expected} visual item(s), but ${slide.layout_id} supports at most ` +
      `${collectionContract.maxItems}`,
    ];
  }
  return [
    `${basePath}.props.${collection.field}: outline visual explicitly requests ${expected} ` +
    `visual item(s), got ${collection.value.length}`,
  ];
}

module.exports = {
  analyzeOutlineLayoutIntent,
  expectedVisualItemCount,
  expectedVisualItemContract,
  outlineHasPlottableChartEvidence,
  outlineHasQuantitativeEvidence,
  outlineIntentRecord,
  validateOutlineVisualCardinality,
};
