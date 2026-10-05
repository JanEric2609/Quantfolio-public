import { useState, useEffect, useRef, useCallback } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type ChatConversation, type ChatMessageItem } from "../../lib/api";
import { ConversationList } from "./components/ConversationList";
import { ChatMessages } from "./components/ChatMessages";
import { ChatInput } from "./components/ChatInput";
import { Button } from "../../components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle, SheetTrigger } from "../../components/ui/sheet";
import { MessagesSquare, Plus } from "lucide-react";

export function ChatPage() {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const activeConversationId = searchParams.get("c") || undefined;
  const [messages, setMessages] = useState<ChatMessageItem[]>([]);
  const [streaming, setStreaming] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  // Abort any in-flight stream when the component unmounts to avoid state updates on an
  // unmounted component and prevent dangling fetch connections.
  useEffect(() => () => { abortRef.current?.abort(); }, []);

  const conversations = useQuery({
    queryKey: ["chat-conversations"],
    queryFn: () => api<ChatConversation[]>("/api/ai/chat/conversations"),
  });

  const conversationDetail = useQuery({
    queryKey: ["chat-conversation", activeConversationId],
    queryFn: () => api<ChatMessageItem[]>(`/api/ai/chat/conversations/${activeConversationId}`),
    enabled: !!activeConversationId,
  });

  useEffect(() => {
    if (conversationDetail.data) {
      setMessages(conversationDetail.data);
    }
  }, [conversationDetail.data]);

  const deleteConv = useMutation({
    mutationFn: (id: string) => api(`/api/ai/chat/conversations/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["chat-conversations"] });
      if (activeConversationId) {
        setSearchParams({}, { replace: true });
        setMessages([]);
      }
    },
  });

  // Below lg the conversation list lives in a sheet behind a "Conversations" button.
  const [listOpen, setListOpen] = useState(false);

  const msgCounterRef = useRef(0);

  const sendMessage = useCallback(async (text: string, context?: string) => {
    const message = context ? `${context}\n\n${text}` : text;
    const convId = activeConversationId || crypto.randomUUID();

    const userMsg: ChatMessageItem = {
      id: crypto.randomUUID(),
      conversation_id: convId,
      role: "user",
      content: text,
      timestamp: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, userMsg]);
    setStreaming(true);

    const assistantTempId = `stream-${++msgCounterRef.current}`;

    if (!activeConversationId) {
      setSearchParams({ c: convId }, { replace: true });
    }

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const response = await fetch("/api/ai/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ message, conversation_id: convId }),
        signal: controller.signal,
      });

      // Surface HTTP-level errors (401, 500, etc.) before reading the stream body.
      // Without this check an error body would be passed to the SSE parser and silently dropped.
      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || `Request failed with status ${response.status}`);
      }

      const reader = response.body?.getReader();
      if (!reader) throw new Error("No reader");

      const decoder = new TextDecoder();
      let assistantContent = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value);
        const lines = chunk.split("\n").filter((l) => l.startsWith("data: "));

        for (const line of lines) {
          try {
            const data = JSON.parse(line.slice(6));
            if (data.delta) {
              assistantContent += data.delta;
              setMessages((prev) => {
                const copy = [...prev];
                const last = copy[copy.length - 1];
                if (last?.role === "assistant" && last.id === assistantTempId) {
                  copy[copy.length - 1] = { ...last, content: assistantContent };
                } else {
                  copy.push({
                    id: data.message_id || assistantTempId,
                    conversation_id: convId,
                    role: "assistant",
                    content: assistantContent,
                    timestamp: new Date().toISOString(),
                  });
                }
                return copy;
              });
            }
            if (data.done && data.message_id) {
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantTempId ? { ...m, id: data.message_id } : m
                )
              );
              queryClient.invalidateQueries({ queryKey: ["chat-conversations"] });
            }
          } catch {}
        }
      }
    } catch (err: any) {
      if (err.name !== "AbortError") {
        setMessages((prev) => [
          ...prev,
          {
            id: `stream-err-${crypto.randomUUID()}`,
            conversation_id: convId,
            role: "assistant",
            content: "Error: Failed to get response from the server.",
            timestamp: new Date().toISOString(),
          },
        ]);
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [activeConversationId, setSearchParams, queryClient]);

  const handleSelectConversation = (id: string) => {
    setSearchParams({ c: id }, { replace: true });
    setListOpen(false);
  };

  const handleNew = () => {
    setSearchParams({}, { replace: true });
    setMessages([]);
    setListOpen(false);
  };

  const activeTitle = (conversations.data ?? []).find((c) => c.conversation_id === activeConversationId)?.title;

  const list = (
    <ConversationList
      conversations={conversations.data ?? []}
      activeId={activeConversationId}
      onSelect={handleSelectConversation}
      onDelete={(id) => deleteConv.mutate(id)}
      onNew={handleNew}
    />
  );

  return (
    // 100dvh, not 100vh: on a phone 100vh is the viewport with the browser chrome retracted,
    // which pushes the input under the address bar / keyboard. `--app-chrome-height` is the
    // space the shell uses around the page (top bar + page padding by default).
    <div className="flex h-[calc(100dvh-var(--app-chrome-height,6.5rem))] gap-0">
      <div className="hidden lg:flex w-[300px] shrink-0 border-r border-border">{list}</div>
      <div className="flex flex-1 flex-col min-w-0">
        <div className="flex items-center gap-2 border-b border-border px-3 py-2 lg:hidden">
          <Sheet open={listOpen} onOpenChange={setListOpen}>
            <SheetTrigger asChild>
              <Button type="button" variant="outline" size="sm">
                <MessagesSquare className="mr-2 h-4 w-4" aria-hidden />
                Conversations
              </Button>
            </SheetTrigger>
            <SheetContent side="left" className="flex w-[85vw] max-w-sm flex-col gap-0 p-0">
              <SheetHeader className="border-b border-border p-4 pr-12 text-left">
                <SheetTitle>Conversations</SheetTitle>
                <SheetDescription className="sr-only">Pick a conversation or start a new one.</SheetDescription>
              </SheetHeader>
              <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">{list}</div>
            </SheetContent>
          </Sheet>
          <span className="min-w-0 flex-1 truncate text-sm text-text-secondary">{activeTitle ?? "New chat"}</span>
          <Button type="button" variant="accent" size="sm" onClick={handleNew}>
            <Plus className="mr-1 h-4 w-4" aria-hidden />
            New
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto p-4">
          <ChatMessages messages={messages} streaming={streaming} />
          {!messages.length && (
            <div className="flex items-center justify-center h-full">
              <p className="text-sm text-text-muted">Start a conversation. Ask about portfolio fit, risk, or market conditions.</p>
            </div>
          )}
        </div>
        <div className="border-t border-border p-4">
          <ChatInput onSend={sendMessage} streaming={streaming} />
        </div>
      </div>
    </div>
  );
}
