import { useState, useRef, useEffect } from "react";
import { Textarea } from "../../../components/ui/textarea";
import { Button } from "../../../components/ui/button";
import { Send } from "lucide-react";
import { ContextAttachMenu, CONTEXT_OPTIONS } from "./ContextAttachMenu";

interface ChatInputProps {
  onSend: (message: string, context?: string) => void;
  streaming: boolean;
}

export function ChatInput({ onSend, streaming }: ChatInputProps) {
  const [text, setText] = useState("");
  const [contextToggles, setContextToggles] = useState<Record<string, boolean>>({});
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!streaming && textareaRef.current) {
      textareaRef.current.focus();
    }
  }, [streaming]);

  const handleSend = () => {
    const trimmed = text.trim();
    if (!trimmed || streaming) return;
    const labels = CONTEXT_OPTIONS.filter((o) => contextToggles[o.id]).map((o) => o.label);
    const contextStr = labels.length ? `Attached context: ${labels.join(", ")}` : undefined;
    onSend(trimmed, contextStr);
    setText("");
    setContextToggles({});
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="flex items-end gap-2">
      <ContextAttachMenu toggles={contextToggles} onChange={setContextToggles} />
      <div className="flex-1 relative">
        <Textarea
          ref={textareaRef}
          placeholder="Ask about portfolio fit, risk, or market conditions… (Cmd+Enter to send)"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          className="min-h-[44px] max-h-[200px] pr-12 resize-none"
          rows={1}
        />
      </div>
      <Button
        variant="accent"
        size="icon"
        aria-label="Send message"
        onClick={handleSend}
        disabled={!text.trim() || streaming}
      >
        <Send className="h-4 w-4" />
      </Button>
    </div>
  );
}
