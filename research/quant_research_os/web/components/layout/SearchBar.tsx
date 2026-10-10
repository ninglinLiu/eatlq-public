"use client";

import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import { getDictionary, localePath, type Locale } from "@/lib/i18n";

export function SearchBar({ locale }: { locale: Locale }) {
  const t = getDictionary(locale);
  const router = useRouter();
  const [query, setQuery] = useState("");

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    const q = query.trim();
    if (q) {
      router.push(`${localePath(locale, "/search")}?q=${encodeURIComponent(q)}`);
    }
  }

  return (
    <form onSubmit={onSubmit} className="flex w-full max-w-md gap-2">
      <input
        type="search"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={t.site.searchPlaceholder}
        className="w-full rounded-lg border border-border-warm bg-white px-4 py-2 text-sm text-ink placeholder:text-ink-faint focus:border-accent-muted focus:outline-none focus:ring-1 focus:ring-accent-muted"
      />
      <button
        type="submit"
        className="shrink-0 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent/90"
      >
        {t.site.search}
      </button>
    </form>
  );
}

export function QuickFilters({
  locale,
  filters,
}: {
  locale: Locale;
  filters: Array<{ href: string; label: string }>;
}) {
  return (
    <div className="flex flex-wrap gap-2">
      {filters.map((f) => (
        <a
          key={f.href}
          href={localePath(locale, f.href)}
          className="rounded-full border border-border-warm bg-white px-3 py-1 text-xs text-ink-muted hover:border-accent-muted hover:text-accent"
        >
          {f.label}
        </a>
      ))}
    </div>
  );
}
