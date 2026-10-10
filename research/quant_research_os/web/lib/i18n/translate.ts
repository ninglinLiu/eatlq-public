import { createHash } from "crypto";
import "server-only";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "fs";
import { join } from "path";
import translate from "google-translate-api-x";

const CACHE_DIR = join(process.cwd(), ".translation-cache", "zh");
const MAX_CHUNK = 4500;
const CACHE_VERSION = "v4";

const GLOSSARY: Array<[RegExp, string]> = [
  [/\bbacktest(s)?\b/gi, "回测"],
  [/\blive\b/gi, "实盘"],
  [/\bholdout\b/gi, "留样"],
  [/\bpaper\b/gi, "模拟"],
  [/\bJSONL\b/g, "源记录"],
  [/\bjsonl\b/g, "源记录"],
  [/\bATH\b/g, "历史高点"],
  [/\bMFE\b/g, "最大有利偏移"],
  [/\bEV\b/g, "期望值"],
  [/\bOOS\b/g, "样本外"],
  [/\bMEV\b/g, "矿工可提取价值"],
  [/\baudit\b/gi, "审计"],
  [/\boperator\b/gi, "操作者"],
  [/\bdeployer\b/gi, "部署者"],
  [/\bhoneypot\b/gi, "蜜罐"],
  [/\bQuoter\b/g, "报价器"],
  [/\bKEYSTONE\b/g, "基石策略"],
  [/\bABANDON\b/g, "放弃策略"],
  [/\bPARADAENGI\b/g, "帕拉丹吉"],
  [/\bEATLQ\b/g, "链上量化研究"],
  [/\bETH\b/g, "以太"],
  [/\brealized_ev\b/gi, "已实现期望值"],
  [/\brealized ev\b/gi, "已实现期望值"],
  [/\bcase-control\b/gi, "病例对照"],
  [/\bath_mc\b/gi, "历史高点市值"],
  [/\bruntime_mismatch\b/gi, "运行时失配"],
];

export function needsTranslation(text: string): boolean {
  if (!text || !text.trim()) return false;
  return (text.match(/[a-zA-Z]/g) ?? []).length >= 3;
}

function cacheKey(text: string): string {
  return createHash("sha256").update(`${CACHE_VERSION}:${text}`).digest("hex");
}

function ensureCacheDir(): void {
  if (!existsSync(CACHE_DIR)) mkdirSync(CACHE_DIR, { recursive: true });
}

export function getCachedTranslation(text: string): string | null {
  if (!needsTranslation(text)) return text;
  ensureCacheDir();
  const file = join(CACHE_DIR, `${cacheKey(text)}.json`);
  if (!existsSync(file)) return null;
  try {
    return (JSON.parse(readFileSync(file, "utf8")) as { translation: string }).translation;
  } catch {
    return null;
  }
}

function writeCache(source: string, translation: string): void {
  ensureCacheDir();
  writeFileSync(
    join(CACHE_DIR, `${cacheKey(source)}.json`),
    JSON.stringify({ source, translation, ts: Date.now() }),
    "utf8"
  );
}

function applyGlossary(text: string): string {
  let out = text;
  for (const [re, rep] of GLOSSARY) out = out.replace(re, rep);
  return out;
}

function finalizeChinese(text: string): string {
  return applyGlossary(text);
}

async function translateChunk(text: string): Promise<string> {
  const cached = getCachedTranslation(text);
  if (cached !== null) return cached;

  const result = await translate(text, { from: "auto", to: "zh-CN" });
  const translated = finalizeChinese(result.text.trim());
  writeCache(text, translated);
  return translated;
}

export async function translateToZh(text: string): Promise<string> {
  const trimmed = text.trim();
  if (!trimmed || !needsTranslation(trimmed)) return text;

  const cached = getCachedTranslation(trimmed);
  if (cached !== null) return cached;

  if (trimmed.length <= MAX_CHUNK) return translateChunk(trimmed);

  const parts: string[] = [];
  let offset = 0;
  while (offset < trimmed.length) {
    let end = Math.min(offset + MAX_CHUNK, trimmed.length);
    if (end < trimmed.length) {
      const slice = trimmed.slice(offset, end);
      const lastBreak = Math.max(
        slice.lastIndexOf("\n\n"),
        slice.lastIndexOf(". "),
        slice.lastIndexOf("。")
      );
      if (lastBreak > MAX_CHUNK * 0.5) end = offset + lastBreak + 1;
    }
    parts.push(await translateChunk(trimmed.slice(offset, end)));
    offset = end;
  }
  return parts.join("");
}

export async function translateMany(texts: string[]): Promise<Map<string, string>> {
  const unique = [...new Set(texts.filter((t) => t && needsTranslation(t)))];
  const result = new Map<string, string>();
  let i = 0;
  const concurrency = 4;

  async function worker(): Promise<void> {
    while (i < unique.length) {
      const idx = i++;
      result.set(unique[idx], await translateToZh(unique[idx]));
    }
  }

  await Promise.all(
    Array.from({ length: Math.min(concurrency, unique.length || 1) }, worker)
  );
  return result;
}

export async function localizeText(text: string): Promise<string> {
  if (!text || !needsTranslation(text)) return text;
  return translateToZh(text);
}
