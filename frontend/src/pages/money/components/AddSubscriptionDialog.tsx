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

const subscriptionSchema = z.object({
  name: z.string().trim().min(1, "Name required"),
  amount: z.coerce.number().positive("Amount must be positive"),
  currency: z.enum(["EUR", "USD", "GBP", "CHF"]),
  billing_cycle: z.enum(["weekly", "monthly", "quarterly", "annual"]),
  next_due_date: z.string().min(1, "Due date required"),
  category_id: z.string().optional(),
  payment_method: z.string().optional(),
  notes: z.string().optional(),
});
export type SubscriptionFormData = z.infer<typeof subscriptionSchema>;
const defaults: SubscriptionFormData = {
  name: "", amount: 0, currency: "EUR", billing_cycle: "monthly",
  next_due_date: "",
  category_id: "", payment_method: "", notes: "",
};

export function AddSubscriptionDialog({ categories, onSave, open, onOpenChange, initialValues, pending }: {
  categories: { id: string; name: string }[];
  onSave: (data: SubscriptionFormData) => void;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  initialValues?: Partial<SubscriptionFormData>;
  pending?: boolean;
}) {
  const [innerOpen, setInnerOpen] = useState(false);
  const actualOpen = open ?? innerOpen;
  const setOpen = onOpenChange ?? setInnerOpen;
  const form = useForm<SubscriptionFormData>({ resolver: zodResolver(subscriptionSchema), defaultValues: { ...defaults, next_due_date: new Date().toISOString().slice(0, 10), ...initialValues } });
  useEffect(() => { if (actualOpen) form.reset({ ...defaults, next_due_date: new Date().toISOString().slice(0, 10), ...initialValues }); }, [actualOpen, form, initialValues]);
  const handleSubmit = (values: SubscriptionFormData) => {
    onSave(values);
    form.reset();
    setOpen(false);
  };

  return (
    <Dialog open={actualOpen} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm"><Plus className="h-4 w-4 mr-1" /> Add Subscription</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add Subscription</DialogTitle>
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
          <FormField control={form.control} name="billing_cycle" render={({ field }) => <FormItem><FormLabel>Billing cycle</FormLabel><Select value={field.value} onValueChange={field.onChange}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="weekly">Weekly</SelectItem>
              <SelectItem value="monthly">Monthly</SelectItem>
              <SelectItem value="quarterly">Quarterly</SelectItem>
              <SelectItem value="annual">Annual</SelectItem>
            </SelectContent>
          </Select><FormMessage /></FormItem>} />
          <FormField control={form.control} name="next_due_date" render={({ field }) => <FormItem><FormLabel>Next due</FormLabel><FormControl><Input type="date" {...field} /></FormControl><FormMessage /></FormItem>} />
          <FormField control={form.control} name="category_id" render={({ field }) => <FormItem><FormLabel>Category</FormLabel><Select value={field.value} onValueChange={field.onChange}>
            <SelectTrigger><SelectValue placeholder="Category" /></SelectTrigger>
            <SelectContent>
              {categories.map((c) => (
                <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>
              ))}
            </SelectContent>
          </Select><FormMessage /></FormItem>} />
          <FormField control={form.control} name="payment_method" render={({ field }) => <FormItem><FormLabel>Payment method</FormLabel><FormControl><Input {...field} /></FormControl><FormMessage /></FormItem>} />
          <FormField control={form.control} name="notes" render={({ field }) => <FormItem><FormLabel>Notes</FormLabel><FormControl><Input {...field} /></FormControl><FormMessage /></FormItem>} />
          <DialogFooter><Button type="submit" disabled={pending}>{pending ? "Saving..." : "Save"}</Button></DialogFooter>
        </form></Form>
      </DialogContent>
    </Dialog>
  );
}
