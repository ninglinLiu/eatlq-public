import type { Locale } from "./index";
import type { ObjectType } from "@/lib/data/types";
import { getObjectTitle } from "@/lib/utils/object";
import {
  tAuthority,
  tLabel,
  tLiveProven,
  tObjectType,
} from "./labels";

export function stripMarkup(text: string): string {
  return text
    .replace(/<[^>]+>/g, " ")
    .replace(/\[tool:[^\]]+\]/g, " ")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** 是否含足够中文 */
export function hasSubstantialChinese(text: string): boolean {
  const cjk = (text.match(/[\u4e00-\u9fff]/g) ?? []).length;
  if (cjk >= 6) return true;
  const stripped = text.replace(/\s/g, "");
  return stripped.length > 0 && cjk / stripped.length >= 0.15;
}

/** Agent 工作日志 / 自动策展原文（不是给人读的结论） */
export function isAgentWorkLog(text: string): boolean {
  if (!text || text.length < 80) return false;
  if (text.includes("##")) return true;
  if (text.includes("[tool:")) return true;
  if (text.includes("Here is the complete")) return true;
  if (text.startsWith("The user has")) return true;
  if (text.startsWith("The user asked")) return true;
  if (text.includes("TodoWrite")) return true;
  if (text.includes("---") && text.length > 200) return true;
  if ((text.match(/^#{1,3}\s/gm)?.length ?? 0) >= 2) return true;
  return false;
}

export function formatObjectId(locale: Locale, id: string): string {
  if (locale !== "zh") return id;
  const stripped = id.replace(/[A-Za-z_]/g, "").replace(/^-+|-+$/g, "");
  if (stripped) return `编号 ${stripped}`;
  return `编号 ${id.replace(/[A-Za-z]/g, "")}` || "编号";
}

export function formatWindowLabel(locale: Locale, windowId: string): string {
  if (locale !== "zh") return windowId.replace("_", " → ");
  const parts = windowId.split("_");
  const fmt = (d: string) => {
    const m = d.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!m) return d.replace(/[A-Za-z_]/g, "");
    return `${m[1]}年${Number(m[2])}月${Number(m[3])}日`;
  };
  if (parts.length === 2) return `${fmt(parts[0])} 至 ${fmt(parts[1])}`;
  return windowId.replace(/[A-Za-z_]/g, " ");
}

export function formatMonthLabel(locale: Locale, month: string): string {
  if (locale !== "zh") return month;
  const m = month.match(/^(\d{4})-(\d{2})$/);
  if (!m) return month.replace(/[A-Za-z]/g, "");
  return `${m[1]}年${Number(m[2])}月`;
}

function dateFromObjectId(id: string): string | null {
  const m = id.match(/(\d{4})(\d{2})(\d{2})/);
  if (!m) return null;
  return `${m[1]}年${Number(m[2])}月${Number(m[3])}日`;
}

/** 自动策展 / 工作日志：仅展示结构化中文摘要 */
export function buildConclusionSummary(obj: Record<string, unknown>): string {
  const parts: string[] = [];
  const id = String(obj.id ?? "");

  if (obj.authority_level) {
    parts.push(tAuthority("zh", String(obj.authority_level)));
  }

  const dateLabel = dateFromObjectId(id);
  if (dateLabel) parts.push(dateLabel);

  if (obj.execution_mode && String(obj.execution_mode) !== "UNKNOWN") {
    parts.push(`执行：${tLabel("zh", String(obj.execution_mode))}`);
  }
  if (obj.live_proven !== undefined) {
    parts.push(tLiveProven("zh", obj.live_proven as boolean));
  }
  if (obj.project_scope && String(obj.project_scope) !== "unknown") {
    parts.push(`项目：${tLabel("zh", String(obj.project_scope))}`);
  }

  if (String(obj.authority_level) === "AUTO_CURATED") {
    parts.push("自动策展工作日志（非正式结论，不含可采信陈述）");
  } else {
    parts.push("原文为 Agent 工作记录，非用户可读结论");
  }

  return parts.join(" · ");
}

export function buildObjectSummary(
  obj: Record<string, unknown>,
  type: ObjectType
): string {
  if (type === "conclusion") return buildConclusionSummary(obj);

  const parts: string[] = [tObjectType("zh", type)];
  if (obj.authority_level) parts.push(tAuthority("zh", String(obj.authority_level)));
  if (obj.failure_type) parts.push(tLabel("zh", String(obj.failure_type)));
  if (obj.name && hasSubstantialChinese(String(obj.name))) {
    parts.push(String(obj.name).slice(0, 80));
  }
  return parts.join(" · ");
}

/** 列表/卡片/详情标题 */
export function getDisplayStatement(
  locale: Locale,
  obj: Record<string, unknown>,
  type: ObjectType,
  maxLen = 480
): string {
  const raw = stripMarkup(getObjectTitle(obj as Parameters<typeof getObjectTitle>[0]));

  if (locale !== "zh") {
    return raw.length <= maxLen ? raw : raw.slice(0, maxLen) + "…";
  }

  const authority = String(obj.authority_level ?? "");

  // 自动策展 / Agent 日志：绝不展示翻译后的垃圾正文
  if (authority === "AUTO_CURATED" || isAgentWorkLog(raw)) {
    return buildObjectSummary(obj, type);
  }

  // 已验证条目：本身已是中文结论，直接展示
  if (hasSubstantialChinese(raw)) {
    return raw.length <= maxLen ? raw : raw.slice(0, maxLen) + "…";
  }

  // 短英文已验证结论：保留原文摘要位，由 localize 层按需翻译
  return raw.length <= maxLen ? raw : raw.slice(0, maxLen) + "…";
}

export function getDetailBody(
  locale: Locale,
  obj: Record<string, unknown>,
  type: ObjectType
): { body: string | null; isSummary: boolean } {
  const raw = stripMarkup(getObjectTitle(obj as Parameters<typeof getObjectTitle>[0]));

  if (locale !== "zh") {
    return { body: raw, isSummary: false };
  }

  if (
    String(obj.authority_level) === "AUTO_CURATED" ||
    isAgentWorkLog(raw)
  ) {
    return { body: null, isSummary: true };
  }

  if (hasSubstantialChinese(raw)) {
    return { body: raw, isSummary: false };
  }

  return { body: raw, isSummary: false };
}

export function getOpenQuestionDisplay(
  locale: Locale,
  q: { title: string; body: string }
): { title: string; body: string } {
  if (locale !== "zh") return q;
  if (hasSubstantialChinese(q.title) && hasSubstantialChinese(q.body)) return q;
  return q;
}

export function getSearchDisplayTitle(
  locale: Locale,
  type: ObjectType,
  title: string,
  authority?: string
): string {
  if (locale !== "zh") return title;
  if (authority === "AUTO_CURATED" || isAgentWorkLog(title)) {
    return buildConclusionSummary({ authority_level: authority, id: "" });
  }
  return title;
}

export function sortConclusionsForZh<T extends Record<string, unknown>>(
  items: T[]
): T[] {
  const rank = (a: string) =>
    a === "VALIDATED" || a === "GOLD" ? 0 : a === "AUTO_CURATED" ? 2 : 1;
  return [...items].sort((a, b) => {
    const ra = rank(String(a.authority_level ?? ""));
    const rb = rank(String(b.authority_level ?? ""));
    if (ra !== rb) return ra - rb;
    return String(b.id ?? "").localeCompare(String(a.id ?? ""));
  });
}
