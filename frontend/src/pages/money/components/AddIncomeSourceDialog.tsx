import { useState } from "react";
import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "../../../lib/zodResolver";
import { z } from "zod";
import { Plus } from "lucide-react";
import { Button } from "../../../components/ui/button";
import { Input } from "../../../components/ui/input";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "../../../components/ui/dialog";
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from "../../../components/ui/form";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../../components/ui/select";

const incomeSourceSchema = z.object({
  name: z.string().trim().min(1, "Name required"),
  amount: z.coerce.number().positive("Amount must be positive"),
  currency: z.enum(["EUR", "USD", "GBP", "CHF"]),
  cadence: z.enum(["weekly", "monthly", "quarterly", "annual"]),
  next_date: z.string().min(1, "Next date required"),
});
export type IncomeSourceFormData = z.infer<typeof incomeSourceSchema>;
const defaults: IncomeSourceFormData = {
  name: "", amount: 0, currency: "EUR", cadence: "monthly",
  next_date: "",
};

export function AddIncomeSourceDialog({ onSave, open, onOpenChange, initialValues, pending }: {
  onSave: (data: IncomeSourceFormData) => void;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  initialValues?: Partial<IncomeSourceFormData>;
  pending?: boolean;
}) {
  const [innerOpen, setInnerOpen] = useState(false);
  const actualOpen = open ?? innerOpen;
  const setOpen = onOpenChange ?? setInnerOpen;
  const form = useForm<IncomeSourceFormData>({ resolver: zodResolver(incomeSourceSchema), defaultValues: { ...defaults, next_date: new Date().toISOString().slice(0, 10), ...initialValues } });
  useEffect(() => { if (actualOpen) form.reset({ ...defaults, next_date: new Date().toISOString().slice(0, 10), ...initialValues }); }, [actualOpen, form, initialValues]);
  const handleSubmit = (values: IncomeSourceFormData) => {
    onSave(values);
    form.reset();
    setOpen(false);
  };

  return (
    <Dialog open={actualOpen} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm"><Plus className="h-4 w-4 mr-1" /> Add Income Source</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add Income Source</DialogTitle>
        </DialogHeader>
        <Form {...form}><form className="grid gap-3" onSubmit={form.handleSubmit(handleSubmit)}>
          <FormField control={form.control} name="name" render={({ field }) => <FormItem><FormLabel>Name</FormLabel><FormControl><Input {...field} /></FormControl><FormMessage /></FormItem>} />
          <FormField control={form.control} name="amount" render={({ field }) => <FormItem><FormLabel>Amount</FormLabel><FormControl><Input type="number" step="0.01" {...field} /></FormControl><FormMessage /></FormItem>} />
          <FormField control={form.control} name="currency" render={({ field }) => <FormItem><FormLabel>Currency</FormLabel><Select value={field.value} onValueChange={field.onChange}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="EUR">EUR</SelectItem>
              <SelectItem value="USD">USD</SelectItem>
              <SelectItem value="GBP">GBP</SelectItem>
              <SelectItem value="CHF">CHF</SelectItem>
            </SelectContent>
          </Select><FormMessage /></FormItem>} />
          <FormField control={form.control} name="cadence" render={({ field }) => <FormItem><FormLabel>Cadence</FormLabel><Select value={field.value} onValueChange={field.onChange}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="weekly">Weekly</SelectItem>
              <SelectItem value="monthly">Monthly</SelectItem>
              <SelectItem value="quarterly">Quarterly</SelectItem>
              <SelectItem value="annual">Annual</SelectItem>
            </SelectContent>
          </Select><FormMessage /></FormItem>} />
          <FormField control={form.control} name="next_date" render={({ field }) => <FormItem><FormLabel>Next expected</FormLabel><FormControl><Input type="date" {...field} /></FormControl><FormMessage /></FormItem>} />
          <DialogFooter><Button type="submit" disabled={pending}>{pending ? "Saving..." : "Save"}</Button></DialogFooter>
        </form></Form>
      </DialogContent>
    </Dialog>
  );
}
