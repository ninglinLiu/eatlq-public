import type { CanonicalObject } from "./types";

export type ListFilters = {
  authority?: string;
  status?: string;
  execution_mode?: string;
  live_proven?: string;
  project_scope?: string;
  regime_dependency?: string;
  persistence_scope?: string;
  always_load?: string;
  q?: string;
};

function matchesMulti(value: string | undefined, filter?: string): boolean {
  if (!filter) return true;
  const allowed = filter.split(",").map((s) => s.trim().toUpperCase());
  return allowed.includes((value ?? "UNKNOWN").toUpperCase());
}

export function filterObjects(
  items: CanonicalObject[],
  filters: ListFilters,
  searchFields: (obj: CanonicalObject) => string[]
): CanonicalObject[] {
  let result = items;

  if (filters.authority) {
    result = result.filter((o) =>
      matchesMulti(o.authority_level as string, filters.authority)
    );
  }
  if (filters.status) {
    result = result.filter((o) =>
      matchesMulti(o.status as string, filters.status)
    );
  }
  if (filters.execution_mode) {
    result = result.filter((o) =>
      matchesMulti(o.execution_mode as string, filters.execution_mode)
    );
  }
  if (filters.live_proven === "true") {
    result = result.filter((o) => o.live_proven === true);
  }
  if (filters.live_proven === "false") {
    result = result.filter((o) => o.live_proven === false);
  }
  if (filters.project_scope) {
    result = result.filter(
      (o) =>
        String(o.project_scope ?? "")
          .toLowerCase()
          .includes(filters.project_scope!.toLowerCase())
    );
  }
  if (filters.regime_dependency) {
    result = result.filter((o) =>
      matchesMulti(o.regime_dependency as string, filters.regime_dependency)
    );
  }
  if (filters.persistence_scope) {
    result = result.filter((o) =>
      matchesMulti(o.persistence_scope as string, filters.persistence_scope)
    );
  }
  if (filters.always_load === "1") {
    result = result.filter((o) => o.reuse_policy === "ALWAYS_LOAD");
  }
  if (filters.q) {
    const q = filters.q.toLowerCase();
    result = result.filter((o) =>
      searchFields(o).some((f) => f.toLowerCase().includes(q))
    );
  }

  return result;
}

export function uniqueFieldValues(
  items: CanonicalObject[],
  field: keyof CanonicalObject
): string[] {
  const set = new Set<string>();
  for (const item of items) {
    const v = item[field];
    if (v && String(v) !== "unknown") set.add(String(v));
  }
  return Array.from(set).sort();
}
