import { useEffect } from "react";
import { useRouteError } from "react-router-dom";
import { reportError } from "../../lib/errorReporting";
import { ErrorState } from "./ErrorState";

export function RouteErrorBoundary() {
  const error = useRouteError();
  const errorMessage = error instanceof Error ? error.message : "An unexpected error occurred";

  useEffect(() => {
    reportError(error, "page crashed");
  }, [error]);

  return (
    <div className="p-6">
      <ErrorState
        title="Something went wrong"
        body={errorMessage}
        onRetry={() => window.location.reload()}
      />
    </div>
  );
}
