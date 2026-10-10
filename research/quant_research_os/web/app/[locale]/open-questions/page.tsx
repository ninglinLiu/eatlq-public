import { SectionHeader, InfoBox } from "@/components/ui/Cards";
import { StatusBadge } from "@/components/ui/Badges";
import { loadOpenQuestions } from "@/lib/data/loader";
import { getDictionary, getPageIntro, type Locale } from "@/lib/i18n";
import { tListCount } from "@/lib/i18n/labels";
import { getOpenQuestionDisplay } from "@/lib/i18n/zh-display";

import { localizeOpenQuestions } from "@/lib/i18n/localize";

export default async function OpenQuestionsPage({
  params,
  searchParams,
}: {
  params: { locale: Locale };
  searchParams: Record<string, string | undefined>;
}) {
  const t = getDictionary(params.locale);
  let filtered = loadOpenQuestions();

  if (searchParams.status) {
    filtered = filtered.filter(
      (q) => q.status.toUpperCase() === searchParams.status!.toUpperCase()
    );
  }

  const intro = getPageIntro(params.locale, "openQuestions");
  const questions =
    params.locale === "zh"
      ? await localizeOpenQuestions(params.locale, filtered)
      : filtered;

  return (
    <div>
      <SectionHeader
        title={t.nav.openQuestions}
        description={tListCount(params.locale, questions.length, loadOpenQuestions().length)}
      />
      {intro && <InfoBox>{intro}</InfoBox>}

      <div className="space-y-4">
        {questions.map((q) => {
          const display = getOpenQuestionDisplay(params.locale, q);
          return (
          <article
            key={q.id}
            className="rounded-xl border border-border-warm bg-white p-5"
          >
            <div className="mb-2 flex flex-wrap gap-2">
              <StatusBadge status={q.status} locale={params.locale} />
              {q.week && (
                <span className="text-xs text-ink-faint">
                  {params.locale === "zh"
                    ? q.week.replace(/[A-Za-z_]/g, " ").trim()
                    : q.week}
                </span>
              )}
            </div>
            <h2 className="text-lg font-medium text-ink">{display.title}</h2>
            <p className="mt-2 text-sm leading-relaxed text-ink-muted">
              {display.body.slice(0, params.locale === "zh" ? 600 : 500)}
            </p>
          </article>
          );
        })}
      </div>
    </div>
  );
}
