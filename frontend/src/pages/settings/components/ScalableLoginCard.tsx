import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, ExternalLink, Loader2, ShieldCheck } from "lucide-react";
import { toast } from "sonner";
import { Button } from "../../../components/ui/button";
import { useScalableStatus } from "../../../components/portfolio/ScalableSync";
import {
  cancelScalableLogin,
  getScalableLogin,
  pinScalableBinary,
  startScalableLogin,
  type ScalableLogin,
} from "../../../lib/api";
import { formatDateTime } from "../../../lib/format";

const SETUP_COMMAND = "bash /opt/quantfolio/infra/scalable/install.sh";
const SETUP_FIX = { text: "Run the one-time setup on the app server:", command: SETUP_COMMAND, label: "Copy setup command" };
// Login failures that only a command on the app server (as root) can fix. The
// sandbox one needs the current service units, which the app update installs.
const SERVER_FIX: Record<string, { text: string; command: string; label: string }> = {
  sandbox_blocks_sudo: {
    text: "Install the current service units on the app server (as root), then log in again:",
    command: "quantfolio-update-app",
    label: "Copy update command",
  },
  sudo_not_configured: SETUP_FIX,
  not_installed: SETUP_FIX,
  wrapper_outdated: SETUP_FIX,
};
const LOGIN_KEY = ["scalable-login"] as const;
const POLL_MS = 2000;

const STATE_TEXT: Record<string, string> = {
  disabled: "Sync is off.",
  running: "A sync is running.",
  never_synced: "Set up, not synced yet. Run the first sync from Portfolio → Accounts.",
  not_installed: "The read-only wrapper or sudo rule is missing on the server (see docs/scalable.md).",
  login_required: "The Scalable session expired. Log in again.",
  guard_unattested: "Sync stopped: the CLI's trade controls don't refuse every order.",
  error: "The last sync failed.",
  ready: "Connected.",
};

async function copyText(text: string, done: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast.success(done);
  } catch {
    toast.error("Copy failed; select the text instead");
  }
}

function CopyableCommand({ command, label }: { command: string; label: string }) {
  return (
    <div className="flex items-center gap-2">
      <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded bg-surface px-2 py-1.5 font-mono text-xs text-text-primary">
        {command}
      </code>
      <Button type="button" variant="ghost" size="sm" onClick={() => copyText(command, "Command copied")} aria-label={label}>
        <Copy className="h-4 w-4" aria-hidden="true" />
      </Button>
    </div>
  );
}

function WaitingPanel({ login, onCancel, cancelling }: { login: ScalableLogin; onCancel: () => void; cancelling: boolean }) {
  return (
    <div className="space-y-3 rounded-md border border-border bg-surface p-3" role="status" aria-live="polite">
      {login.user_code ? (
        <div className="flex items-center gap-2">
          <code className="select-all rounded bg-surface-2 px-3 py-2 font-mono text-2xl font-semibold tracking-widest text-text-primary">
            {login.user_code}
          </code>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => copyText(login.user_code ?? "", "Code copied")}
            aria-label="Copy code"
          >
            <Copy className="h-4 w-4" aria-hidden="true" />
          </Button>
        </div>
      ) : (
        <p className="flex items-center gap-2 text-text-secondary">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          Waiting for Scalable to show the code…
        </p>
      )}
      {login.verification_url ? (
        <a
          href={login.verification_url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-accent hover:underline"
        >
          Open the Scalable approval page
          <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
        </a>
      ) : login.verification_host ? (
        <p className="text-text-secondary">Open the link sc showed on {login.verification_host}.</p>
      ) : null}
      <p className="text-xs text-text-secondary">
        Approve it in the Scalable app (and confirm the second factor on your linked device if asked). The code
        expires after a few minutes.
      </p>
      <Button type="button" variant="outline" size="sm" onClick={onCancel} disabled={cancelling}>
        Cancel
      </Button>
    </div>
  );
}

function ResultPanel({ login }: { login: ScalableLogin }) {
  if (login.state === "succeeded") {
    return (
      <div className="space-y-1 rounded-md border border-success/40 bg-success/10 p-3" role="status">
        <p className="text-text-primary">
          {login.read_only_confirmed
            ? "Logged in. The session is read-only: Quantfolio cannot trade or change anything at Scalable."
            : login.message ?? "Logged in."}
        </p>
        <p className="text-text-secondary">Press Test to check the connection.</p>
      </div>
    );
  }
  if (login.state === "failed" || login.state === "expired") {
    const tone = login.state === "expired" ? "border-warn/40 bg-warn/10" : "border-danger/40 bg-danger/10";
    const fix = login.error_code && Object.hasOwn(SERVER_FIX, login.error_code) ? SERVER_FIX[login.error_code] : null;
    return (
      <div className={`space-y-2 rounded-md border p-3 ${tone}`} role="alert">
        <p className="text-text-primary">{login.message ?? "The login did not finish."}</p>
        {fix ? (
          <div className="space-y-1">
            <p className="text-text-secondary">{fix.text}</p>
            <CopyableCommand command={fix.command} label={fix.label} />
          </div>
        ) : null}
      </div>
    );
  }
  if (login.state === "cancelled") {
    return <p className="text-text-secondary">{login.message ?? "Login cancelled."}</p>;
  }
  return null;
}

/**
 * Starts `sc login --local-read-only` on the server and walks through the
 * device-code approval. The login itself lives on the server, owned by its own
 * Unix user; the app never sees it.
 */
export function ScalableLoginCard() {
  const queryClient = useQueryClient();
  const status = useScalableStatus();
  // An old finished login would be stale news: only show results of a login
  // started (or already running) while this card is open.
  const [engaged, setEngaged] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  const loginQuery = useQuery<ScalableLogin>({
    queryKey: LOGIN_KEY,
    queryFn: getScalableLogin,
    retry: false,
    refetchInterval: (query) => (query.state.data?.state === "waiting" ? POLL_MS : false),
  });

  const publish = async (login: ScalableLogin) => {
    await queryClient.cancelQueries({ queryKey: LOGIN_KEY });
    queryClient.setQueryData(LOGIN_KEY, login);
  };
  const start = useMutation({
    mutationFn: startScalableLogin,
    onMutate: () => {
      setEngaged(true);
      setStartError(null);
    },
    onSuccess: publish,
    onError: (error: Error) => {
      setStartError(error.message);
      toast.error(error.message);
    },
  });
  const cancel = useMutation({
    mutationFn: cancelScalableLogin,
    onSuccess: publish,
    onError: (error: Error) => toast.error(error.message),
  });
  const pin = useMutation({
    mutationFn: pinScalableBinary,
    onSuccess: (result) => toast.success(`Pinned sc ${result.sha256.slice(0, 12)}`),
    onError: (error: Error) => toast.error(error.message),
  });

  const login = start.isPending ? undefined : loginQuery.data;
  useEffect(() => {
    if (login?.state === "waiting") setEngaged(true);
  }, [login?.state]);

  // A finished login changes the connection status: refresh it once per login.
  const refreshedFor = useRef<string | null>(null);
  useEffect(() => {
    if (!engaged || login?.state !== "succeeded") return;
    const key = login.finished_at ?? "succeeded";
    if (refreshedFor.current === key) return;
    refreshedFor.current = key;
    void queryClient.invalidateQueries({ queryKey: ["scalable-status"] });
  }, [engaged, login, queryClient]);

  const shown = login && (engaged || login.state === "waiting") ? login : undefined;
  const waiting = shown?.state === "waiting";
  const data = status.data;
  const buttonLabel = start.isPending ? "Starting…" : data?.state === "login_required" ? "Log in again" : "Log in to Scalable";

  return (
    <div className="space-y-3 rounded-md border border-border bg-surface-2 p-4 text-sm">
      <div className="flex items-center gap-2 font-medium">
        <ShieldCheck className="h-4 w-4 text-success" aria-hidden="true" />
        Read-only: Quantfolio cannot trade or change anything at Scalable
      </div>
      {data ? (
        <p className="text-text-secondary">
          {STATE_TEXT[data.state] ?? data.state}
          {data.message && data.state !== "ready" ? ` ${data.message}` : ""}
          {data.last_success_at ? ` Last successful sync ${formatDateTime(data.last_success_at)}.` : ""}
        </p>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" onClick={() => start.mutate()} disabled={start.isPending || waiting}>
          {start.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" /> : null}
          {buttonLabel}
        </Button>
        <Button
          type="button"
          variant="outline"
          onClick={() => pin.mutate()}
          disabled={pin.isPending}
          title="Remember the checksum of the installed sc so a swapped binary is refused"
        >
          {pin.isPending ? "Pinning…" : "Pin installed sc"}
        </Button>
      </div>

      {start.isPending ? (
        <p className="text-text-secondary">Starting the login on the server. This can take up to 20 seconds.</p>
      ) : null}
      {startError ? (
        <p className="rounded-md border border-danger/40 bg-danger/10 p-3 text-text-primary" role="alert">
          {startError}
        </p>
      ) : null}
      {shown ? (
        waiting ? (
          <WaitingPanel login={shown} onCancel={() => cancel.mutate()} cancelling={cancel.isPending} />
        ) : (
          <ResultPanel login={shown} />
        )
      ) : null}

      <details className="text-text-secondary" open={startError ? true : undefined}>
        <summary className="cursor-pointer font-medium hover:text-text-primary">One-time server setup</summary>
        <ol className="mt-2 list-inside list-decimal space-y-2">
          <li>In the Scalable web app, enable Profile › Security › Agentic Investing › Scalable CLI.</li>
          <li>
            On the app LXC, as root, run the setup script. It downloads sc, verifies Scalable's signature and installs
            the read-only wrapper.
            <div className="mt-1">
              <CopyableCommand command={SETUP_COMMAND} label="Copy setup command" />
            </div>
          </li>
        </ol>
      </details>
    </div>
  );
}
