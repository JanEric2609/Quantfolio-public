import { useState } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../../../components/ui/tabs";
import { Button } from "../../../components/ui/button";
import { Skeleton } from "../../../components/ui/skeleton";
import { EmptyState } from "../../../components/composed/EmptyState";
import { ExperimentDetailSheet } from "../components/ExperimentDetailSheet";
import { useExperimentRun } from "../hooks/useExperimentRun";
import { useExperiments } from "../hooks/useExperiments";
import { useGraveyard } from "../hooks/useGraveyard";
import { FlaskConical, XCircle } from "lucide-react";

export function ExperimentsView() {
  const experiments = useExperiments();
  const graveyard = useGraveyard();
  const run = useExperimentRun();
  const [selected, setSelected] = useState<any>();

  if (experiments.isLoading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-10 w-48" />
        <div className="space-y-2">
          {[1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-14 w-full" />
          ))}
        </div>
      </div>
    );
  }

  if (experiments.error) {
    return (
      <EmptyState
        icon={XCircle}
        title="Failed to load experiments"
        body={experiments.error instanceof Error ? experiments.error.message : "An unexpected error occurred."}
      />
    );
  }

  const experimentList = experiments.data ?? [];
  const graveyardList = graveyard.data ?? [];

  return (
    <Tabs defaultValue="active" className="space-y-3">
      <TabsList>
        <TabsTrigger value="active">Active</TabsTrigger>
        <TabsTrigger value="graveyard">Graveyard</TabsTrigger>
      </TabsList>
      <TabsContent value="active">
        {experimentList.length === 0 ? (
          <EmptyState
            icon={FlaskConical}
            title="No experiments yet"
            body="Create an experiment to start backtesting strategies."
          />
        ) : (
          <div className="space-y-2">
            {experimentList.map((experiment) => (
              <div
                key={experiment.id}
                role="button"
                tabIndex={0}
                onClick={() => setSelected(experiment)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    setSelected(experiment);
                  }
                }}
                className="flex w-full items-center justify-between rounded-md p-3 text-left text-sm transition-colors hover:bg-surface-2"
                style={{ background: "rgb(var(--c-surface))" }}
              >
                <span>
                  <b>{experiment.name}</b>
                  <small className="ml-2 text-text-secondary">
                    {experiment.strategy_type}
                  </small>
                </span>
                <Button
                  size="sm"
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation();
                    run.mutate(experiment.id);
                  }}
                >
                  Run
                </Button>
              </div>
            ))}
          </div>
        )}
      </TabsContent>
      <TabsContent value="graveyard">
        {graveyardList.length === 0 ? (
          <EmptyState
            icon={FlaskConical}
            title="Graveyard is empty"
            body="Rejected or deprecated experiments will appear here."
          />
        ) : (
          <div className="space-y-2">
            {graveyardList.map((entry) => (
              <div
                key={entry.id}
                className="rounded-md p-3 text-sm"
                style={{ background: "rgb(var(--c-surface))" }}
              >
                <b>{entry.name}</b>
                <div className="text-xs text-text-secondary">
                  {entry.reason ?? entry.rejection_reason}
                </div>
              </div>
            ))}
          </div>
        )}
      </TabsContent>
      <ExperimentDetailSheet
        experiment={selected}
        onClose={() => setSelected(undefined)}
        onRun={(id) => run.mutate(id)}
      />
    </Tabs>
  );
}
