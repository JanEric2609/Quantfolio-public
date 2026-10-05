import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Key, Trash2, ShieldCheck, AlertTriangle } from "lucide-react";
import { api } from "../../../lib/api";
import { Button } from "../../../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../../../components/ui/card";
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger } from "../../../components/ui/alert-dialog";
import { usePasskeyRegister } from "./usePasskeyRegister";
import { toast } from "sonner";

export type PasskeyCredential = {
  id: string;
  name: string;
  created_at: string;
  last_used?: string;
};

export function PasskeyList() {
  const queryClient = useQueryClient();
  const addPasskey = usePasskeyRegister();

  const passkeys = useQuery<PasskeyCredential[]>({
    queryKey: ["passkeys"],
    queryFn: () => api<PasskeyCredential[]>("/api/auth/passkey/credentials"),
    retry: false,
  });

  const deletePasskey = useMutation({
    mutationFn: (id: string) =>
      api(`/api/auth/passkey/credentials/${encodeURIComponent(id)}`, { method: "DELETE" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["passkeys"] });
      toast.success("Passkey removed");
    },
    onError: (err: any) => toast.error(`Passkey removal failed: ${err.message ?? err}`),
  });

  return (
    <Card className="bg-surface border-border">
      <CardHeader className="flex flex-row items-center justify-between space-y-0">
        <CardTitle className="flex items-center gap-2 text-base">
          <ShieldCheck size={16} className="text-accent" />
          Passkeys
        </CardTitle>
        <Button variant="outline" size="sm" onClick={() => addPasskey.mutate()} disabled={addPasskey.isPending}>
          {addPasskey.isPending ? "Registering…" : "Add passkey"}
        </Button>
      </CardHeader>
      <CardContent className="space-y-2">
        {passkeys.isLoading && (
          <div className="text-sm text-text-muted">Loading passkeys…</div>
        )}
        {passkeys.isError && (
          <div className="flex items-center gap-2 text-sm text-danger">
            <AlertTriangle size={14} />
            Failed to load passkeys
          </div>
        )}
        {(passkeys.data ?? []).map((key) => (
          <div
            key={key.id}
            className="flex items-center justify-between gap-3 rounded-md border border-border bg-surface-2 p-3 text-sm"
          >
            <div className="flex items-center gap-2">
              <Key size={14} className="text-text-muted" />
              <div>
                <div className="font-medium">{key.name}</div>
                <div className="text-xs text-text-muted">
                  Registered {new Date(key.created_at).toLocaleDateString()}
                  {key.last_used ? ` / used ${new Date(key.last_used).toLocaleDateString()}` : ""}
                </div>
              </div>
            </div>
            <AlertDialog>
              <AlertDialogTrigger asChild>
                <Button variant="ghost" size="sm" className="h-7 text-xs text-danger hover:text-danger">
                  <Trash2 size={12} className="mr-1" /> Delete
                </Button>
              </AlertDialogTrigger>
              <AlertDialogContent className="bg-surface border-border">
                <AlertDialogHeader>
                  <AlertDialogTitle>Delete passkey?</AlertDialogTitle>
                  <AlertDialogDescription>
                    This will permanently remove "{key.name}". This action cannot be undone.
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                  <AlertDialogAction
                    onClick={() => deletePasskey.mutate(key.id)}
                    className="bg-danger hover:bg-red-600"
                  >
                    Delete
                  </AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          </div>
        ))}
        {!passkeys.data?.length && !passkeys.isLoading && (
          <div className="rounded-md border border-dashed border-border p-4 text-sm text-text-muted">
            No passkeys yet. Add one to sign in without a password.
          </div>
        )}
      </CardContent>
    </Card>
  );
}
