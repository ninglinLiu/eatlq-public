import { FailuresPageClient } from "@/components/pages/FailuresPageClient";
import { loadGlobalCollection } from "@/lib/data/loader";
import { GLOBAL_JSONL } from "@/lib/data/paths";
import type { Locale } from "@/lib/i18n";

import { localizeObjects } from "@/lib/i18n/localize";

export default async function Page({ params }: { params: { locale: Locale } }) {
  const failures = loadGlobalCollection(GLOBAL_JSONL.failures);
  const guardrails = loadGlobalCollection(GLOBAL_JSONL.permanent_guardrails);

  const [localizedFailures, localizedGuardrails] =
    params.locale === "zh"
      ? await Promise.all([
          localizeObjects(params.locale, failures),
          localizeObjects(params.locale, guardrails),
        ])
      : [failures, guardrails];

  return (
    <FailuresPageClient
      locale={params.locale}
      failures={localizedFailures}
      guardrails={localizedGuardrails}
    />
  );
}
