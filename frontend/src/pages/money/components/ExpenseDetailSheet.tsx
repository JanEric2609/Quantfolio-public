import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "../../../lib/zodResolver";
import { Trash2 } from "lucide-react";
import { Button } from "../../../components/ui/button";
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from "../../../components/ui/form";
import { Input } from "../../../components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../../components/ui/select";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "../../../components/ui/sheet";
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger } from "../../../components/ui/alert-dialog";
import type { ExpenseRow } from "./ExpenseTable";
import { expenseEditSchema, type ExpenseFormData } from "./expenseSchema";

function formValues(row: ExpenseRow): ExpenseFormData {
  return { date: row.date, amount: Number(row.amount), currency: row.currency as ExpenseFormData["currency"], description: row.description, category_id: row.category_id ?? "", notes: row.notes ?? "" };
}

export function ExpenseDetailSheet({ expense, categories, pending, onClose, onSave, onDelete }: {
  expense: ExpenseRow | null;
  categories: { id: string; name: string }[];
  pending?: boolean;
  onClose: () => void;
  onSave: (values: ExpenseFormData) => void;
  onDelete: () => void;
}) {
  const form = useForm<ExpenseFormData>({ resolver: zodResolver(expenseEditSchema) });
  useEffect(() => { if (expense) form.reset(formValues(expense)); }, [expense, form]);
  return <Sheet open={Boolean(expense)} onOpenChange={(open) => !open && onClose()}>
    <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-lg">
      {expense && <>
        <SheetHeader><SheetTitle>Edit expense</SheetTitle><SheetDescription>{expense.source}{expense.account_name ? ` / ${expense.account_name}` : ""}</SheetDescription></SheetHeader>
        <Form {...form}><form className="mt-5 grid gap-3" onSubmit={form.handleSubmit(onSave)}>
          <FormField control={form.control} name="date" render={({ field }) => <FormItem><FormLabel>Date</FormLabel><FormControl><Input type="date" {...field} /></FormControl><FormMessage /></FormItem>} />
          <FormField control={form.control} name="amount" render={({ field }) => <FormItem><FormLabel>Amount</FormLabel><FormControl><Input type="number" step="0.01" {...field} /></FormControl><FormMessage /></FormItem>} />
          <FormField control={form.control} name="currency" render={({ field }) => <FormItem><FormLabel>Currency</FormLabel><Select value={field.value} onValueChange={field.onChange}><FormControl><SelectTrigger><SelectValue /></SelectTrigger></FormControl><SelectContent>{["EUR", "USD", "GBP", "CHF"].map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}</SelectContent></Select><FormMessage /></FormItem>} />
          <FormField control={form.control} name="description" render={({ field }) => <FormItem><FormLabel>Description</FormLabel><FormControl><Input {...field} /></FormControl><FormMessage /></FormItem>} />
          <FormField control={form.control} name="category_id" render={({ field }) => <FormItem><FormLabel>Category</FormLabel><Select value={field.value} onValueChange={field.onChange}><FormControl><SelectTrigger><SelectValue placeholder="Uncategorized" /></SelectTrigger></FormControl><SelectContent>{categories.map((c) => <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>)}</SelectContent></Select><FormMessage /></FormItem>} />
          <FormField control={form.control} name="notes" render={({ field }) => <FormItem><FormLabel>Notes</FormLabel><FormControl><Input {...field} /></FormControl><FormMessage /></FormItem>} />
          <div className="flex justify-between pt-2">
            <AlertDialog><AlertDialogTrigger asChild><Button type="button" variant="destructive"><Trash2 className="h-4 w-4" />Delete</Button></AlertDialogTrigger><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>Delete expense?</AlertDialogTitle><AlertDialogDescription>This expense will be removed.</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>Cancel</AlertDialogCancel><AlertDialogAction onClick={onDelete}>Delete</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>
            <Button type="submit" disabled={pending}>{pending ? "Saving..." : "Save changes"}</Button>
          </div>
        </form></Form>
      </>}
    </SheetContent>
  </Sheet>;
}
