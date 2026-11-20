import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "../../../lib/api";
import { credentialToJSON, prepareCreationOptions } from "../../../lib/webauthn";

export function usePasskeyRegister(name = "Settings passkey") {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async () => {
      const options = await api<any>("/api/auth/passkey/register/options", {
        method: "POST",
        body: JSON.stringify({ name }),
      });
      const credential = await navigator.credentials.create({
        publicKey: prepareCreationOptions(options),
      });
      const json = credentialToJSON(credential);
      return api("/api/auth/passkey/register/verify", {
        method: "POST",
        body: JSON.stringify({
          challenge: options.challenge,
          credential_id: json.id,
          credential: json,
          name,
          transports: json.response?.transports ?? [],
        }),
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["passkeys"] });
      toast.success("Passkey registered");
    },
    onError: (err: any) => toast.error(`Passkey registration failed: ${err.message ?? err}`),
  });
}
