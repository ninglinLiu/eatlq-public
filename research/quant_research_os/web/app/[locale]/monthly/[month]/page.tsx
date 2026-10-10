import Link from "next/link";
import { SectionHeader, InfoBox } from "@/components/ui/Cards";
import { MarkdownReader } from "@/components/ui/MarkdownReader";
import { loadMonthlySummary, listMonthlyDirs } from "@/lib/data/loader";
import { getDictionary, getPageIntro, localePath, type Locale } from "@/lib/i18n";
import { formatMonthLabel } from "@/lib/i18n/zh-display";
import { localizeMarkdown } from "@/lib/i18n/localize";
import { notFound } from "next/navigation";

export default async function MonthlyDetailPage({
  params,
}: {
  params: { locale: Locale; month: string };
}) {
  const months = listMonthlyDirs();
  if (!months.includes(params.month)) notFound();

  const t = getDictionary(params.locale);
  const summary = loadMonthlySummary(params.month);
  const intro = getPageIntro(params.locale, "monthly");
  const localizedSummary =
    params.locale === "zh"
      ? await localizeMarkdown(params.locale, summary)
      : summary;

  return (
    <div className="max-w-content">
      <Link
        href={localePath(params.locale, "/monthly")}
        className="text-sm text-ink-faint hover:text-accent"
      >
        ← {t.site.back}
      </Link>
      <SectionHeader
        title={`${t.nav.monthly} — ${formatMonthLabel(params.locale, params.month)}`}
        description={intro}
      />
      <div className="rounded-xl border border-border-warm bg-white p-6">
        <MarkdownReader content={localizedSummary} locale={params.locale} />
      </div>
    </div>
  );
}
