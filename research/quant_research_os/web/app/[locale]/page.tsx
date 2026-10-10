import Link from "next/link";
import { ObjectCard, SectionHeader, StatCard, InfoBox } from "@/components/ui/Cards";
import { MarkdownReader } from "@/components/ui/MarkdownReader";
import { StatusBadge, AuthorityBadge } from "@/components/ui/Badges";
import { QuickFilters } from "@/components/layout/SearchBar";
import {
  loadAuthorityCounts,
  loadGlobalCollection,
  loadKnowledgeCounts,
  loadOpenQuestions,
  loadPipelineState,
  getTotalRawSelectedCanonical,
  getMonthlyGrowth,
  getObjectId,
  PATHS,
  readMarkdown,
} from "@/lib/data/loader";
import { GLOBAL_JSONL } from "@/lib/data/paths";
import { getDictionary, localePath, type Locale } from "@/lib/i18n";
import { tAuthority, tObjectsCount } from "@/lib/i18n/labels";
import {
  formatObjectId,
  formatMonthLabel,
  getDisplayStatement,
  getOpenQuestionDisplay,
} from "@/lib/i18n/zh-display";
import { localizeObjects, localizeOpenQuestions } from "@/lib/i18n/localize";

export default async function HomePage({
  params,
}: {
  params: { locale: Locale };
}) {
  const locale = params.locale;
  const t = getDictionary(locale);
  const pipeline = loadPipelineState();
  const counts = loadKnowledgeCounts();
  const authority = loadAuthorityCounts();
  const totals = getTotalRawSelectedCanonical();
  const currentStateMd = readMarkdown(`${PATHS.global}/current_state.md`);
  const openQuestions = loadOpenQuestions().filter(
    (q) => q.status === "OPEN" || q.status === "PARTIALLY_RESOLVED"
  );

  const guardrails = loadGlobalCollection(GLOBAL_JSONL.permanent_guardrails);
  const topGuardrailsRaw = guardrails.slice(0, 8);
  const conclusionsRaw = loadGlobalCollection(GLOBAL_JSONL.conclusions)
    .filter((c) => c.authority_level === "VALIDATED")
    .slice(-8)
    .reverse();
  const monthly = getMonthlyGrowth();

  const [topGuardrails, conclusions, openQuestionsLocalized] =
    locale === "zh"
      ? await Promise.all([
          localizeObjects(locale, topGuardrailsRaw),
          localizeObjects(locale, conclusionsRaw),
          localizeOpenQuestions(locale, openQuestions),
        ])
      : [topGuardrailsRaw, conclusionsRaw, openQuestions];

  const knowledgeCards: Array<{
    label: string;
    href: string;
    count: number;
    desc?: string;
    warn?: boolean;
  }> = [
    {
      label: t.nav.userTheses,
      href: "/theses",
      count: counts.user_theses,
      desc: locale === "zh" ? "用户明确的研究思想与论点" : undefined,
    },
    {
      label: t.nav.researchPrinciples,
      href: "/principles",
      count: counts.research_principles,
      desc: locale === "zh" ? "系统应继承的方法论原则" : undefined,
    },
    {
      label: t.nav.hypotheses,
      href: "/hypotheses",
      count: counts.hypotheses,
      desc: locale === "zh" ? "含操作定义与制度依赖" : undefined,
    },
    {
      label: t.nav.experiments,
      href: "/experiments",
      count: counts.experiments,
      warn: true,
      desc: locale === "zh" ? "计数可能含重复，见去重提示" : undefined,
    },
    {
      label: t.nav.conclusions,
      href: "/conclusions",
      count: counts.conclusions,
      desc: locale === "zh" ? "含执行模式与实盘验证状态" : undefined,
    },
    {
      label: t.nav.failuresGuardrails,
      href: "/failures",
      count: counts.failures,
      desc:
        locale === "zh"
          ? `${counts.guardrails} ${t.home.guardrailsTotal}`
          : undefined,
    },
    {
      label: t.nav.methods,
      href: "/methods",
      count: counts.methods,
    },
    {
      label: t.nav.decisions,
      href: "/decisions",
      count: counts.decisions,
      desc: locale === "zh" ? "区分已授权与实盘成交" : undefined,
    },
  ];

  return (
    <div className="max-w-content">
      <header className="mb-10">
        <h1 className="font-serif text-3xl font-semibold tracking-tight text-ink">
          {t.site.title}
        </h1>
        <p className="mt-2 text-lg text-ink-muted">{t.site.tagline}</p>
        <div className="mt-4 space-y-2 rounded-lg border border-accent/20 bg-accent-soft/40 px-4 py-3 text-sm text-accent">
          <p>{t.site.mixedAuthority}</p>
          <p className="text-ink-muted">{t.site.mixedAuthorityDetail}</p>
        </div>
      </header>

      <QuickFilters
        locale={locale}
        filters={[
          { href: "/conclusions?authority=VALIDATED", label: t.filters.onlyValidated },
          { href: "/conclusions?authority=GOLD,VALIDATED", label: t.filters.onlyGoldValidated },
          { href: "/conclusions?live_proven=true", label: t.filters.onlyLiveProven },
          { href: "/conclusions?execution_mode=BACKTEST", label: t.filters.onlyBacktest },
          { href: "/principles?always_load=1", label: t.filters.onlyAlwaysLoad },
          { href: "/open-questions?status=OPEN", label: t.filters.onlyUnresolved },
        ]}
      />

      <section className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <StatCard
          label={t.home.historicalRange}
          value={`${pipeline.earliest_research_date ?? "—"} → ${pipeline.latest_research_date ?? "—"}`}
        />
        <StatCard
          label={t.home.registeredWindows}
          value={pipeline.TOTAL_REGISTERED_WINDOWS ?? "—"}
        />
        <StatCard
          label={t.home.nonEmptyProcessed}
          value={pipeline.NON_EMPTY_PROCESSED_WINDOWS ?? "—"}
        />
        <StatCard
          label={t.home.qaPassedWindows}
          value={pipeline.QA_PASSED_WINDOWS ?? "—"}
        />
        <StatCard
          label={t.home.qaPassRateAmongProcessed}
          value={pipeline.QA_PASS_RATE_AMONG_PROCESSED ?? "—"}
          sub={locale === "zh" ? t.home.processedNoteZh : t.home.processedNote}
        />
        <StatCard
          label={t.home.qaPassedShareOfAll}
          value={pipeline.QA_PASSED_SHARE_OF_ALL_WINDOWS ?? "—"}
          sub={t.home.qaNote}
          warn
        />
        <StatCard
          label={t.home.emptyConfirmed}
          value={pipeline.EMPTY_CONFIRMED ?? "—"}
        />
        <StatCard
          label={t.home.artifactsOnly}
          value={pipeline.ARTIFACTS_ONLY ?? "—"}
        />
        <StatCard label={t.home.rawEvents} value={totals.raw.toLocaleString()} />
        <StatCard label={t.home.selectedEvents} value={totals.selected.toLocaleString()} />
        <StatCard label={t.home.canonicalObjects} value={totals.canonical.toLocaleString()} />
      </section>

      <section className="mt-14">
        <SectionHeader
          title={t.home.currentState}
          description={t.home.currentStateLegend}
        />
        <div className="mb-6 flex flex-wrap gap-2">
          <StatusBadge status="PANIC" locale={locale} large />
          <StatusBadge status="PAUSED" locale={locale} large />
          <StatusBadge status="LIVE" locale={locale} large />
          <StatusBadge status="PAPER" locale={locale} large />
          <StatusBadge status="BACKTEST" locale={locale} large />
        </div>

        <div className="mb-6 rounded-xl border border-red-200 bg-red-50/50 p-5">
          <h3 className="mb-3 text-sm font-semibold text-red-900">
            {t.home.liveFacts}
          </h3>
          <ul className="space-y-2 text-sm leading-relaxed text-red-900/90">
            <li>• {t.home.liveFact1}</li>
            <li>• {t.home.liveFact2}</li>
            <li>• {t.home.liveFact3}</li>
            <li>• {t.home.liveFact4}</li>
          </ul>
        </div>

        <div className="mb-6 rounded-xl border border-border-warm bg-white p-5">
          <h3 className="mb-2 text-sm font-semibold text-ink">{t.home.readExample}</h3>
          <p className="text-sm leading-relaxed text-ink-muted">{t.home.readExampleText}</p>
        </div>

        <div className="rounded-xl border border-border-warm bg-white p-6">
          {locale === "zh" ? (
            <div className="prose-reader space-y-4 text-[17px] leading-relaxed">
              <h2>窗口指标（正确语义）</h2>
              <table>
                <thead>
                  <tr>
                    <th>指标</th>
                    <th>数值</th>
                  </tr>
                </thead>
                <tbody>
                  <tr><td>注册窗口总数</td><td>{pipeline.TOTAL_REGISTERED_WINDOWS}</td></tr>
                  <tr><td>完整流水线质量审计通过</td><td>{pipeline.QA_PASSED_WINDOWS}</td></tr>
                  <tr><td>已确认为空</td><td>{pipeline.EMPTY_CONFIRMED}</td></tr>
                  <tr><td>仅工件（待策展）</td><td>{pipeline.ARTIFACTS_ONLY}</td></tr>
                  <tr><td><strong>已处理窗口质量审计通过率</strong></td><td><strong>{pipeline.QA_PASS_RATE_AMONG_PROCESSED}</strong></td></tr>
                  <tr><td>全部窗口质量审计通过占比</td><td>{pipeline.QA_PASSED_SHARE_OF_ALL_WINDOWS}</td></tr>
                </tbody>
              </table>
              <p className="text-ink-muted">{t.home.qaNote}</p>
              <h2>{t.home.globalTotals}</h2>
              <ul>
                <li>用户论点：{counts.user_theses}</li>
                <li>研究原则：{counts.research_principles}</li>
                <li>假设：{counts.hypotheses}</li>
                <li>实验：{counts.experiments}（{t.site.needsDedup}）</li>
                <li>结论：{counts.conclusions}</li>
                <li>失败：{counts.failures}</li>
                <li>方法：{counts.methods}</li>
                <li>决策：{counts.decisions}</li>
                <li>{t.home.guardrailsTotal}：{counts.guardrails}</li>
              </ul>
              <h2>全局对象权威分布</h2>
              <ul>
                {Object.entries(authority)
                  .sort((a, b) => b[1] - a[1])
                  .map(([level, n]) => (
                    <li key={level}>{tAuthority(locale, level)}：{n}</li>
                  ))}
              </ul>
            </div>
          ) : (
            <MarkdownReader content={currentStateMd} locale={locale} />
          )}
        </div>
      </section>

      <section className="mt-14">
        <SectionHeader title={t.home.knowledgeOverview} />
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {knowledgeCards.map((card) => (
            <Link
              key={card.href}
              href={localePath(locale, card.href)}
              className="rounded-xl border border-border-warm bg-white p-5 hover:border-accent-muted"
            >
              <p className="text-sm font-medium text-ink">{card.label}</p>
              <p className="mt-2 text-3xl font-semibold text-ink">{card.count}</p>
              {card.warn && (
                <p className="mt-1 text-xs text-accent">{t.site.needsDedup}</p>
              )}
              {card.desc && (
                <p className="mt-1 text-xs text-ink-faint">{card.desc}</p>
              )}
            </Link>
          ))}
        </div>
      </section>

      <section className="mt-14">
        <SectionHeader title={t.home.mostImportantGuardrails} />
        <div className="grid gap-4">
          {topGuardrails.map((g) => (
            <div
              key={String(g.guardrail_id)}
              className="rounded-xl border border-accent/20 bg-accent-soft/30 p-4"
            >
              <div className="mb-2 flex flex-wrap gap-2">
                <AuthorityBadge level={g.authority_level as string} locale={locale} />
                <span className="text-xs text-ink-faint">
                  {formatObjectId(locale, String(g.guardrail_id))}
                </span>
              </div>
              <p className="text-sm leading-relaxed text-ink">
                {getDisplayStatement(locale, g as Record<string, unknown>, "guardrail")}
              </p>
            </div>
          ))}
        </div>
        <Link
          href={localePath(locale, "/failures")}
          className="mt-4 inline-block text-sm text-accent hover:underline"
        >
          {t.site.readMore} →
        </Link>
      </section>

      <section className="mt-14">
        <SectionHeader
          title={t.home.latestConclusions}
          description={t.home.latestConclusionsDesc}
        />
        <div className="grid gap-4">
          {conclusions.map((c) => (
            <ObjectCard
              key={getObjectId(c)}
              locale={locale}
              type="conclusion"
              id={getObjectId(c)}
              obj={c as Record<string, unknown>}
              authority={c.authority_level as string}
              status={c.status as string}
              executionMode={c.execution_mode as string}
              liveProven={c.live_proven as boolean}
            />
          ))}
        </div>
      </section>

      <section className="mt-14">
        <SectionHeader title={t.home.unresolvedQuestions} />
        <div className="space-y-3">
          {openQuestionsLocalized.slice(0, 10).map((q) => {
            const display = getOpenQuestionDisplay(locale, q);
            return (
            <div
              key={q.id}
              className="rounded-xl border border-border-warm bg-white p-4"
            >
              <div className="mb-2 flex flex-wrap gap-2">
                <StatusBadge status={q.status} locale={locale} />
                {q.week && (
                  <span className="text-xs text-ink-faint">
                    {locale === "zh"
                      ? q.week.replace(/[A-Za-z_]/g, " ").trim()
                      : q.week}
                  </span>
                )}
              </div>
              <p className="font-medium text-ink">{display.title}</p>
              <p className="mt-1 text-sm leading-relaxed text-ink-muted">
                {display.body.slice(0, locale === "zh" ? 400 : 200)}
              </p>
            </div>
            );
          })}
        </div>
        <Link
          href={localePath(locale, "/open-questions")}
          className="mt-4 inline-block text-sm text-accent hover:underline"
        >
          {t.site.readMore} →
        </Link>
      </section>

      <section className="mt-14">
        <SectionHeader title={t.home.knowledgeByAuthority} />
        <div className="flex flex-wrap gap-3">
          {Object.entries(authority)
            .sort((a, b) => b[1] - a[1])
            .map(([level, count]) => (
              <div
                key={level}
                className="flex items-center gap-2 rounded-xl border border-border-warm bg-white px-4 py-3"
              >
                <AuthorityBadge level={level} locale={locale} />
                <span className="text-lg font-semibold text-ink">{count}</span>
              </div>
            ))}
        </div>
      </section>

      <section className="mt-14">
        <SectionHeader title={t.home.researchHistory} />
        <div className="space-y-2">
          {monthly.map((m) => (
            <Link
              key={m.month}
              href={localePath(locale, `/monthly/${m.month}`)}
              className="flex items-center justify-between rounded-lg border border-border-warm bg-white px-4 py-3 hover:border-accent-muted"
            >
              <span className="font-medium text-ink">
                {formatMonthLabel(locale, m.month)}
              </span>
              <span className="text-sm text-ink-muted">
                {tObjectsCount(locale, m.count)}
              </span>
            </Link>
          ))}
        </div>
      </section>
    </div>
  );
}
