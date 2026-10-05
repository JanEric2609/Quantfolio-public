import { useState } from "react";
import { useForm } from "react-hook-form";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { toast } from "sonner";
import { api, type TaxEvent } from "../../../lib/api";
import { DataTable } from "../../../components/composed/DataTable";
import { Badge } from "../../../components/ui/badge";
import { Button } from "../../../components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "../../../components/ui/dialog";
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from "../../../components/ui/form";
import { Input } from "../../../components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../../components/ui/select";
import { Textarea } from "../../../components/ui/textarea";
import { formatCurrency, formatDate } from "../../../lib/format";
import type { ColumnDef } from "@tanstack/react-table";

type ManualTaxEventValues = {
  event_type: TaxEvent["event_type"];
  event_date: string;
  isin: string;
  name: string;
  gross_eur: number;
  withheld_eur: number;
  foreign_wht_eur: number;
  realised_gain_eur: string;
  institution: "none" | "dkb" | "scalable" | "other";
  notes: string;
};

// Each bank applies only its own Freistellungsauftrag, so the cockpit needs to know where it was booked.
const BANKS: { value: ManualTaxEventValues["institution"]; label: string }[] = [
  { value: "none", label: "Not set" },
  { value: "dkb", label: "DKB" },
  { value: "scalable", label: "Scalable Capital" },
  { value: "other", label: "Another bank" },
];
const BANK_LABEL: Record<string, string> = { dkb: "DKB", scalable: "Scalable", other: "Other" };

const eventTypes: TaxEvent["event_type"][] = [
  "dividend",
  "interest",
  "sale",
  "vorabpauschale",
  "withholding",
  "fee",
];

function defaultEventValues(year: number): ManualTaxEventValues {
  const today = new Date().toISOString().slice(0, 10);
  return {
    event_type: "dividend",
    event_date: today.startsWith(`${year}-`) ? today : `${year}-01-01`,
    isin: "",
    name: "",
    gross_eur: 0,
    withheld_eur: 0,
    foreign_wht_eur: 0,
    realised_gain_eur: "",
    institution: "none",
    notes: "",
  };
}

const columns: ColumnDef<TaxEvent>[] = [
  { accessorKey: "event_date", header: "Date", cell: ({ row }) => formatDate(row.original.event_date, { style: "short" }) },
  { accessorKey: "event_type", header: "Type", cell: ({ row }) => <Badge variant="outline" className="capitalize">{row.original.event_type}</Badge> },
  { accessorKey: "isin", header: "ISIN", cell: ({ row }) => row.original.isin ? <span className="font-mono text-xs">{row.original.isin}</span> : "—" },
  { accessorKey: "gross_eur", header: "Gross", cell: ({ row }) => formatCurrency(row.original.gross_eur, "EUR") },
  { accessorKey: "withheld_eur", header: "Withheld", cell: ({ row }) => formatCurrency(row.original.withheld_eur, "EUR") },
  { accessorKey: "foreign_wht_eur", header: "Foreign WHT", cell: ({ row }) => formatCurrency(row.original.foreign_wht_eur, "EUR") },
  { accessorKey: "realised_gain_eur", header: "Realised gain", cell: ({ row }) => row.original.realised_gain_eur != null ? formatCurrency(row.original.realised_gain_eur, "EUR") : "—" },
  { accessorKey: "institution", header: "Bank", cell: ({ row }) => row.original.institution ? (BANK_LABEL[row.original.institution] ?? row.original.institution) : "—" },
  { accessorKey: "source", header: "Source" },
];

export function EventsTable({ year, onRowClick }: { year: number; onRowClick?: (event: TaxEvent) => void }) {
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const events = useQuery({
    queryKey: ["tax", "events", year],
    queryFn: () => api<{ items: TaxEvent[] }>(`/api/tax/events?year=${year}`),
  });

  const form = useForm<ManualTaxEventValues>({
    defaultValues: defaultEventValues(year),
  });

  const handleCreateOpenChange = (open: boolean) => {
    if (open) {
      form.reset(defaultEventValues(year));
    }
    setCreateOpen(open);
  };

  const createEvent = useMutation({
    mutationFn: (values: ManualTaxEventValues) =>
      api<{ id: string }>("/api/tax/events", {
        method: "POST",
        body: JSON.stringify({
          event_type: values.event_type,
          event_date: values.event_date,
          isin: values.isin || undefined,
          name: values.name || undefined,
          gross_eur: values.gross_eur,
          withheld_eur: values.withheld_eur,
          foreign_wht_eur: values.foreign_wht_eur,
          realised_gain_eur: values.realised_gain_eur === "" ? undefined : Number(values.realised_gain_eur),
          source: "manual",
          institution: values.institution === "none" ? undefined : values.institution,
          notes: values.notes || undefined,
        }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tax", "events", year] });
      queryClient.invalidateQueries({ queryKey: ["tax", "overview"] });
      form.reset(defaultEventValues(year));
      setCreateOpen(false);
      toast.success("Tax event added");
    },
    onError: (err: Error) => toast.error(`Failed to add tax event: ${err.message}`),
  });

  const items = events.data?.items ?? [];

  return (
    <Dialog open={createOpen} onOpenChange={handleCreateOpenChange}>
      <DataTable
        data={items}
        columns={columns}
        onRowClick={onRowClick}
        enableFilter
        csvFilename={`tax-events-${year}.csv`}
        emptyState={<span>No tax events recorded for {year}.</span>}
        rightSlot={
          <DialogTrigger asChild>
            <Button size="sm" className="gap-2">
              <Plus className="h-4 w-4" />
              Add event
            </Button>
          </DialogTrigger>
        }
      />
      <DialogContent className="max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Add tax event</DialogTitle>
          <DialogDescription>Record a manual tax event for the tax ledger.</DialogDescription>
        </DialogHeader>
        <Form {...form}>
          <form onSubmit={form.handleSubmit((values) => createEvent.mutate(values))} className="space-y-4">
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
              <FormField
                control={form.control}
                name="event_type"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Event type</FormLabel>
                    <Select value={field.value} onValueChange={field.onChange}>
                      <FormControl>
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                      </FormControl>
                      <SelectContent>
                        {eventTypes.map((type) => (
                          <SelectItem key={type} value={type} className="capitalize">
                            {type}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="event_date"
                rules={{
                  required: "Event date is required",
                  validate: (value) => value.startsWith(`${year}-`) || `Date must be in ${year}`,
                }}
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Date</FormLabel>
                    <FormControl>
                      <Input type="date" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="institution"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Bank</FormLabel>
                    <Select value={field.value} onValueChange={field.onChange}>
                      <FormControl>
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                      </FormControl>
                      <SelectContent>
                        {BANKS.map((bank) => (
                          <SelectItem key={bank.value} value={bank.value}>
                            {bank.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <TextField control={form.control} name="isin" label="ISIN" />
              <TextField control={form.control} name="name" label="Name" />
              <NumberField control={form.control} name="gross_eur" label="Gross (EUR)" />
              <NumberField control={form.control} name="withheld_eur" label="Withheld (EUR)" />
              <NumberField control={form.control} name="foreign_wht_eur" label="Foreign WHT (EUR)" />
              <FormField
                control={form.control}
                name="realised_gain_eur"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Realised gain (EUR)</FormLabel>
                    <FormControl>
                      <Input type="number" step="0.01" {...field} placeholder="Optional" />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
            </div>
            <FormField
              control={form.control}
              name="notes"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Notes</FormLabel>
                  <FormControl>
                    <Textarea {...field} placeholder="Optional source notes" />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <DialogFooter>
              <Button type="submit" disabled={createEvent.isPending}>
                {createEvent.isPending ? "Adding..." : "Add event"}
              </Button>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  );
}

function TextField({
  control,
  name,
  label,
}: {
  control: ReturnType<typeof useForm<ManualTaxEventValues>>["control"];
  name: "isin" | "name";
  label: string;
}) {
  return (
    <FormField
      control={control}
      name={name}
      render={({ field }) => (
        <FormItem>
          <FormLabel>{label}</FormLabel>
          <FormControl>
            <Input {...field} />
          </FormControl>
          <FormMessage />
        </FormItem>
      )}
    />
  );
}

function NumberField({
  control,
  name,
  label,
}: {
  control: ReturnType<typeof useForm<ManualTaxEventValues>>["control"];
  name: "gross_eur" | "withheld_eur" | "foreign_wht_eur";
  label: string;
}) {
  return (
    <FormField
      control={control}
      name={name}
      render={({ field }) => (
        <FormItem>
          <FormLabel>{label}</FormLabel>
          <FormControl>
            <Input
              type="number"
              min="0"
              step="0.01"
              {...field}
              value={field.value ?? ""}
              onChange={(event) => field.onChange(Number(event.target.value))}
            />
          </FormControl>
          <FormMessage />
        </FormItem>
      )}
    />
  );
}
