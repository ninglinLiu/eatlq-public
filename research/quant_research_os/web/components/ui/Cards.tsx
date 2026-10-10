import Link from "next/link";
import {
  AuthorityBadge,
  AuthorshipBadge,
  LiveProvenBadge,
  StatusBadge,
} from "./Badges";
import { getDictionary, localePath, type Locale } from "@/lib/i18n";
import { tLabel, formatFilterOption } from "@/lib/i18n/labels";
import { formatObjectId, getDisplayStatement } from "@/lib/i18n/zh-display";
import type { ObjectType } from "@/lib/data/types";

type ObjectCardProps = {
  locale: Locale;
  type: ObjectType;
  id: string;
  obj: Record<string, unknown>;
  authority?: string;
  status?: string;
  authorship?: string | string[];
  liveProven?: boolean;
  executionMode?: string;
  meta?: Array<{ label: string; value: string }>;
  highlight?: boolean;
};

export function ObjectCard({
  locale,
  type,
  id,
  obj,
  authority,
  status,
  authorship,
  liveProven,
  executionMode,
  meta,
  highlight,
}: ObjectCardProps) {
  const t = getDictionary(locale);

  return (
    <Link
      href={localePath(locale, `/object/${type}/${id}`)}
      className={`block rounded-xl border bg-white p-5 transition-shadow hover:shadow-sm ${
        highlight
          ? "border-accent/40 ring-1 ring-accent/20"
          : "border-border-warm"
      }`}
    >
      {highlight && (
        <p className="mb-2 text-xs text-accent">{t.site.highlightNote}</p>
      )}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {authority && <AuthorityBadge level={authority} locale={locale} />}
        {status && status !== "unknown" && (
          <StatusBadge status={status} locale={locale} />
        )}
        {executionMode && executionMode !== "UNKNOWN" && (
          <StatusBadge status={executionMode} locale={locale} />
        )}
        {liveProven !== undefined && (
          <LiveProvenBadge proven={liveProven} locale={locale} />
        )}
        {authorship && (
          <AuthorshipBadge authorship={authorship} locale={locale} />
        )}
      </div>
      <p className="mb-2 text-xs text-ink-faint">
        {formatObjectId(locale, id)}
      </p>
      <p className="text-[15px] leading-relaxed text-ink">
        {getDisplayStatement(locale, obj, type)}
      </p>
      {meta && meta.length > 0 && (
        <dl className="mt-4 grid grid-cols-1 gap-y-2 text-xs sm:grid-cols-2 sm:gap-x-4">
          {meta.map((m) => (
            <div key={m.label}>
              <dt className="text-ink-faint">{m.label}</dt>
              <dd className="mt-0.5 text-ink-muted">{m.value}</dd>
            </div>
          ))}
        </dl>
      )}
    </Link>
  );
}

export function StatCard({
  label,
  value,
  sub,
  warn,
}: {
  label: string;
  value: string | number;
  sub?: string;
  warn?: boolean;
}) {
  return (
    <div className="rounded-xl border border-border-warm bg-white p-5">
      <p className="text-xs font-medium leading-snug text-ink-faint">{label}</p>
      <p className="mt-2 text-2xl font-semibold text-ink">{value}</p>
      {sub && (
        <p
          className={`mt-1 text-xs leading-relaxed ${warn ? "text-accent" : "text-ink-faint"}`}
        >
          {sub}
        </p>
      )}
    </div>
  );
}

export function SectionHeader({
  title,
  description,
}: {
  title: string;
  description?: string;
}) {
  return (
    <header className="mb-8">
      <h1 className="text-2xl font-semibold tracking-tight text-ink">{title}</h1>
      {description && (
        <p className="mt-2 max-w-3xl text-sm leading-relaxed text-ink-muted">
          {description}
        </p>
      )}
    </header>
  );
}

export function Notice({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-6 rounded-xl border border-accent/30 bg-accent-soft/50 p-4">
      <p className="text-sm font-medium text-accent">{title}</p>
      <div className="mt-1 text-sm leading-relaxed text-ink-muted">{children}</div>
    </div>
  );
}

export function FilterSelect({
  label,
  name,
  options,
  defaultValue,
  locale = "en",
  booleanLiveProven,
}: {
  label: string;
  name: string;
  options: string[];
  defaultValue?: string;
  locale?: Locale;
  booleanLiveProven?: boolean;
}) {
  const t = getDictionary(locale);

  function optionLabel(o: string): string {
    if (booleanLiveProven) {
      return o === "true"
        ? locale === "zh"
          ? "是（实盘已验证）"
          : "Yes (live proven)"
        : locale === "zh"
          ? "否（未经实盘验证）"
          : "No (not live proven)";
    }
    return formatFilterOption(locale, o);
  }

  return (
    <label className="flex min-w-[140px] flex-col gap-1 text-xs">
      <span className="text-ink-faint">{label}</span>
      <select
        name={name}
        defaultValue={defaultValue ?? ""}
        className="rounded-md border border-border-warm bg-white px-2 py-1.5 text-sm text-ink"
      >
        <option value="">{t.site.filterAll}</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {optionLabel(o)}
          </option>
        ))}
      </select>
    </label>
  );
}

export function InfoBox({ children }: { children: React.ReactNode }) {
  return (
    <div className="mb-8 rounded-xl border border-border-warm bg-white p-5 text-sm leading-relaxed text-ink-muted">
      {children}
    </div>
  );
}
