import type { ChatConversation } from "../../../lib/api";
import { cn } from "../../../lib/utils";
import { Button } from "../../../components/ui/button";
import {
  AlertDialog,
  AlertDialogTrigger,
  AlertDialogContent,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogCancel,
  AlertDialogAction,
} from "../../../components/ui/alert-dialog";
import { Plus, Trash2, MessageSquare } from "lucide-react";

interface ConversationListProps {
  conversations: ChatConversation[];
  activeId?: string;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onNew: () => void;
}

export function ConversationList({ conversations, activeId, onSelect, onDelete, onNew }: ConversationListProps) {
  return (
    <div className="flex flex-col w-full">
      <div className="p-3 border-b border-border">
        <Button variant="accent" size="sm" className="w-full" onClick={onNew}>
          <Plus className="h-4 w-4 mr-1" /> New Chat
        </Button>
      </div>
      <div className="flex-1 overflow-y-auto">
        {!conversations.length && (
          <div className="p-4 text-xs text-text-muted text-center">No conversations yet.</div>
        )}
        {conversations.map((conv) => (
          <div
            key={conv.conversation_id}
            className={cn(
              "flex items-center border-l-2 text-sm transition-colors hover:bg-surface-2",
              conv.conversation_id === activeId
                ? "border-accent bg-surface-2 text-accent"
                : "border-transparent text-text-secondary",
            )}
          >
            {/* The row is a real button (keyboard + screen reader); the delete control is its sibling and always visible. */}
            <button
              type="button"
              className="flex min-h-11 min-w-0 flex-1 items-start gap-2 p-3 text-left"
              aria-current={conv.conversation_id === activeId ? "true" : undefined}
              onClick={() => onSelect(conv.conversation_id)}
            >
              <MessageSquare className="h-4 w-4 mt-0.5 shrink-0" aria-hidden />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-xs">{conv.title}</span>
                <span className="mt-0.5 block text-[10px] text-text-muted">{conv.message_count} messages</span>
              </span>
            </button>
            <AlertDialog>
              <AlertDialogTrigger asChild>
                <button
                  type="button"
                  aria-label={`Delete conversation ${conv.title}`}
                  className="mr-1 flex h-11 w-11 shrink-0 items-center justify-center rounded text-text-muted hover:bg-surface-3 hover:text-danger sm:h-9 sm:w-9"
                >
                  <Trash2 className="h-4 w-4" aria-hidden />
                </button>
              </AlertDialogTrigger>
              <AlertDialogContent className="bg-surface border-border">
                <AlertDialogHeader>
                  <AlertDialogTitle>Delete conversation?</AlertDialogTitle>
                  <AlertDialogDescription>
                    This will permanently delete this conversation and all its messages. This action cannot be undone.
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                  <AlertDialogAction
                    onClick={() => onDelete(conv.conversation_id)}
                    className="bg-danger hover:bg-danger/90"
                  >
                    Delete
                  </AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          </div>
        ))}
      </div>
    </div>
  );
}
