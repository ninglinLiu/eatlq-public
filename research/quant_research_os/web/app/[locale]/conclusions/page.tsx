import { ObjectListPage, LIST_CONFIGS } from "@/components/pages/ObjectListPage";
import type { Locale } from "@/lib/i18n";

export default function Page({
  params,
  searchParams,
}: {
  params: { locale: Locale };
  searchParams: Record<string, string | undefined>;
}) {
  return (
    <ObjectListPage
      locale={params.locale}
      config={LIST_CONFIGS.conclusions}
      searchParams={searchParams}
    />
  );
}
