import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Button } from "../../../components/ui/button";
import { IntegrationCard } from "./IntegrationCard";
import { createTelegramPairingCode, getTelegramLinkStatus, sendTelegramTestMessage } from "../../../lib/api";

export function TelegramPairingCard() {
  // The code and the link time when it was made: once the link time changes, '/start CODE' has been used.
  const [pairing, setPairing] = useState<{ code: string; linkedAt: string | null } | null>(null);

  const { data: status, isLoading: statusLoading } = useQuery({
    queryKey: ["telegram", "link-status"],
    queryFn: getTelegramLinkStatus,
    // While a code is waiting for '/start CODE', look every few seconds so the card updates by itself.
    refetchInterval: (query) =>
      pairing && (query.state.data?.linked_at ?? null) === pairing.linkedAt ? 3000 : false,
  });
  const linked = Boolean(status?.linked);
  // Shown until it is used, also to an already linked user re-linking another chat.
  const pairingCode = pairing && (status?.linked_at ?? null) === pairing.linkedAt ? pairing.code : null;

  const generate = useMutation({
    mutationFn: createTelegramPairingCode,
    onSuccess: (result) => setPairing({ code: result.code, linkedAt: status?.linked_at ?? null }),
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

      {pairingCode && (
        <div className="rounded-md border border-border bg-surface-secondary p-3 text-sm">
          Send{" "}
          <code className="font-mono">
            /start <span>{pairingCode}</span>
          </code>{" "}
          to your bot in Telegram within 15 minutes.
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        {/* Wait for the link status: the code is shown until the link time moves past the one seen here. */}
        <Button onClick={() => generate.mutate()} disabled={generate.isPending || statusLoading}>
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
