import Link from "next/link";
import { SectionHeader, InfoBox } from "@/components/ui/Cards";
import { StatusBadge } from "@/components/ui/Badges";
import { loadWindows, loadWeeklyStats } from "@/lib/data/loader";
import { getDictionary, getPageIntro, localePath, type Locale } from "@/lib/i18n";
import { formatWindowStats, tEmptyClass } from "@/lib/i18n/labels";
import { formatWindowLabel } from "@/lib/i18n/zh-display";

export default function TimelinePage({
  params,
}: {
  params: { locale: Locale };
}) {
  const locale = params.locale;
  const t = getDictionary(locale);
  const windows = loadWindows().slice().reverse();
  const intro = getPageIntro(locale, "timeline");

  return (
    <div>
      <SectionHeader title={t.nav.timeline} description={intro} />

      <InfoBox>
        <p className="mb-2 font-medium text-ink">{t.timeline.statusLegend}</p>
        <div className="flex flex-wrap gap-2">
          {["GOLD", "VALIDATED", "AUTO_CURATED", "ARTIFACTS_ONLY", "EMPTY_CONFIRMED", "UNVERIFIED"].map(
            (s) => (
              <StatusBadge key={s} status={s} locale={locale} />
            )
          )}
        </div>
      </InfoBox>

      <div className="space-y-3">
        {windows.map((w) => {
          const stats = loadWeeklyStats(w.dir_name);
          const label = formatWindowLabel(locale, w.dir_name);

          return (
            <Link
              key={w.window_id}
              href={localePath(locale, `/timeline/${w.dir_name}`)}
              className="block rounded-xl border border-border-warm bg-white p-5 hover:border-accent-muted"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-ink">{label}</span>
                <StatusBadge status={w.status} locale={locale} />
              </div>
              {stats && (
                <p className="mt-2 text-sm text-ink-muted">
                  {formatWindowStats(locale, stats)}
                </p>
              )}
              {stats && (
                <p className="mt-1 text-xs text-ink-faint">
                  {locale === "zh"
                    ? `${t.timeline.conclusions} ${stats.conclusions ?? 0} · ${t.timeline.hypotheses} ${stats.hypotheses ?? 0} · ${t.timeline.experiments} ${stats.experiments ?? 0} · ${t.timeline.failures} ${stats.failures ?? 0}`
                    : `Conclusions ${stats.conclusions ?? 0} · Hypotheses ${stats.hypotheses ?? 0} · Experiments ${stats.experiments ?? 0} · Failures ${stats.failures ?? 0}`}
                </p>
              )}
              {w.empty_classification && (
                <p className="mt-1 text-xs text-ink-faint">
                  {tEmptyClass(locale, w.empty_classification)}
                </p>
              )}
            </Link>
          );
        })}
      </div>
    </div>
  );
}
