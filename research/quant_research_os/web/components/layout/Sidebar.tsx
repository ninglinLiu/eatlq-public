"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { getDictionary, localePath, type Locale } from "@/lib/i18n";

const NAV = [
  { href: "", labelKey: "overview" as const, section: null },
  {
    section: "knowledge" as const,
    items: [
      { href: "/conclusions", labelKey: "conclusions" as const },
      { href: "/hypotheses", labelKey: "hypotheses" as const },
      { href: "/experiments", labelKey: "experiments" as const },
      { href: "/failures", labelKey: "failuresGuardrails" as const },
      { href: "/methods", labelKey: "methods" as const },
      { href: "/decisions", labelKey: "decisions" as const },
    ],
  },
  {
    section: "thinking" as const,
    items: [
      { href: "/theses", labelKey: "userTheses" as const },
      { href: "/principles", labelKey: "researchPrinciples" as const },
    ],
  },
  {
    section: "history" as const,
    items: [
      { href: "/timeline", labelKey: "timeline" as const },
      { href: "/monthly", labelKey: "monthly" as const },
      { href: "/open-questions", labelKey: "openQuestions" as const },
    ],
  },
  {
    section: "system" as const,
    items: [
      { href: "/current-state", labelKey: "currentState" as const },
      { href: "/authority", labelKey: "authorityModel" as const },
      { href: "/production-status", labelKey: "productionStatus" as const },
    ],
  },
];

export function Sidebar({
  locale,
  currentPath,
}: {
  locale: Locale;
  currentPath: string;
}) {
  const t = getDictionary(locale);

  return (
    <aside className="hidden w-56 shrink-0 border-r border-border-warm bg-cream/80 lg:block">
      <div className="sticky top-0 flex h-screen flex-col overflow-y-auto px-4 py-8">
        <Link
          href={localePath(locale)}
          className="mb-1 text-sm font-semibold text-ink"
        >
          {t.site.title}
        </Link>
        <p className="mb-8 text-xs text-ink-faint">{t.site.tagline}</p>

        <nav className="space-y-6 text-sm">
          <div>
            <Link
              href={localePath(locale)}
              className={`block rounded-md px-2 py-1.5 ${
                currentPath === `/${locale}` || currentPath === `/${locale}/`
                  ? "bg-accent-soft font-medium text-accent"
                  : "text-ink-muted hover:bg-cream-dark hover:text-ink"
              }`}
            >
              {t.nav.overview}
            </Link>
          </div>

          {NAV.filter((n) => n.section).map((group) => (
            <div key={group.section}>
              <p className="mb-2 px-2 text-xs font-medium uppercase tracking-wider text-ink-faint">
                {t.nav[group.section!]}
              </p>
              <ul className="space-y-0.5">
                {group.items!.map((item) => {
                  const href = localePath(locale, item.href);
                  const active = currentPath.startsWith(href);
                  return (
                    <li key={item.href}>
                      <Link
                        href={href}
                        className={`block rounded-md px-2 py-1.5 ${
                          active
                            ? "bg-accent-soft font-medium text-accent"
                            : "text-ink-muted hover:bg-cream-dark hover:text-ink"
                        }`}
                      >
                        {t.nav[item.labelKey]}
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </nav>

        <div className="mt-auto pt-8">
          <LocaleSwitcher locale={locale} />
        </div>
      </div>
    </aside>
  );
}

function LocaleSwitcher({ locale }: { locale: Locale }) {
  const pathname = usePathname();
  const other = locale === "en" ? "zh" : "en";
  const label = locale === "en" ? "中文" : "英文";
  const switched = pathname.replace(`/${locale}`, `/${other}`);

  return (
    <Link
      href={switched}
      className="block rounded-md border border-border-warm bg-white px-3 py-2 text-center text-xs text-ink-muted hover:border-accent-muted hover:text-accent"
    >
      {label}
    </Link>
  );
}

export function MobileNav({ locale }: { locale: Locale }) {
  const t = getDictionary(locale);
  return (
    <div className="flex gap-2 overflow-x-auto border-b border-border-warm bg-cream px-4 py-2 text-xs lg:hidden">
      {[
        { href: "", label: t.nav.overview },
        { href: "/conclusions", label: t.nav.conclusions },
        { href: "/failures", label: t.nav.failuresGuardrails },
        { href: "/timeline", label: t.nav.timeline },
        { href: "/open-questions", label: t.nav.openQuestions },
      ].map((item) => (
        <Link
          key={item.href}
          href={localePath(locale, item.href)}
          className="whitespace-nowrap rounded-full border border-border-warm bg-white px-3 py-1 text-ink-muted"
        >
          {item.label}
        </Link>
      ))}
    </div>
  );
}
