#!/usr/bin/env node
/** 预热中文翻译缓存 */
import { createHash } from "crypto";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
  readdirSync,
  statSync,
} from "fs";
import { join } from "path";
import translate from "google-translate-api-x";

const ROOT = join(process.cwd(), "..");
const CACHE_DIR = join(process.cwd(), ".translation-cache", "zh");

function needsTranslation(text) {
  if (!text?.trim()) return false;
  const cjk = (text.match(/[\u4e00-\u9fff]/g) ?? []).length;
  const latin = (text.match(/[a-zA-Z]/g) ?? []).length;
  if (latin === 0) return false;
  if (cjk >= 20 && latin <= 4) return false;
  return latin >= 3;
}

function cacheKey(text) {
  return createHash("sha256").update(text).digest("hex");
}

function getCached(text) {
  const file = join(CACHE_DIR, `${cacheKey(text)}.json`);
  if (!existsSync(file)) return null;
  try {
    return JSON.parse(readFileSync(file, "utf8")).translation;
  } catch {
    return null;
  }
}

function writeCache(source, translation) {
  if (!existsSync(CACHE_DIR)) mkdirSync(CACHE_DIR, { recursive: true });
  writeFileSync(
    join(CACHE_DIR, `${cacheKey(source)}.json`),
    JSON.stringify({ source, translation, ts: Date.now() })
  );
}

async function translateToZh(text) {
  const trimmed = text.trim();
  if (!needsTranslation(trimmed)) return trimmed;
  const cached = getCached(trimmed);
  if (cached) return cached;
  const result = await translate(trimmed, { from: "en", to: "zh-CN" });
  writeCache(trimmed, result.text.trim());
  return result.text.trim();
}

function collectFromObject(obj, out) {
  if (typeof obj === "string") {
    if (needsTranslation(obj)) out.add(obj);
    return;
  }
  if (Array.isArray(obj)) obj.forEach((v) => collectFromObject(v, out));
  else if (obj && typeof obj === "object")
    Object.values(obj).forEach((v) => collectFromObject(v, out));
}

function readJsonl(filePath, out) {
  for (const line of readFileSync(filePath, "utf8").split("\n")) {
    if (!line.trim()) continue;
    try {
      collectFromObject(JSON.parse(line), out);
    } catch {
      /* skip */
    }
  }
}

function scanDir(dir, out, ext) {
  if (!existsSync(dir)) return;
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) scanDir(p, out, ext);
    else if (name.endsWith(ext)) {
      if (ext === ".jsonl") readJsonl(p, out);
      else {
        const t = readFileSync(p, "utf8");
        if (needsTranslation(t)) out.add(t);
      }
    }
  }
}

async function main() {
  const strings = new Set();
  scanDir(join(ROOT, "global"), strings, ".jsonl");
  scanDir(join(ROOT, "global"), strings, ".md");
  scanDir(join(ROOT, "weekly"), strings, ".md");

  const list = [...strings];
  console.log(`Translating ${list.length} unique strings...`);

  let done = 0;
  let skipped = 0;
  for (const text of list) {
    if (getCached(text.trim())) {
      skipped++;
      continue;
    }
    try {
      await translateToZh(text);
      done++;
      if (done % 5 === 0) console.log(`  ${done} new, ${skipped} cached...`);
      await new Promise((r) => setTimeout(r, 300));
    } catch (e) {
      console.error(`FAIL ${text.slice(0, 50)}: ${e.message}`);
    }
  }
  console.log(`Finished. New translations: ${done}, already cached: ${skipped}`);
}

main();
