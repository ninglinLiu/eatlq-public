import Link from "next/link";
import { SectionHeader, InfoBox } from "@/components/ui/Cards";
import { AuthorityBadge } from "@/components/ui/Badges";
import { searchObjects } from "@/lib/data/loader";
import { getDictionary, getPageIntro, localePath, type Locale } from "@/lib/i18n";
import { tObjectType, tSearchResults } from "@/lib/i18n/labels";
import { formatObjectId, getSearchDisplayTitle } from "@/lib/i18n/zh-display";
import type { ObjectType } from "@/lib/data/types";

import { translateMany } from "@/lib/i18n/translate";

export default async function SearchPage({
  params,
  searchParams,
}: {
  params: { locale: Locale };
  searchParams: { q?: string };
}) {
  const t = getDictionary(params.locale);
  const q = searchParams.q ?? "";
  const rawResults = q ? searchObjects(q) : [];
  const intro = getPageIntro(params.locale, "search");

  let results = rawResults;
  if (params.locale === "zh" && rawResults.length > 0) {
    const map = await translateMany(rawResults.map((r) => r.title));
    results = rawResults.map((r) => ({
      ...r,
      title: map.get(r.title) ?? r.title,
    }));
  }

  return (
    <div>
      <SectionHeader
        title={t.site.search}
        description={q ? tSearchResults(params.locale, q, results.length) : intro}
      />

      {!q && intro && <InfoBox>{intro}</InfoBox>}

      {q && results.length === 0 && (
        <p className="text-ink-muted">{t.site.noResults}</p>
      )}

      <div className="space-y-3">
        {results.map((r) => (
          <Link
            key={`${r.type}-${r.id}`}
            href={localePath(params.locale, `/object/${r.type}/${r.id}`)}
            className="block rounded-xl border border-border-warm bg-white p-4 hover:border-accent-muted"
          >
            <div className="mb-2 flex flex-wrap gap-2">
              <span className="text-xs uppercase text-ink-faint">
                {tObjectType(params.locale, r.type)}
              </span>
              {r.authority_level && (
                <AuthorityBadge level={r.authority_level} locale={params.locale} />
              )}
              <span className="text-xs text-ink-faint">
                {formatObjectId(params.locale, r.id)}
              </span>
            </div>
            <p className="text-sm text-ink">
              {getSearchDisplayTitle(
                params.locale,
                r.type as ObjectType,
                r.title,
                r.authority_level
              )}
            </p>
          </Link>
        ))}
      </div>
    </div>
  );
}
