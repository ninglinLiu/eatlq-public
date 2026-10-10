"use client";

import { usePathname } from "next/navigation";
import { Sidebar, MobileNav } from "@/components/layout/Sidebar";
import { SearchBar } from "@/components/layout/SearchBar";
import { getDictionary, type Locale } from "@/lib/i18n";
import { tWarnings } from "@/lib/i18n/labels";

export function AppShell({
  locale,
  loadedAt,
  warningCount,
  children,
}: {
  locale: Locale;
  loadedAt: string;
  warningCount: number;
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const t = getDictionary(locale);

  return (
    <div className="flex min-h-screen">
      <Sidebar locale={locale} currentPath={pathname} />
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-10 border-b border-border-warm bg-cream/95 backdrop-blur-sm">
          <div className="flex flex-col gap-3 px-6 py-4 sm:flex-row sm:items-center sm:justify-between">
            <div className="lg:hidden">
              <p className="text-sm font-semibold text-ink">{t.site.title}</p>
              <p className="text-xs text-ink-faint">{t.site.mixedAuthority}</p>
            </div>
            <SearchBar locale={locale} />
          </div>
          <MobileNav locale={locale} />
        </header>
        <main className="flex-1 px-6 py-8 lg:px-10">{children}</main>
        <footer className="border-t border-border-warm px-6 py-4 text-xs text-ink-faint">
          {t.site.dataUpdated}: {loadedAt}
          {warningCount > 0 && (
            <span className="ml-2 text-accent">
              {locale === "zh"
                ? `（${tWarnings(locale, warningCount)}）`
                : `(${tWarnings(locale, warningCount)})`}
            </span>
          )}
        </footer>
      </div>
    </div>
  );
}
