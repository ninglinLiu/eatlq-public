import type { Locale } from "./index";

/** 权威等级 */
const AUTHORITY: Record<string, { en: string; zh: string }> = {
  GOLD: { en: "GOLD", zh: "金标准" },
  VALIDATED: { en: "VALIDATED", zh: "已验证" },
  AUTO_CURATED: { en: "AUTO_CURATED", zh: "自动策展" },
  ARTIFACT_ONLY: { en: "ARTIFACT_ONLY", zh: "仅工件" },
  ARTIFACTS_ONLY: { en: "ARTIFACTS_ONLY", zh: "仅工件" },
  UNVERIFIED: { en: "UNVERIFIED", zh: "未验证" },
  MIXED_AUTHORITY: { en: "MIXED_AUTHORITY", zh: "混合权威" },
  UNKNOWN: { en: "UNKNOWN", zh: "未知" },
};

/** 状态 / 执行 / 窗口 */
const STATUS: Record<string, { en: string; zh: string }> = {
  LIVE: { en: "LIVE", zh: "实盘" },
  LIVE_ENABLED: { en: "LIVE_ENABLED", zh: "实盘已启用" },
  LIVE_ORDER_SENT: { en: "LIVE_ORDER_SENT", zh: "实盘订单已发送" },
  LIVE_FILL_CONFIRMED: { en: "LIVE_FILL_CONFIRMED", zh: "实盘成交已确认" },
  PAPER: { en: "PAPER", zh: "模拟" },
  PAPER_FORWARD: { en: "PAPER_FORWARD", zh: "模拟转发" },
  BACKTEST: { en: "BACKTEST", zh: "回测" },
  PAUSED: { en: "PAUSED", zh: "已暂停" },
  STOPPED: { en: "STOPPED", zh: "已停止" },
  PANIC: { en: "PANIC", zh: "紧急停止" },
  ACTIVE: { en: "ACTIVE", zh: "活跃" },
  SUPPORTED: { en: "SUPPORTED", zh: "有证据支持" },
  WEAK_EVIDENCE: { en: "WEAK_EVIDENCE", zh: "证据薄弱" },
  REJECTED: { en: "REJECTED", zh: "已拒绝" },
  INVALIDATED: { en: "INVALIDATED", zh: "已失效" },
  SUPERSEDED: { en: "SUPERSEDED", zh: "已被取代" },
  PROPOSED: { en: "PROPOSED", zh: "已提议" },
  AUTHORIZED: { en: "AUTHORIZED", zh: "已授权" },
  STARTED: { en: "STARTED", zh: "已开始" },
  COMPLETED: { en: "COMPLETED", zh: "已完成" },
  FAILED: { en: "FAILED", zh: "已失败" },
  OPEN: { en: "OPEN", zh: "未解决" },
  PARTIALLY_RESOLVED: { en: "PARTIALLY_RESOLVED", zh: "部分解决" },
  RESOLVED: { en: "RESOLVED", zh: "已解决" },
  VALIDATED: { en: "VALIDATED", zh: "已验证" },
  GOLD: { en: "GOLD", zh: "金标准" },
  AUTO_CURATED: { en: "AUTO_CURATED", zh: "自动策展" },
  ARTIFACTS_ONLY: { en: "ARTIFACTS_ONLY", zh: "仅工件" },
  EMPTY_CONFIRMED: { en: "EMPTY_CONFIRMED", zh: "已确认为空" },
  UNVERIFIED: { en: "UNVERIFIED", zh: "未验证" },
  UNKNOWN: { en: "UNKNOWN", zh: "未知" },
};

/** 作者类型 */
const AUTHORSHIP: Record<string, { en: string; zh: string }> = {
  USER_EXPLICIT: { en: "USER_EXPLICIT", zh: "用户明确" },
  AGENT_EXPLICIT: { en: "AGENT_EXPLICIT", zh: "系统明确记录" },
  JOINTLY_DEVELOPED: { en: "JOINTLY_DEVELOPED", zh: "共同形成" },
  COMPILER_INFERRED: { en: "COMPILER_INFERRED", zh: "系统自动推断" },
  USER_BELIEF: { en: "USER_BELIEF", zh: "用户信念" },
};

/** 认识论角色 */
const EPISTEMIC: Record<string, { en: string; zh: string }> = {
  USER_BELIEF: { en: "USER_BELIEF", zh: "用户信念" },
  USER_OBJECTION: { en: "USER_OBJECTION", zh: "用户异议" },
  META_RESEARCH_REFLECTION: { en: "META_RESEARCH_REFLECTION", zh: "元研究反思" },
  EMPIRICAL_RESULT: { en: "EMPIRICAL_RESULT", zh: "实证结果" },
  METHODOLOGY_RULE: { en: "METHODOLOGY_RULE", zh: "方法论规则" },
  HYPOTHESIS: { en: "HYPOTHESIS", zh: "假设" },
  DECISION: { en: "DECISION", zh: "决策" },
};

/** 持久范围 */
const PERSISTENCE: Record<string, { en: string; zh: string }> = {
  LONG_TERM: { en: "LONG_TERM", zh: "长期有效" },
  REGIME_DEPENDENT: { en: "REGIME_DEPENDENT", zh: "依赖市场制度" },
  PROJECT_SPECIFIC: { en: "PROJECT_SPECIFIC", zh: "项目特定" },
  SESSION_ONLY: { en: "SESSION_ONLY", zh: "仅当次会话" },
};

/** 复用策略 */
const REUSE: Record<string, { en: string; zh: string }> = {
  ALWAYS_LOAD: { en: "ALWAYS_LOAD", zh: "始终加载" },
  TOPIC_MATCH_ONLY: { en: "TOPIC_MATCH_ONLY", zh: "仅主题匹配时" },
  AUDIT_REFERENCE_ONLY: { en: "AUDIT_REFERENCE_ONLY", zh: "仅审计参考" },
  DO_NOT_REUSE: { en: "DO_NOT_REUSE", zh: "禁止复用" },
};

const MISC: Record<string, { en: string; zh: string }> = {
  CORRECTION: { en: "CORRECTION", zh: "修正链" },
  high: { en: "high", zh: "高" },
  medium: { en: "medium", zh: "中" },
  low: { en: "low", zh: "低" },
  hypothesis: { en: "hypothesis", zh: "假设级" },
  high_in_backtest: { en: "high_in_backtest", zh: "回测中高" },
  HYPOTHESIS_MISIDENTIFICATION: { en: "HYPOTHESIS_MISIDENTIFICATION", zh: "假设识别错误" },
  METHODOLOGY_ERROR: { en: "METHODOLOGY_ERROR", zh: "方法论错误" },
  EMPIRICAL_INVALIDATION: { en: "EMPIRICAL_INVALIDATION", zh: "实证失效" },
  OVERCLAIM: { en: "OVERCLAIM", zh: "过度宣称" },
  EATLQ: { en: "EATLQ", zh: "链上量化研究" },
  "See source_pointers; authority pass partial": {
    en: "See source_pointers; authority pass partial",
    zh: "见来源指针；权威提升部分完成",
  },
  "Future regime; live execution not confirmed unless stated": {
    en: "Future regime; live execution not confirmed unless stated",
    zh: "未来制度环境；除非另有说明否则未确认实盘",
  },
};

/** 对象类型 */
const OBJECT_TYPE: Record<string, { en: string; zh: string }> = {
  user_thesis: { en: "User Thesis", zh: "用户论点" },
  research_principle: { en: "Research Principle", zh: "研究原则" },
  hypothesis: { en: "Hypothesis", zh: "假设" },
  experiment: { en: "Experiment", zh: "实验" },
  conclusion: { en: "Conclusion", zh: "结论" },
  failure: { en: "Failure", zh: "失败记录" },
  method: { en: "Method", zh: "方法" },
  decision: { en: "Decision", zh: "决策" },
  guardrail: { en: "Guardrail", zh: "永久护栏" },
};

/** 空窗口分类 */
const EMPTY_CLASS: Record<string, { en: string; zh: string }> = {
  "No transcript and no artifacts in window": {
    en: "No transcript and no artifacts in window",
    zh: "该窗口内无对话记录且无工件",
  },
};

const ALL_MAPS = [
  AUTHORITY,
  STATUS,
  AUTHORSHIP,
  EPISTEMIC,
  PERSISTENCE,
  REUSE,
  MISC,
  OBJECT_TYPE,
  EMPTY_CLASS,
];

function lookup(
  maps: Record<string, { en: string; zh: string }>[],
  key: string,
  locale: Locale
): string | null {
  const upper = key.toUpperCase();
  for (const map of maps) {
    if (map[key]) return map[key][locale];
    if (map[upper]) return map[upper][locale];
  }
  return null;
}

export function tLabel(locale: Locale, value?: string | null): string {
  if (!value || value === "—") return locale === "zh" ? "—" : "—";
  if (value === "unknown") return locale === "zh" ? "未知" : "unknown";
  if (value === "true") return locale === "zh" ? "是" : "Yes";
  if (value === "false") return locale === "zh" ? "否" : "No";

  const found = lookup(ALL_MAPS, value, locale);
  if (found) return found;

  // 尝试匹配含逗号的多值（如 authorship 数组 join）
  if (value.includes(",")) {
    return value
      .split(",")
      .map((v) => tLabel(locale, v.trim()))
      .join(locale === "zh" ? "、" : ", ");
  }

  return value;
}

export function tAuthority(locale: Locale, level?: string): string {
  return tLabel(locale, level ?? "UNKNOWN");
}

export function tStatus(locale: Locale, status?: string): string {
  return tLabel(locale, status ?? "UNKNOWN");
}

export function tAuthorship(locale: Locale, authorship?: string | string[]): string {
  if (Array.isArray(authorship)) {
    return authorship.map((a) => tLabel(locale, a)).join(locale === "zh" ? "、" : ", ");
  }
  return tLabel(locale, authorship ?? "UNKNOWN");
}

export function tEpistemic(locale: Locale, role?: string): string {
  return tLabel(locale, role ?? "UNKNOWN");
}

export function tObjectType(locale: Locale, type: string): string {
  return OBJECT_TYPE[type]?.[locale] ?? type;
}

export function tBoolean(locale: Locale, value?: boolean): string {
  if (value === true) return locale === "zh" ? "是" : "Yes";
  if (value === false) return locale === "zh" ? "否" : "No";
  return locale === "zh" ? "未知" : "Unknown";
}

export function tLiveProven(locale: Locale, proven?: boolean): string {
  if (proven === true) return locale === "zh" ? "实盘已验证" : "Live proven";
  if (proven === false) return locale === "zh" ? "未经实盘验证" : "Not live proven";
  return locale === "zh" ? "未知" : "Unknown";
}

export function tEmptyClass(locale: Locale, text?: string): string {
  if (!text) return "";
  return EMPTY_CLASS[text]?.[locale] ?? text;
}

export function formatSourcePointer(
  locale: Locale,
  ptr: {
    source_file?: string;
    file?: string;
    transcript_id?: string;
    jsonl_index?: number;
    message_id?: string;
  }
): string {
  if (locale === "zh") {
    const file = ptr.source_file ?? ptr.file ?? "";
    const dateMatch = file.match(/(\d{4})-(\d{2})-(\d{2})/);
    const parts: string[] = [];
    if (dateMatch) {
      parts.push(`${dateMatch[1]}年${dateMatch[2]}月${dateMatch[3]}日`);
    }
    if (ptr.jsonl_index !== undefined) {
      parts.push(`源记录第 ${ptr.jsonl_index + 1} 行`);
    }
    return parts.length > 0 ? parts.join(" · ") : "来源未标注";
  }

  const parts: string[] = [];
  const file = ptr.source_file ?? ptr.file ?? "";
  const dateMatch = file.match(/(\d{4}-\d{2}-\d{2})/);
  if (dateMatch) parts.push(dateMatch[1]);

  const tid =
    ptr.transcript_id?.slice(0, 8) ??
    file.match(/([a-f0-9-]{8})/)?.[1]?.slice(0, 8);
  if (tid) {
    parts.push(`Transcript ${tid}`);
  }
  if (ptr.jsonl_index !== undefined) {
    parts.push(`JSONL index ${ptr.jsonl_index}`);
  }
  if (ptr.message_id) parts.push(ptr.message_id);

  if (parts.length === 0) {
    const base = file.split("/").pop();
    return base ?? "Unknown source";
  }
  return parts.join(" · ");
}

export function formatDisplayValue(locale: Locale, value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return tBoolean(locale, value);
  if (Array.isArray(value)) {
    if (value.length === 0) return "—";
    return value.map((v) => formatDisplayValue(locale, v)).join(locale === "zh" ? "、" : ", ");
  }
  if (typeof value === "string") {
    const labeled = tLabel(locale, value);
    if (labeled !== value) return labeled;
    if (locale === "zh") {
      const iso = value.match(
        /^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?/
      );
      if (iso) {
        const base = `${iso[1]}年${Number(iso[2])}月${Number(iso[3])}日`;
        if (iso[4] != null) {
          return `${base} ${Number(iso[4])}时${Number(iso[5])}分`;
        }
        return base;
      }
      if (isMostlyLatinForValue(value)) {
        return value;
      }
    }
    return value;
  }
  if (typeof value === "object") {
    return locale === "zh" ? "（结构化数据）" : JSON.stringify(value, null, 2);
  }
  return String(value);
}

export function tListCount(
  locale: Locale,
  filtered: number,
  total: number
): string {
  return locale === "zh"
    ? `显示 ${filtered} 条，共 ${total} 条`
    : `${filtered} / ${total}`;
}

export function tObjectsCount(locale: Locale, count: number): string {
  return locale === "zh" ? `${count} 个对象` : `${count} objects`;
}

export function tWarnings(locale: Locale, count: number): string {
  return locale === "zh" ? `${count} 条警告` : `${count} warnings`;
}

export function tSearchResults(
  locale: Locale,
  query: string,
  count: number
): string {
  return locale === "zh"
    ? `搜索「${query}」— 共 ${count} 条结果`
    : `"${query}" — ${count} results`;
}

/** 时间线统计行 */
export function formatWindowStats(
  locale: Locale,
  stats: {
    raw_events?: number;
    selected?: number;
    research_principles?: number;
    user_theses?: number;
    hypotheses?: number;
    conclusions?: number;
    failures?: number;
    experiments?: number;
    methods?: number;
    decisions?: number;
  }
): string {
  const canonical =
    (stats.user_theses ?? 0) +
    (stats.research_principles ?? 0) +
    (stats.hypotheses ?? 0) +
    (stats.experiments ?? 0) +
    (stats.conclusions ?? 0) +
    (stats.failures ?? 0) +
    (stats.methods ?? 0) +
    (stats.decisions ?? 0);

  if (locale === "zh") {
    return `原始 ${stats.raw_events ?? 0} · 精选 ${stats.selected ?? 0} · 规范对象 ${canonical}`;
  }
  return `Raw ${stats.raw_events ?? 0} · Selected ${stats.selected ?? 0} · Canonical ${canonical}`;
}

export { AUTHORITY, STATUS, AUTHORSHIP, EPISTEMIC, PERSISTENCE, REUSE, OBJECT_TYPE };

export function formatFilterOption(locale: Locale, value: string): string {
  const labeled = tLabel(locale, value);
  if (labeled !== value) return labeled;
  return value;
}

function isMostlyLatinForValue(text: string): boolean {
  const cjk = (text.match(/[\u4e00-\u9fff]/g) ?? []).length;
  if (cjk >= 4) return false;
  const letters = (text.match(/[a-zA-Z]/g) ?? []).length;
  return letters > 6;
}
