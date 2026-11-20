import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "../../../lib/zodResolver";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { toast } from "sonner";
import { api } from "../../../lib/api";
import { Button } from "../../../components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "../../../components/ui/dialog";
import { Form } from "../../../components/ui/form";
import { HoldingFormFields } from "./HoldingFormFields";
import { holdingDefaults, holdingSchema, type HoldingFormData } from "./holdingSchema";

export function AddHoldingDialog({ initialValues }: { initialValues?: Partial<HoldingFormData> }) {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const form = useForm<HoldingFormData>({ resolver: zodResolver(holdingSchema), defaultValues: { ...holdingDefaults, ...initialValues } });
  const create = useMutation({
    mutationFn: (values: HoldingFormData) => api("/api/portfolio/holdings", { method: "POST", body: JSON.stringify({ ...values, buy_date: values.buy_date || null }) }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["holdings"] });
      queryClient.invalidateQueries({ queryKey: ["wealth"] });
      queryClient.invalidateQueries({ queryKey: ["portfolio-activity"] });
      queryClient.invalidateQueries({ queryKey: ["portfolio-snapshots"] });
      toast.success("Holding added");
      form.reset({ ...holdingDefaults, ...initialValues });
      setOpen(false);
    },
    onError: (error: Error) => toast.error(error.message),
  });
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button><Plus className="h-4 w-4" />Add holding</Button></DialogTrigger>
      <DialogContent aria-describedby={undefined}>
        <DialogHeader><DialogTitle>Add holding</DialogTitle></DialogHeader>
        <Form {...form}><form className="space-y-4" onSubmit={form.handleSubmit((values) => create.mutate(values))}>
          <HoldingFormFields form={form} />
          <DialogFooter><Button type="submit" disabled={create.isPending}>{create.isPending ? "Adding..." : "Add holding"}</Button></DialogFooter>
        </form></Form>
      </DialogContent>
    </Dialog>
  );
}
