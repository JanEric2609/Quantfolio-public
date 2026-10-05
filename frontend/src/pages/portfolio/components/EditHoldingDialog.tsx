import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "../../../lib/zodResolver";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type Holding } from "../../../lib/api";
import { Button } from "../../../components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "../../../components/ui/dialog";
import { Form } from "../../../components/ui/form";
import { HoldingFormFields } from "./HoldingFormFields";
import { holdingSchema, type HoldingFormData } from "./holdingSchema";

const valuesFor = (holding: Holding): Partial<HoldingFormData> => ({
  ticker: holding.ticker ?? "",
  isin: holding.isin ?? "",
  name: holding.name,
  asset_type: holding.asset_type as HoldingFormData["asset_type"],
  quantity: Number(holding.quantity),
  avg_buy_price: holding.avg_buy_price != null ? Number(holding.avg_buy_price) : undefined,
  currency: holding.currency as HoldingFormData["currency"],
  buy_date: holding.buy_date ?? "",
});

export function EditHoldingDialog({ holding, open, onOpenChange }: { holding: Holding; open: boolean; onOpenChange: (next: boolean) => void }) {
  const queryClient = useQueryClient();
  const [current] = useState(holding);
  const form = useForm<HoldingFormData>({ resolver: zodResolver(holdingSchema), defaultValues: valuesFor(current) });
  useEffect(() => form.reset(valuesFor(holding)), [form, holding]);
  const update = useMutation({
    mutationFn: (values: HoldingFormData) => api(`/api/portfolio/holdings/${holding.id}`, { method: "PUT", body: JSON.stringify({ ...values, buy_date: values.buy_date || null }) }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["holdings"] });
      queryClient.invalidateQueries({ queryKey: ["wealth"] });
      queryClient.invalidateQueries({ queryKey: ["portfolio-activity"] });
      queryClient.invalidateQueries({ queryKey: ["portfolio-snapshots"] });
      toast.success("Holding updated");
      onOpenChange(false);
    },
    onError: (error: Error) => toast.error(error.message),
  });
  return <Dialog open={open} onOpenChange={onOpenChange}><DialogContent aria-describedby={undefined}><DialogHeader><DialogTitle>Edit holding</DialogTitle></DialogHeader><Form {...form}><form className="space-y-4" onSubmit={form.handleSubmit((values) => update.mutate(values))}><HoldingFormFields form={form} /><DialogFooter><Button type="submit" disabled={update.isPending}>Save</Button></DialogFooter></form></Form></DialogContent></Dialog>;
}
