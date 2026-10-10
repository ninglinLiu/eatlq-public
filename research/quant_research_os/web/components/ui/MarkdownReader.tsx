import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Locale } from "@/lib/i18n";
import { getDictionary } from "@/lib/i18n";

export function MarkdownReader({
  content,
  locale = "en",
}: {
  content: string;
  locale?: Locale;
}) {
  const t = getDictionary(locale);

  if (!content.trim()) {
    return <p className="text-ink-faint italic">{t.site.noContent}</p>;
  }

  return (
    <article className="prose-reader">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </article>
  );
}
