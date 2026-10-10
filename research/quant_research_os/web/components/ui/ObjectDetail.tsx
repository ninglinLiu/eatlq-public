import Link from "next/link";
import {
  AuthorityBadge,
  AuthorshipBadge,
  EpistemicBadge,
  LiveProvenBadge,
  StatusBadge,
} from "@/components/ui/Badges";
import { formatList } from "@/lib/utils/format";
import {
  formatDisplayValue,
  formatSourcePointer,
} from "@/lib/i18n/labels";
import {
  formatObjectId,
  getDetailBody,
  getDisplayStatement,
  buildConclusionSummary,
} from "@/lib/i18n/zh-display";
import { getDictionary, localePath, type Locale } from "@/lib/i18n";
import { tObjectType } from "@/lib/i18n/labels";
import type { CanonicalObject, ObjectType } from "@/lib/data/types";

export function ObjectDetailView({
  locale,
  type,
  obj,
}: {
  locale: Locale;
  type: ObjectType;
  obj: CanonicalObject;
}) {
  const t = getDictionary(locale);
  const id = String(obj.id ?? obj.hypothesis_id ?? obj.experiment_id ?? obj.guardrail_id ?? "");
  const { body: detailBody, isSummary } = getDetailBody(locale, obj as Record<string, unknown>, type);
  const displayTitle = getDisplayStatement(locale, obj as Record<string, unknown>, type, 2000);

  const executionConfirmed = obj.execution_confirmed;
  const executionStatus = obj.execution_status as string | undefined;

  return (
    <article className="max-w-content">
      <div className="mb-6">
        <Link
          href={localePath(locale, `/${typeRoute(type)}`)}
          className="text-sm text-ink-faint hover:text-accent"
        >
          ← {t.site.back}
        </Link>
      </div>

      <div className="mb-4 flex flex-wrap gap-2">
        <AuthorityBadge level={obj.authority_level as string} locale={locale} />
        {obj.status != null && String(obj.status) !== "" && (
          <StatusBadge status={String(obj.status)} locale={locale} large />
        )}
        {obj.execution_mode != null &&
          String(obj.execution_mode) !== "" &&
          String(obj.execution_mode) !== "UNKNOWN" && (
          <StatusBadge status={String(obj.execution_mode)} locale={locale} />
        )}
        <LiveProvenBadge proven={obj.live_proven as boolean | undefined} locale={locale} />
        <AuthorshipBadge authorship={obj.authorship as string | string[]} locale={locale} />
        {obj.epistemic_role != null && String(obj.epistemic_role) !== "" && (
          <EpistemicBadge role={String(obj.epistemic_role)} locale={locale} />
        )}
      </div>

      <p className="mb-1 text-xs font-medium uppercase tracking-wide text-ink-faint">
        {tObjectType(locale, type)}
      </p>
      <p className="mb-2 text-xs text-ink-faint">
        {t.site.objectId}: {formatObjectId(locale, id)}
      </p>
      <h1 className="mb-8 text-2xl font-semibold leading-snug text-ink">
        {displayTitle}
      </h1>

      {isSummary && locale === "zh" && (
        <div className="mb-8 rounded-xl border border-border-warm bg-cream-dark/50 px-5 py-4 text-sm leading-relaxed text-ink-muted">
          {buildConclusionSummary(obj as Record<string, unknown>)}
          <p className="mt-3 text-xs text-ink-faint">
            本条为自动策展 Agent 工作日志，非正式结论。完整原始记录请切换英文界面查看。
          </p>
        </div>
      )}

      {detailBody && !isSummary && (
        <div className="mb-8 text-[17px] leading-relaxed text-ink">
          {detailBody}
        </div>
      )}

      {type === "decision" && (
        <div className="mb-8 rounded-xl border border-border-warm bg-white p-5">
          <p className="text-xs font-medium uppercase text-ink-faint">
            {t.site.executionConfirmed}
          </p>
          <div className="mt-2 flex items-center gap-3">
            {executionStatus && (
              <StatusBadge status={executionStatus} locale={locale} large />
            )}
            <span className="text-sm text-ink-muted">
              {executionConfirmed === true
                ? t.site.yes
                : executionConfirmed === false
                  ? t.site.no
                  : t.site.unknown}
            </span>
          </div>
        </div>
      )}

      <MetaGrid locale={locale} obj={obj} type={type} />

      {type === "failure" && <FailureFields locale={locale} obj={obj} />}

      {obj.can_prove !== undefined && (
        <Section title={t.site.canProve}>
          <ValueBlock value={obj.can_prove} locale={locale} />
        </Section>
      )}

      {obj.cannot_prove !== undefined && (
        <Section title={t.site.cannotProve}>
          <ValueBlock value={obj.cannot_prove} locale={locale} />
        </Section>
      )}

      {obj.limitations != null && String(obj.limitations) !== "" && (
        <Section title={t.site.limitations}>
          <p className="text-sm text-ink-muted">{formatDisplayValue(locale, obj.limitations)}</p>
        </Section>
      )}

      {formatList(obj.contradictions).length > 0 && (
        <Section title={t.site.contradictions}>
          <TagList items={formatList(obj.contradictions)} locale={locale} />
        </Section>
      )}

      {obj.superseded_by != null &&
        String(obj.superseded_by) !== "unknown" &&
        String(obj.superseded_by) !== "" && (
        <Section title={t.site.supersededBy}>
          <TagList items={[String(obj.superseded_by)]} locale={locale} />
        </Section>
      )}

      <Section title={t.site.sourcePointers}>
        <ul className="space-y-2">
          {(
            (obj.source_pointers as Array<Record<string, unknown>> | undefined) ?? []
          ).map((ptr, i) => (
            <li
              key={i}
              className="rounded-md border border-border-warm bg-cream-dark/50 px-3 py-2 text-sm text-ink-muted"
            >
              {formatSourcePointer(locale, ptr as Parameters<typeof formatSourcePointer>[1])}
            </li>
          ))}
        </ul>
      </Section>
    </article>
  );
}

function typeRoute(type: ObjectType): string {
  const map: Record<ObjectType, string> = {
    user_thesis: "theses",
    research_principle: "principles",
    hypothesis: "hypotheses",
    experiment: "experiments",
    conclusion: "conclusions",
    failure: "failures",
    method: "methods",
    decision: "decisions",
    guardrail: "failures",
  };
  return map[type];
}

function MetaGrid({
  locale,
  obj,
  type,
}: {
  locale: Locale;
  obj: CanonicalObject;
  type: ObjectType;
}) {
  const t = getDictionary(locale);
  const fields: Array<{ label: string; value: unknown }> = [
    { label: t.fields.persistenceScope, value: obj.persistence_scope },
    { label: t.fields.reusePolicy, value: obj.reuse_policy },
    { label: t.fields.validFrom, value: obj.valid_from },
    { label: t.fields.validUntil, value: obj.valid_until },
    { label: t.fields.regimeDependency, value: obj.regime_dependency },
    { label: t.fields.projectScope, value: obj.project_scope },
  ];

  if (type === "hypothesis") {
    fields.push(
      { label: t.fields.operationalDefinition, value: obj.operational_definition },
      { label: t.fields.origin, value: obj.origin }
    );
  }

  if (type === "experiment") {
    fields.push(
      { label: t.fields.hypothesisId, value: obj.hypothesis_id },
      { label: t.fields.universe, value: obj.universe },
      { label: t.fields.sampleSize, value: obj.sample_size },
      { label: t.fields.timeRange, value: obj.time_range },
      { label: t.fields.entry, value: obj.entry },
      { label: t.fields.exit, value: obj.exit },
      { label: t.fields.costModel, value: obj.cost_model },
      { label: t.fields.trainTestProtocol, value: obj.train_test_protocol },
      { label: t.fields.result, value: obj.result }
    );
  }

  if (type === "failure") {
    fields.push(
      { label: t.fields.failureType, value: obj.failure_type },
      { label: t.fields.weekScope, value: obj.week_scope }
    );
  }

  if (type === "conclusion") {
    fields.push(
      { label: t.fields.evidenceLevel, value: obj.evidence_level },
      { label: t.fields.executionMode, value: obj.execution_mode },
      { label: t.fields.liveProven, value: obj.live_proven }
    );
  }

  const visible = fields.filter(
    (f) =>
      f.value !== null &&
      f.value !== undefined &&
      String(f.value) !== "unknown" &&
      String(f.value) !== "—" &&
      String(f.value) !== ""
  );

  if (visible.length === 0) return null;

  return (
    <dl className="mb-8 grid gap-3 rounded-xl border border-border-warm bg-white p-5 sm:grid-cols-2">
      {visible.map((f) => (
        <div key={f.label}>
          <dt className="text-xs text-ink-faint">{f.label}</dt>
          <dd className="mt-0.5 text-sm text-ink">{formatDisplayValue(locale, f.value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function FailureFields({
  locale,
  obj,
}: {
  locale: Locale;
  obj: CanonicalObject;
}) {
  const t = getDictionary(locale);
  const items = [
    { label: t.fields.whatHappened, value: obj.what_happened },
    { label: t.fields.whyItHappened, value: obj.why_it_happened },
    { label: t.fields.impact, value: obj.impact },
    { label: t.fields.howDetected, value: obj.how_detected },
    { label: t.fields.futureGuardrail, value: obj.future_guardrail },
  ];

  return (
    <div className="mb-8 space-y-4">
      {items.map(
        (item) =>
          item.value != null &&
          String(item.value) !== "" && (
            <Section key={item.label} title={item.label}>
              <p className="text-sm leading-relaxed text-ink-muted">
                {formatDisplayValue(locale, item.value)}
              </p>
            </Section>
          )
      )}
    </div>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="mb-6">
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-ink-faint">
        {title}
      </h2>
      {children}
    </section>
  );
}

function ValueBlock({ value, locale }: { value: unknown; locale: Locale }) {
  const items = formatList(value);
  if (items.length <= 1 && typeof value !== "object") {
    return <p className="text-sm text-ink-muted">{formatDisplayValue(locale, value)}</p>;
  }
  return (
    <ul className="list-inside list-disc space-y-1 text-sm text-ink-muted">
      {items.map((item, i) => (
        <li key={i}>{formatDisplayValue(locale, item)}</li>
      ))}
    </ul>
  );
}

function TagList({ items, locale }: { items: string[]; locale: Locale }) {
  return (
    <div className="flex flex-wrap gap-2">
      {items.map((item) => (
        <span
          key={item}
          className="rounded-md border border-border-warm bg-cream-dark px-2 py-1 text-xs text-ink-muted"
        >
          {formatDisplayValue(locale, item)}
        </span>
      ))}
    </div>
  );
}
