import { SectionHeader, InfoBox } from "@/components/ui/Cards";
import { MarkdownReader } from "@/components/ui/MarkdownReader";
import { PATHS, readMarkdown } from "@/lib/data/loader";
import { getDictionary, getPageIntro, type Locale } from "@/lib/i18n";

import { localizeMarkdown } from "@/lib/i18n/localize";

export default async function ProductionStatusPage({
  params,
}: {
  params: { locale: Locale };
}) {
  const t = getDictionary(params.locale);
  const intro = getPageIntro(params.locale, "productionStatus");
  const content = readMarkdown(PATHS.productionStatus);
  const localizedContent =
    params.locale === "zh"
      ? await localizeMarkdown(params.locale, content)
      : content;

  return (
    <div className="max-w-content">
      <SectionHeader title={t.nav.productionStatus} description={intro} />
      {intro && <InfoBox>{intro}</InfoBox>}
      <div className="rounded-xl border border-border-warm bg-white p-8">
        <MarkdownReader content={localizedContent} locale={params.locale} />
      </div>
    </div>
  );
}
