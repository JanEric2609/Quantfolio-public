import { formatNumber } from "../../../lib/format";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../../../components/ui/table";
import { GlossaryTooltip } from "../../../components/composed/GlossaryTooltip";

interface GreeksData {
  delta: number;
  delta_stderr: number;
  gamma: number;
  gamma_stderr: number;
  vega: number;
  vega_stderr: number;
  theta?: number;
  rho?: number;
}

interface GreeksTableProps {
  greeks: GreeksData | undefined;
  isLoading?: boolean;
}

export function GreeksTable({ greeks, isLoading = false }: GreeksTableProps) {
  if (isLoading) {
    return (
      <div className="rounded-md border border-line bg-panel p-4 text-center text-sm text-text-secondary">
        Computing Greeks...
      </div>
    );
  }

  if (!greeks) {
    return (
      <div className="rounded-md border border-line bg-panel p-4 text-center text-sm text-text-secondary">
        No Greeks available. Enable "Compute Greeks" to calculate.
      </div>
    );
  }

  return (
    <div className="rounded-md border border-line bg-panel">
      <Table>
        <TableHeader>
          <TableRow className="border-b border-line">
            <TableHead className="h-10 px-3 text-xs font-semibold text-text-secondary">Greek</TableHead>
            <TableHead className="h-10 px-3 text-right text-xs font-semibold text-text-secondary">Estimate</TableHead>
            <TableHead className="h-10 px-3 text-right text-xs font-semibold text-text-secondary">Std Error</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          <TableRow className="border-b border-line hover:bg-surface-2">
            <TableCell className="px-3 py-2 text-xs">
              <GlossaryTooltip k="delta">Δ (Delta)</GlossaryTooltip>
            </TableCell>
            <TableCell className="px-3 py-2 text-right text-xs font-mono text-success">
              {formatNumber(greeks.delta ?? 0, { digits: 4 })}
            </TableCell>
            <TableCell className="px-3 py-2 text-right text-xs font-mono text-text-muted">
              {formatNumber(greeks.delta_stderr ?? 0, { digits: 6 })}
            </TableCell>
          </TableRow>

          <TableRow className="border-b border-line hover:bg-surface-2">
            <TableCell className="px-3 py-2 text-xs">
              <GlossaryTooltip k="gamma">Γ (Gamma)</GlossaryTooltip>
            </TableCell>
            <TableCell className="px-3 py-2 text-right text-xs font-mono text-success">
              {formatNumber(greeks.gamma ?? 0, { digits: 6 })}
            </TableCell>
            <TableCell className="px-3 py-2 text-right text-xs font-mono text-text-muted">
              {formatNumber(greeks.gamma_stderr ?? 0, { digits: 8 })}
            </TableCell>
          </TableRow>

          <TableRow className="border-b border-line hover:bg-surface-2">
            <TableCell className="px-3 py-2 text-xs">
              <GlossaryTooltip k="vega">ν (Vega)</GlossaryTooltip>
            </TableCell>
            <TableCell className="px-3 py-2 text-right text-xs font-mono text-success">
              {formatNumber(greeks.vega ?? 0, { digits: 4 })}
            </TableCell>
            <TableCell className="px-3 py-2 text-right text-xs font-mono text-text-muted">
              {formatNumber(greeks.vega_stderr ?? 0, { digits: 6 })}
            </TableCell>
          </TableRow>

          {greeks.theta !== undefined && (
            <TableRow className="border-b border-line hover:bg-surface-2">
              <TableCell className="px-3 py-2 text-xs">
                <GlossaryTooltip k="theta">θ (Theta)</GlossaryTooltip>
              </TableCell>
              <TableCell className="px-3 py-2 text-right text-xs font-mono text-success">
                {formatNumber(greeks.theta ?? 0, { digits: 4 })}
              </TableCell>
              <TableCell className="px-3 py-2 text-right text-xs font-mono text-text-muted">—</TableCell>
            </TableRow>
          )}

          {greeks.rho !== undefined && (
            <TableRow className="hover:bg-surface-2">
              <TableCell className="px-3 py-2 text-xs">
                <GlossaryTooltip k="rho">ρ (Rho)</GlossaryTooltip>
              </TableCell>
              <TableCell className="px-3 py-2 text-right text-xs font-mono text-success">
                {formatNumber(greeks.rho ?? 0, { digits: 4 })}
              </TableCell>
              <TableCell className="px-3 py-2 text-right text-xs font-mono text-text-muted">—</TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </div>
  );
}
