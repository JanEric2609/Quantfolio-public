import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "../../lib/utils";

/**
 * Renders LLM-generated prose as markdown with the app's typography.
 * Use this instead of dropping model output into a bare <p>/<span>, where
 * any markdown the model emits (**bold**, lists, headings) shows up as
 * raw syntax.
 */
export function LlmProse({ text, className }: { text: string; className?: string }) {
  return (
    <div className={cn("prose prose-sm max-w-none", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}
