import { useOutletContext } from "react-router-dom";
import { useState } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../../components/ui/tabs";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "../../components/ui/sheet";
import { LotsTable } from "./components/LotsTable";
import { EventsTable } from "./components/EventsTable";
import { formatCurrency, formatDate, formatPercent } from "../../lib/format";
import type { TaxLot, TaxEvent } from "../../lib/api";

export function LotsTab() {
  const { year } = useOutletContext<{ year: number }>();
  const [selectedLot, setSelectedLot] = useState<TaxLot | null>(null);
  const [selectedEvent, setSelectedEvent] = useState<TaxEvent | null>(null);

  return (
    <Tabs defaultValue="lots" className="space-y-4">
      <TabsList>
        <TabsTrigger value="lots">Lots</TabsTrigger>
        <TabsTrigger value="events">Events</TabsTrigger>
      </TabsList>

      <TabsContent value="lots">
        <LotsTable onRowClick={setSelectedLot} />
      </TabsContent>

      <TabsContent value="events">
        <EventsTable year={year} onRowClick={setSelectedEvent} />
      </TabsContent>

      <Sheet open={!!selectedLot} onOpenChange={(open) => { if (!open) setSelectedLot(null); }}>
        <SheetContent aria-describedby={undefined}>
          {selectedLot && (
            <>
              <SheetHeader>
                <SheetTitle>{selectedLot.name ?? selectedLot.isin}</SheetTitle>
              </SheetHeader>
              <div className="space-y-3 mt-4 text-sm">
                <Row label="ISIN" value={selectedLot.isin} mono />
                <Row label="Name" value={selectedLot.name ?? "—"} />
                <Row label="Fund class" value={selectedLot.fund_class} />
                <Row label="Teilfreistellung" value={formatPercent(selectedLot.teilfreistellung_pct, { digits: 0 })} />
                <Row label="Acquired" value={formatDate(selectedLot.acquired_at, { style: "medium" })} />
                <Row label="Quantity" value={`${selectedLot.quantity_remaining} / ${selectedLot.quantity_initial}`} />
                <Row label="Cost basis" value={formatCurrency(selectedLot.cost_basis_eur, "EUR")} />
                <Row label="Fees" value={formatCurrency(selectedLot.fees_eur, "EUR")} />
                <Row label="Source" value={selectedLot.source} />
                {selectedLot.closed_at && <Row label="Closed at" value={formatDate(selectedLot.closed_at, { style: "medium" })} />}
              </div>
            </>
          )}
        </SheetContent>
      </Sheet>

      <Sheet open={!!selectedEvent} onOpenChange={(open) => { if (!open) setSelectedEvent(null); }}>
        <SheetContent aria-describedby={undefined}>
          {selectedEvent && (
            <>
              <SheetHeader>
                <SheetTitle>{selectedEvent.event_type} — {selectedEvent.isin ?? "N/A"}</SheetTitle>
              </SheetHeader>
              <div className="space-y-3 mt-4 text-sm">
                <Row label="Date" value={formatDate(selectedEvent.event_date, { style: "medium" })} />
                <Row label="Type" value={selectedEvent.event_type} />
                <Row label="ISIN" value={selectedEvent.isin ?? "—"} mono />
                <Row label="Gross" value={formatCurrency(selectedEvent.gross_eur, "EUR")} />
                <Row label="Withheld" value={formatCurrency(selectedEvent.withheld_eur, "EUR")} />
                <Row label="Foreign WHT" value={formatCurrency(selectedEvent.foreign_wht_eur, "EUR")} />
                {selectedEvent.realised_gain_eur != null && <Row label="Realised gain" value={formatCurrency(selectedEvent.realised_gain_eur, "EUR")} />}
                <Row label="Source" value={selectedEvent.source} />
                {selectedEvent.notes && <Row label="Notes" value={selectedEvent.notes} />}
              </div>
            </>
          )}
        </SheetContent>
      </Sheet>
    </Tabs>
  );
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex justify-between border-b border-border/50 py-1">
      <span className="text-text-secondary">{label}</span>
      <span className={`text-text-primary ${mono ? "font-mono text-xs" : ""}`}>{value}</span>
    </div>
  );
}
