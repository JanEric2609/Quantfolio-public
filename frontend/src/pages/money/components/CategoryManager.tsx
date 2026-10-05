import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Settings2, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { api } from "../../../lib/api";
import { Button } from "../../../components/ui/button";
import { Input } from "../../../components/ui/input";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger } from "../../../components/ui/sheet";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../../components/ui/select";
import { Badge } from "../../../components/ui/badge";
import { formatCurrency } from "../../../lib/format";
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger } from "../../../components/ui/alert-dialog";

type Category = {
  id: string;
  name: string;
  color: string;
  icon?: string;
  type: string;
  target_amount?: number | null;
  target_date?: string | null;
};
const empty = { name: "", color: "#3B82F6", icon: "tag", type: "expense", target_amount: "", target_date: "" };

function toPayload(form: typeof empty) {
  return {
    ...form,
    target_amount: form.target_amount === "" ? null : form.target_amount,
    target_date: form.target_date === "" ? null : form.target_date,
  };
}

export function CategoryManager({ categories }: {
  categories: Category[];
}) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<Category | null>(null);
  const [form, setForm] = useState(empty);
  const queryClient = useQueryClient();
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["categories"] });
  const create = useMutation({ mutationFn: () => api("/api/budget/categories", { method: "POST", body: JSON.stringify(toPayload(form)) }), onSuccess: () => { refresh(); setForm(empty); toast.success("Category added"); }, onError: (e: Error) => toast.error(e.message) });
  const update = useMutation({ mutationFn: () => api(`/api/budget/categories/${editing?.id}`, { method: "PUT", body: JSON.stringify(toPayload(form)) }), onSuccess: () => { refresh(); setEditing(null); setForm(empty); toast.success("Category updated"); }, onError: (e: Error) => toast.error(e.message) });
  const remove = useMutation({ mutationFn: (id: string) => api(`/api/budget/categories/${id}`, { method: "DELETE" }), onSuccess: () => { refresh(); toast.success("Category deleted"); }, onError: (e: Error) => toast.error(e.message) });
  const edit = (category: Category) => {
    setEditing(category);
    setForm({
      name: category.name,
      color: category.color,
      icon: category.icon ?? "tag",
      type: category.type,
      target_amount: category.target_amount != null ? String(category.target_amount) : "",
      target_date: category.target_date ?? "",
    });
  };
  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetTrigger asChild>
        <Button variant="outline" size="sm"><Settings2 className="h-4 w-4 mr-1" /> Categories</Button>
      </SheetTrigger>
      <SheetContent side="right" className="w-full overflow-y-auto sm:max-w-lg">
        <SheetHeader><SheetTitle>Manage Categories</SheetTitle></SheetHeader>
        <form className="mt-5 grid gap-2 rounded-md border border-border p-3" onSubmit={(e) => { e.preventDefault(); editing ? update.mutate() : create.mutate(); }}>
          <Input aria-label="Category name" placeholder="Category name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
          <div className="grid grid-cols-[1fr_7rem] gap-2">
            <Select value={form.type} onValueChange={(type) => setForm({ ...form, type })}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{["expense", "income", "investment"].map((type) => <SelectItem key={type} value={type}>{type}</SelectItem>)}</SelectContent></Select>
            <Input aria-label="Category color" type="color" value={form.color} onChange={(e) => setForm({ ...form, color: e.target.value })} />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Input aria-label="Goal target amount" type="number" step="0.01" placeholder="Goal amount (optional)" value={form.target_amount} onChange={(e) => setForm({ ...form, target_amount: e.target.value })} />
            <Input aria-label="Goal target date" type="date" value={form.target_date} onChange={(e) => setForm({ ...form, target_date: e.target.value })} />
          </div>
          <div className="flex gap-2"><Button size="sm" type="submit" disabled={create.isPending || update.isPending}>{editing ? "Save" : "Create"}</Button>{editing && <Button size="sm" variant="outline" type="button" onClick={() => { setEditing(null); setForm(empty); }}>Cancel</Button>}</div>
        </form>
        <div className="space-y-2 max-h-80 overflow-y-auto">
          {categories.length === 0 && (
            <p className="text-sm text-text-muted">No categories defined.</p>
          )}
          {categories.map((cat) => (
            <div key={cat.id} className="flex items-center justify-between gap-2 rounded-md border border-border p-3 text-sm">
              <div className="flex items-center gap-2">
                <span className="h-3 w-3 rounded-full" style={{ backgroundColor: cat.color }} />
                <span>{cat.name}</span>
                {cat.target_amount != null && (
                  <Badge variant="outline">
                    {formatCurrency(Number(cat.target_amount), "EUR", { digits: 0 })} goal
                  </Badge>
                )}
              </div>
              <div className="flex items-center gap-1"><Badge variant="secondary">{cat.type}</Badge><Button size="sm" variant="ghost" onClick={() => edit(cat)}>Edit</Button>
                <AlertDialog><AlertDialogTrigger asChild><Button size="sm" variant="ghost" aria-label={`Delete ${cat.name}`}><Trash2 className="h-4 w-4" /></Button></AlertDialogTrigger><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>Delete category?</AlertDialogTitle><AlertDialogDescription>Expenses using it become uncategorized.</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>Cancel</AlertDialogCancel><AlertDialogAction onClick={() => remove.mutate(cat.id)}>Delete</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog>
              </div>
            </div>
          ))}
        </div>
      </SheetContent>
    </Sheet>
  );
}
