import type { CanonicalObject } from "@/lib/data/types";

export function getObjectTitle(obj: CanonicalObject): string {
  return String(
    obj.statement ??
      obj.name ??
      obj.decision ??
      obj.question ??
      obj.description ??
      obj.id ??
      "Untitled"
  );
}

export function getObjectId(obj: CanonicalObject): string {
  return String(
    obj.id ??
      obj.hypothesis_id ??
      obj.experiment_id ??
      obj.guardrail_id ??
      "unknown"
  );
}

export function truncate(text: string, max = 280): string {
  const cleaned = text.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
  if (cleaned.length <= max) return cleaned;
  return cleaned.slice(0, max) + "…";
}
