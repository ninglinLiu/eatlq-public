import fs from "fs";
import path from "path";
import {
  GLOBAL_JSONL,
  PATHS,
  isAllowedPath,
  OS_ROOT,
} from "./paths";
import { getObjectId, getObjectTitle, truncate } from "@/lib/utils/object";
import type {
  AuthorityCounts,
  CanonicalObject,
  DataMeta,
  KnowledgeCounts,
  OpenQuestionItem,
  PipelineState,
  SearchResult,
  WeeklyStats,
  WindowRecord,
  ObjectType,
} from "./types";

type Cache<T> = { data: T; loadedAt: number };

const CACHE_TTL_MS = 60_000;
const cache = new Map<string, Cache<unknown>>();
const warnings: string[] = [];

function getCached<T>(key: string, loader: () => T): T {
  const existing = cache.get(key) as Cache<T> | undefined;
  if (existing && Date.now() - existing.loadedAt < CACHE_TTL_MS) {
    return existing.data;
  }
  const data = loader();
  cache.set(key, { data, loadedAt: Date.now() });
  return data;
}

export function getDataMeta(): DataMeta {
  return {
    loadedAt: new Date().toISOString(),
    warnings: [...warnings],
  };
}

export function readJsonl<T extends Record<string, unknown>>(
  filePath: string
): T[] {
  if (!isAllowedPath(filePath)) {
    warnings.push(`Blocked read: ${filePath}`);
    return [];
  }
  if (!fs.existsSync(filePath)) return [];

  const results: T[] = [];
  const content = fs.readFileSync(filePath, "utf-8");
  const lines = content.split("\n");

  lines.forEach((line, index) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    try {
      results.push(JSON.parse(trimmed) as T);
    } catch {
      warnings.push(
        `Malformed JSONL line ${index + 1} in ${path.basename(filePath)}`
      );
    }
  });

  return results;
}

export function readMarkdown(filePath: string): string {
  if (!isAllowedPath(filePath)) {
    warnings.push(`Blocked markdown read: ${filePath}`);
    return "";
  }
  if (!fs.existsSync(filePath)) return "";
  return fs.readFileSync(filePath, "utf-8");
}

export function readJson<T>(filePath: string): T | null {
  if (!isAllowedPath(filePath)) return null;
  if (!fs.existsSync(filePath)) return null;
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf-8")) as T;
  } catch {
    warnings.push(`Malformed JSON: ${filePath}`);
    return null;
  }
}

function globalPath(filename: string): string {
  return path.join(PATHS.global, filename);
}

export function loadGlobalCollection(
  filename: string
): CanonicalObject[] {
  return getCached(`global:${filename}`, () =>
    readJsonl<CanonicalObject>(globalPath(filename))
  );
}

export function loadPipelineState(): PipelineState {
  return (
    getCached("pipeline_state", () =>
      readJson<PipelineState>(path.join(PATHS.state, "pipeline_state.json"))
    ) ?? {}
  );
}

export function loadWindows(): WindowRecord[] {
  return getCached("windows", () =>
    readJsonl<WindowRecord>(path.join(PATHS.state, "windows.jsonl"))
  );
}

export function loadKnowledgeCounts(): KnowledgeCounts {
  return getCached("counts", () => ({
    user_theses: loadGlobalCollection(GLOBAL_JSONL.user_theses).length,
    research_principles: loadGlobalCollection(
      GLOBAL_JSONL.research_principles
    ).length,
    hypotheses: loadGlobalCollection(GLOBAL_JSONL.hypotheses).length,
    experiments: loadGlobalCollection(GLOBAL_JSONL.experiments).length,
    conclusions: loadGlobalCollection(GLOBAL_JSONL.conclusions).length,
    failures: loadGlobalCollection(GLOBAL_JSONL.failures).length,
    methods: loadGlobalCollection(GLOBAL_JSONL.methods).length,
    decisions: loadGlobalCollection(GLOBAL_JSONL.decisions).length,
    guardrails: loadGlobalCollection(GLOBAL_JSONL.permanent_guardrails)
      .length,
    open_questions: loadOpenQuestions().length,
  }));
}

export function loadAuthorityCounts(): AuthorityCounts {
  return getCached("authority_counts", () => {
    const counts: AuthorityCounts = {};
    const files = Object.values(GLOBAL_JSONL).filter(
      (f) => f !== GLOBAL_JSONL.knowledge_graph
    );
    for (const file of files) {
      for (const obj of loadGlobalCollection(file)) {
        const level = String(obj.authority_level ?? "UNKNOWN");
        counts[level] = (counts[level] ?? 0) + 1;
      }
    }
    return counts;
  });
}

export function loadWeeklyStats(dirName: string): WeeklyStats | null {
  const statsPath = path.join(PATHS.weekly, dirName, "_build_stats.json");
  return readJson<WeeklyStats>(statsPath);
}

export function loadWeeklyMarkdown(
  dirName: string,
  filename: string
): string {
  const filePath = path.join(PATHS.weekly, dirName, filename);
  return readMarkdown(filePath);
}

export function listMonthlyDirs(): string[] {
  return getCached("monthly_dirs", () => {
    if (!fs.existsSync(PATHS.monthly)) return [];
    return fs
      .readdirSync(PATHS.monthly, { withFileTypes: true })
      .filter((d) => d.isDirectory())
      .map((d) => d.name)
      .sort();
  });
}

export function loadMonthlySummary(month: string): string {
  return readMarkdown(path.join(PATHS.monthly, month, "monthly_summary.md"));
}

function parseOpenQuestionsMd(
  content: string,
  source: string,
  week?: string
): OpenQuestionItem[] {
  const items: OpenQuestionItem[] = [];
  const lines = content.split("\n");
  let current: OpenQuestionItem | null = null;

  for (const line of lines) {
    const match = line.match(/^\d+\.\s+\*\*(.+?)\*\*\s*[—–-]\s*(.+)$/);
    if (match) {
      if (current) items.push(current);
      current = {
        id: `${week ?? source}-${items.length + 1}`,
        title: match[1].trim(),
        body: match[2].trim(),
        status: inferQuestionStatus(match[2]),
        source,
        week,
      };
      continue;
    }
    if (current && line.trim() && !line.startsWith("#")) {
      current.body += " " + line.trim();
    }
  }
  if (current) items.push(current);
  return items;
}

function inferQuestionStatus(body: string): string {
  const lower = body.toLowerCase();
  if (
    lower.includes("resolved") ||
    lower.includes("已解决") ||
    lower.includes("window answer: no confirmed") === false &&
      lower.includes("confirmed")
  ) {
    // keep OPEN default unless clearly resolved
  }
  if (lower.includes("partially") || lower.includes("partial")) {
    return "PARTIALLY_RESOLVED";
  }
  if (lower.includes("resolved") || lower.includes("已解决")) {
    return "RESOLVED";
  }
  if (lower.includes("unresolved") || lower.includes("no confirmed")) {
    return "OPEN";
  }
  return "OPEN";
}

export function loadOpenQuestions(): OpenQuestionItem[] {
  return getCached("open_questions", () => {
    const fromJsonl = loadGlobalCollection(GLOBAL_JSONL.open_questions);
    if (fromJsonl.length > 0) {
      return fromJsonl.map((obj, i) => ({
        id: String(obj.id ?? `OQ-${i + 1}`),
        title: String(
          obj.title ?? obj.statement ?? obj.question ?? "Open question"
        ),
        body: String(obj.body ?? obj.statement ?? ""),
        status: String(obj.status ?? "OPEN"),
        source: "global",
      }));
    }

    const items: OpenQuestionItem[] = [];
    const windows = loadWindows().filter(
      (w) =>
        w.status === "VALIDATED" ||
        w.status === "GOLD" ||
        w.status === "AUTO_CURATED"
    );

    for (const w of windows) {
      const md = loadWeeklyMarkdown(w.dir_name, "open_questions.md");
      if (md) {
        items.push(
          ...parseOpenQuestionsMd(md, w.dir_name, w.dir_name.replace(/_/g, " → "))
        );
      }
    }

    const seen = new Set<string>();
    return items.filter((item) => {
      const key = item.title.toLowerCase();
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  });
}

export { getObjectId, getObjectTitle, truncate };

export function findObjectById(
  type: ObjectType,
  id: string
): CanonicalObject | null {
  const fileMap: Record<ObjectType, string> = {
    user_thesis: GLOBAL_JSONL.user_theses,
    research_principle: GLOBAL_JSONL.research_principles,
    hypothesis: GLOBAL_JSONL.hypotheses,
    experiment: GLOBAL_JSONL.experiments,
    conclusion: GLOBAL_JSONL.conclusions,
    failure: GLOBAL_JSONL.failures,
    method: GLOBAL_JSONL.methods,
    decision: GLOBAL_JSONL.decisions,
    guardrail: GLOBAL_JSONL.permanent_guardrails,
  };

  const items = loadGlobalCollection(fileMap[type]);
  return (
    items.find((obj) => {
      const objId = getObjectId(obj);
      return objId === id;
    }) ?? null
  );
}

export function getAllSearchableObjects(): Array<{
  type: ObjectType;
  obj: CanonicalObject;
}> {
  const types: ObjectType[] = [
    "user_thesis",
    "research_principle",
    "hypothesis",
    "experiment",
    "conclusion",
    "failure",
    "method",
    "decision",
    "guardrail",
  ];

  const results: Array<{ type: ObjectType; obj: CanonicalObject }> = [];
  for (const type of types) {
    const fileMap: Record<ObjectType, string> = {
      user_thesis: GLOBAL_JSONL.user_theses,
      research_principle: GLOBAL_JSONL.research_principles,
      hypothesis: GLOBAL_JSONL.hypotheses,
      experiment: GLOBAL_JSONL.experiments,
      conclusion: GLOBAL_JSONL.conclusions,
      failure: GLOBAL_JSONL.failures,
      method: GLOBAL_JSONL.methods,
      decision: GLOBAL_JSONL.decisions,
      guardrail: GLOBAL_JSONL.permanent_guardrails,
    };
    for (const obj of loadGlobalCollection(fileMap[type])) {
      results.push({ type, obj });
    }
  }
  return results;
}

export function searchObjects(query: string): SearchResult[] {
  const q = query.toLowerCase().trim();
  if (!q) return [];

  const results: SearchResult[] = [];
  for (const { type, obj } of getAllSearchableObjects()) {
    const title = getObjectTitle(obj);
    const id = getObjectId(obj);
    const haystack = [
      title,
      id,
      String(obj.status ?? ""),
      String(obj.project_scope ?? ""),
      JSON.stringify(obj.source_pointers ?? []),
    ]
      .join(" ")
      .toLowerCase();

    if (haystack.includes(q)) {
      results.push({
        type,
        id,
        title: truncate(title, 120),
        snippet: truncate(title, 200),
        authority_level: obj.authority_level as string | undefined,
      });
    }
  }

  for (const oq of loadOpenQuestions()) {
    const haystack = `${oq.title} ${oq.body}`.toLowerCase();
    if (haystack.includes(q)) {
      results.push({
        type: "conclusion",
        id: oq.id,
        title: oq.title,
        snippet: truncate(oq.body, 200),
      });
    }
  }

  return results.slice(0, 50);
}

export function getTotalRawSelectedCanonical(): {
  raw: number;
  selected: number;
  canonical: number;
} {
  let raw = 0;
  let selected = 0;
  let canonical = 0;
  for (const w of loadWindows()) {
    const stats = loadWeeklyStats(w.dir_name);
    if (stats) {
      raw += stats.raw_events ?? 0;
      selected += stats.selected ?? 0;
      canonical +=
        (stats.user_theses ?? 0) +
        (stats.research_principles ?? 0) +
        (stats.hypotheses ?? 0) +
        (stats.experiments ?? 0) +
        (stats.conclusions ?? 0) +
        (stats.failures ?? 0) +
        (stats.methods ?? 0) +
        (stats.decisions ?? 0);
    }
  }
  return { raw, selected, canonical };
}

export function getMonthlyGrowth(): Array<{ month: string; count: number }> {
  const months = listMonthlyDirs();
  return months.map((month) => {
    const dir = path.join(PATHS.monthly, month);
    let count = 0;
    if (fs.existsSync(dir)) {
      for (const f of fs.readdirSync(dir)) {
        if (f.endsWith(".jsonl")) {
          count += readJsonl(path.join(dir, f)).length;
        }
      }
    }
    return { month, count };
  });
}

export { OS_ROOT, PATHS };
