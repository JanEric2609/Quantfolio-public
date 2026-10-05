import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "../../../lib/zodResolver";
import { Plus } from "lucide-react";
import { Button } from "../../../components/ui/button";
import { Input } from "../../../components/ui/input";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "../../../components/ui/dialog";
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from "../../../components/ui/form";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../../components/ui/select";

import { expenseDefaults, expenseSchema, type ExpenseFormData } from "./expenseSchema";
export type { ExpenseFormData } from "./expenseSchema";

export function AddExpenseDialog({ categories, onSave, pending }: {
  categories: { id: string; name: string }[];
  onSave: (data: ExpenseFormData) => void;
  pending?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const form = useForm<ExpenseFormData>({ resolver: zodResolver(expenseSchema), defaultValues: expenseDefaults });
  const handleSubmit = (values: ExpenseFormData) => {
    onSave(values);
    form.reset(expenseDefaults);
    setOpen(false);
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm"><Plus className="h-4 w-4 mr-1" /> Add Expense</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add Expense</DialogTitle>
        </DialogHeader>
        <Form {...form}><form className="grid gap-3" onSubmit={form.handleSubmit(handleSubmit)}>
          <FormField control={form.control} name="date" render={({ field }) => <FormItem><FormLabel>Date</FormLabel><FormControl><Input type="date" {...field} /></FormControl><FormMessage /></FormItem>} />
          <FormField control={form.control} name="amount" render={({ field }) => <FormItem><FormLabel>Amount</FormLabel><FormControl><Input type="number" step="0.01" {...field} /></FormControl><FormMessage /></FormItem>} />
          <FormField control={form.control} name="currency" render={({ field }) => <FormItem><FormLabel>Currency</FormLabel><Select value={field.value} onValueChange={field.onChange}><FormControl><SelectTrigger><SelectValue /></SelectTrigger></FormControl><SelectContent>{["EUR", "USD", "GBP", "CHF"].map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}</SelectContent></Select><FormMessage /></FormItem>} />
          <FormField control={form.control} name="description" render={({ field }) => <FormItem><FormLabel>Description</FormLabel><FormControl><Input {...field} /></FormControl><FormMessage /></FormItem>} />
          <FormField control={form.control} name="category_id" render={({ field }) => <FormItem><FormLabel>Category</FormLabel><Select value={field.value} onValueChange={field.onChange}><FormControl><SelectTrigger><SelectValue placeholder="Uncategorized" /></SelectTrigger></FormControl><SelectContent>{categories.map((c) => <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>)}</SelectContent></Select><FormMessage /></FormItem>} />
          <FormField control={form.control} name="notes" render={({ field }) => <FormItem><FormLabel>Notes</FormLabel><FormControl><Input {...field} /></FormControl><FormMessage /></FormItem>} />
          <DialogFooter><Button type="submit" disabled={pending}>{pending ? "Saving..." : "Save"}</Button></DialogFooter>
        </form></Form>
      </DialogContent>
    </Dialog>
  );
}
