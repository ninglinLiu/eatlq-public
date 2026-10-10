import {
  ObjectCard,
  SectionHeader,
  Notice,
  FilterSelect,
  InfoBox,
} from "@/components/ui/Cards";
import {
  loadGlobalCollection,
  getObjectId,
  getObjectTitle,
} from "@/lib/data/loader";
import { GLOBAL_JSONL } from "@/lib/data/paths";
import { filterObjects, uniqueFieldValues } from "@/lib/data/filters";
import { getDictionary, getPageIntro, type Locale } from "@/lib/i18n";
import { formatDisplayValue, tListCount } from "@/lib/i18n/labels";
import { localizeObjects } from "@/lib/i18n/localize";
import { sortConclusionsForZh } from "@/lib/i18n/zh-display";
import type { ObjectType } from "@/lib/data/types";

type ListConfig = {
  file: string;
  type: ObjectType;
  titleKey: string;
  pageKey: string;
  searchFields: (obj: Record<string, unknown>) => string[];
  getMeta?: (
    obj: Record<string, unknown>,
    locale: Locale
  ) => Array<{ label: string; value: string }>;
  filterFields?: readonly (
    | "authority"
    | "status"
    | "execution_mode"
    | "live_proven"
    | "project_scope"
    | "regime_dependency"
    | "persistence_scope"
  )[];
  notice?: {
    titleKey: keyof ReturnType<typeof getDictionary>["site"];
    bodyKey: keyof ReturnType<typeof getDictionary>["site"];
    zhOnly?: boolean;
  };
  highlightFn?: (obj: Record<string, unknown>) => boolean;
};

const HIGHLIGHT_KEYWORDS = [
  "quoter",
  "sellability",
  "ath",
  "mfe",
  "state override",
  "honeypot",
  "pipeline stall",
  "authorization",
  "backtest",
  "survivor",
  "operational definition",
];

function defaultHighlight(obj: Record<string, unknown>): boolean {
  const text = JSON.stringify(obj).toLowerCase();
  return HIGHLIGHT_KEYWORDS.some((k) => text.includes(k));
}

export async function ObjectListPage({
  locale,
  config,
  searchParams,
}: {
  locale: Locale;
  config: ListConfig;
  searchParams: Record<string, string | undefined>;
}) {
  const t = getDictionary(locale);
  const items = loadGlobalCollection(config.file);
  const filtered = filterObjects(items, searchParams, config.searchFields);

  let displayItems = filtered;
  if (locale === "zh") {
    if (config.type === "conclusion") {
      displayItems = sortConclusionsForZh(filtered);
    }
    displayItems = await localizeObjects(locale, displayItems, config.type);
  }

  const intro = getPageIntro(locale, config.pageKey);

  const authorityOpts = uniqueFieldValues(items, "authority_level");
  const statusOpts = uniqueFieldValues(items, "status");
  const execOpts = uniqueFieldValues(items, "execution_mode");
  const regimeOpts = uniqueFieldValues(items, "regime_dependency");
  const persistOpts = uniqueFieldValues(items, "persistence_scope");
  const projectOpts = uniqueFieldValues(items, "project_scope");

  const highlightFn = config.highlightFn ?? defaultHighlight;

  return (
    <div>
      <SectionHeader
        title={t.nav[config.titleKey]}
        description={tListCount(locale, filtered.length, items.length)}
      />

      {intro && <InfoBox>{intro}</InfoBox>}

      {config.notice && (!config.notice.zhOnly || locale === "zh") && (
        <Notice title={t.site[config.notice.titleKey]}>
          {t.site[config.notice.bodyKey]}
        </Notice>
      )}

      <form className="mb-8 flex flex-wrap gap-4 rounded-xl border border-border-warm bg-white p-4">
        <input
          type="search"
          name="q"
          defaultValue={searchParams.q}
          placeholder={t.site.searchPlaceholder}
          className="min-w-[200px] flex-1 rounded-md border border-border-warm px-3 py-1.5 text-sm"
        />
        {config.filterFields?.includes("authority") && (
          <FilterSelect
            locale={locale}
            label={t.fields.authority}
            name="authority"
            options={authorityOpts}
            defaultValue={searchParams.authority}
          />
        )}
        {config.filterFields?.includes("status") && (
          <FilterSelect
            locale={locale}
            label={t.fields.status}
            name="status"
            options={statusOpts}
            defaultValue={searchParams.status}
          />
        )}
        {config.filterFields?.includes("execution_mode") && (
          <FilterSelect
            locale={locale}
            label={t.fields.executionMode}
            name="execution_mode"
            options={execOpts}
            defaultValue={searchParams.execution_mode}
          />
        )}
        {config.filterFields?.includes("live_proven") && (
          <FilterSelect
            locale={locale}
            label={t.fields.liveProven}
            name="live_proven"
            options={["true", "false"]}
            defaultValue={searchParams.live_proven}
            booleanLiveProven
          />
        )}
        {config.filterFields?.includes("project_scope") && (
          <FilterSelect
            locale={locale}
            label={t.fields.projectScope}
            name="project_scope"
            options={projectOpts}
            defaultValue={searchParams.project_scope}
          />
        )}
        {config.filterFields?.includes("regime_dependency") && (
          <FilterSelect
            locale={locale}
            label={t.fields.regimeDependency}
            name="regime_dependency"
            options={regimeOpts}
            defaultValue={searchParams.regime_dependency}
          />
        )}
        {config.filterFields?.includes("persistence_scope") && (
          <FilterSelect
            locale={locale}
            label={t.fields.persistenceScope}
            name="persistence_scope"
            options={persistOpts}
            defaultValue={searchParams.persistence_scope}
          />
        )}
        <button
          type="submit"
          className="self-end rounded-md bg-accent px-4 py-1.5 text-sm text-white"
        >
          {t.site.filters}
        </button>
      </form>

      <div className="grid gap-4">
        {displayItems.map((obj) => (
          <ObjectCard
            key={getObjectId(obj)}
            locale={locale}
            type={config.type}
            id={getObjectId(obj)}
            obj={obj}
            authority={obj.authority_level as string}
            status={obj.status as string}
            authorship={obj.authorship as string | string[]}
            executionMode={obj.execution_mode as string}
            liveProven={obj.live_proven as boolean | undefined}
            meta={config.getMeta?.(obj, locale)}
            highlight={highlightFn(obj)}
          />
        ))}
      </div>
    </div>
  );
}

function metaField(
  locale: Locale,
  label: string,
  value: unknown
): { label: string; value: string } {
  return { label, value: formatDisplayValue(locale, value) };
}

function isEmptyMetaValue(value: string): boolean {
  return value === "—" || value === "unknown" || value === "未知";
}

export const LIST_CONFIGS = {
  conclusions: {
    file: GLOBAL_JSONL.conclusions,
    type: "conclusion" as const,
    titleKey: "conclusions" as const,
    pageKey: "conclusions",
    notice: {
      titleKey: "dataQualityNotice" as const,
      bodyKey: "conclusionsAutoCurated" as const,
      zhOnly: true,
    },
    filterFields: [
      "authority",
      "status",
      "execution_mode",
      "live_proven",
      "project_scope",
      "regime_dependency",
      "persistence_scope",
    ] as const,
    searchFields: (obj: Record<string, unknown>) => [
      String(obj.statement ?? ""),
      String(obj.id ?? ""),
      String(obj.project_scope ?? ""),
    ],
    getMeta: (obj: Record<string, unknown>, locale: Locale) => {
      const t = getDictionary(locale);
      return [
        metaField(locale, t.fields.executionMode, obj.execution_mode),
        metaField(locale, t.fields.liveProven, obj.live_proven),
        metaField(locale, t.fields.validFrom, obj.valid_from),
        metaField(locale, t.fields.validUntil, obj.valid_until),
        metaField(locale, t.fields.regimeDependency, obj.regime_dependency),
        metaField(locale, t.fields.persistenceScope, obj.persistence_scope),
        metaField(locale, t.fields.evidenceLevel, obj.evidence_level),
        metaField(locale, t.fields.projectScope, obj.project_scope),
      ].filter((m) => !isEmptyMetaValue(m.value));
    },
  },
  hypotheses: {
    file: GLOBAL_JSONL.hypotheses,
    type: "hypothesis" as const,
    titleKey: "hypotheses" as const,
    pageKey: "hypotheses",
    filterFields: ["authority", "status"] as const,
    searchFields: (obj: Record<string, unknown>) => [
      String(obj.statement ?? ""),
      String(obj.operational_definition ?? ""),
      String(obj.id ?? ""),
    ],
    getMeta: (obj: Record<string, unknown>, locale: Locale) => {
      const t = getDictionary(locale);
      return [
        metaField(locale, t.fields.operationalDefinition, obj.operational_definition),
        metaField(locale, t.fields.origin, obj.origin),
        metaField(locale, t.fields.regimeDependency, obj.regime_dependency),
        metaField(locale, t.fields.validFrom, obj.valid_from),
        metaField(locale, t.fields.epistemicRole, obj.epistemic_role),
      ].filter((m) => !isEmptyMetaValue(m.value));
    },
  },
  experiments: {
    file: GLOBAL_JSONL.experiments,
    type: "experiment" as const,
    titleKey: "experiments" as const,
    pageKey: "experiments",
    notice: {
      titleKey: "dataQualityNotice" as const,
      bodyKey: "experimentsDedup" as const,
    },
    filterFields: ["authority"] as const,
    searchFields: (obj: Record<string, unknown>) => [
      String(obj.question ?? ""),
      String(obj.id ?? ""),
      String(obj.hypothesis_id ?? ""),
    ],
    getMeta: (obj: Record<string, unknown>, locale: Locale) => {
      const t = getDictionary(locale);
      return [
        metaField(locale, t.fields.hypothesisId, obj.hypothesis_id),
        metaField(locale, t.fields.universe, obj.universe),
        metaField(locale, t.fields.sampleSize, obj.sample_size),
        metaField(locale, t.fields.timeRange, obj.time_range),
        metaField(locale, t.fields.entry, obj.entry),
        metaField(locale, t.fields.exit, obj.exit),
        metaField(locale, t.fields.costModel, obj.cost_model),
        metaField(locale, t.fields.trainTestProtocol, obj.train_test_protocol),
        metaField(locale, t.fields.result, obj.result),
        metaField(locale, t.fields.executionMode, obj.execution_mode),
      ].filter((m) => !isEmptyMetaValue(m.value));
    },
  },
  decisions: {
    file: GLOBAL_JSONL.decisions,
    type: "decision" as const,
    titleKey: "decisions" as const,
    pageKey: "decisions",
    filterFields: ["authority"] as const,
    searchFields: (obj: Record<string, unknown>) => [
      String(obj.decision ?? ""),
      String(obj.id ?? ""),
      String(obj.execution_status ?? ""),
    ],
    getMeta: (obj: Record<string, unknown>, locale: Locale) => {
      const t = getDictionary(locale);
      return [
        metaField(locale, t.fields.executionStatus, obj.execution_status),
        metaField(locale, t.site.executionConfirmed, obj.execution_confirmed),
        metaField(locale, t.fields.executionMode, obj.execution_mode),
      ].filter((m) => !isEmptyMetaValue(m.value));
    },
  },
  theses: {
    file: GLOBAL_JSONL.user_theses,
    type: "user_thesis" as const,
    titleKey: "userTheses" as const,
    pageKey: "theses",
    filterFields: ["authority", "persistence_scope"] as const,
    searchFields: (obj: Record<string, unknown>) => [
      String(obj.statement ?? ""),
      String(obj.epistemic_role ?? ""),
    ],
    getMeta: (obj: Record<string, unknown>, locale: Locale) => {
      const t = getDictionary(locale);
      return [
        metaField(locale, t.fields.epistemicRole, obj.epistemic_role),
        metaField(locale, t.fields.persistenceScope, obj.persistence_scope),
        metaField(locale, t.fields.reusePolicy, obj.reuse_policy),
        metaField(locale, t.fields.validFrom, obj.valid_from),
        metaField(locale, t.fields.validUntil, obj.valid_until),
      ].filter((m) => !isEmptyMetaValue(m.value));
    },
  },
  principles: {
    file: GLOBAL_JSONL.research_principles,
    type: "research_principle" as const,
    titleKey: "researchPrinciples" as const,
    pageKey: "principles",
    filterFields: ["authority"] as const,
    searchFields: (obj: Record<string, unknown>) => [
      String(obj.statement ?? ""),
      String(obj.id ?? ""),
    ],
    getMeta: (obj: Record<string, unknown>, locale: Locale) => {
      const t = getDictionary(locale);
      return [
        metaField(locale, t.fields.reusePolicy, obj.reuse_policy),
        metaField(locale, t.fields.persistenceScope, obj.persistence_scope),
        metaField(locale, t.fields.epistemicRole, obj.epistemic_role),
      ].filter((m) => !isEmptyMetaValue(m.value));
    },
  },
  methods: {
    file: GLOBAL_JSONL.methods,
    type: "method" as const,
    titleKey: "methods" as const,
    pageKey: "methods",
    filterFields: ["authority"] as const,
    searchFields: (obj: Record<string, unknown>) => [
      String(obj.name ?? ""),
      String(obj.description ?? ""),
    ],
    getMeta: (obj: Record<string, unknown>, locale: Locale) => {
      const t = getDictionary(locale);
      return [
        metaField(locale, t.fields.whenToUse, obj.when_to_use),
        metaField(locale, t.fields.description, obj.description),
        metaField(locale, t.fields.reusePolicy, obj.reuse_policy),
      ].filter((m) => !isEmptyMetaValue(m.value));
    },
  },
};
