"use strict";

const NAMED_COLORS = Object.freeze([
  { id: "deep-navy", value: "#173B63", pattern: /(?:深蓝|海军蓝|藏蓝|deep\s*navy|navy\s*blue)/i },
  { id: "cream", value: "#F4EFE4", pattern: /(?:米白|暖白|奶油白|象牙白|cream|ivory|warm\s*white|bone)/i },
  { id: "orange", value: "#D97706", pattern: /(?:橙色|橘色|橙黄|orange|amber)/i },
  { id: "blue", value: "#2563EB", pattern: /(?:蓝色|blue)/i },
  { id: "green", value: "#15803D", pattern: /(?:绿色|green)/i },
  { id: "red", value: "#B91C1C", pattern: /(?:红色|red)/i },
  { id: "black", value: "#111111", pattern: /(?:黑色|纯黑|黑字|black)/i },
  { id: "white", value: "#FFFFFF", pattern: /(?:白色|white)/i },
]);

const PALETTE_REQUEST_RE = /(?:配色|色彩|颜色|色系|主色|背景色|底色|点缀色|点缀强调|米白底|纯黑字|背景[黑白]|正文[黑白]|黑白为主|palette|color\s*(?:palette|scheme)|accent)/i;
const SPARSE_ACCENT_RE = /(?:少量|少许|小面积|克制|仅作|只作|点缀|sparse|restrained|limited|small\s+amount)/i;
const HEX_COLOR_RE = /#[0-9a-f]{6}\b/ig;
const QUADRANT_RELATIONSHIP_RE = /(?:优先级矩阵|影响[^\n]{0,24}(?:紧急|投入|难度)|(?:紧急|投入|难度)[^\n]{0,24}影响|[xX横]\s*轴|[yY纵]\s*轴|横轴[^\n]{0,32}纵轴|纵轴[^\n]{0,32}横轴|impact[^\n]{0,24}(?:effort|urgency)|(?:effort|urgency)[^\n]{0,24}impact)/i;
const STYLE_OVERRIDE_VALUES = Object.freeze({
  collage: Object.freeze(["off"]),
  decorations: Object.freeze(["off"]),
  stagger: Object.freeze(["off"]),
  irregular_grid: Object.freeze(["off"]),
  shadow: Object.freeze(["off"]),
  texture: Object.freeze(["off"]),
  gradient: Object.freeze(["off"]),
  radius: Object.freeze(["square", "rounded"]),
});

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function clone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

function collectText(value, output = []) {
  if (typeof value === "string") output.push(value);
  else if (Array.isArray(value)) value.forEach(item => collectText(item, output));
  else if (isPlainObject(value)) Object.values(value).forEach(item => collectText(item, output));
  return output;
}

function normalizeHex(value) {
  const text = String(value || "").trim().toUpperCase();
  return /^#[0-9A-F]{6}$/.test(text) ? text : null;
}

function contrastRatio(left, right) {
  const luminance = value => {
    const hex = normalizeHex(value);
    if (!hex) return null;
    const channels = [1, 3, 5].map(index => {
      const normalized = parseInt(hex.slice(index, index + 2), 16) / 255;
      return normalized <= 0.03928
        ? normalized / 12.92
        : ((normalized + 0.055) / 1.055) ** 2.4;
    });
    return (0.2126 * channels[0]) + (0.7152 * channels[1]) + (0.0722 * channels[2]);
  };
  const leftLuminance = luminance(left);
  const rightLuminance = luminance(right);
  if (leftLuminance == null || rightLuminance == null) return null;
  return (
    (Math.max(leftLuminance, rightLuminance) + 0.05)
    / (Math.min(leftLuminance, rightLuminance) + 0.05)
  );
}

function readableForeground(background, preferred) {
  return [...new Set([preferred, "#111111", "#FFFFFF"].map(normalizeHex).filter(Boolean))]
    .sort((left, right) => contrastRatio(right, background) - contrastRatio(left, background))[0];
}

function readableEmphasis(palette, preferred) {
  // Emphasis is ink on ordinary surfaces, not ink on a solid primary fill.
  const surfaces = [palette.background, palette.surface, palette.surface_strong,
    palette.primary_soft, mixHex(palette.surface, palette.primary, 0.1)]
    .map(normalizeHex).filter(Boolean);
  const minimumContrast = color => Math.min(...surfaces.map(bg => contrastRatio(color, bg)));
  const candidates = [...new Set([preferred, palette.text, "#111111", "#FFFFFF"]
    .map(normalizeHex).filter(Boolean))];
  if (normalizeHex(preferred) && minimumContrast(preferred) >= 4.5) return normalizeHex(preferred);
  return candidates.sort((a, b) => minimumContrast(b) - minimumContrast(a))[0];
}

function resolvePaletteTextRoles(palette) {
  palette.primary_text = readableEmphasis(palette, palette.primary_text || palette.primary);
  palette.muted = readableEmphasis(palette, palette.muted || palette.text);
  if (contrastRatio(palette.inverse, palette.primary) < 4.5) {
    palette.inverse = readableForeground(palette.primary, palette.text);
  }
  if (palette.alt_background) {
    const alternate = {
      background: palette.alt_background,
      surface: palette.alt_surface || palette.surface,
      text: palette.alt_text || palette.text,
      primary: palette.alt_primary || palette.primary,
    };
    palette.alt_primary_text = readableEmphasis(alternate,
      palette.alt_primary_text || palette.alt_primary || palette.primary);
    palette.alt_muted = readableEmphasis(alternate, palette.alt_muted || palette.muted);
  }
  return palette;
}

function colorRecord(color, requested, source = "explicit") {
  return { value: color, requested, source };
}

function namedColorMatches(text) {
  return NAMED_COLORS.filter(item => item.pattern.test(text));
}

function inferStyleOverrides(value) {
  const source = isPlainObject(value) && value.source_text
    ? String(value.source_text)
    : collectText(value).join("\n");
  const text = source.normalize("NFKC");
  const overrides = {};
  if (/(?:不要|避免|禁用|不使用|拒绝|无|去掉)(?:任何)?(?:拼贴|collage)|(?:no|without|avoid)\s+collage/i.test(text)) {
    overrides.collage = "off";
  }
  if (/(?:不要|避免|禁用|去掉)(?:装饰|装饰物|装饰元素|饰品|pins?)|(?:no|without)\s+(?:decorations?|ornaments?|pins?)/i.test(text)) {
    overrides.decorations = "off";
  }
  if (/(?:不要|避免|禁用)(?:错位|参差|交错|stagger)|(?:no|without)\s+stagger/i.test(text)) {
    overrides.stagger = "off";
  }
  if (/(?:不要|避免|禁用)(?:不规则网格|马赛克网格|mosaic)|(?:no|without)\s+(?:irregular\s+grid|mosaic)/i.test(text)) {
    overrides.irregular_grid = "off";
  }
  if (/(?:不要|避免|禁用|去掉)(?:阴影|投影)|(?:no|without)\s+(?:shadow|drop[- ]shadow)/i.test(text)) {
    overrides.shadow = "off";
  }
  if (/(?:不要|避免|禁用|去掉)(?:纹理|底纹|网点)|(?:no|without)\s+(?:texture|pattern)/i.test(text)) {
    overrides.texture = "off";
  }
  if (/(?:不要|避免|禁用|去掉)(?:渐变)|(?:no|without)\s+gradient/i.test(text)) {
    overrides.gradient = "off";
  }
  if (/(?:不要|避免|禁用)(?:圆角)|(?:零|无)圆角|直角|方角|square\s+corners?|zero\s+radius/i.test(text)) {
    overrides.radius = "square";
  } else if (/(?:大圆角|圆润卡片|胶囊圆角|rounded\s+cards?|pill\s+corners?)/i.test(text)) {
    overrides.radius = "rounded";
  }
  return Object.keys(overrides).length ? overrides : null;
}

function mergeStyleOverrides(existing, requested, { explicit = false } = {}) {
  if (!isPlainObject(requested)) return existing || null;
  const base = isPlainObject(existing) ? clone(existing) : {};
  Object.entries(requested).forEach(([key, value]) => {
    if (explicit || base[key] === undefined) base[key] = value;
  });
  return Object.keys(base).length ? base : null;
}

function labeledHex(text, labels) {
  const label = labels.join("|");
  const after = text.match(new RegExp(`(?:${label})[^#\\n]{0,32}(#[0-9a-f]{6})\\b`, "i"));
  if (after) return normalizeHex(after[1]);
  const before = text.match(new RegExp(`(#[0-9a-f]{6})\\b[^\\n]{0,24}(?:${label})`, "i"));
  return before ? normalizeHex(before[1]) : null;
}

function exactHexPalette(text, hex) {
  if (hex.length < 2) return null;
  const background = labeledHex(text, ["背景色", "底色", "background"]);
  const primary = labeledHex(text, ["主色", "正文色", "primary"]);
  const accent = labeledHex(text, ["点缀色", "强调色", "accent"]);
  const takeUnused = used => hex.find(value => !used.has(value)) || null;
  const used = new Set();
  const resolvedBackground = background || hex[0];
  used.add(resolvedBackground);
  const resolvedPrimary = primary || takeUnused(used) || resolvedBackground;
  used.add(resolvedPrimary);
  const resolvedAccent = accent || takeUnused(used);
  return {
    source: "explicit",
    requested: hex,
    background: colorRecord(resolvedBackground, resolvedBackground),
    primary: colorRecord(resolvedPrimary, resolvedPrimary),
    ...(resolvedAccent
      ? {
        accent: colorRecord(resolvedAccent, resolvedAccent),
        accent_usage: SPARSE_ACCENT_RE.test(text) ? "sparse" : "balanced",
      }
      : {}),
  };
}

function inferPaletteContract(value) {
  const text = collectText(value).join("\n").normalize("NFKC");
  if (!PALETTE_REQUEST_RE.test(text)) return null;
  const named = namedColorMatches(text);
  const hex = [...new Set((text.match(HEX_COLOR_RE) || []).map(normalizeHex).filter(Boolean))];
  if (!named.length && !hex.length) return null;

  // Two or more exact values form an explicit palette contract. Do not let
  // downstream outline paraphrases such as "red" or "cream" replace them.
  const exact = exactHexPalette(text, hex);
  if (exact) return exact;

  const byId = new Map(named.map(item => [item.id, item]));
  const cream = byId.get("cream") || byId.get("white");
  const blackIsText = /(?:纯黑|黑色?|black)\s*(?:字|文字|正文|text)/i.test(text);
  const navy = byId.get("deep-navy")
    || byId.get("blue")
    || (blackIsText ? null : byId.get("black"));
  const accent = byId.get("orange") || byId.get("green") || byId.get("red");
  const values = [...named.map(item => item.value), ...hex];
  const background = cream || (hex[0] ? { id: hex[0], value: hex[0] } : null);
  const primary = navy || (hex[1] ? { id: hex[1], value: hex[1] } : null);
  const accentColor = accent || (hex[2] ? { id: hex[2], value: hex[2] } : null);
  if (!background) return null;

  return {
    source: "explicit",
    requested: values,
    background: colorRecord(background.value, background.id),
    ...(primary ? { primary: colorRecord(primary.value, primary.id) } : {}),
    ...(accentColor
      ? {
        accent: colorRecord(accentColor.value, accentColor.id),
        accent_usage: SPARSE_ACCENT_RE.test(text) ? "sparse" : "balanced",
      }
      : {}),
  };
}

function withModelPaletteContract(
  existing,
  value,
  identityBasis,
  issues,
  source = "inferred",
) {
  if (!isPlainObject(value)) return existing || null;
  if (
    source !== "explicit"
    && existing
    && existing.palette
    && existing.palette.source === "explicit"
  ) {
    return existing;
  }
  const normalized = {};
  ["background", "text", "primary", "accent"].forEach(key => {
    const color = normalizeHex(value[key]);
    if (!color) issues.push(`model_palette.${key}: expected #RRGGBB color`);
    else normalized[key] = color;
  });
  if (value.secondary !== undefined) {
    const secondary = normalizeHex(value.secondary);
    if (!secondary) issues.push("model_palette.secondary: expected #RRGGBB color");
    else normalized.secondary = secondary;
  }
  const accentUsage = value.accent_usage || "balanced";
  if (!["sparse", "balanced", "dominant"].includes(accentUsage)) {
    issues.push("model_palette.accent_usage: expected sparse, balanced, or dominant");
  }
  if (
    normalized.background
    && normalized.text
    && contrastRatio(normalized.background, normalized.text) < 4.5
  ) {
    issues.push("model_palette: background and text contrast must be at least 4.5:1");
  }
  if (issues.length) return existing || null;
  const requested = String(identityBasis || "model structured palette").trim();
  const palette = {
    source,
    requested: [requested],
    background: colorRecord(normalized.background, requested, source),
    text: colorRecord(normalized.text, requested, source),
    primary: colorRecord(normalized.primary, requested, source),
    accent: colorRecord(normalized.accent, requested, source),
    ...(normalized.secondary
      ? { secondary: colorRecord(normalized.secondary, requested, source) }
      : {}),
    accent_usage: accentUsage,
  };
  return {
    version: 1,
    ...(existing && isPlainObject(existing.style_overrides)
      ? { style_overrides: clone(existing.style_overrides) }
      : {}),
    ...(existing && isPlainObject(existing.slides)
      ? { slides: clone(existing.slides) }
      : {}),
    palette,
  };
}

function visualKind(slide) {
  const text = [slide && slide.title, slide && slide.layout, slide && slide.visual]
    .filter(Boolean)
    .join("\n");
  if (/(?:金字塔|pyramid)/i.test(text)) return "pyramid";
  if (/(?:客户旅程|用户旅程|customer\s+journey|user\s+journey|journey\s+map)/i.test(text)) return "customer-journey";
  if (/(?:泳道|swim\s*lane|swimlane|role\s*[×xX*]\s*phase)/i.test(text)) return "swimlane";
  if (/(?:成熟度|maturity\s+(?:model|ladder|assessment))/i.test(text)) return "maturity";
  if (/(?:根因|原因树|鱼骨图|root\s+cause|cause\s+tree|fishbone)/i.test(text)) return "cause-tree";
  if (/(?:四象限|象限图|2\s*[×xX*]\s*2|二乘二|优先级矩阵|quadrant)/i.test(text)) return "quadrant";
  if (/(?:行动清单|编号行动|action\s*(?:list|items?))/i.test(text)) return "numbered-actions";
  if (/(?:时间轴|路线图|里程碑|timeline|roadmap)/i.test(text)) return "timeline";
  if (/(?:流程|路径|process|journey)/i.test(text)) return "process";
  if (/(?:双栏对比|前后对比|comparison|before\s*(?:and|\/)?\s*after)/i.test(text)) return "comparison";
  if (/(?:架构|architecture)/i.test(text)) return "architecture";
  if (/(?:系统集成|integration)/i.test(text)) return "integration";
  if (/(?:数据管道|pipeline)/i.test(text)) return "pipeline";
  if (/(?:卡片|cards?)/i.test(text)) return "cards";
  return null;
}

function countDimension(value) {
  const text = String(value || "");
  const rules = [
    [/(?:泳道|角色|lanes?|roles?)/i, "lanes"],
    [/(?:原因|分支|causes?|branches?)/i, "causes"],
    [/(?:成熟度等级|等级|levels?)/i, "levels"],
    [/(?:连接|连线|边(?:关系)?|edges?)/i, "edges"],
    [/(?:节点|nodes?)/i, "nodes"],
    [/(?:外围系统|系统(?:项)?|systems?)/i, "systems"],
    [/(?:数据序列|系列|series)/i, "series"],
    [/(?:类别|分类|categories?)/i, "categories"],
    [/(?:亮点|highlights?)/i, "highlights"],
    [/(?:指标|KPI|metrics?)/i, "metrics"],
    [/(?:标签|tags?)/i, "tags"],
    [/(?:论据|证明|proofs?)/i, "proofs"],
    [/(?:章节|小节|sections?)/i, "sections"],
    [/(?:层级|分层|层(?!面)|layers?)/i, "layers"],
    [/(?:工位|stations?)/i, "stations"],
    [/(?:分区|区域|zones?)/i, "zones"],
    [/(?:行动|actions?)/i, "actions"],
    [/(?:阶段|步骤|里程碑|steps?|milestones?)/i, "steps"],
    [/(?:(?<!排)列(?:字段|表)?|columns?)/i, "columns"],
    [/(?:行(?:项目)?|rows?)/i, "rows"],
    [/(?:议程|行程|agenda)/i, "agenda"],
    [/(?:卡片|cards?)/i, "cards"],
  ];
  const matched = rules.find(([pattern]) => pattern.test(text));
  return matched ? matched[1] : "items";
}

function explicitCountContract(slide) {
  const persisted = slide && slide.visual_item_contract;
  if (
    persisted
    && Number.isInteger(persisted.count)
    && persisted.count >= 0
    && typeof persisted.dimension === "string"
    && persisted.dimension.trim()
  ) {
    return { count: persisted.count, dimension: persisted.dimension.trim() };
  }
  const visual = [slide && slide.title, slide && slide.visual]
    .filter(Boolean)
    .join("\n");
  const kind = visualKind(slide);
  const words = { 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10 };
  const parseCount = value => /^\d+$/.test(value) ? Number(value) : words[value];
  if (kind === "quadrant") return { count: 4, dimension: "items" };
  if (kind === "pyramid") {
    const lowerLayer = visual.match(
      /(?:下层|下方|支撑层)[^。；\n]{0,48}?([0-9]{1,2}|[一二三四五六七八九十])\s*(?:条|个|项|类|节点|卡片|支撑)/u
    );
    if (lowerLayer) {
      const parsed = parseCount(lowerLayer[1]);
      if (Number.isInteger(parsed) && parsed >= 1 && parsed <= 23) {
        return { count: parsed + 1, dimension: "items" };
      }
    }
  }
  const match = visual.match(/(?:^|[^0-9一二三四五六七八九十])([0-9]{1,2}|[一二三四五六七八九十])\s*(?:条|个(?!月|季度|周|星期|年|天|小时|分钟|秒)|项|类|张|段(?:式)?|象限|节点|阶段|主线|里程碑|卡片|标签|层|系统|行|列|章节|工位|分区|区域|序列|系列|指标|KPI|连接|连线)/u);
  if (match) {
    const parsed = parseCount(match[1]);
    if (Number.isInteger(parsed) && parsed >= 2 && parsed <= 24) {
      const localText = visual.slice(match.index, match.index + match[0].length + 18);
      const localDimension = countDimension(localText);
      return {
        count: parsed,
        dimension: localDimension === "items"
          ? countDimension(visual)
          : localDimension,
      };
    }
  }
  const bullets = Array.isArray(slide && slide.bullets)
    ? slide.bullets.filter(item => String(item || "").trim())
    : [];
  return ["pyramid", "numbered-actions"].includes(kind) && bullets.length >= 2
    ? { count: bullets.length, dimension: kind === "pyramid" ? "items" : "actions" }
    : null;
}

function explicitCount(slide) {
  const contract = explicitCountContract(slide);
  return contract ? contract.count : null;
}

function slideContract(slide) {
  const kind = visualKind(slide);
  if (!["pyramid", "numbered-actions", "quadrant", "customer-journey", "swimlane", "maturity", "cause-tree"].includes(kind)) return null;
  if (
    kind === "quadrant"
    && !QUADRANT_RELATIONSHIP_RE.test([
      slide && slide.title,
      slide && slide.message,
      slide && slide.visual,
    ].filter(Boolean).join("\n"))
  ) {
    return null;
  }
  const itemContract = explicitCountContract(slide);
  const itemCount = itemContract && itemContract.count;
  return {
    visual_kind: kind,
    source: "explicit",
    ...(itemCount
      ? { item_count: itemCount, item_dimension: itemContract.dimension }
      : {}),
    ...(kind === "pyramid"
      ? { direction: "top-down", relationship: "one-to-many", hierarchy_depth: 2 }
      : {}),
    ...(kind === "quadrant"
      ? { direction: "x-y", relationship: "matrix" }
      : {}),
    ...(kind === "customer-journey"
      ? { direction: "left-to-right", relationship: "experience" }
      : {}),
    ...(kind === "swimlane"
      ? { direction: "left-to-right", relationship: "role-phase" }
      : {}),
    ...(kind === "maturity"
      ? { direction: "bottom-up", relationship: "progression" }
      : {}),
    ...(kind === "cause-tree"
      ? { direction: "left-to-right", relationship: "causal" }
      : {}),
    ...(["timeline", "process", "numbered-actions"].includes(kind)
      ? { direction: "left-to-right", relationship: "ordered" }
      : {}),
  };
}

function inferDesignContract(context, outlineSlides = []) {
  const mergedPalette = inferPaletteContract(context);
  const sourcePalette = inferPaletteContract({
    source_text: context && context.source_text ? context.source_text : "",
  });
  const sourceExactCount = sourcePalette && Array.isArray(sourcePalette.requested)
    ? sourcePalette.requested.filter(value => normalizeHex(value)).length
    : 0;
  const palette = sourceExactCount >= 2
    ? sourcePalette
    : mergedPalette;
  const styleOverrides = inferStyleOverrides(context);
  const slides = {};
  outlineSlides.forEach((slide, index) => {
    const contract = slideContract(slide);
    if (contract) slides[`slide-${String(index + 1).padStart(2, "0")}`] = contract;
  });
  if (!palette && !styleOverrides && !Object.keys(slides).length) return null;
  return {
    version: 1,
    ...(palette ? { palette } : {}),
    ...(styleOverrides ? { style_overrides: styleOverrides } : {}),
    ...(Object.keys(slides).length ? { slides } : {}),
  };
}

function normalizeColorRecord(value, fieldPath, issues) {
  if (!isPlainObject(value)) {
    issues.push(`${fieldPath}: expected color contract object`);
    return null;
  }
  const color = normalizeHex(value.value);
  if (!color) issues.push(`${fieldPath}.value: expected #RRGGBB color`);
  const source = ["explicit", "recommended", "inferred"].includes(value.source)
    ? value.source
    : null;
  if (!source) issues.push(`${fieldPath}.source: expected explicit, recommended, or inferred`);
  return color && source
    ? { value: color, requested: String(value.requested || color), source }
    : null;
}

function validateAndNormalizeDesignContract(value, issues) {
  if (value === undefined || value === null) return null;
  if (!isPlainObject(value)) {
    issues.push("design_contract: expected object");
    return null;
  }
  const unknown = Object.keys(value).filter(key => !["version", "palette", "style_overrides", "slides"].includes(key));
  if (unknown.length) issues.push(`design_contract: unknown field(s): ${unknown.join(", ")}`);
  if (value.version !== 1) issues.push("design_contract.version: must be 1");
  const normalized = { version: 1 };
  if (value.palette !== undefined) {
    if (!isPlainObject(value.palette)) {
      issues.push("design_contract.palette: expected object");
    } else {
      const palette = { source: value.palette.source === "explicit" ? "explicit" : "inferred" };
      ["background", "surface", "text", "primary", "accent", "secondary"].forEach(key => {
        if (value.palette[key] === undefined) return;
        const record = normalizeColorRecord(value.palette[key], `design_contract.palette.${key}`, issues);
        if (record) palette[key] = record;
      });
      if (value.palette.accent_usage !== undefined) {
        if (!["sparse", "balanced", "dominant"].includes(value.palette.accent_usage)) {
          issues.push("design_contract.palette.accent_usage: expected sparse, balanced, or dominant");
        } else palette.accent_usage = value.palette.accent_usage;
      }
      if (Array.isArray(value.palette.requested)) palette.requested = clone(value.palette.requested);
      normalized.palette = palette;
    }
  }
  if (value.style_overrides !== undefined) {
    if (!isPlainObject(value.style_overrides)) {
      issues.push("design_contract.style_overrides: expected object");
    } else {
      normalized.style_overrides = {};
      Object.entries(value.style_overrides).forEach(([key, setting]) => {
        const allowed = STYLE_OVERRIDE_VALUES[key];
        if (!allowed) {
          issues.push(`design_contract.style_overrides: unknown key ${key}`);
        } else if (!allowed.includes(setting)) {
          issues.push(
            `design_contract.style_overrides.${key}: expected ${allowed.join(" or ")}`
          );
        } else {
          normalized.style_overrides[key] = setting;
        }
      });
    }
  }
  if (value.slides !== undefined) {
    if (!isPlainObject(value.slides)) {
      issues.push("design_contract.slides: expected object keyed by slide id");
    } else {
      normalized.slides = {};
      Object.entries(value.slides).forEach(([slideId, contract]) => {
        if (!isPlainObject(contract) || !String(contract.visual_kind || "").trim()) {
          issues.push(`design_contract.slides.${slideId}.visual_kind: required`);
          return;
        }
        normalized.slides[slideId] = {
          visual_kind: String(contract.visual_kind).trim(),
          source: ["explicit", "recommended", "inferred"].includes(contract.source)
            ? contract.source
            : "inferred",
          ...(Number.isInteger(contract.item_count) ? { item_count: contract.item_count } : {}),
          ...(typeof contract.item_dimension === "string" && contract.item_dimension.trim()
            ? { item_dimension: contract.item_dimension.trim() }
            : {}),
          ...(contract.direction ? { direction: String(contract.direction) } : {}),
          ...(contract.relationship ? { relationship: String(contract.relationship) } : {}),
          ...(Number.isInteger(contract.hierarchy_depth)
            ? { hierarchy_depth: contract.hierarchy_depth }
            : {}),
        };
      });
    }
  }
  return normalized;
}

function paletteWithOverrides(themePalette, designContract) {
  const palette = clone(themePalette || {});
  const requested = designContract && designContract.palette;
  if (!requested) return resolvePaletteTextRoles(palette);
  const background = requested.background && requested.background.value;
  const surface = requested.surface && requested.surface.value;
  const requestedPrimary = requested.primary && requested.primary.value;
  const requestedText = requested.text && requested.text.value;
  const primary = requestedPrimary;
  const accent = requested.accent && requested.accent.value;
  const secondary = requested.secondary && requested.secondary.value;
  if (background) palette.background = background;
  if (surface) palette.surface = surface;
  if (background) {
    const preferredText = requestedText || requestedPrimary || palette.text;
    const foreground = contrastRatio(background, preferredText) >= 4.5
      ? preferredText
      : readableForeground(background, palette.text);
    palette.text = foreground;
    palette.surface = surface || mixHex(background, foreground, 0.06);
    palette.surface_strong = mixHex(background, foreground, 0.12);
    palette.border = mixHex(background, foreground, 0.24);
  }
  if (primary) {
    palette.primary = primary;
    palette.primary_text = primary;
    palette.muted = mixHex(
      background || palette.background || "#FFFFFF",
      palette.text,
      0.68,
    );
    palette.inverse = readableForeground(primary, palette.text);
    palette.primary_soft = mixHex(background || palette.background || "#FFFFFF", primary, 0.14);
    palette.chart = [
      primary,
      accent || primary,
      secondary || accent || primary,
      ...(Array.isArray(palette.chart) ? palette.chart : []),
    ].slice(0, 4);
  }
  if (accent) palette.accent = accent;
  if (secondary) palette.secondary = secondary;
  if (background && primary) {
    // Explicit palettes are deck-wide contracts. Theme alternation must not
    // silently restore the template's original foreground/background colors.
    palette.alt_background = palette.background;
    palette.alt_surface = palette.surface;
    palette.alt_text = palette.text;
    palette.alt_muted = palette.muted;
    palette.alt_border = palette.border;
    palette.alt_primary = palette.primary;
    palette.alt_primary_text = palette.primary_text;
  }
  return resolvePaletteTextRoles(palette);
}

function mixHex(base, overlay, overlayWeight) {
  const left = normalizeHex(base);
  const right = normalizeHex(overlay);
  if (!left || !right) return left || right || base;
  const weight = Math.max(0, Math.min(1, Number(overlayWeight) || 0));
  const channels = [1, 3, 5].map(index => {
    const a = parseInt(left.slice(index, index + 2), 16);
    const b = parseInt(right.slice(index, index + 2), 16);
    return Math.round(a * (1 - weight) + b * weight)
      .toString(16)
      .padStart(2, "0");
  });
  return `#${channels.join("")}`.toUpperCase();
}

module.exports = {
  explicitCount,
  explicitCountContract,
  inferDesignContract,
  inferPaletteContract,
  inferStyleOverrides,
  mergeStyleOverrides,
  paletteWithOverrides,
  readableForeground,
  slideContract,
  validateAndNormalizeDesignContract,
  visualKind,
  withModelPaletteContract,
};
