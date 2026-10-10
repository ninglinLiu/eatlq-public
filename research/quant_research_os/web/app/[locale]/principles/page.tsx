import { ObjectListPage, LIST_CONFIGS } from "@/components/pages/ObjectListPage";
import type { Locale } from "@/lib/i18n";

export default function Page({
  params,
  searchParams,
}: {
  params: { locale: Locale };
  searchParams: Record<string, string | undefined>;
}) {
  const config = {
    ...LIST_CONFIGS.principles,
    filterFields: searchParams.always_load
      ? (["authority"] as const)
      : LIST_CONFIGS.principles.filterFields,
  };
  const sp = searchParams.always_load
    ? { ...searchParams, always_load: "1" }
    : searchParams;

  return (
    <ObjectListPage locale={params.locale} config={config} searchParams={sp} />
  );
}
