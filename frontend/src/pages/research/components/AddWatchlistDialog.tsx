import { useState } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "../../../lib/zodResolver";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { z } from "zod";
import { toast } from "sonner";
import { api } from "../../../lib/api";
import { Button } from "../../../components/ui/button";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "../../../components/ui/dialog";
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from "../../../components/ui/form";
import { Input } from "../../../components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../../components/ui/select";

const watchlistSchema = z.object({
  ticker: z.string().trim().min(1, "Ticker required"),
  name: z.string().trim().optional(),
  horizon_tag: z.enum(["short", "mid", "long"]),
});
type WatchlistForm = z.infer<typeof watchlistSchema>;

export function AddWatchlistDialog() {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const form = useForm<WatchlistForm>({ resolver: zodResolver(watchlistSchema), defaultValues: { ticker: "", name: "", horizon_tag: "mid" } });
  const create = useMutation({
    mutationFn: (values: WatchlistForm) => api("/api/portfolio/watchlist", { method: "POST", body: JSON.stringify({ ...values, ticker: values.ticker.toUpperCase(), name: values.name || values.ticker.toUpperCase() }) }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
      toast.success("Watchlist item added");
      form.reset();
      setOpen(false);
    },
    onError: (error: Error) => toast.error(error.message),
  });
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild><Button size="sm"><Plus className="h-4 w-4" />Add</Button></DialogTrigger>
      <DialogContent aria-describedby={undefined}>
        <DialogHeader><DialogTitle>Add watchlist item</DialogTitle></DialogHeader>
        <Form {...form}><form className="space-y-4" onSubmit={form.handleSubmit((values) => create.mutate(values))}>
          <FormField control={form.control} name="ticker" render={({ field }) => <FormItem><FormLabel>Ticker</FormLabel><FormControl><Input {...field} className="font-mono" onChange={(event) => field.onChange(event.target.value.toUpperCase())} /></FormControl><FormMessage /></FormItem>} />
          <FormField control={form.control} name="name" render={({ field }) => <FormItem><FormLabel>Name</FormLabel><FormControl><Input {...field} /></FormControl><FormMessage /></FormItem>} />
          <FormField control={form.control} name="horizon_tag" render={({ field }) => <FormItem><FormLabel>Horizon</FormLabel><Select value={field.value} onValueChange={field.onChange}><FormControl><SelectTrigger><SelectValue /></SelectTrigger></FormControl><SelectContent><SelectItem value="short">Short</SelectItem><SelectItem value="mid">Mid</SelectItem><SelectItem value="long">Long</SelectItem></SelectContent></Select><FormMessage /></FormItem>} />
          <DialogFooter><Button type="submit" disabled={create.isPending}>Add</Button></DialogFooter>
        </form></Form>
      </DialogContent>
    </Dialog>
  );
}
