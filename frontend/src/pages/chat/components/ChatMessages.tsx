import { useEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessageItem } from "../../../lib/api";
import { cn } from "../../../lib/utils";
import { Bot, User } from "lucide-react";

interface ChatMessagesProps {
  messages: ChatMessageItem[];
  streaming: boolean;
}

export function ChatMessages({ messages, streaming }: ChatMessagesProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  return (
    <div className="space-y-4">
      {messages.map((msg) => (
        <div
          key={msg.id}
          className={cn(
            "flex gap-3",
            msg.role === "user" ? "justify-end" : "justify-start",
          )}
        >
          {msg.role === "assistant" && (
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent/10">
              <Bot className="h-4 w-4 text-accent" />
            </div>
          )}
          <div
            className={cn(
              "min-w-0 max-w-[85%] break-words rounded-lg p-3 text-sm sm:max-w-[80%]",
              msg.role === "user"
                ? "bg-accent text-accent-fg"
                : "bg-surface-2 border border-border",
            )}
          >
            {msg.role === "assistant" ? (
              <div className="prose prose-invert prose-sm max-w-none [&_table]:block [&_table]:overflow-x-auto">
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={{
                    a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
                    code: ({ className, children, ...props }) => {
                      const isInline = !className;
                      if (isInline) return <code className="bg-surface-3 px-1 py-0.5 rounded text-xs">{children}</code>;
                      return <pre className="bg-surface-3 p-3 rounded-md overflow-x-auto text-xs"><code className={className} {...props}>{children}</code></pre>;
                    },
                  }}
                >
                  {msg.content}
                </ReactMarkdown>
              </div>
            ) : (
              <p>{msg.content}</p>
            )}
          </div>
          {msg.role === "user" && (
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-surface-3">
              <User className="h-4 w-4 text-text-secondary" />
            </div>
          )}
        </div>
      ))}
      {streaming && (
        <div className="flex gap-3">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent/10">
            <Bot className="h-4 w-4 text-accent" />
          </div>
          <div className="bg-surface-2 border border-border rounded-lg p-3 text-sm">
            <span className="inline-block w-2 h-4 bg-accent animate-pulse" />
          </div>
        </div>
      )}
      <div ref={bottomRef} />
    </div>
  );
}
