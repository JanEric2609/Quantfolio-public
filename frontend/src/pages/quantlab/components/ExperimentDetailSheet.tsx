import { formatDateTime } from "../../../lib/format";
import { useQuery } from "@tanstack/react-query";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "../../../components/ui/sheet";
import { Button } from "../../../components/ui/button";
import { api } from "../../../lib/api";

export interface QuantExperiment {
  id: string;
  name: string;
  [key: string]: unknown;
}

export interface QuantExperimentRun {
  id: string;
  status: string;
  started_at?: string;
  [key: string]: unknown;
}

export function ExperimentDetailSheet({ experiment, onClose, onRun }: { experiment?: QuantExperiment; onClose: () => void; onRun: (id: string) => void }) {
  const runs = useQuery({ enabled: Boolean(experiment), queryKey: ["quant", "experiment", experiment?.id, "runs"], queryFn: () => api<QuantExperimentRun[]>(`/api/quant/experiments/${experiment?.id}/runs`) });
  return <Sheet open={Boolean(experiment)} onOpenChange={(open) => !open && onClose()}><SheetContent className="w-full overflow-y-auto sm:max-w-xl" aria-describedby={undefined}>{experiment && <><SheetHeader><SheetTitle>{experiment.name}</SheetTitle></SheetHeader><Button className="my-3" onClick={() => onRun(experiment.id)}>Run now</Button><div className="space-y-2">{(runs.data ?? []).map((run) => <div key={run.id} className="rounded-md border border-line bg-panel p-2 text-xs"><b>{run.status}</b><div className="text-text-secondary">{run.started_at ? formatDateTime(run.started_at) : "queued"}</div></div>)}</div></>}</SheetContent></Sheet>;
}
