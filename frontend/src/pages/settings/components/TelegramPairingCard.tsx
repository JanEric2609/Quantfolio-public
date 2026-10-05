import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Button } from "../../../components/ui/button";
import { IntegrationCard } from "./IntegrationCard";
import { createTelegramPairingCode, getTelegramLinkStatus } from "../../../lib/api";

export function TelegramPairingCard() {
  const [pairingCode, setPairingCode] = useState<string | null>(null);

  const { data: status } = useQuery({
    queryKey: ["telegram", "link-status"],
    queryFn: getTelegramLinkStatus,
  });

  const generate = useMutation({
    mutationFn: createTelegramPairingCode,
    onSuccess: (result) => setPairingCode(result.code),
  });

  return (
    <IntegrationCard
      title="Telegram Account Linking"
      description="Link your Telegram account to log expenses and receive alerts via the bot."
    >
      {status?.linked ? (
        <p className="text-sm text-text-secondary">
          Linked since {status.linked_at ? new Date(status.linked_at).toLocaleString() : "—"}. Generate a new code
          below to re-link a different chat.
        </p>
      ) : (
        <p className="text-sm text-text-secondary">Not linked yet.</p>
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

      <Button onClick={() => generate.mutate()} disabled={generate.isPending}>
        {generate.isPending ? "Generating…" : "Generate pairing code"}
      </Button>
    </IntegrationCard>
  );
}
