import { notFound } from "next/navigation";
import { ObjectDetailView } from "@/components/ui/ObjectDetail";
import { findObjectById } from "@/lib/data/loader";
import type { Locale } from "@/lib/i18n";
import type { ObjectType } from "@/lib/data/types";

const VALID_TYPES: ObjectType[] = [
  "user_thesis",
  "research_principle",
  "hypothesis",
  "experiment",
  "conclusion",
  "failure",
  "method",
  "decision",
  "guardrail",
];

import { localizeCanonicalObject } from "@/lib/i18n/localize";

export default async function ObjectPage({
  params,
}: {
  params: { locale: Locale; type: string; id: string };
}) {
  const type = params.type as ObjectType;
  if (!VALID_TYPES.includes(type)) notFound();

  const obj = findObjectById(type, params.id);
  if (!obj) notFound();

  const localized = await localizeCanonicalObject(params.locale, obj);

  return (
    <ObjectDetailView locale={params.locale} type={type} obj={localized} />
  );
}
