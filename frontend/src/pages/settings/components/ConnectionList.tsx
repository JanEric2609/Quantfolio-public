import { useSearchParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { formatDistanceToNow } from "date-fns";
import { ChevronRight, Loader2, PlugZap } from "lucide-react";
import { toast } from "sonner";
import { testSettingsIntegration, type SettingsCatalogEntry, type SettingsConnection, type SettingsPayload } from "../../../lib/api";
import { Button } from "../../../components/ui/button";
import { cn } from "../../../lib/utils";
import { connectionStatus, STATE_DOT, type ConnectionState } from "../lib/connections";
import { ATTENTION_KEY, SETTINGS_KEY } from "../lib/schema";
import { ConnectionDrawer } from "./ConnectionDrawer";

const ACTION: Record<ConnectionState, string> = {
  working: "Configure",
  untested: "Configure",
  failing: "Fix",
  missing: "Set up",
  off: "Configure",
};

export function ConnectionList({
  connections,
  catalog,
  values,
  headingId,
}: {
  headingId?: string;
  connections: SettingsConnection[];
  catalog: Record<string, SettingsCatalogEntry>;
  values: SettingsPayload | undefined;
}) {
  const [params, setParams] = useSearchParams();
  const openConnection = connections.find((connection) => connection.id === params.get("connection"));

  const setOpen = (id: string | null) => {
    const next = new URLSearchParams(params);
    if (id) next.set("connection", id);
    else next.delete("connection");
    setParams(next, { replace: true });
  };

  // Working first is noise; problems and unconfigured services lead.
  const rank: Record<ConnectionState, number> = { failing: 0, working: 1, untested: 2, off: 3, missing: 4 };
  const rows = connections
    .map((connection) => ({ connection, status: connectionStatus(connection, values, catalog) }))
    .sort((a, b) => rank[a.status.state] - rank[b.status.state]);

  const queryClient = useQueryClient();
  const testable = rows.filter(({ connection, status }) => connection.testable && status.state !== "missing" && status.state !== "off");
  // Sequential: several providers share rate limits, and results stream into the list.
  const testAll = useMutation({
    mutationFn: async () => {
      let failed = 0;
      for (const { connection } of testable) {
        const result = await testSettingsIntegration({ service: connection.id }).catch(() => ({ ok: false }));
        if (!result.ok) failed += 1;
        await queryClient.invalidateQueries({ queryKey: SETTINGS_KEY, exact: true });
      }
      return failed;
    },
    onSuccess: (failed) => {
      queryClient.invalidateQueries({ queryKey: ATTENTION_KEY });
      if (failed) toast.warning(`${failed} of ${testable.length} connections failed their test`);
      else toast.success(`All ${testable.length} connections work`);
    },
  });

  return (
    <>
      <div className="flex min-h-8 items-center justify-between gap-2">
        <h3 id={headingId} className="text-xs font-semibold uppercase tracking-wide text-text-muted">
          {connections.length === 1 ? "Connection" : "Connections"}
        </h3>
        {testable.length > 1 ? (
          <Button type="button" variant="ghost" size="sm" disabled={testAll.isPending} onClick={() => testAll.mutate()}>
            {testAll.isPending ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden="true" /> : <PlugZap className="mr-1.5 h-3.5 w-3.5" aria-hidden="true" />}
            Test all ({testable.length})
          </Button>
        ) : null}
      </div>
      <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-surface">
        {rows.map(({ connection, status }) => (
          <li key={connection.id}>
            <button
              type="button"
              onClick={() => setOpen(connection.id)}
              className="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-surface-2 focus-visible:bg-surface-2 focus-visible:outline-none"
            >
              <span className={cn("h-2 w-2 shrink-0 rounded-full", STATE_DOT[status.state])} aria-hidden="true" />
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-medium text-text-primary">{connection.label}</span>
                <span className="block truncate text-xs text-text-muted">{connection.description}</span>
              </span>
              <span className="hidden text-right text-xs sm:block">
                <span className={cn("block", status.state === "failing" ? "text-danger" : "text-text-secondary")}>{status.label}</span>
                {status.lastTest ? (
                  <span className="block text-text-muted">
                    tested {formatDistanceToNow(new Date(status.lastTest.tested_at), { addSuffix: true })}
                  </span>
                ) : null}
              </span>
              <span className="inline-flex items-center gap-0.5 text-xs font-medium text-accent">
                {ACTION[status.state]}
                <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
              </span>
            </button>
          </li>
        ))}
      </ul>
      {openConnection ? (
        <ConnectionDrawer
          key={openConnection.id}
          connection={openConnection}
          catalog={catalog}
          values={values}
          open
          onOpenChange={(open) => setOpen(open ? openConnection.id : null)}
        />
      ) : null}
    </>
  );
}
