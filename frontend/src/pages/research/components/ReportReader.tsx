import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface ReportReaderProps { content: string; }

function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^\w\s-]/g, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export function ReportReader({ content }: ReportReaderProps) {
  const headings = content.match(/^#{1,3}\s.+$/gm) ?? [];

  return (
    <div className="flex gap-6">
      {headings.length > 0 && (
        <nav className="hidden lg:block w-48 shrink-0">
          <div className="text-xs uppercase tracking-wider text-text-muted mb-2">Contents</div>
          <ul className="space-y-1">
            {headings.map((h, i) => {
              const level = h.match(/^#+/)?.[0].length ?? 1;
              const text = h.replace(/^#+\s/, "");
              return (
                <li key={i} style={{ paddingLeft: `${(level - 1) * 12}px` }}>
                  <a
                    href={`#${slugify(text)}`}
                    className="text-xs text-text-secondary hover:text-accent"
                    onClick={(e) => {
                      e.preventDefault();
                      const el = document.getElementById(slugify(text));
                      el?.scrollIntoView({ behavior: "smooth" });
                    }}
                  >
                    {text}
                  </a>
                </li>
              );
            })}
          </ul>
        </nav>
      )}
      <div className="prose prose-invert prose-sm max-w-none">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
            code: ({ className, children, ...props }) => {
              const isInline = !className;
              if (isInline) return <code className="bg-surface-2 px-1 py-0.5 rounded text-xs">{children}</code>;
              return (
                <div className="relative">
                  <pre className="bg-surface-2 p-4 rounded-md overflow-x-auto text-xs">
                    <code className={className} {...props}>{children}</code>
                  </pre>
                </div>
              );
            },
          }}
        >
          {content}
        </ReactMarkdown>
      </div>
    </div>
  );
}
