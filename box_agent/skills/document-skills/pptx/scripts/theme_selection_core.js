"use strict";

const { familyForTheme } = require("./composition_core.js");

const KEYWORD_INDUSTRY_DEDUP_WEIGHT = 18;

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function selectionText(value) {
  const parts = [];
  const visit = item => {
    if (typeof item === "string") {
      const text = item.trim();
      if (text) parts.push(text);
      return;
    }
    if (Array.isArray(item)) {
      item.forEach(visit);
      return;
    }
    if (isPlainObject(item)) Object.values(item).forEach(visit);
  };
  visit(value);
  return parts.join("\n").normalize("NFKC").toLowerCase();
}

function selectionIntentText(value) {
  if (!isPlainObject(value)) return selectionText(value);
  const parts = [];
  const add = item => {
    const text = selectionText(item);
    if (text) parts.push(text);
  };
  ["title", "deck_title", "prompt", "brief", "request"].forEach(key => add(value[key]));
  const outline = isPlainObject(value.outline) ? value.outline : null;
  if (outline) {
    ["deck_goal", "audience", "storyline", "tone", "design_requirements", "title"].forEach(key => add(outline[key]));
    (Array.isArray(outline.slides) ? outline.slides : []).forEach(slide => {
      if (!isPlainObject(slide)) return;
      // Global theme answers what the deck should feel like. Page-level
      // structure answers how one slide should present its information. Keep
      // layout/visual implementation tokens out of theme inference so values
      // such as `kpi-grid-v1` cannot turn a sports biography into a dashboard.
      ["title", "message", "bullets"].forEach(key => add(slide[key]));
    });
  } else {
    add(value.source_text);
    add(value.source_facts);
  }
  return parts.length ? parts.join("\n") : selectionText(value);
}

function themeProfile(theme) {
  const selection = isPlainObject(theme && theme.selection) ? theme.selection : {};
  const mood = Array.isArray(selection.mood_keywords) ? selection.mood_keywords : [];
  const industry = Array.isArray(selection.industry_fit) ? selection.industry_fit : [];
  return {
    mood: selectionText(mood),
    mood_terms: mood.map(value => selectionText(value)).filter(Boolean),
    industry: selectionText(industry),
    industry_terms: industry.map(value => selectionText(value)).filter(Boolean),
    description: selectionText(theme && theme.description),
    scheme: String(selection.scheme || "").trim().toLowerCase(),
    formality: String(selection.formality || "").trim().toLowerCase(),
    fallback: selection.fallback === true,
    family: familyForTheme(theme),
  };
}

const NEGATED_PREFERENCE_CLAUSE_SOURCE =
  "(?:不要|避免|拒绝|不用|不使用|禁用|\\b(?:do\\s+not|don't|avoid|without|never|no)\\b)[^。；;\\n]{0,96}";

function inferPreferences(content) {
  const text = selectionText(content);
  const negatedClauses = text.match(
    new RegExp(NEGATED_PREFERENCE_CLAUSE_SOURCE, "gi")
  ) || [];
  const negatedText = negatedClauses.join("\n");
  const positiveText = text.replace(
    new RegExp(NEGATED_PREFERENCE_CLAUSE_SOURCE, "gi"),
    " "
  );
  const avoids = subject => new RegExp(`(?:${subject})`, "i").test(negatedText);
  return {
    text,
    positive_text: positiveText,
    wants_light: /(?:浅色|浅底|白底|明亮|亮色|light[- ]?(?:background|canvas|theme)|bright|airy)/i.test(positiveText),
    rejects_dark: avoids("深色|暗色|黑底|高冷|dark|black background"),
    wants_friendly: /(?:亲和|友好|亲切|欢迎|不端着|有温度|friendly|welcoming|approachable)/i.test(positiveText),
    rejects_friendly: avoids("亲和|友好|亲切|欢迎|friendly|welcoming|approachable"),
    wants_soft: /(?:柔和|粉彩|低饱和|温柔|\bsoft\b|pastel|gentle)/i.test(positiveText),
    wants_clean: /(?:清爽|干净|简洁|留白|不拥挤|clean|airy|uncluttered|minimal)/i.test(positiveText),
    wants_lively: /(?:活力|活泼|轻松|有趣|lively|playful|cheerful|energetic)/i.test(positiveText),
    rejects_lively: avoids("活力|活泼|轻松|有趣|lively|playful|cheerful|energetic"),
    wants_comic: /(?:漫画|分镜|对话气泡|对白气泡|拟声词|网点纸|波普漫画|漫画书|连环画|comic(?:[- ]?book)?|comic\s+panel|graphic\s+novel|storyboard|speech\s+bubble|halftone|manga|pop[- ]?art)/i.test(positiveText),
    rejects_comic: avoids("漫画|分镜|对话气泡|对白气泡|拟声词|网点纸|波普漫画|comic(?:[- ]?book)?|comic\\s+panel|graphic\\s+novel|storyboard|speech\\s+bubble|halftone|manga|pop[- ]?art"),
    wants_pixel: /(?:像素风|像素艺术|像素街机|复古游戏|电玩|街机|点阵|8[- ]?bit|16[- ]?bit|pixel(?:[- ]?art|[- ]?style)?|retro[- ]?(?:game|gaming)|arcade|game\s+ui|crt|neon\s+arcade)/i.test(positiveText),
    rejects_pixel: avoids("像素风|像素艺术|像素街机|复古游戏|电玩|街机|点阵|8[- ]?bit|16[- ]?bit|pixel(?:[- ]?art|[- ]?style)?|retro[- ]?(?:game|gaming)|arcade|game\\s+ui|crt|neon\\s+arcade"),
    wants_restrained_palette: /(?:一(?:到|至)?两(?:个|种)?.{0,8}(?:色|颜色)|一两个.{0,8}(?:色|颜色)|少量.{0,8}点缀|颜色干净|克制配色|limited palette|one or two accent)/i.test(positiveText),
    wants_cool_palette: /(?:冷色|深蓝|海军蓝|钢灰|蓝灰|浅灰|cool(?:[- ]tone|[- ]palette)?|deep navy|navy blue|steel gr[ae]y|blue gr[ae]y)/i.test(positiveText),
    formal_solution_review: /(?:评标|投标|招标|采购负责人|客户交付|解决方案|采购评审|技术评审|bid evaluation|tender|procurement|solution proposal|client deliverable)/i.test(positiveText),
    internal_training: /(?:新员工|员工入职|入职培训|内部培训|员工培训|迎新|onboarding|employee orientation|internal training|training deck)/i.test(positiveText),
    enterprise_context: /(?:企业|集团|公司|会议室|职场|组织|b2b|enterprise|corporate|business|workplace)/i.test(positiveText),
    rejects_collage: avoids("拼贴|collage"),
    rejects_retro: avoids("复古|怀旧|像素|retro|vintage|nostalgia|pixel"),
    rejects_handwritten: avoids("手绘|手写|便签|hand[- ]?drawn|handwritten|sticky notes"),
    rejects_stiff: /(?:不端着|不要高冷|不高冷|不严肃|not stiff|not cold|approachable)/i.test(text),
  };
}

function profileHas(profile, pattern) {
  return pattern.test(`${profile.mood}\n${profile.industry}\n${profile.description}`);
}

const THEME_KEYWORD_RULES = Object.freeze([
  Object.freeze({
    theme_id: "soft-editorial",
    signal: "user intent rule: modern editorial design",
    pattern: /(?:现代编辑(?:设计|风格)|编辑式设计|cover[- ]editorial|editorial[- ]cover|modern\s+editorial\s+(?:design|style))/i,
    weight: 20,
  }),
  Object.freeze({
    theme_id: "vellum",
    signal: "subject rule: fantasy and wizarding worlds",
    pattern: /(?:哈利[·・]?波特|霍格沃茨|魔法世界|魔法学校|巫师世界|奇幻文学|魔幻文学|wizarding\s+world|harry\s+potter|hogwarts|wizard(?:ry|ing)?|witchcraft|fantasy\s+(?:novel|world|story|literature))/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "8-bit-orbit",
    signal: "subject rule: sandbox and voxel games",
    pattern: /(?:我的世界|麦块|方块世界|沙盒游戏|体素|像素方块|建造与冒险|minecraft|voxel|block[- ]?building|sandbox\s+game|crafting\s+game)/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "biennale-yellow",
    signal: "subject rule: museums and cultural heritage",
    pattern: /(?:故宫|紫禁城|博物院|文化遗产|文物展览|传统文化|古建筑|宫廷文化|forbidden\s+city|palace\s+museum|cultural\s+heritage|museum\s+exhibition|historic\s+architecture)/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "neo-grid-bold",
    signal: "subject rule: electric mobility and future vehicles",
    pattern: /(?:特斯拉|电动汽车|智能汽车|新能源汽车|自动驾驶|未来出行|充电网络|tesla|electric\s+vehicles?|smart\s+vehicles?|autonomous\s+driving|future\s+mobility|charging\s+network)/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "daisy-days",
    signal: "audience rule: children and primary education",
    pattern: /(?:小学生|幼儿园|幼儿|少儿|儿童科普|亲子课堂|小学课堂|儿童生日|儿童派对|primary\s+school|elementary\s+school|kindergarten|kids?\s+(?:class|lesson|science|party)|children(?:'s)?\s+(?:class|lesson|science|party))/i,
    weight: 22,
  }),
  Object.freeze({
    theme_id: "pin-and-paper",
    signal: "user intent rule: classroom teaching and school workshop",
    pattern: /(?:课堂教学|教学课件|课程教案|班会|开学第一课|新学期|校园课堂|课堂活动|teacher\s+lesson|classroom\s+teaching|lesson\s+plan|class\s+meeting|school\s+workshop)/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "cobalt-grid",
    signal: "subject rule: science history and space exploration",
    pattern: /(?:太阳系|天文学|宇宙探索|太空探索|航天史|人工智能发展史|计算机发展史|科技发展史|星际穿越|科幻电影|solar\s+system|astronomy|space\s+exploration|history\s+of\s+(?:AI|computing|technology)|interstellar|science\s+fiction\s+film)/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "stencil-tablet",
    signal: "subject rule: archaeology and ancient civilizations",
    pattern: /(?:敦煌|莫高窟|三星堆|考古|古文明|史前文明|恐龙时代|化石|古生物|遗址|塞尔达传说|海拉鲁|dunhuang|mogao|sanxingdui|archaeolog|ancient\s+civilization|prehistoric|dinosaur|fossil|zelda|hyrule)/i,
    weight: 20,
  }),
  Object.freeze({
    theme_id: "data-intelligence",
    signal: "user intent rule: operating and performance review",
    pattern: /(?:经营复盘|业绩复盘|销售复盘|季度复盘|月度复盘|年度复盘|季度经营|经营月报|经营季报|运营数据|业务指标|performance\s+review|business\s+review|quarterly\s+review|monthly\s+review|sales\s+review|operating\s+metrics)/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "people-handbook",
    signal: "user intent rule: employee onboarding and people programs",
    pattern: /(?:新员工入职|员工手册|企业文化培训|组织文化|人才发展|招聘宣讲|雇主品牌|员工体验|employee\s+onboarding|employee\s+handbook|people\s+ops|talent\s+development|employer\s+brand|culture\s+handbook)/i,
    weight: 21,
  }),
  Object.freeze({
    theme_id: "capital-ledger",
    signal: "subject rule: finance investment and capital strategy",
    pattern: /(?:投资备忘录|投资分析|估值分析|资本配置|财报解读|投资者关系|上市公司财报|IPO\s*分析|investment\s+(?:memo|thesis|analysis)|valuation\s+analysis|capital\s+allocation|earnings\s+(?:review|analysis)|investor\s+relations|IPO\s+analysis)/i,
    weight: 21,
  }),
  Object.freeze({
    theme_id: "clinical-atlas",
    signal: "subject rule: clinical medicine and patient care",
    pattern: /(?:临床试验|病例分析|患者路径|诊疗路径|医疗方案|医学教育|医院管理|药物研发|循证医学|clinical\s+trial|case\s+study|patient\s+(?:journey|pathway|care)|medical\s+education|hospital\s+management|drug\s+development|evidence[- ]based\s+medicine)/i,
    weight: 21,
  }),
  Object.freeze({
    theme_id: "civic-brief",
    signal: "subject rule: government policy and public governance",
    pattern: /(?:政务汇报|政府工作|公共政策|城市治理|社会治理|民生工程|监管政策|政策解读|公共服务|public\s+policy|civic\s+(?:brief|program)|government\s+(?:brief|report)|regulatory\s+policy|municipal\s+(?:services|governance)|public\s+governance)/i,
    weight: 21,
  }),
  Object.freeze({
    theme_id: "research-notebook",
    signal: "user intent rule: thesis and academic research",
    pattern: /(?:学术论文|论文答辩|开题报告|文献综述|学术研究方法|实验设计|学术会议|博士论文|硕士论文|thesis\s+(?:defense|proposal)|dissertation|literature\s+review|research\s+methodology|experimental\s+design|academic\s+conference)/i,
    weight: 20,
  }),
  Object.freeze({
    theme_id: "factory-floor",
    signal: "subject rule: manufacturing operations and production quality",
    pattern: /(?:智能制造|制造业|生产线|生产车间|工厂运营|工业工程|精益生产|质量管理|质量改善|设备管理|\bOEE\b|良率|manufacturing|factory\s+operations?|production\s+line|shop\s+floor|industrial\s+engineering|lean\s+manufacturing|quality\s+management|overall\s+equipment\s+effectiveness)/i,
    weight: 22,
  }),
  Object.freeze({
    theme_id: "legal-docket",
    signal: "subject rule: legal matters evidence and compliance",
    pattern: /(?:法律意见书|案件分析|诉讼策略|争议解决|证据分析|合规审查|合规整改|法务汇报|监管合规|法律尽职调查|legal\s+opinion|case\s+analysis|litigation\s+strategy|dispute\s+resolution|evidence\s+analysis|compliance\s+review|corporate\s+counsel|legal\s+due\s+diligence)/i,
    weight: 22,
  }),
  Object.freeze({
    theme_id: "property-atlas",
    signal: "subject rule: real estate development and asset facts",
    pattern: /(?:房地产|地产开发|项目投拓|拿地|土地研判|城市更新|商业地产|住宅项目|项目去化|楼盘|容积率|货值|real\s+estate|property\s+development|land\s+acquisition|urban\s+renewal|commercial\s+property|residential\s+development|project\s+sell[- ]?through)/i,
    weight: 22,
  }),
  Object.freeze({
    theme_id: "commerce-pulse",
    signal: "subject rule: retail ecommerce and merchandising performance",
    pattern: /(?:零售电商|零售运营|电商运营|商品运营|\bSKU\b|\bGMV\b|动销|客单价|复购率|转化漏斗|全渠道零售|直播电商|retail\s+operations?|e-?commerce\s+operations?|merchandising|SKU\s+performance|GMV|average\s+order\s+value|repeat\s+purchase|conversion\s+funnel|omnichannel\s+retail)/i,
    weight: 22,
  }),
  Object.freeze({
    theme_id: "logistics-control-tower",
    signal: "subject rule: supply chain logistics and fulfillment",
    pattern: /(?:供应链|物流运营|物流网络|仓储管理|库存周转|订单履约|\bOTIF\b|\bS&OP\b|采购计划|运输管理|配送网络|supply\s+chain|logistics\s+operations?|logistics\s+network|warehouse\s+management|inventory\s+turnover|order\s+fulfillment|transportation\s+management|distribution\s+network|sales\s+and\s+operations\s+planning)/i,
    weight: 23,
  }),
  Object.freeze({
    theme_id: "consulting-navy",
    signal: "user intent rule: formal proposal and procurement review",
    pattern: /(?:投标方案|招标方案|评标方案|采购方案|售前方案|咨询交付|企业解决方案|客户汇报方案|bid\s+proposal|tender\s+proposal|procurement\s+review|solution\s+proposal|consulting\s+deliverable|enterprise\s+proposal)/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "long-table",
    signal: "subject rule: food hospitality and social dining",
    pattern: /(?:餐厅|餐饮品牌|咖啡店|咖啡馆|咖啡品牌|茶饮店|烘焙店|酒吧|民宿|精品酒店|川菜|火锅|甜品店|restaurant|cafe|coffee\s+shop|food\s+brand|hospitality|supper\s+club|bakery|boutique\s+hotel)/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "capsule",
    signal: "subject rule: youthful beauty and lifestyle launch",
    pattern: /(?:(?:小红书|春季|清新|年轻|少女|潮流|社交媒体|social\s+media|spring|fresh|youthful|trendy)[^。；;\n]{0,28}(?:美妆|护肤|香水|时尚|beauty|skincare|fragrance|fashion)|(?:美妆|护肤|香水|时尚|beauty|skincare|fragrance|fashion)[^。；;\n]{0,28}(?:小红书|春季|清新|年轻|少女|潮流|社交媒体|social\s+media|spring|fresh|youthful|trendy))/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "soft-editorial",
    signal: "subject rule: gentle animation travel and wedding stories",
    pattern: /(?:宫崎骏|吉卜力|动画世界|旅行计划|旅行攻略|行程规划|目的地旅行|婚礼策划|婚礼方案|婚礼故事|miyazaki|studio\s+ghibli|animation\s+world|travel\s+(?:plan|guide|itinerary)|destination\s+travel|wedding\s+(?:plan|proposal|story))/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "bold-poster",
    signal: "subject rule: sports culture and high-energy competition",
    pattern: /(?:篮球文化|足球文化|体育文化|球迷文化|NBA|世界杯|奥运会|电竞赛事|sports?\s+culture|basketball|football\s+culture|soccer\s+culture|world\s+cup|olympic|esports?\s+(?:event|tournament))/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "block-frame",
    signal: "user intent rule: personal and creative portfolio",
    pattern: /(?:个人作品集|设计作品集|求职作品集|摄影作品集|创意作品集|个人品牌介绍|(?:工作室|\bstudio\b)[^。；;\n]{0,32}(?:年度)?作品集|personal\s+portfolio|design\s+portfolio|career\s+portfolio|creative\s+portfolio|studio\s+portfolio|personal\s+brand)/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "block-frame-mono-blue",
    signal: "user intent rule: monochrome electric-blue neo-brutalism",
    pattern: /(?:(?:新野兽主义|neo[- ]?brutal(?:ism|ist)?)[^。；;\n]{0,56}(?:黑白|单色|电光蓝|monochrome|black\s+and\s+white|electric\s+blue|mono[- ]?blue)|(?:黑白|单色|电光蓝|monochrome|black\s+and\s+white|electric\s+blue|mono[- ]?blue)[^。；;\n]{0,56}(?:新野兽主义|neo[- ]?brutal(?:ism|ist)?))/i,
    weight: 24,
  }),
  Object.freeze({
    theme_id: "creative-mode",
    signal: "user intent rule: multicolor creative-agency poster",
    pattern: /(?:(?:创意机构|创意工作室|广告创意|creative\s+agency|design\s+studio|ad\s+shop)[^。；;\n]{0,56}(?:多彩海报|高饱和色块|彩色拼块|multicolor|poster\s+blocks?|campaign\s+creative)|(?:多彩海报|高饱和色块|彩色拼块|multicolor|poster\s+blocks?|campaign\s+creative)[^。；;\n]{0,56}(?:创意机构|创意工作室|广告创意|creative\s+agency|design\s+studio|ad\s+shop))/i,
    weight: 22,
  }),
  Object.freeze({
    theme_id: "retro-windows",
    signal: "user intent rule: classic desktop operating-system interface",
    pattern: /(?:Windows\s*95|Windows\s*98|Win\s*95|Win\s*98|复古电脑窗口|复古桌面界面|旧式操作系统界面|retro\s+windows|classic\s+desktop\s+(?:UI|interface)|old[- ]school\s+operating[- ]system\s+interface)/i,
    weight: 26,
  }),
  Object.freeze({
    theme_id: "studio",
    signal: "user intent rule: warm-black and electric-yellow studio",
    pattern: /(?:(?:黑底|暖黑|warm[- ]?black|black\s+canvas)[^。；;\n]{0,56}(?:电光黄|高压黄|electric\s+yellow|high[- ]voltage\s+yellow)|(?:电光黄|高压黄|electric\s+yellow|high[- ]voltage\s+yellow)[^。；;\n]{0,56}(?:黑底|暖黑|warm[- ]?black|black\s+canvas))/i,
    weight: 24,
  }),
  Object.freeze({
    theme_id: "peoples-platform",
    signal: "user intent rule: public-interest and community campaign",
    pattern: /(?:公益活动|公益项目|志愿者活动|社区倡议|社会行动|流浪动物|动物领养|环境倡议|public[- ]?interest\s+campaign|nonprofit\s+campaign|community\s+initiative|volunteer\s+campaign|animal\s+adoption|social\s+impact)/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "grove",
    signal: "subject rule: climate nature and sustainability research",
    pattern: /(?:气候变化|生态保护|自然保护|生物多样性|森林保护|海洋保护|可持续发展|climate\s+change|conservation|biodiversity|forest\s+protection|ocean\s+protection|sustainable\s+development)/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "retro-zine",
    signal: "subject rule: independent music and DIY culture",
    pattern: /(?:独立乐队|独立音乐|新专辑|音乐专辑|地下音乐|乐队巡演|音乐节阵容|indie\s+(?:band|music)|new\s+album|album\s+launch|underground\s+music|band\s+tour|music\s+festival)/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "technical-blueprint",
    signal: "keyword rule: architecture and infrastructure",
    pattern: /(?:系统架构|技术架构|架构图|系统集成|系统对接|系统连接|云基础设施|平台工程|运行时架构|接口架构|事件总线|消息队列|数据管道|数据流|CDC|system\s+architecture|technical\s+architecture|architecture\s+diagram|system\s+integration|system\s+connection|cloud\s+infrastructure|platform\s+engineering|runtime\s+architecture|event\s+bus|message\s+queue|data\s+pipeline|data\s+flow)/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "mat",
    signal: "keyword rule: wine and vineyard",
    pattern: /(?:葡萄酒|红酒|酒庄|葡萄园|酒款|酿酒|\b(?:wine|winery|wineries|vineyard|vineyards|winemaking|viticulture|oenology)\b)/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "product-console",
    signal: "keyword rule: SaaS and product interface",
    pattern: /(?:SaaS|软件产品|AI\s*产品|产品介绍|产品发布|产品演示|核心功能|功能演示|使用流程|产品价值|产品界面|控制台|工作台|客户端界面|开发者平台|product\s+introduction|product\s+launch|product\s+demo|core\s+features?|feature\s+demo|usage\s+flow|product\s+value|product\s+interface|software\s+product|AI\s+product|developer\s+platform|app\s+console)/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "data-intelligence",
    signal: "keyword rule: KPI and business intelligence",
    pattern: /(?:商业智能|经营分析|数据分析|指标复盘|财务分析|经营看板|数据看板|核心指标|KPI|同比|环比|business\s+intelligence|data\s+analytics|KPI\s+review|operating\s+review|performance\s+dashboard|metrics?\s+review|growth\s+analytics)/i,
    weight: 18,
  }),
  Object.freeze({
    theme_id: "signal",
    signal: "keyword rule: board risk and advisory",
    pattern: /(?:董事会|风险委员会|年度风险|风险分析|风险治理|战略建议|预警信号|board\s+(?:risk|review|presentation)|risk\s+(?:analysis|governance|committee)|strategic\s+recommendations?|early\s+warning\s+signals?)/i,
    weight: 17,
  }),
  Object.freeze({
    theme_id: "soft-editorial",
    signal: "keyword rule: qualitative user research",
    pattern: /(?:用户访谈|用户研究|定性研究|访谈洞察|研究发现|研究方法|user\s+interviews?|user\s+research|qualitative\s+research|interview\s+insights?|research\s+findings?)/i,
    weight: 16,
  }),
  Object.freeze({
    theme_id: "pink-script",
    signal: "keyword rule: premium beauty launch",
    pattern: /(?:(?:高端|奢华|高级感|精品|premium|luxury|luxe)[^。；;\n]{0,28}(?:美妆|护肤|时尚|珠宝|香水|beauty|skincare|fashion|jewelry|fragrance)|(?:美妆|护肤|时尚|珠宝|香水|beauty|skincare|fashion|jewelry|fragrance)[^。；;\n]{0,28}(?:高端|奢华|高级感|精品|premium|luxury|luxe))/i,
    weight: 7,
  }),
]);

const INDUSTRY_MATCH_RULES = Object.freeze([
  Object.freeze({
    signal: "industry match: technical systems",
    content: /(?:系统架构|技术架构|系统集成|云平台|云基础设施|平台工程|运行时|接口|数据管道|架构图|system\s+architecture|technical\s+architecture|system\s+integration|cloud\s+(?:platform|infrastructure)|platform\s+engineering|runtime|API\s+platform|data\s+pipeline)/i,
    profile: /(?:system architecture|cloud infrastructure|platform engineering|system integration|enterprise technology|developer tools|系统架构|云基础设施|平台工程|系统集成)/i,
    weight: 7,
  }),
  Object.freeze({
    signal: "industry match: software product",
    content: /(?:SaaS|软件产品|AI\s*产品|产品发布|产品演示|功能演示|产品界面|开发者平台|product\s+(?:launch|demo|interface|management)|software\s+product|AI\s+product|developer\s+platform|B2B\s+software)/i,
    profile: /(?:SaaS|software product|AI product|product management|developer platform|B2B software|软件产品|AI 产品|产品发布|开发者平台)/i,
    weight: 7,
  }),
  Object.freeze({
    signal: "industry match: analytics and operations",
    content: /(?:商业智能|数据分析|经营分析|财务分析|指标复盘|经营看板|数据看板|KPI|同比|环比|收入|成本|business\s+intelligence|data\s+analytics|operating\s+(?:analysis|review)|finance\s+analysis|KPI\s+review|dashboard|metrics?|revenue|growth\s+analytics)/i,
    profile: /(?:business intelligence|data analytics|KPI review|finance|operations|growth analytics|商业智能|数据分析|经营分析|财务分析|指标复盘)/i,
    weight: 7,
  }),
  Object.freeze({
    signal: "industry match: sustainability",
    content: /(?:可持续|ESG|气候|环保|新能源|绿色发展|sustainability|climate|environment|renewable|green\s+transition)/i,
    profile: /(?:sustainability|organic|森林绿|有机|可持续|环境|能源)/i,
    weight: 7,
  }),
  Object.freeze({
    signal: "industry match: luxury and beauty",
    content: /(?:奢侈|高端|美妆|护肤|时尚|珠宝|香水|luxury|premium|beauty|skincare|fashion|jewelry|fragrance)/i,
    profile: /(?:luxury|beauty|fashion|consumer product|creative brand|portfolio)/i,
    weight: 7,
  }),
  Object.freeze({
    signal: "industry match: education and training",
    content: /(?:教育|培训|课堂|课程|学校|校园|新员工|入职|工作坊|education|training|classroom|course|school|campus|onboarding|workshop)/i,
    profile: /(?:education|training|classroom|course|school|campus|workshop|children|research notes)/i,
    weight: 5,
  }),
  Object.freeze({
    signal: "industry match: board and advisory",
    content: /(?:董事会|投资人|投标|评标|咨询方案|政策|法务|board|investor|procurement|tender|consulting|policy|legal|advisory)/i,
    profile: /(?:board presentation|investor deck|procurement|bid evaluation|consulting|policy|legal|advisory)/i,
    weight: 5,
  }),
]);

const MOOD_MATCH_RULES = Object.freeze([
  Object.freeze({
    signal: "mood match: technical precision",
    content: /(?:技术感|精密|严谨|系统化|工程感|蓝图|technical|precise|systematic|engineering|blueprint)/i,
    profile: /(?:technical|precise|systematic|structured|engineering|blueprint|技术|精密|系统化|严谨|蓝图)/i,
    weight: 7,
  }),
  Object.freeze({
    signal: "mood match: clean modern product",
    content: /(?:现代|清爽|产品化|模块化|精致|简洁|modern|clean|product-led|polished|modular|minimal)/i,
    profile: /(?:modern|clean|product-led|polished|modular|现代|清爽|产品化|模块化)/i,
    weight: 7,
  }),
  Object.freeze({
    signal: "mood match: analytical evidence",
    content: /(?:分析感|数据驱动|证据驱动|理性|高密度|analytical|data-driven|evidence-led|intelligent|high density)/i,
    profile: /(?:analytical|intelligent|evidence-led|precise|authoritative|high density|分析|智能|证据驱动|高密度)/i,
    weight: 7,
  }),
  Object.freeze({
    signal: "mood match: institutional authority",
    content: /(?:权威|可信|稳重|正式|机构感|专业|authoritative|trustworthy|credible|weighty|formal|professional)/i,
    profile: /(?:institutional|trustworthy|authoritative|credible|weighty|professional|机构|可信|稳重|专业)/i,
    weight: 4,
  }),
  Object.freeze({
    signal: "mood match: soft warmth",
    content: /(?:柔和|温暖|亲和|安静|留白|粉彩|\bsoft\b|warm|friendly|quiet|airy|pastel|gentle)/i,
    profile: /(?:\bsoft\b|warm|friendly|quiet|pastel|literary|considered|柔和|亲和|留白|沉静)/i,
    weight: 5,
  }),
  Object.freeze({
    signal: "mood match: premium atmosphere",
    content: /(?:高级感|奢华|精品|夜色|情绪化|premium|luxury|luxe|nocturnal|moody|cinematic)/i,
    profile: /(?:premium|luxury|luxe|nocturnal|moody|atmospheric|奢华|夜色)/i,
    weight: 5,
  }),
  Object.freeze({
    signal: "mood match: bold energy",
    content: /(?:大胆|醒目|强视觉|活力|有趣|bold|graphic|energetic|playful|punchy|high contrast)/i,
    profile: /(?:bold|graphic|energetic|playful|punchy|high contrast|大胆|醒目|活泼)/i,
    weight: 4,
  }),
  Object.freeze({
    signal: "mood match: literary editorial",
    content: /(?:文学|编辑感|杂志感|人文|克制|沉静|literary|editorial|magazine|scholarly|patient|quiet)/i,
    profile: /(?:literary|editorial|magazine|scholarly|patient|quiet|文学|编辑|杂志|沉静)/i,
    weight: 4,
  }),
]);

function metadataTermMatches(text, term) {
  const normalized = selectionText(term);
  if (!normalized || normalized.length < 2) return false;
  if (/^[a-z0-9][a-z0-9+.#\s-]*$/i.test(normalized)) {
    const escaped = normalized
      .replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
      .replace(/\s+/g, "\\s+");
    return new RegExp(`(?:^|[^a-z0-9])${escaped}(?:$|[^a-z0-9])`, "i").test(text);
  }
  return text.includes(normalized);
}

function directMetadataHits(text, terms) {
  return [...new Set((terms || []).filter(term => metadataTermMatches(text, term)))];
}

function applyTaxonomyMatches(rules, dimension, text, profile, add) {
  rules.forEach(rule => {
    if (!rule.content.test(text)) return;
    const profileText = dimension === "industry" ? profile.industry : profile.mood;
    if (rule.profile.test(profileText)) add(rule.signal, rule.weight);
  });
}

function evaluateModelThemeChoice(themes, requestedThemeId) {
  const normalizedId = String(requestedThemeId || "").trim();
  if (!normalizedId) {
    return { accepted: false, reason: "missing_theme_id", candidate: null };
  }
  const candidate = (Array.isArray(themes) ? themes : [])
    .find(item => item && item.id === normalizedId) || null;
  if (!candidate) {
    return { accepted: false, reason: "unregistered_theme_id", candidate: null };
  }
  return { accepted: true, reason: "accepted", candidate };
}

function inferTheme(themes, content, defaultThemeId = "blue-professional") {
  const candidates = Array.isArray(themes) ? themes.filter(theme => theme && theme.id) : [];
  const preferences = inferPreferences(content);
  const intentText = selectionIntentText(content);
  const intentPreferences = inferPreferences(intentText);
  const intentPositiveText = intentPreferences.positive_text;
  const scoreByTheme = new Map();
  const matchesByTheme = new Map();

  candidates.forEach(theme => {
    const profile = themeProfile(theme);
    let score = theme.id === defaultThemeId ? 0.25 : 0;
    const matches = [];
    const add = (signal, weight) => {
      score += weight;
      matches.push({ signal, weight });
    };

    if (profile.fallback) add("fallback theme", -1);

    let strongKeywordRuleMatched = false;
    THEME_KEYWORD_RULES.forEach(rule => {
      if (theme.id === rule.theme_id && rule.pattern.test(intentPositiveText)) {
        add(rule.signal, rule.weight);
        if (rule.weight >= KEYWORD_INDUSTRY_DEDUP_WEIGHT) {
          strongKeywordRuleMatched = true;
        }
      }
    });

    // A keyword rule and industry taxonomy commonly describe the same phrase.
    // Do not count that phrase twice for one theme; otherwise a single `KPI`
    // token receives both the keyword and analytics-industry weights and can
    // become an incorrectly protected global decision.
    if (!strongKeywordRuleMatched) {
      const industryHits = directMetadataHits(intentPositiveText, profile.industry_terms);
      if (industryHits.length) {
        add(`industry metadata: ${industryHits.slice(0, 2).join(", ")}`, Math.min(6, industryHits.length * 3));
      }
      applyTaxonomyMatches(
        INDUSTRY_MATCH_RULES,
        "industry",
        intentPositiveText,
        profile,
        add
      );
    }

    const moodHits = directMetadataHits(intentPositiveText, profile.mood_terms);
    if (moodHits.length) {
      add(`mood metadata: ${moodHits.slice(0, 2).join(", ")}`, Math.min(4, moodHits.length * 2));
    }
    applyTaxonomyMatches(
      MOOD_MATCH_RULES,
      "mood",
      intentPositiveText,
      profile,
      add
    );

    if (preferences.wants_light) {
      if (profile.scheme.includes("light")) add("light canvas", 6);
      else if (profile.scheme === "mixed") add("mixed canvas conflicts with light-first brief", -3);
      else if (profile.scheme === "dark") add("dark canvas conflicts with light-first brief", -8);
    }
    if (preferences.rejects_dark) {
      if (profile.scheme.includes("light")) add("explicit dark-theme opt-out", 4);
      else if (profile.scheme === "mixed") add("explicit dark-theme opt-out", -4);
      else if (profile.scheme === "dark") add("explicit dark-theme opt-out", -12);
    }

    if (preferences.wants_friendly) {
      if (profileHas(profile, /(?:friendly|亲和|cheerful|social)/i)) add("friendly mood", 7);
      if (profileHas(profile, /(?:warm|\bsoft\b|pastel|considered|温暖|柔和)/i)) {
        add("warm or soft supporting mood", 2);
      }
      if (profileHas(profile, /(?:raw|brutalist|authoritative|technical|neon|硬朗|高冷)/i)) {
        add("hard or cold mood conflicts with friendly brief", -3);
      }
    }

    if (preferences.wants_soft) {
      if (profileHas(profile, /(?:\bsoft\b|pastel|quiet|sage|blush|peach|柔和|粉彩|鼠尾草|桃色)/i)) {
        add("soft palette mood", 7);
      }
      if (profileHas(profile, /(?:warm|friendly|亲和)/i)) add("warm supporting palette", 2);
      if (profileHas(profile, /(?:bold|raw|neon|high contrast|硬朗|高饱和)/i)) {
        add("hard palette conflicts with soft brief", -4);
      }
    }

    if (preferences.wants_clean) {
      if (profileHas(profile, /(?:quiet|minimal|considered|modern|precise|restrained|editorial|bichromatic|克制|现代|严谨|留白)/i)) {
        add("clean and ordered mood", 3);
      }
      if (profileHas(profile, /(?:collage|sticky notes|handmade|pixel|neon|zine|raw|拼贴|便利贴|手作|像素)/i)) {
        add("busy visual language conflicts with clean brief", -4);
      }
    }

    if (preferences.wants_lively) {
      if (profileHas(profile, /(?:playful|cheerful|friendly|energetic|活泼|亲和)/i)) {
        add("lively supporting mood", 3);
      } else if (profileHas(profile, /(?:quiet|literary|沉静)/i)) {
        add("quiet mood underplays requested energy", -1);
      }
    }

    if (preferences.wants_comic) {
      if (profileHas(profile, /(?:comic|graphic novel|storyboard|speech bubble|halftone|manga|pop art|漫画|分镜|对话气泡|拟声词|网点纸|波普漫画)/i)) {
        add("comic-panel visual language", 18);
      } else if (profileHas(profile, /(?:playful|graphic|bold|pop|活泼|图形)/i)) {
        add("graphic supporting mood for comic brief", 2);
      }
    }
    if (
      preferences.rejects_comic
      && profileHas(profile, /(?:comic|graphic novel|storyboard|speech bubble|halftone|manga|pop art|漫画|分镜|对话气泡|拟声词|网点纸|波普漫画)/i)
    ) {
      add("explicit comic-style opt-out", -20);
    }

    if (preferences.wants_pixel) {
      if (profileHas(profile, /(?:pixel[- ]art|8[- ]?bit|16[- ]?bit|arcade|retro-tech|cyberpunk|像素街机|像素艺术|霓虹街机)/i)) {
        add("pixel-arcade visual language", 18);
      } else if (profileHas(profile, /(?:pixel|retro|nostalgia|gaming|像素|复古|怀旧)/i)) {
        add("retro supporting mood for pixel brief", 2);
      }
    }
    if (
      preferences.rejects_pixel
      && profileHas(profile, /(?:pixel|8[- ]?bit|16[- ]?bit|arcade|retro-tech|cyberpunk|像素|街机|电玩)/i)
    ) {
      add("explicit pixel-style opt-out", -20);
    }

    if (
      preferences.rejects_friendly
      && profileHas(profile, /(?:friendly|approachable|welcoming|cheerful|亲和|友好|欢迎)/i)
    ) {
      add("explicit friendly-style opt-out", -14);
    }
    if (
      preferences.rejects_lively
      && profileHas(profile, /(?:playful|cheerful|energetic|upbeat|fun|活泼|活力)/i)
    ) {
      add("explicit lively-style opt-out", -14);
    }

    if (preferences.internal_training) {
      if (/training/i.test(profile.industry)) add("training industry fit", 8);
      else if (/education/i.test(profile.industry)) add("education industry fit", 6);
      else if (/workshop/i.test(profile.industry)) add("workshop industry fit", 4);
      else if (/community/i.test(profile.industry)) add("community industry fit", 3);
      else if (/startup/i.test(profile.industry)) add("startup industry fit", 2);
    }

    if (preferences.enterprise_context) {
      if (/(?:enterprise|business|technology|consulting|b2b)/i.test(profile.industry)) {
        add("enterprise industry fit", 4);
      } else if (/startup/i.test(profile.industry)) {
        add("startup industry fit", 2);
      }
      if (/^medium(?:-low|-high)?$/.test(profile.formality)) add("workplace formality fit", 2);
      else if (profile.formality === "low") add("too informal for workplace training", -3);
    }

    if (preferences.rejects_stiff && profile.formality === "high") {
      add("high formality conflicts with approachable brief", -2);
    }

    if (preferences.wants_restrained_palette) {
      if (profileHas(profile, /(?:bichromatic|monochrome|restrained|quiet|\bsoft\b|pastel|克制|柔和)/i)) {
        add("restrained accent palette", 4);
      }
      if (profileHas(profile, /(?:multicolor|rainbow|chromatic|neon|多彩|高饱和)/i)) {
        add("multicolor palette conflicts with limited accents", -5);
      }
    }

    if (preferences.wants_cool_palette) {
      if (profile.scheme.includes("cool")) add("cool light palette", 8);
      if (profileHas(profile, /(?:cool|navy|steel gr[ae]y|blue gr[ae]y|冷色|深蓝|钢灰|蓝灰)/i)) {
        add("navy and steel palette", 5);
      }
      if (profile.scheme.includes("warm") || profileHas(profile, /(?:warm paper|warm parchment|暖色|暖黄)/i)) {
        add("warm palette conflicts with cool brief", -8);
      }
    }

    if (preferences.formal_solution_review) {
      if (/(?:procurement|bid evaluation|consulting|enterprise|b2b|technology|advisory)/i.test(profile.industry)) {
        add("formal solution-review industry fit", 6);
      }
      if (profile.formality === "high") add("high-formality review fit", 4);
      else if (profile.formality === "low") add("too informal for formal review", -6);
    }

    if (preferences.rejects_collage) {
      if (profile.family === "playful-collage") add("explicit collage opt-out", -14);
      if (profileHas(profile, /(?:collage|sticky notes|handmade|拼贴|便利贴|手作)/i)) {
        add("explicit collage opt-out", -10);
      }
    }
    if (preferences.rejects_retro) {
      if (profile.family === "retro-interface") add("explicit retro opt-out", -14);
      if (profileHas(profile, /(?:retro|vintage|nostalgia|pixel|zine|riso|复古|怀旧|像素)/i)) {
        add("explicit retro opt-out", -12);
      }
    }
    if (
      preferences.rejects_handwritten
      && profileHas(profile, /(?:handwritten|handmade|hand[- ]?drawn|sticky notes|annotation|手写|手作|便签)/i)
    ) {
      add("explicit handwritten opt-out", -14);
    }

    if (
      theme.id === "soft-editorial"
      && preferences.wants_light
      && preferences.wants_soft
      && preferences.wants_clean
      && preferences.wants_restrained_palette
    ) {
      add("soft clean light signature", 8);
    }
    if (
      theme.id === "consulting-navy"
      && preferences.wants_light
      && preferences.wants_cool_palette
      && preferences.formal_solution_review
    ) {
      add("cool consulting review signature", 8);
    }
    if (
      theme.id === "pin-and-paper"
      && preferences.internal_training
      && !preferences.rejects_handwritten
    ) {
      add("training workshop signature", 4);
    }
    if (theme.id === "comic-panel" && preferences.wants_comic && !preferences.rejects_comic) {
      add("comic-panel signature", 10);
    }
    if (theme.id === "8-bit-orbit" && preferences.wants_pixel && !preferences.rejects_pixel) {
      add("8-bit-orbit signature", 10);
    }
    if (
      theme.id === defaultThemeId
      && preferences.enterprise_context
      && !preferences.wants_friendly
      && !preferences.wants_soft
    ) {
      add("neutral enterprise fallback", 3);
    }

    scoreByTheme.set(theme.id, score);
    matchesByTheme.set(theme.id, matches);
  });

  const ranked = candidates
    .map((theme, index) => ({ theme_id: theme.id, score: scoreByTheme.get(theme.id) || 0, index }))
    .sort((left, right) => right.score - left.score || left.index - right.index);
  const top = ranked[0] || { theme_id: defaultThemeId, score: 0, index: 0 };
  const runnerUp = ranked[1] || { score: 0 };
  const margin = top.score - runnerUp.score;
  const confidence = top.score >= 18 && margin >= 2
    ? "high"
    : top.score >= 10 && margin >= 1
      ? "medium"
      : "low";
  const themeId = confidence === "low" ? defaultThemeId : top.theme_id;
  return {
    theme_id: themeId,
    source: confidence === "low" ? "fallback_default" : "content_inference",
    confidence,
    score: scoreByTheme.get(themeId) || 0,
    margin,
    matched_signals: matchesByTheme.get(themeId) || [],
    ranking: ranked.slice(0, 5).map(({ theme_id, score }) => ({ theme_id, score })),
  };
}

module.exports = {
  evaluateModelThemeChoice,
  inferPreferences,
  inferTheme,
  selectionIntentText,
  selectionText,
};
