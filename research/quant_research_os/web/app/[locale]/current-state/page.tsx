import { SectionHeader, InfoBox } from "@/components/ui/Cards";
import { MarkdownReader } from "@/components/ui/MarkdownReader";
import { PATHS, readMarkdown } from "@/lib/data/loader";
import { getDictionary, getPageIntro, type Locale } from "@/lib/i18n";
import { localizeMarkdown } from "@/lib/i18n/localize";

export default async function CurrentStatePage({
  params,
}: {
  params: { locale: Locale };
}) {
  const t = getDictionary(params.locale);
  const intro = getPageIntro(params.locale, "currentState");
  const raw = readMarkdown(`${PATHS.global}/current_state.md`);
  const content =
    params.locale === "zh"
      ? await localizeMarkdown(params.locale, raw)
      : raw;

  return (
    <div className="max-w-content">
      <SectionHeader title={t.nav.currentState} description={intro} />
      {intro && <InfoBox>{intro}</InfoBox>}
      <div className="rounded-xl border border-border-warm bg-white p-8">
        <MarkdownReader content={content} locale={params.locale} />
      </div>
    </div>
  );
}
