import { cn } from "@/lib/utils/cn";
import type { Locale } from "@/lib/i18n";
import {
  tAuthority,
  tStatus,
  tAuthorship,
  tEpistemic,
  tLiveProven,
} from "@/lib/i18n/labels";

const AUTHORITY_STYLES: Record<string, string> = {
  GOLD: "bg-amber-100 text-amber-900 border-amber-200",
  VALIDATED: "bg-emerald-50 text-emerald-800 border-emerald-200",
  AUTO_CURATED: "bg-stone-100 text-stone-700 border-stone-200",
  ARTIFACT_ONLY: "bg-orange-50 text-orange-800 border-orange-200",
  UNVERIFIED: "bg-gray-100 text-gray-600 border-gray-200",
};

export function AuthorityBadge({
  level,
  locale = "en",
  className,
}: {
  level?: string;
  locale?: Locale;
  className?: string;
}) {
  const key = (level ?? "UNKNOWN").toUpperCase();
  const style =
    AUTHORITY_STYLES[key] ?? "bg-cream-dark text-ink-muted border-border-warm";
  const label = tAuthority(locale, key);

  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium tracking-wide",
        style,
        className
      )}
      title={locale === "en" ? key : undefined}
    >
      {label}
    </span>
  );
}

const STATUS_STYLES: Record<string, string> = {
  LIVE: "bg-red-50 text-red-800 border-red-200",
  LIVE_ENABLED: "bg-red-50 text-red-800 border-red-200",
  LIVE_ORDER_SENT: "bg-red-100 text-red-900 border-red-300",
  LIVE_FILL_CONFIRMED: "bg-red-200 text-red-950 border-red-400 font-semibold",
  PAPER: "bg-sky-50 text-sky-800 border-sky-200",
  PAPER_FORWARD: "bg-sky-50 text-sky-800 border-sky-200",
  BACKTEST: "bg-indigo-50 text-indigo-800 border-indigo-200",
  PAUSED: "bg-yellow-50 text-yellow-800 border-yellow-200",
  STOPPED: "bg-stone-100 text-stone-700 border-stone-200",
  PANIC: "bg-red-100 text-red-900 border-red-300 font-semibold",
  ACTIVE: "bg-emerald-50 text-emerald-800 border-emerald-200",
  SUPPORTED: "bg-emerald-50 text-emerald-800 border-emerald-200",
  WEAK_EVIDENCE: "bg-yellow-50 text-yellow-700 border-yellow-200",
  REJECTED: "bg-stone-100 text-stone-600 border-stone-200",
  INVALIDATED: "bg-orange-50 text-orange-800 border-orange-200",
  SUPERSEDED: "bg-stone-100 text-stone-500 border-stone-200 line-through",
  PROPOSED: "bg-cream-dark text-ink-muted border-border-warm",
  AUTHORIZED: "bg-amber-50 text-amber-800 border-amber-200",
  STARTED: "bg-blue-50 text-blue-800 border-blue-200",
  COMPLETED: "bg-emerald-50 text-emerald-800 border-emerald-200",
  FAILED: "bg-red-50 text-red-800 border-red-200",
  OPEN: "bg-accent-soft text-accent border-accent-muted/30",
  PARTIALLY_RESOLVED: "bg-yellow-50 text-yellow-800 border-yellow-200",
  RESOLVED: "bg-stone-100 text-stone-500 border-stone-200",
  VALIDATED: "bg-emerald-50 text-emerald-800 border-emerald-200",
  GOLD: "bg-amber-100 text-amber-900 border-amber-200",
  AUTO_CURATED: "bg-stone-100 text-stone-700 border-stone-200",
  ARTIFACTS_ONLY: "bg-orange-50 text-orange-800 border-orange-200",
  EMPTY_CONFIRMED: "bg-stone-50 text-stone-500 border-stone-200",
  UNVERIFIED: "bg-gray-100 text-gray-600 border-gray-200",
  UNKNOWN: "bg-cream-dark text-ink-faint border-border-warm",
};

export function StatusBadge({
  status,
  locale = "en",
  className,
  large,
}: {
  status?: string;
  locale?: Locale;
  className?: string;
  large?: boolean;
}) {
  const key = (status ?? "UNKNOWN").toUpperCase();
  const style =
    STATUS_STYLES[key] ?? "bg-cream-dark text-ink-muted border-border-warm";

  const label = tStatus(locale, key);

  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md border font-medium tracking-wide",
        large ? "px-3 py-1 text-sm" : "px-2 py-0.5 text-xs",
        style,
        className
      )}
      title={locale === "en" ? key : undefined}
    >
      {label}
    </span>
  );
}

export function AuthorshipBadge({
  authorship,
  locale = "en",
}: {
  authorship?: string | string[];
  locale?: Locale;
}) {
  const value = Array.isArray(authorship) ? authorship.join(", ") : authorship;
  const isUser =
    value?.includes("USER") ||
    value === "USER_EXPLICIT" ||
    value === "USER_BELIEF";

  return (
    <span
      className={cn(
        "inline-flex rounded-md border px-2 py-0.5 text-xs font-medium",
        isUser
          ? "border-accent/30 bg-accent-soft text-accent"
          : "border-border-warm bg-white text-ink-muted"
      )}
      title={locale === "en" ? value : undefined}
    >
      {tAuthorship(locale, authorship)}
    </span>
  );
}

export function EpistemicBadge({
  role,
  locale = "en",
}: {
  role?: string;
  locale?: Locale;
}) {
  const isUserBelief = role === "USER_BELIEF";
  const isObjection = role === "USER_OBJECTION";
  const isMeta = role === "META_RESEARCH_REFLECTION";

  return (
    <span
      className={cn(
        "inline-flex rounded-md border px-2 py-0.5 text-xs font-medium",
        isUserBelief && "border-accent/40 bg-accent-soft text-accent",
        isObjection && "border-orange-200 bg-orange-50 text-orange-800",
        isMeta && "border-indigo-200 bg-indigo-50 text-indigo-800",
        !isUserBelief &&
          !isObjection &&
          !isMeta &&
          "border-border-warm bg-white text-ink-muted"
      )}
      title={locale === "en" ? role : undefined}
    >
      {tEpistemic(locale, role)}
    </span>
  );
}

export function LiveProvenBadge({
  proven,
  locale = "en",
}: {
  proven?: boolean;
  locale?: Locale;
}) {
  if (proven === true) {
    return (
      <span className="inline-flex rounded-md border border-red-200 bg-red-50 px-2 py-0.5 text-xs font-medium text-red-800">
        {tLiveProven(locale, true)}
      </span>
    );
  }
  if (proven === false) {
    return (
      <span className="inline-flex rounded-md border border-border-warm bg-white px-2 py-0.5 text-xs text-ink-faint">
        {tLiveProven(locale, false)}
      </span>
    );
  }
  return <StatusBadge status="UNKNOWN" locale={locale} />;
}
