import { notFound } from "next/navigation";
import { AppShell } from "@/components/layout/AppShell";
import { HtmlLang } from "@/components/layout/HtmlLang";
import { isValidLocale, getDictionary, type Locale } from "@/lib/i18n";
import { getDataMeta } from "@/lib/data/loader";

export function generateMetadata({ params }: { params: { locale: string } }) {
  if (!isValidLocale(params.locale)) return {};
  const t = getDictionary(params.locale);
  return {
    title: t.site.title,
    description: t.site.tagline,
  };
}

export default function LocaleLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: { locale: string };
}) {
  if (!isValidLocale(params.locale)) notFound();

  const locale = params.locale as Locale;
  const meta = getDataMeta();

  return (
    <>
      <HtmlLang locale={locale} />
      <AppShell
      locale={locale}
      loadedAt={meta.loadedAt}
      warningCount={meta.warnings.length}
    >
      {children}
    </AppShell>
    </>
  );
}

export function generateStaticParams() {
  return [{ locale: "en" }, { locale: "zh" }];
}
