export type Locale = "en" | "zh";

export const locales: Locale[] = ["en", "zh"];
export const defaultLocale: Locale = "en";

export function isValidLocale(value: string): value is Locale {
  return locales.includes(value as Locale);
}

export type PageCopy = {
  intro: string;
  emptyDoc?: string;
  originalDoc?: string;
};

export type Dictionary = {
  site: Record<string, string>;
  nav: Record<string, string>;
  home: Record<string, string>;
  filters: Record<string, string>;
  tabs: Record<string, string>;
  timeline: Record<string, string>;
  fields: Record<string, string>;
  objectTypes: Record<string, string>;
  pages: Record<string, PageCopy>;
};

const en: Dictionary = {
  site: {
    title: "Quant Research OS",
    tagline: "Your research memory, compiled.",
    mixedAuthority: "MIXED_AUTHORITY — not all objects are equally reliable.",
    mixedAuthorityDetail:
      "This global pack mixes GOLD, VALIDATED, AUTO_CURATED, ARTIFACT_ONLY, and UNVERIFIED objects. Always check authority badges before trusting a conclusion.",
    dataUpdated: "Data loaded",
    searchPlaceholder: "Search statements, IDs, projects…",
    search: "Search",
    noResults: "No results found.",
    filters: "Apply filters",
    filterAll: "All",
    showAll: "Show all",
    readMore: "Read more",
    back: "Back",
    related: "Related objects",
    sourcePointers: "Source pointers",
    canProve: "Can prove",
    cannotProve: "Cannot prove",
    limitations: "Limitations",
    contradictions: "Contradictions",
    supersededBy: "Superseded by",
    invalidates: "Invalidates",
    supports: "Supports",
    executionConfirmed: "Was execution confirmed?",
    yes: "Yes",
    no: "No",
    unknown: "Unknown",
    dataQualityNotice: "Data quality notice",
    experimentsDedup:
      "Auto-promoted experiment objects may contain lineage pointers and duplicates. Treat count as provisional until deduplication is complete.",
    conclusionsAutoCurated:
      "59 total: 19 VALIDATED conclusions (shown first), 40 AUTO_CURATED agent work logs (summary only). Prefer VALIDATED entries.",
    needsDedup: "Needs deduplication",
    noContent: "No content available.",
    guardrailsCount: "guardrails",
    fromFailure: "From failure",
    objectId: "Object ID",
    highlightNote: "Highlighted — matches a known critical failure pattern.",
  },
  nav: {
    overview: "Overview",
    knowledge: "Knowledge",
    conclusions: "Conclusions",
    hypotheses: "Hypotheses",
    experiments: "Experiments",
    failuresGuardrails: "Failures & Guardrails",
    methods: "Methods",
    decisions: "Decisions",
    thinking: "Thinking",
    userTheses: "User Theses",
    researchPrinciples: "Research Principles",
    history: "History",
    timeline: "Timeline",
    monthly: "Monthly",
    openQuestions: "Open Questions",
    system: "System",
    currentState: "Current State",
    authorityModel: "Authority Model",
    productionStatus: "Production Status",
  },
  home: {
    historicalRange: "Historical range",
    registeredWindows: "Registered windows",
    nonEmptyProcessed: "Non-empty processed windows",
    qaPassedWindows: "QA passed windows (full pipeline)",
    qaPassRateAmongProcessed: "QA pass rate among processed",
    qaPassedShareOfAll: "QA passed share of all windows",
    qaNote:
      "Note: share of all windows ≠ pass rate among processed. EMPTY_CONFIRMED and ARTIFACTS_ONLY did not enter full QA.",
    emptyConfirmed: "Empty confirmed windows",
    artifactsOnly: "Artifacts only (needs curation)",
    rawEvents: "Raw events (processed weeks)",
    selectedEvents: "Selected events",
    canonicalObjects: "Canonical objects (processed weeks)",
    currentState: "Current State",
    currentStateLegend: "System status indicators",
    knowledgeOverview: "Knowledge overview — eight canonical classes",
    mostImportantGuardrails: "Most important permanent guardrails",
    latestConclusions: "Latest VALIDATED conclusions",
    latestConclusionsDesc:
      "Showing conclusions with authority_level = VALIDATED. Check execution_mode and live_proven before acting.",
    unresolvedQuestions: "Unresolved questions",
    knowledgeByAuthority: "Knowledge by authority level",
    researchHistory: "Research history by month",
    liveFacts: "Known live facts (VALIDATED, Round 6)",
    liveFact1: "KEYSTONE live: 2× PARADAENGI fills Jun 23, ABANDON −100%, total −0.114 ETH",
    liveFact2: "No confirmed profitable round-trip (live_proven: false)",
    liveFact3: "SYSTEM_PANIC: active after Jun 23 disaster",
    liveFact4: "deep_edge_no: Jun 25 (AUTO_CURATED promotion pass)",
    readExample: "How to read a conclusion",
    readExampleText:
      "KEYSTONE holdout +59.7% — authority: VALIDATED, execution_mode: BACKTEST, live_proven: false. Backtest strength does not imply live profitability.",
    globalTotals: "Global deduped totals",
    guardrailsTotal: "Permanent guardrails",
    processedNote: "10/10 processed weeks passed",
    processedNoteZh: "10/10 已处理窗口全部通过",
  },
  filters: {
    onlyValidated: "Only VALIDATED",
    onlyGoldValidated: "Only GOLD + VALIDATED",
    onlyLiveProven: "Only live proven",
    onlyBacktest: "Only BACKTEST",
    onlyAlwaysLoad: "Only ALWAYS_LOAD principles",
    onlyUnresolved: "Only unresolved questions",
    onlyInvalidated: "Only invalidated",
    onlyPermanentGuardrails: "Only permanent guardrails",
    showPermanentPrinciples: "Show only permanent / always-load principles",
  },
  tabs: { failures: "Failures", guardrails: "Permanent Guardrails" },
  timeline: {
    intro:
      "Weekly research windows from earliest discovery to latest. Click a window for summary, open questions, source conflicts, and QA audit.",
    summary: "Weekly summary",
    openQuestions: "Open questions",
    sourceConflicts: "Source conflicts",
    qa: "Quality audit",
    raw: "Raw events",
    selected: "Selected",
    conclusions: "Conclusions",
    hypotheses: "Hypotheses",
    experiments: "Experiments",
    failures: "Failures",
    methods: "Methods",
    decisions: "Decisions",
    theses: "User theses",
    principles: "Principles",
    statusLegend: "Window status legend",
    canonicalTotal: "Canonical total",
  },
  fields: {
    status: "Status",
    authority: "Authority",
    authorship: "Authorship",
    epistemicRole: "Epistemic role",
    persistenceScope: "Persistence scope",
    reusePolicy: "Reuse policy",
    executionMode: "Execution mode",
    liveProven: "Live proven",
    validFrom: "Valid from",
    validUntil: "Valid until",
    regimeDependency: "Regime dependency",
    operationalDefinition: "Operational definition",
    origin: "Origin",
    whatHappened: "What happened",
    whyItHappened: "Why it happened",
    impact: "Impact",
    howDetected: "How detected",
    futureGuardrail: "Future guardrail",
    executionStatus: "Execution status",
    projectScope: "Project scope",
    sampleSize: "Sample size",
    timeRange: "Time range",
    entry: "Entry rule",
    exit: "Exit rule",
    costModel: "Cost model",
    trainTestProtocol: "Train/test protocol",
    result: "Result",
    universe: "Universe",
    hypothesisId: "Hypothesis ID",
    evidenceLevel: "Evidence level",
    name: "Name",
    description: "Description",
    whenToUse: "When to use",
    design: "Design",
    features: "Features",
    relatedHypotheses: "Related hypotheses",
    failureType: "Failure type",
    weekScope: "Week scope",
  },
  objectTypes: {
    user_thesis: "User Thesis",
    research_principle: "Research Principle",
    hypothesis: "Hypothesis",
    experiment: "Experiment",
    conclusion: "Conclusion",
    failure: "Failure",
    method: "Method",
    decision: "Decision",
    guardrail: "Guardrail",
  },
  pages: {
    conclusions: {
      intro:
        "Browse compiled conclusions with authority, execution mode, live_proven, regime dependency, and validity window.",
    },
    hypotheses: { intro: "Research hypotheses with operational definitions and status." },
    experiments: { intro: "Experiment records. Count may be inflated until deduplication." },
    failures: { intro: "Documented failures and permanent guardrails." },
    decisions: { intro: "Decision timeline. AUTHORIZED ≠ LIVE_FILL_CONFIRMED." },
    theses: { intro: "Your research thinking history." },
    principles: { intro: "Methodology principles for future agents." },
    methods: { intro: "Documented research methods." },
    openQuestions: { intro: "Unresolved questions from validated weekly windows." },
    timeline: { intro: "Chronological weekly windows." },
    monthly: { intro: "Monthly consolidation summaries." },
    currentState: { intro: "Live snapshot.", originalDoc: "Original English document" },
    authority: { intro: "Authority level interpretation.", originalDoc: "Original English document" },
    productionStatus: { intro: "Pipeline status.", originalDoc: "Original English document" },
    search: { intro: "Search across all canonical objects." },
  },
};

const zh: Dictionary = {
  site: {
    title: "量化研究操作系统",
    tagline: "你的研究记忆，已编译归档。",
    mixedAuthority: "混合权威 — 并非所有对象具有同等可信度。",
    mixedAuthorityDetail:
      "全局知识包混合了金标准、已验证、自动策展、仅工件、未验证 五类对象。在采信任何结论前，务必查看权威等级徽章，并区分回测与实盘。",
    dataUpdated: "数据加载时间",
    searchPlaceholder: "搜索陈述、标题、假设、结论、失败、项目、来源指针、编号…",
    search: "搜索",
    noResults: "未找到匹配结果。",
    filters: "应用筛选",
    filterAll: "全部",
    showAll: "显示全部",
    readMore: "查看全部",
    back: "返回",
    related: "相关对象",
    sourcePointers: "来源指针",
    canProve: "能证明什么",
    cannotProve: "不能证明什么",
    limitations: "局限性",
    contradictions: "矛盾关系",
    supersededBy: "已被以下对象取代",
    invalidates: "使以下对象失效",
    supports: "支持以下对象",
    executionConfirmed: "执行是否已确认？",
    yes: "是",
    no: "否",
    unknown: "未知",
    dataQualityNotice: "数据质量提示",
    experimentsDedup:
      "自动提升的实验对象可能包含谱系指针与重复项。计数 353 为临时值，在去重完成前请勿将其视为 353 个独立实验。",
    conclusionsAutoCurated:
      "共 59 条：其中 19 条为已验证结论（置顶显示），40 条为自动策展 Agent 工作日志，仅展示结构化摘要，不含可采信陈述。建议优先阅读已验证条目。",
    needsDedup: "需要去重",
    noContent: "暂无内容。",
    guardrailsCount: "条护栏",
    fromFailure: "源自失败记录",
    objectId: "对象编号",
    highlightNote: "已高亮 — 匹配已知关键失败模式（如报价器≠可卖性、回测≠实盘等）。",
    latinBodyNotice:
      "本条为西文自动策展条目，正文不在中文模式展示。请阅读下方结构化字段与来源信息；完整原文请切换至英文界面查看。",
    markdownNotice: "以下为西文原始文档，中文模式不展开正文。请阅读页面上方中文说明。",
    foldedLatin: "（西文原文·已折叠）",
    switchToEn: "英文界面",
  },
  nav: {
    overview: "概览",
    knowledge: "知识库",
    conclusions: "结论",
    hypotheses: "假设",
    experiments: "实验",
    failuresGuardrails: "失败与护栏",
    methods: "方法",
    decisions: "决策",
    thinking: "研究思想",
    userTheses: "用户论点",
    researchPrinciples: "研究原则",
    history: "历史归档",
    timeline: "时间线 / 周窗口",
    monthly: "月度汇总",
    openQuestions: "开放问题",
    system: "系统文档",
    currentState: "当前状态",
    authorityModel: "权威模型",
    productionStatus: "生产状态",
  },
  home: {
    historicalRange: "历史时间范围",
    registeredWindows: "注册窗口总数",
    nonEmptyProcessed: "非空且已处理窗口",
    qaPassedWindows: "完整流水线质量审计通过窗口",
    qaPassRateAmongProcessed: "已处理窗口质量审计通过率",
    qaPassedShareOfAll: "全部窗口中质量审计通过占比",
    qaNote:
      "注意：「全部窗口占比」≠「已处理窗口通过率」。已确认为空 与 仅工件 的窗口未进入完整质量审计。",
    emptyConfirmed: "已确认为空窗口",
    artifactsOnly: "仅工件窗口（待策展）",
    rawEvents: "原始事件（已处理周合计）",
    selectedEvents: "精选事件（已处理周合计）",
    canonicalObjects: "规范对象（已处理周合计）",
    currentState: "当前状态",
    currentStateLegend: "系统状态指示（请与下方详情对照阅读）",
    knowledgeOverview: "知识概览 — 八类规范对象",
    mostImportantGuardrails: "最重要的永久护栏",
    latestConclusions: "最新已验证结论",
    latestConclusionsDesc:
      "以下为已验证权威等级的结论。行动前务必核对执行模式与实盘验证状态。",
    unresolvedQuestions: "仍未解决的问题",
    knowledgeByAuthority: "按权威等级分布",
    researchHistory: "按月份的研究增长",
    liveFacts: "已知实盘事实（已验证，第六轮）",
    liveFact1:
      "基石策略实盘：6 月 23 日 2 笔帕拉丹吉成交，放弃策略 −100%，合计 −0.114 以太",
    liveFact2: "尚无已确认的盈利往返（实盘未验证）",
    liveFact3: "系统紧急停止：6 月 23 日灾难后仍激活",
    liveFact4: "深度优势否定：6 月 25 日（自动策展提升通过）",
    readExample: "如何阅读一条结论",
    readExampleText:
      "示例：基石策略留样 +59.7% — 权威：已验证，执行模式：回测，实盘未验证。回测强度不等于实盘盈利。",
    globalTotals: "全局去重合计",
    guardrailsTotal: "永久护栏",
    processedNote: "10/10 已处理窗口全部通过",
    processedNoteZh: "10/10 已处理窗口全部通过",
  },
  filters: {
    onlyValidated: "仅已验证",
    onlyGoldValidated: "仅金标准 + 已验证",
    onlyLiveProven: "仅实盘已验证",
    onlyBacktest: "仅回测",
    onlyAlwaysLoad: "仅始终加载原则",
    onlyUnresolved: "仅未解决问题",
    onlyInvalidated: "仅已失效",
    onlyPermanentGuardrails: "仅永久护栏",
    showPermanentPrinciples: "仅显示永久 / 始终加载原则",
  },
  tabs: { failures: "失败记录", guardrails: "永久护栏" },
  timeline: {
    intro:
      "从最早发现到最新的每周研究窗口。点击窗口可查看周摘要、开放问题、来源冲突与质量审计。",
    summary: "周摘要",
    openQuestions: "开放问题",
    sourceConflicts: "来源冲突",
    qa: "质量审计",
    raw: "原始事件",
    selected: "精选事件",
    conclusions: "结论",
    hypotheses: "假设",
    experiments: "实验",
    failures: "失败",
    methods: "方法",
    decisions: "决策",
    theses: "用户论点",
    principles: "研究原则",
    statusLegend: "窗口状态说明",
    canonicalTotal: "规范对象合计",
  },
  fields: {
    status: "状态",
    authority: "权威等级",
    authorship: "作者类型",
    epistemicRole: "认识论角色",
    persistenceScope: "持久范围",
    reusePolicy: "复用策略",
    executionMode: "执行模式",
    liveProven: "实盘是否已验证",
    validFrom: "生效起始",
    validUntil: "生效截止",
    regimeDependency: "市场制度依赖",
    operationalDefinition: "操作定义",
    origin: "来源类型",
    whatHappened: "发生了什么",
    whyItHappened: "为什么发生",
    impact: "影响",
    howDetected: "如何发现",
    futureGuardrail: "未来护栏",
    executionStatus: "执行状态",
    projectScope: "项目范围",
    sampleSize: "样本量",
    timeRange: "时间范围",
    entry: "入场规则",
    exit: "出场规则",
    costModel: "成本模型",
    trainTestProtocol: "训练/测试协议",
    result: "结果",
    universe: "标的范围",
    hypothesisId: "关联假设编号",
    evidenceLevel: "证据等级",
    name: "名称",
    description: "描述",
    whenToUse: "适用场景",
    design: "实验设计",
    features: "特征",
    relatedHypotheses: "相关假设",
    failureType: "失败类型",
    weekScope: "所属周窗口",
  },
  objectTypes: {
    user_thesis: "用户论点",
    research_principle: "研究原则",
    hypothesis: "假设",
    experiment: "实验",
    conclusion: "结论",
    failure: "失败记录",
    method: "方法",
    decision: "决策",
    guardrail: "永久护栏",
  },
  pages: {
    conclusions: {
      intro:
        "浏览已编译结论。已验证条目置顶；自动策展条目为 Agent 工作日志归档，仅展示摘要。行动前务必核对执行模式与实盘验证状态。",
    },
    hypotheses: {
      intro:
        "研究假设及其操作定义、状态、来源与制度依赖。状态颜色刻意克制 — 请阅读陈述本身，而非只看徽章。",
    },
    experiments: {
      intro:
        "实验记录：标的范围、样本量、入场/出场、成本模型、训练测试协议。计数在去重完成前可能偏高。",
    },
    failures: {
      intro:
        "已记录失败及衍生的永久护栏。报价器≠可卖性、历史高点/最大有利偏移≠已实现期望、回测≠实盘、授权≠成交 等关键模式会高亮显示。",
    },
    decisions: {
      intro:
        "决策时间线，明确展示执行状态。已授权 ≠ 实盘成交已确认 — 详情页务必查看「执行是否已确认」。",
    },
    theses: {
      intro:
        "你的研究思想历史。区分用户信念、用户异议、元研究反思 — 不要把临时观点当作永久信念。",
    },
    principles: {
      intro: "未来系统应继承的方法论原则。「始终加载」类原则为最高优先级继承集。",
    },
    methods: { intro: "已文档化的研究方法，含前置条件与适用场景说明。" },
    openQuestions: {
      intro: "跨已验证周窗口仍未解决的问题。状态：未解决、部分解决、已解决、未知。",
    },
    timeline: { intro: "按时间排列的周窗口，含原始/精选/规范对象计数与质量审计状态。" },
    monthly: { intro: "月度合并摘要，展示知识随时间的增长。" },
    currentState: {
      intro: "项目活跃/暂停状态、紧急停止、全局包指标的快照。",
      originalDoc: "原始西文文档（参考）",
    },
    authority: {
      intro: "权威等级（金标准、已验证、自动策展等）的解读方式。",
      originalDoc: "原始西文文档（参考）",
    },
    productionStatus: {
      intro: "流水线生产状态、批量运行结果与已知限制。",
      originalDoc: "原始西文文档（参考）",
    },
    search: { intro: "在陈述、编号、项目、来源指针中搜索。" },
  },
};

export const dictionaries: Record<Locale, Dictionary> = { en, zh };

export function getDictionary(locale: Locale): Dictionary {
  return dictionaries[locale];
}

export function localePath(locale: Locale, path: string = ""): string {
  const clean = path.startsWith("/") ? path : `/${path}`;
  return `/${locale}${clean === "/" ? "" : clean}`;
}

export function getPageIntro(locale: Locale, pageKey: string): string {
  return dictionaries[locale].pages[pageKey]?.intro ?? "";
}
