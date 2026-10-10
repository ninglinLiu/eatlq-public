"use client";

import { useState } from "react";
import Link from "next/link";
import { ObjectCard, SectionHeader } from "@/components/ui/Cards";
import { AuthorityBadge } from "@/components/ui/Badges";
import { getObjectId, truncate } from "@/lib/utils/object";
import { formatDisplayValue } from "@/lib/i18n/labels";
import { formatObjectId, getDisplayStatement } from "@/lib/i18n/zh-display";
import { getDictionary, getPageIntro, localePath, type Locale } from "@/lib/i18n";
import type { CanonicalObject } from "@/lib/data/types";

export function FailuresPageClient({
  locale,
  failures,
  guardrails,
}: {
  locale: Locale;
  failures: CanonicalObject[];
  guardrails: CanonicalObject[];
}) {
  const t = getDictionary(locale);
  const intro = getPageIntro(locale, "failures");
  const [tab, setTab] = useState<"failures" | "guardrails">("failures");

  const highlightKeywords = [
    "quoter",
    "sellability",
    "ath",
    "mfe",
    "state override",
    "honeypot",
    "pipeline stall",
    "authorization",
    "backtest",
    "survivor",
    "operational definition",
  ];

  function isHighlight(obj: CanonicalObject): boolean {
    const text = JSON.stringify(obj).toLowerCase();
    return highlightKeywords.some((k) => text.includes(k));
  }

  return (
    <div>
      <SectionHeader
        title={t.nav.failuresGuardrails}
        description={intro}
      />

      <div className="mb-8 flex gap-2 border-b border-border-warm">
        <button
          type="button"
          onClick={() => setTab("failures")}
          className={`px-4 py-2 text-sm ${
            tab === "failures"
              ? "border-b-2 border-accent font-medium text-accent"
              : "text-ink-muted"
          }`}
        >
          {t.tabs.failures} ({failures.length})
        </button>
        <button
          type="button"
          onClick={() => setTab("guardrails")}
          className={`px-4 py-2 text-sm ${
            tab === "guardrails"
              ? "border-b-2 border-accent font-medium text-accent"
              : "text-ink-muted"
          }`}
        >
          {t.tabs.guardrails} ({guardrails.length})
        </button>
      </div>

      {tab === "failures" ? (
        <div className="grid gap-4">
          {failures.map((f) => (
            <ObjectCard
              key={getObjectId(f)}
              locale={locale}
              type="failure"
              id={getObjectId(f)}
              obj={f}
              authority={f.authority_level as string}
              authorship={f.authorship as string | string[]}
              highlight={isHighlight(f)}
              meta={[
                {
                  label: t.fields.whatHappened,
                  value:
                    locale === "zh"
                      ? formatDisplayValue("zh", f.what_happened).slice(0, 80)
                      : truncate(String(f.what_happened ?? "—"), 120),
                },
                {
                  label: t.fields.whyItHappened,
                  value:
                    locale === "zh"
                      ? formatDisplayValue("zh", f.why_it_happened).slice(0, 80)
                      : truncate(String(f.why_it_happened ?? "—"), 120),
                },
                {
                  label: t.fields.impact,
                  value:
                    locale === "zh"
                      ? formatDisplayValue("zh", f.impact).slice(0, 80)
                      : truncate(String(f.impact ?? "—"), 80),
                },
                {
                  label: t.fields.futureGuardrail,
                  value:
                    locale === "zh"
                      ? formatDisplayValue("zh", f.future_guardrail)
                      : String(f.future_guardrail ?? "—"),
                },
              ].filter((m) => m.value !== "—")}
            />
          ))}
        </div>
      ) : (
        <div className="grid gap-4">
          {guardrails.map((g) => (
            <div
              key={String(g.guardrail_id)}
              className="rounded-xl border border-border-warm bg-white p-5"
            >
              <div className="mb-3 flex flex-wrap gap-2">
                <AuthorityBadge level={g.authority_level as string} locale={locale} />
                <span className="text-xs text-ink-faint">
                  {formatObjectId(locale, String(g.guardrail_id))}
                </span>
              </div>
              <p className="text-sm leading-relaxed text-ink">
                {getDisplayStatement(locale, g as Record<string, unknown>, "guardrail")}
              </p>
              {g.from_failure != null && String(g.from_failure) !== "" && (
                <Link
                  href={localePath(
                    locale,
                    `/object/failure/${String(g.from_failure)}`
                  )}
                  className="mt-3 inline-block text-xs text-accent hover:underline"
                >
                  ← {t.site.fromFailure}: {formatObjectId(locale, String(g.from_failure))}
                </Link>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
