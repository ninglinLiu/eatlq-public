import type { SourcePointer } from "@/lib/data/types";

function extractTranscriptId(ptr: SourcePointer): string | null {
  if (ptr.transcript_id) return ptr.transcript_id.slice(0, 8);
  const file = ptr.source_file ?? ptr.file ?? "";
  const match = file.match(/([a-f0-9-]{8,})/);
  return match ? match[1].slice(0, 8) : null;
}

function extractDate(ptr: SourcePointer): string | null {
  const file = ptr.source_file ?? ptr.file ?? "";
  const dateMatch = file.match(/(\d{4}-\d{2}-\d{2})/);
  return dateMatch ? dateMatch[1] : null;
}

export function formatSourcePointer(ptr: SourcePointer): string {
  const parts: string[] = [];
  const date = extractDate(ptr);
  if (date) parts.push(date);
  const tid = extractTranscriptId(ptr);
  if (tid) parts.push(`Transcript ${tid}`);
  if (ptr.jsonl_index !== undefined) {
    parts.push(`JSONL index ${ptr.jsonl_index}`);
  }
  if (ptr.message_id) parts.push(ptr.message_id);
  if (parts.length === 0) {
    const file = ptr.source_file ?? ptr.file;
    if (file) {
      const base = file.split("/").pop();
      return base ?? file;
    }
    return "Unknown source";
  }
  return parts.join(" · ");
}

export function formatSourcePointers(
  pointers?: SourcePointer[]
): string[] {
  if (!pointers?.length) return [];
  return pointers.map(formatSourcePointer);
}

export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) {
    if (value.length === 0) return "—";
    return value.map((v) => String(v)).join(", ");
  }
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

export function formatList(value: unknown): string[] {
  if (!value) return [];
  if (Array.isArray(value)) return value.map(String);
  if (typeof value === "string") return [value];
  return [String(value)];
}
