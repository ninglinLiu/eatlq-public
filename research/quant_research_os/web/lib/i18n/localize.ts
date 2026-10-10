import "server-only";
import type { Locale } from "./index";
import type { CanonicalObject, ObjectType } from "@/lib/data/types";
import {
  hasSubstantialChinese,
  isAgentWorkLog,
} from "./zh-display";
import { needsTranslation, translateMany, translateToZh } from "./translate";
import { tLabel } from "./labels";

/** 仅翻译叙事性字段；枚举/元数据字段走 tLabel，禁止机器翻译 */
const NARRATIVE_FIELDS = new Set([
  "statement",
  "name",
  "what_happened",
  "why_it_happened",
  "impact",
  "how_detected",
  "future_guardrail",
  "operational_definition",
  "limitations",
  "can_prove",
  "cannot_prove",
  "description",
  "question",
  "entry",
  "exit",
  "result",
  "design",
  "when_to_use",
  "features",
  "title",
  "body",
]);

/** 永不机器翻译的字段（枚举、标识、指针） */
const SKIP_FIELDS = new Set([
  "id",
  "hypothesis_id",
  "experiment_id",
  "guardrail_id",
  "failure_id",
  "authority_level",
  "status",
  "execution_mode",
  "execution_status",
  "live_proven",
  "execution_confirmed",
  "authorship",
  "epistemic_role",
  "persistence_scope",
  "reuse_policy",
  "failure_type",
  "evidence_level",
  "regime_dependency",
  "project_scope",
  "origin",
  "week_scope",
  "valid_from",
  "valid_until",
  "superseded_by",
  "source_pointers",
  "date_context",
]);

function isEnumLike(value: string): boolean {
  if (/^[A-Z][A-Z0-9_]*$/.test(value)) return true;
  if (tLabel("zh", value) !== value) return true;
  if (["high", "medium", "low", "unknown", "true", "false"].includes(value)) return true;
  return false;
}

function shouldTranslateField(
  key: string,
  value: string,
  obj: Record<string, unknown>
): boolean {
  if (SKIP_FIELDS.has(key)) return false;
  if (!NARRATIVE_FIELDS.has(key)) return false;
  if (!needsTranslation(value)) return false;
  if (isEnumLike(value)) return false;

  if (key === "statement") {
    const authority = String(obj.authority_level ?? "");
    if (authority === "AUTO_CURATED") return false;
    if (isAgentWorkLog(value)) return false;
    // 已验证且主体为中文：保留原文
    if (hasSubstantialChinese(value)) return false;
    // 过长日志型文本不翻译
    if (value.length > 400) return false;
  }

  return true;
}

function collectTranslatableStrings(
  obj: Record<string, unknown>,
  out: Set<string>
): void {
  for (const [key, value] of Object.entries(obj)) {
    if (typeof value === "string") {
      if (shouldTranslateField(key, value, obj)) out.add(value);
    } else if (Array.isArray(value)) {
      for (const item of value) {
        if (typeof item === "string" && needsTranslation(item) && !isEnumLike(item)) {
          out.add(item);
        }
      }
    }
  }
}

function applyTranslations(value: unknown, map: Map<string, string>): unknown {
  if (typeof value === "string") return map.get(value) ?? value;
  if (Array.isArray(value)) return value.map((v) => applyTranslations(v, map));
  return value;
}

function applyToObject<T extends Record<string, unknown>>(
  obj: T,
  map: Map<string, string>
): T {
  const next = { ...obj } as T;
  for (const key of Object.keys(next)) {
    if (SKIP_FIELDS.has(key)) continue;
    if (typeof next[key] === "string") {
      const val = next[key] as string;
      if (shouldTranslateField(key, val, obj)) {
        (next as Record<string, unknown>)[key] = map.get(val) ?? val;
      }
    } else if (Array.isArray(next[key])) {
      (next as Record<string, unknown>)[key] = applyTranslations(next[key], map);
    }
  }
  return next;
}

async function localizeDeep<T extends Record<string, unknown>>(obj: T): Promise<T> {
  const strings = new Set<string>();
  collectTranslatableStrings(obj, strings);
  if (strings.size === 0) return obj;
  const map = await translateMany([...strings]);
  return applyToObject(obj, map);
}

export async function localizeObject<T extends Record<string, unknown>>(
  locale: Locale,
  obj: T
): Promise<T> {
  if (locale !== "zh") return obj;
  return localizeDeep(obj);
}

export async function localizeObjects<T extends Record<string, unknown>>(
  locale: Locale,
  objects: T[],
  type?: ObjectType
): Promise<T[]> {
  if (locale !== "zh") return objects;

  const strings = new Set<string>();
  for (const obj of objects) collectTranslatableStrings(obj, strings);
  if (strings.size === 0) return objects;

  const map = await translateMany([...strings]);
  return objects.map((obj) => applyToObject(obj, map));
}

export async function localizeMarkdown(
  locale: Locale,
  content: string
): Promise<string> {
  if (locale !== "zh" || !content.trim()) return content;
  return translateToZh(content);
}

export async function localizeOpenQuestions<T extends { title: string; body: string }>(
  locale: Locale,
  questions: T[]
): Promise<T[]> {
  if (locale !== "zh") return questions;
  return localizeObjects(locale, questions);
}

export async function localizeCanonicalObject(
  locale: Locale,
  obj: CanonicalObject
): Promise<CanonicalObject> {
  if (locale !== "zh") return obj;
  return localizeDeep(obj as Record<string, unknown>) as Promise<CanonicalObject>;
}
