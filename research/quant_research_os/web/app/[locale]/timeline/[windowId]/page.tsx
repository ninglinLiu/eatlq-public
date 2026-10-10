import Link from "next/link";
import { StatusBadge } from "@/components/ui/Badges";
import { MarkdownReader } from "@/components/ui/MarkdownReader";
import {
  loadWeeklyStats,
  loadWeeklyMarkdown,
  loadWindows,
} from "@/lib/data/loader";
import { getDictionary, localePath, type Locale } from "@/lib/i18n";
import { formatWindowLabel } from "@/lib/i18n/zh-display";
import { notFound } from "next/navigation";

import { localizeMarkdown } from "@/lib/i18n/localize";

export default async function WindowDetailPage({
  params,
}: {
  params: { locale: Locale; windowId: string };
}) {
  const { locale, windowId } = params;
  const t = getDictionary(locale);
  const windows = loadWindows();
  const window = windows.find((w) => w.dir_name === windowId);
  if (!window) notFound();

  const stats = loadWeeklyStats(windowId);
  const summary = loadWeeklyMarkdown(windowId, "weekly_canonical_summary.md");
  const openQuestions = loadWeeklyMarkdown(windowId, "open_questions.md");
  const conflicts = loadWeeklyMarkdown(windowId, "source_conflicts.md");
  const qa = loadWeeklyMarkdown(windowId, "quality_sample_audit.md");

  const tabsRaw = [
    { key: "summary", label: t.timeline.summary, content: summary },
    { key: "questions", label: t.timeline.openQuestions, content: openQuestions },
    { key: "conflicts", label: t.timeline.sourceConflicts, content: conflicts },
    { key: "qa", label: t.timeline.qa, content: qa },
  ].filter((tab) => tab.content);

  const tabs =
    locale === "zh"
      ? await Promise.all(
          tabsRaw.map(async (tab) => ({
            ...tab,
            content: await localizeMarkdown(locale, tab.content),
          }))
        )
      : tabsRaw;

  const canonicalTotal = stats
    ? (stats.user_theses ?? 0) +
      (stats.research_principles ?? 0) +
      (stats.hypotheses ?? 0) +
      (stats.experiments ?? 0) +
      (stats.conclusions ?? 0) +
      (stats.failures ?? 0) +
      (stats.methods ?? 0) +
      (stats.decisions ?? 0)
    : 0;

  return (
    <div className="max-w-content">
      <Link
        href={localePath(locale, "/timeline")}
        className="text-sm text-ink-faint hover:text-accent"
      >
        ← {t.site.back}
      </Link>

      <header className="mb-8 mt-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-2xl font-semibold text-ink">
            {formatWindowLabel(locale, windowId)}
          </h1>
          <StatusBadge status={window.status} locale={locale} large />
        </div>
        {stats && (
          <dl className="mt-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4 lg:grid-cols-5">
            <div>
              <dt className="text-ink-faint">{t.timeline.raw}</dt>
              <dd className="font-medium">{stats.raw_events ?? 0}</dd>
            </div>
            <div>
              <dt className="text-ink-faint">{t.timeline.selected}</dt>
              <dd className="font-medium">{stats.selected ?? 0}</dd>
            </div>
            <div>
              <dt className="text-ink-faint">{t.timeline.conclusions}</dt>
              <dd className="font-medium">{stats.conclusions ?? 0}</dd>
            </div>
            <div>
              <dt className="text-ink-faint">{t.timeline.hypotheses}</dt>
              <dd className="font-medium">{stats.hypotheses ?? 0}</dd>
            </div>
            <div>
              <dt className="text-ink-faint">{t.timeline.experiments}</dt>
              <dd className="font-medium">{stats.experiments ?? 0}</dd>
            </div>
            <div>
              <dt className="text-ink-faint">{t.timeline.failures}</dt>
              <dd className="font-medium">{stats.failures ?? 0}</dd>
            </div>
            <div>
              <dt className="text-ink-faint">{t.timeline.methods}</dt>
              <dd className="font-medium">{stats.methods ?? 0}</dd>
            </div>
            <div>
              <dt className="text-ink-faint">{t.timeline.decisions}</dt>
              <dd className="font-medium">{stats.decisions ?? 0}</dd>
            </div>
            <div>
              <dt className="text-ink-faint">{t.timeline.theses}</dt>
              <dd className="font-medium">{stats.user_theses ?? 0}</dd>
            </div>
            <div>
              <dt className="text-ink-faint">{t.timeline.principles}</dt>
              <dd className="font-medium">{stats.research_principles ?? 0}</dd>
            </div>
            <div className="col-span-2 sm:col-span-4">
              <dt className="text-ink-faint">{t.timeline.canonicalTotal}</dt>
              <dd className="font-medium">{canonicalTotal}</dd>
            </div>
          </dl>
        )}
      </header>

      <div className="space-y-10">
        {tabs.map((tab) => (
          <section key={tab.key}>
            <h2 className="mb-4 text-lg font-semibold text-ink">{tab.label}</h2>
            <div className="rounded-xl border border-border-warm bg-white p-6">
              <MarkdownReader content={tab.content} locale={locale} />
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
