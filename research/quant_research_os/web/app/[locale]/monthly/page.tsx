import Link from "next/link";
import { SectionHeader, InfoBox } from "@/components/ui/Cards";
import { listMonthlyDirs } from "@/lib/data/loader";
import { getDictionary, getPageIntro, localePath, type Locale } from "@/lib/i18n";
import { formatMonthLabel } from "@/lib/i18n/zh-display";

export default function MonthlyIndexPage({
  params,
}: {
  params: { locale: Locale };
}) {
  const t = getDictionary(params.locale);
  const months = listMonthlyDirs().reverse();
  const intro = getPageIntro(params.locale, "monthly");

  return (
    <div>
      <SectionHeader title={t.nav.monthly} description={intro} />
      {intro && <InfoBox>{intro}</InfoBox>}
      <div className="space-y-2">
        {months.map((month) => (
          <Link
            key={month}
            href={localePath(params.locale, `/monthly/${month}`)}
            className="block rounded-xl border border-border-warm bg-white px-5 py-4 hover:border-accent-muted"
          >
            <span className="font-medium text-ink">
              {formatMonthLabel(params.locale, month)}
            </span>
          </Link>
        ))}
      </div>
    </div>
  );
}
