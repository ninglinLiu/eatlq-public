import { SectionHeader, InfoBox } from "@/components/ui/Cards";
import { MarkdownReader } from "@/components/ui/MarkdownReader";
import { PATHS, readMarkdown } from "@/lib/data/loader";
import { getDictionary, getPageIntro, type Locale } from "@/lib/i18n";

import { localizeMarkdown } from "@/lib/i18n/localize";

export default async function AuthorityPage({
  params,
}: {
  params: { locale: Locale };
}) {
  const t = getDictionary(params.locale);
  const intro = getPageIntro(params.locale, "authority");
  const content = readMarkdown(PATHS.authorityModel);
  const localizedContent =
    params.locale === "zh"
      ? await localizeMarkdown(params.locale, content)
      : content;

  return (
    <div className="max-w-content">
      <SectionHeader title={t.nav.authorityModel} description={intro} />
      {intro && <InfoBox>{intro}</InfoBox>}
      {params.locale === "zh" && (
        <div className="mb-6 rounded-xl border border-border-warm bg-white p-5 text-sm leading-relaxed text-ink-muted">
          <p className="mb-2 font-medium text-ink">权威等级速查</p>
          <ul className="list-inside list-disc space-y-1">
            <li><strong>金标准</strong>：第三轮单日金标准，最高保真度</li>
            <li><strong>已验证</strong>：第五/六轮人工质量审计导入，高可信</li>
            <li><strong>自动策展</strong>：五月至七月密集周自动提取，中等可信</li>
            <li><strong>仅工件</strong>：有 git/审计但无用户对话，低可信，待策展</li>
            <li><strong>未验证</strong>：未提升 / 稀疏，勿自动加载</li>
          </ul>
        </div>
      )}
      <div className="rounded-xl border border-border-warm bg-white p-8">
        <MarkdownReader content={localizedContent} locale={params.locale} />
      </div>
    </div>
  );
}
