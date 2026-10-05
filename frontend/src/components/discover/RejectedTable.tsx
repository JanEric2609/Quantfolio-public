import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { Badge } from "../../components/ui/badge";
import { type DiscoverCandidate, parseConcerns } from "../../lib/api";
import { ConcernBadges } from "./ConcernBadges";

interface RejectedTableProps {
  candidates: DiscoverCandidate[];
}

export function RejectedTable({ candidates }: RejectedTableProps) {
  const [expanded, setExpanded] = useState(false);
  const rejected = candidates.filter((c) => c.status === "rejected");

  if (rejected.length === 0) return null;

  return (
    <Card>
      <CardHeader className="pb-2">
        <button
          onClick={() => setExpanded((v) => !v)}
          className="flex items-center gap-2 text-sm font-semibold text-text-primary"
        >
          {expanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
          Rejected ({rejected.length})
        </button>
      </CardHeader>
      {expanded && (
        <CardContent>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border">
                  <th className="text-left py-2 pr-4 font-medium text-text-secondary">Symbol</th>
                  <th className="text-left py-2 pr-4 font-medium text-text-secondary">Stage Rejected</th>
                  <th className="text-left py-2 pr-4 font-medium text-text-secondary">Flags</th>
                  <th className="text-left py-2 font-medium text-text-secondary">Reason</th>
                </tr>
              </thead>
              <tbody>
                {rejected.map((c) => {
                  const concerns = parseConcerns(c.scores);
                  return (
                    <tr key={c.id} className="border-b border-border/50">
                      <td className="py-2 pr-4 text-text-muted">{c.symbol}</td>
                      <td className="py-2 pr-4">
                        <Badge variant="secondary" className="text-[10px]">
                          {c.reject_stage ?? "—"}
                        </Badge>
                      </td>
                      <td className="py-2 pr-4">
                        <ConcernBadges concerns={concerns} max={2} />
                        {concerns.length === 0 && (
                          <span className="text-text-muted text-xs">—</span>
                        )}
                      </td>
                      <td className="py-2 text-text-muted text-xs">{c.reject_reason ?? "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </CardContent>
      )}
    </Card>
  );
}
