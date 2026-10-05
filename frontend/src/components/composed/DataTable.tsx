import { useCallback, useId, useState, type KeyboardEvent } from "react";
import {
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { cn } from "../../lib/utils";
import { useIsPhone } from "../../hooks/useMediaQuery";

export interface DataTableProps<T> {
  data: T[];
  columns: ColumnDef<T>[];
  onRowClick?: (row: T) => void;
  initialSort?: { id: string; desc?: boolean }[];
  enableFilter?: boolean;
  enableColumnVisibility?: boolean;
  pageSize?: number;
  stickyHeader?: boolean;
  emptyState?: React.ReactNode;
  rightSlot?: React.ReactNode;
  density?: "comfortable" | "compact" | "auto";
  csvFilename?: string;
  /**
   * Phone layout. When given, rows render as a list of cards below `sm` (640 px)
   * instead of a table whose columns would truncate. The card is the whole row,
   * so it becomes a button when `onRowClick` is set. Return inline content
   * (spans) since it sits inside that button. Filtering, sorting and paging are
   * shared with the table layout.
   */
  mobileCard?: (row: T) => React.ReactNode;
}

// Enter / Space on a focused row activates it like a click would. Only when the
// row itself has focus, so keys typed in a button or input inside the row keep
// their own behaviour.
function activateOnKey(event: KeyboardEvent<HTMLElement>, activate: () => void) {
  if (event.target !== event.currentTarget) return;
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    activate();
  }
}

export function DataTable<T>({
  data,
  columns,
  onRowClick,
  initialSort,
  enableFilter,
  pageSize = 25,
  stickyHeader = true,
  emptyState,
  rightSlot,
  density: propDensity,
  csvFilename,
  mobileCard,
}: DataTableProps<T>) {
  const isPhone = useIsPhone();
  const cardMode = Boolean(mobileCard) && isPhone;
  const sortId = useId();
  const [sorting, setSorting] = useState<SortingState>(initialSort?.map((s) => ({ id: s.id, desc: s.desc ?? false })) ?? []);
  const [filter, setFilter] = useState("");

  const effectiveDensity = propDensity === "auto" ? (document.documentElement.dataset.density as "comfortable" | "compact") ?? "comfortable" : propDensity;
  const padY = effectiveDensity === "compact" ? "py-1.5" : "py-2.5";

  const table = useReactTable({
    data,
    columns,
    state: { sorting, globalFilter: filter },
    onSortingChange: setSorting,
    onGlobalFilterChange: setFilter,
    getCoreRowModel: getCoreRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    getSortedRowModel: getSortedRowModel(),
    globalFilterFn: (row, _columnId, value) =>
      row.getAllCells().some((cell) => String(cell.getValue() ?? "").toLowerCase().includes(String(value).toLowerCase())),
    initialState: { pagination: { pageSize } },
  });

  const handleExport = useCallback(() => {
    if (!csvFilename) return;
    const cols = table.getAllLeafColumns();
    const header = cols
      .map((c) => typeof c.columnDef.header === "string" ? c.columnDef.header : c.id)
      .map((h) => JSON.stringify(h))
      .join(",");
    const body = table.getFilteredRowModel().rows.map((r) =>
      r.getVisibleCells().map((cell) => {
        const v = cell.getValue();
        return JSON.stringify(v == null ? "" : String(v));
      }).join(","),
    ).join("\n");
    const blob = new Blob([`${header}\n${body}\n`], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = csvFilename;
    a.click();
    URL.revokeObjectURL(url);
  }, [csvFilename, table]);

  const sortableColumns = cardMode
    ? table.getAllLeafColumns().filter((c) => c.getCanSort() && typeof c.columnDef.header === "string")
    : [];
  const activeSort = sorting[0];

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2 sm:gap-4">
        {enableFilter && (
          <Input placeholder="Filter rows…" value={filter} onChange={(e) => setFilter(e.target.value)} className="w-full sm:max-w-xs" />
        )}
        <div className="flex items-center gap-2 sm:ml-auto">
          {rightSlot}
          {csvFilename && <Button variant="outline" size="sm" onClick={handleExport}>Export CSV</Button>}
        </div>
      </div>
      {cardMode && sortableColumns.length > 0 && (
        <div className="flex items-center gap-2">
          <label htmlFor={sortId} className="text-xs text-text-secondary">Sort by</label>
          <select
            id={sortId}
            value={activeSort?.id ?? ""}
            onChange={(e) => setSorting(e.target.value ? [{ id: e.target.value, desc: activeSort?.desc ?? false }] : [])}
            className="h-11 min-w-0 flex-1 rounded-md border border-border bg-surface px-3 text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            <option value="">Default order</option>
            {sortableColumns.map((c) => (
              <option key={c.id} value={c.id}>{c.columnDef.header as string}</option>
            ))}
          </select>
          {activeSort && (
            <Button
              type="button"
              variant="outline"
              size="icon"
              aria-label={activeSort.desc ? "Sorted descending, switch to ascending" : "Sorted ascending, switch to descending"}
              onClick={() => setSorting([{ id: activeSort.id, desc: !activeSort.desc }])}
            >
              <span aria-hidden>{activeSort.desc ? "↓" : "↑"}</span>
            </Button>
          )}
        </div>
      )}
      {cardMode && mobileCard ? (
        table.getRowModel().rows.length === 0 ? (
          <div className="rounded-md border border-border py-8 text-center text-sm text-text-muted">{emptyState ?? "No rows"}</div>
        ) : (
          <ul className="space-y-2">
            {table.getRowModel().rows.map((row) => (
              <li key={row.id}>
                {onRowClick ? (
                  <button
                    type="button"
                    onClick={() => onRowClick(row.original)}
                    className="block min-h-11 w-full min-w-0 rounded-md border border-border bg-surface p-3 text-left transition-colors hover:bg-surface-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    {mobileCard(row.original)}
                  </button>
                ) : (
                  <div className="min-w-0 rounded-md border border-border bg-surface p-3">{mobileCard(row.original)}</div>
                )}
              </li>
            ))}
          </ul>
        )
      ) : (
      <div className="overflow-x-auto rounded-md border border-border">
        <table className={cn("w-full text-sm", stickyHeader && "[&_thead]:sticky [&_thead]:top-0 [&_thead]:bg-surface")}>
          <thead className="border-b border-border">
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id}>
                {hg.headers.map((h) => (
                  <th key={h.id} className={cn("px-4 py-2 text-left font-medium text-text-secondary", stickyHeader && "bg-surface")}>
                    {h.isPlaceholder ? null : (
                      <button type="button" className="inline-flex min-h-11 items-center gap-1 sm:min-h-0" onClick={h.column.getToggleSortingHandler()}>
                        {flexRender(h.column.columnDef.header, h.getContext())}
                        {h.column.getIsSorted() === "asc" && <span aria-hidden>↑</span>}
                        {h.column.getIsSorted() === "desc" && <span aria-hidden>↓</span>}
                      </button>
                    )}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.length === 0 ? (
              <tr><td colSpan={columns.length} className="py-8 text-center text-text-muted">{emptyState ?? "No rows"}</td></tr>
            ) : (
              table.getRowModel().rows.map((row) => (
                <tr
                  key={row.id}
                  className={cn(
                    "border-b border-border transition-colors hover:bg-surface-2",
                    onRowClick && "cursor-pointer focus-visible:bg-surface-2 focus-visible:[outline-offset:-2px]",
                  )}
                  onClick={() => onRowClick?.(row.original)}
                  {...(onRowClick
                    ? { tabIndex: 0, onKeyDown: (event: KeyboardEvent<HTMLTableRowElement>) => activateOnKey(event, () => onRowClick(row.original)) }
                    : {})}
                >
                  {row.getVisibleCells().map((cell) => (
                    // Phones scroll the table sideways instead of truncating every cell to nothing.
                    <td key={cell.id} className={cn("px-4 whitespace-nowrap sm:max-w-0 sm:truncate", padY)}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      )}
      {(table.getCanPreviousPage() || table.getCanNextPage()) && (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Button variant="outline" size="sm" onClick={() => table.setPageIndex(0)} disabled={!table.getCanPreviousPage()}>First</Button>
          <Button variant="outline" size="sm" onClick={() => table.previousPage()} disabled={!table.getCanPreviousPage()}>Prev</Button>
          <span className="text-xs text-text-muted">Page {table.getState().pagination.pageIndex + 1} of {table.getPageCount()}</span>
          <Button variant="outline" size="sm" onClick={() => table.nextPage()} disabled={!table.getCanNextPage()}>Next</Button>
          <Button variant="outline" size="sm" onClick={() => table.setPageIndex(table.getPageCount() - 1)} disabled={!table.getCanNextPage()}>Last</Button>
        </div>
      )}
    </div>
  );
}
