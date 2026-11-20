import type { UseFormReturn } from "react-hook-form";
import { useMutation } from "@tanstack/react-query";
import { api } from "../../../lib/api";
import { Input } from "../../../components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../../components/ui/select";
import { FormControl, FormField, FormItem, FormLabel, FormMessage } from "../../../components/ui/form";
import type { HoldingFormData } from "./holdingSchema";

const assetTypes = ["stock", "etf", "bond", "fund", "cash", "crypto", "other"] as const;
const currencies = ["EUR", "USD", "GBP", "CHF", "JPY"] as const;

export function HoldingFormFields({ form }: { form: UseFormReturn<HoldingFormData> }) {
  const ensureTicker = useMutation({
    mutationFn: (ticker: string) => api("/api/quant/price-data/ensure", { method: "POST", body: JSON.stringify({ ticker, days: 90 }) }),
  });
  const fillFundamentals = async (ticker: string) => {
    if (!ticker) return;
    ensureTicker.mutate(ticker);
    try {
      const row = await api<{ data?: { name?: string; longName?: string; shortName?: string } }>(`/api/market/fundamentals/${ticker}`);
      const name = row.data?.longName ?? row.data?.shortName ?? row.data?.name;
      if (name && !form.getValues("name")) form.setValue("name", name, { shouldValidate: true });
    } catch {
      // Cached fundamentals are optional; the required name field stays user-editable.
    }
  };
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <FormField control={form.control} name="ticker" render={({ field }) => <FormItem><FormLabel>Ticker</FormLabel><FormControl><Input {...field} className="font-mono" onBlur={(event) => { field.onBlur(); void fillFundamentals(event.target.value.toUpperCase()); }} onChange={(event) => field.onChange(event.target.value.toUpperCase())} /></FormControl><FormMessage /></FormItem>} />
      <FormField control={form.control} name="isin" render={({ field }) => <FormItem><FormLabel>ISIN</FormLabel><FormControl><Input {...field} className="font-mono" onChange={(event) => field.onChange(event.target.value.toUpperCase())} /></FormControl><FormMessage /></FormItem>} />
      <FormField control={form.control} name="name" render={({ field }) => <FormItem className="sm:col-span-2"><FormLabel>Name</FormLabel><FormControl><Input {...field} /></FormControl><FormMessage /></FormItem>} />
      <FormField control={form.control} name="asset_type" render={({ field }) => <FormItem><FormLabel>Asset type</FormLabel><Select value={field.value} onValueChange={field.onChange}><FormControl><SelectTrigger><SelectValue /></SelectTrigger></FormControl><SelectContent>{assetTypes.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectContent></Select><FormMessage /></FormItem>} />
      <FormField control={form.control} name="currency" render={({ field }) => <FormItem><FormLabel>Currency</FormLabel><Select value={field.value} onValueChange={field.onChange}><FormControl><SelectTrigger><SelectValue /></SelectTrigger></FormControl><SelectContent>{currencies.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectContent></Select><FormMessage /></FormItem>} />
      <FormField control={form.control} name="quantity" render={({ field }) => <FormItem><FormLabel>Quantity</FormLabel><FormControl><Input type="number" step="any" {...field} /></FormControl><FormMessage /></FormItem>} />
      <FormField control={form.control} name="avg_buy_price" render={({ field }) => <FormItem><FormLabel>Average buy</FormLabel><FormControl><Input type="number" step="any" {...field} /></FormControl><FormMessage /></FormItem>} />
      <FormField control={form.control} name="buy_date" render={({ field }) => <FormItem><FormLabel>Buy date</FormLabel><FormControl><Input type="date" {...field} /></FormControl><FormMessage /></FormItem>} />
    </div>
  );
}
