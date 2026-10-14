import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Button } from "../../../components/ui/button";
import { IntegrationCard } from "./IntegrationCard";
import { createTelegramPairingCode, getTelegramLinkStatus, sendTelegramTestMessage } from "../../../lib/api";

export function TelegramPairingCard() {
  const [pairingCode, setPairingCode] = useState<string | null>(null);

  const { data: status } = useQuery({
    queryKey: ["telegram", "link-status"],
    queryFn: getTelegramLinkStatus,
    // While a code is waiting for '/start CODE', look every few seconds so the card turns "linked" by itself.
    refetchInterval: (query) => (pairingCode && !query.state.data?.linked ? 3000 : false),
  });
  const linked = Boolean(status?.linked);

  const generate = useMutation({
    mutationFn: createTelegramPairingCode,
    onSuccess: (result) => setPairingCode(result.code),
  });
  const test = useMutation({ mutationFn: sendTelegramTestMessage });

  return (
    <IntegrationCard
      title="Telegram Account Linking"
      description="Link your Telegram chat to receive the daily self-check and crash-guard alerts and to log expenses."
    >
      {linked ? (
        <p className="text-sm text-text-secondary">
          Linked since {status?.linked_at ? new Date(status.linked_at).toLocaleString() : "—"}. Generate a new code
          below to re-link a different chat.
        </p>
      ) : (
        <p className="text-sm text-text-secondary">Not linked yet: alerts reach only the bell in this app.</p>
      )}

      {pairingCode && !linked && (
        <div className="rounded-md border border-border bg-surface-secondary p-3 text-sm">
          Send{" "}
          <code className="font-mono">
            /start <span>{pairingCode}</span>
          </code>{" "}
          to your bot in Telegram within 15 minutes.
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        <Button onClick={() => generate.mutate()} disabled={generate.isPending}>
          {generate.isPending ? "Generating…" : "Generate pairing code"}
        </Button>
        {linked && (
          <Button variant="outline" onClick={() => test.mutate()} disabled={test.isPending}>
            {test.isPending ? "Sending…" : "Send test message"}
          </Button>
        )}
      </div>
      {test.data && (
        <p role="status" className={`text-sm ${test.data.sent ? "text-success" : "text-danger"}`}>
          {test.data.message}
        </p>
      )}
      {test.isError && (
        <p role="status" className="text-sm text-danger">
          The test message could not be sent.
        </p>
      )}
    </IntegrationCard>
  );
}
