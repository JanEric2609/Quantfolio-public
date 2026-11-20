import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Check, ChevronRight } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { zodResolver } from "../lib/zodResolver";
import { api, SetupStatus } from "../lib/api";
import { credentialToJSON, prepareCreationOptions } from "../lib/webauthn";
import { Button } from "../components/ui/button";
import { Form, FormField, FormItem, FormLabel, FormControl, FormMessage } from "../components/ui/form";
import { Input } from "../components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { toast } from "sonner";

const steps = [
  { id: 0, label: "Account", description: "Create your account" },
  { id: 1, label: "Passkey", description: "Register a passkey (optional)" },
  { id: 2, label: "LLM", description: "Configure LLM settings" },
  { id: 3, label: "DKB", description: "Connect to DKB (optional)" },
  { id: 4, label: "Alpha Vantage", description: "Add API key (optional)" },
  { id: 5, label: "Finnhub", description: "Add API key (optional)" },
  { id: 6, label: "Done", description: "Review and complete" },
];

// Zod schemas per step
const accountSchema = z.object({
  username: z.string().min(3, "Username must be at least 3 characters"),
  password: z.string().min(8, "Password must be at least 8 characters"),
  setup_token: z.string().trim().min(1, "Enter the setup token from the server log"),
});

const llmSchema = z.object({
  llm_base_url: z.string().url("Must be a valid URL").default("http://127.0.0.1:8080/v1"),
  llm_model: z.string().default("qwen3.5-9b"),
});

const dkbSchema = z.object({
  dkb_username: z.string().optional(),
  dkb_pin: z.string().optional(),
  dkb_product_id: z.string().optional(),
});

const alphaSchema = z.object({
  alphavantage_key: z.string().optional(),
});

const finnhubSchema = z.object({
  finnhub_key: z.string().optional(),
});

type AccountFormValues = z.infer<typeof accountSchema>;
type LlmFormValues = z.infer<typeof llmSchema>;
type DkbFormValues = z.infer<typeof dkbSchema>;
type AlphaFormValues = z.infer<typeof alphaSchema>;
type FinnhubFormValues = z.infer<typeof finnhubSchema>;

export function SetupWizard() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { data: setupStatus } = useQuery({
    queryKey: ["setup"],
    queryFn: () => api<SetupStatus>("/api/setup/status"),
  });

  const [currentStep, setCurrentStep] = useState(0);
  const [skipPasskey, setSkipPasskey] = useState(false);

  // Forms per step
  const accountForm = useForm<AccountFormValues>({
    resolver: zodResolver(accountSchema),
    defaultValues: { username: "", password: "", setup_token: "" },
  });

  const llmForm = useForm<LlmFormValues>({
    resolver: zodResolver(llmSchema),
    defaultValues: { llm_base_url: "http://127.0.0.1:8080/v1", llm_model: "qwen3.5-9b" },
  });

  const dkbForm = useForm<DkbFormValues>({
    resolver: zodResolver(dkbSchema),
    defaultValues: { dkb_username: "", dkb_pin: "", dkb_product_id: "" },
  });

  const alphaForm = useForm<AlphaFormValues>({
    resolver: zodResolver(alphaSchema),
    defaultValues: { alphavantage_key: "" },
  });

  const finnhubForm = useForm<FinnhubFormValues>({
    resolver: zodResolver(finnhubSchema),
    defaultValues: { finnhub_key: "" },
  });

  // Mutations
  const register = useMutation({
    mutationFn: (data: AccountFormValues) =>
      api("/api/auth/register", {
        method: "POST",
        body: JSON.stringify({ ...data, setup_token: data.setup_token.trim() }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["setup"] });
      toast.success("Account created");
      setCurrentStep(1);
    },
    onError: (err) => toast.error(err.message),
  });

  const registerPasskey = useMutation({
    mutationFn: async () => {
      if (!window.PublicKeyCredential) {
        throw new Error("This browser does not support passkeys.");
      }
      const options = await api<any>("/api/auth/passkey/register/options", {
        method: "POST",
        body: JSON.stringify({ name: "Primary passkey" }),
      });
      const credential = await navigator.credentials.create({
        publicKey: prepareCreationOptions(options),
      });
      if (!credential) throw new Error("Passkey registration cancelled");
      const credentialJson = credentialToJSON(credential);
      return api("/api/auth/passkey/register/verify", {
        method: "POST",
        body: JSON.stringify({
          challenge: options.challenge,
          credential_id: credentialJson.id,
          credential: credentialJson,
          name: "Primary passkey",
          transports: credentialJson.response?.transports ?? [],
        }),
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["setup"] });
      toast.success("Passkey registered");
      setCurrentStep(2);
    },
    onError: (err) => toast.error(err.message),
  });

  const saveSettings = useMutation({
    mutationFn: (body: unknown) =>
      api("/api/settings", { method: "PUT", body: JSON.stringify(body) }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["setup"] });
    },
    onError: (err) => toast.error(err.message),
  });

  // Step handlers
  const onAccountSubmit = (data: AccountFormValues) => register.mutate(data);

  const onPasskeySkip = () => {
    setSkipPasskey(true);
    setCurrentStep(2);
  };

  const onPasskeyRegister = () => {
    registerPasskey.mutate();
  };

  const onLlmSubmit = (data: LlmFormValues) => {
    saveSettings.mutate({
      settings: { llm_base_url: data.llm_base_url, llm_model: data.llm_model },
      integrations: { llm: { secret: "local" } },
    });
    setCurrentStep(3);
  };

  const onDkbSubmit = (data: DkbFormValues) => {
    if (data.dkb_username && data.dkb_pin) {
      saveSettings.mutate({
        settings: {
          dkb_fints_url: "https://fints.dkb.de/fints",
          dkb_blz: "12030000",
          dkb_provider: "fints",
          dkb_product_id: data.dkb_product_id || null,
        },
        integrations: { dkb: { secret: data.dkb_pin, username: data.dkb_username, product_id: data.dkb_product_id || null } },
      });
    }
    setCurrentStep(4);
  };

  const onAlphaSubmit = (data: AlphaFormValues) => {
    if (data.alphavantage_key) {
      saveSettings.mutate({
        settings: {},
        integrations: { alphavantage: { secret: data.alphavantage_key } },
      });
    }
    setCurrentStep(5);
  };

  const onFinnhubSubmit = (data: FinnhubFormValues) => {
    if (data.finnhub_key) {
      saveSettings.mutate({
        settings: {},
        integrations: { finnhub: { secret: data.finnhub_key } },
      });
    }
    setCurrentStep(6);
  };

  const onComplete = () => {
    // Use React Router navigation instead of a full-page reload so the SPA state is preserved.
    navigate("/dashboard", { replace: true });
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-bg to-surface flex items-center justify-center p-4">
      <Card className="w-full max-w-2xl">
        {/* Horizontal Stepper */}
        <CardHeader>
          <CardTitle>Setup Quantfolio</CardTitle>
          <div className="mt-6 flex items-center justify-between flex-wrap gap-2">
            {steps.map((step, idx) => (
              <div key={step.id} className="flex items-center min-w-0 shrink-0 md:flex-1">
                <div
                  className={`flex h-10 w-10 items-center justify-center rounded-full font-semibold transition-colors ${
                    idx < currentStep
                      ? "bg-accent text-accent-fg"
                      : idx === currentStep
                      ? "bg-accent text-accent-fg ring-2 ring-accent ring-offset-2 ring-offset-surface"
                      : "bg-surface-2 text-text-secondary"
                  }`}
                >
                  {idx < currentStep ? <Check size={18} /> : idx + 1}
                </div>
                {idx < steps.length - 1 && (
                  <div className={`hidden md:block flex-1 h-1 mx-2 ${idx < currentStep ? "bg-accent" : "bg-border"}`} />
                )}
              </div>
            ))}
          </div>
          <p className="mt-4 text-sm text-text-secondary">
            Step {currentStep + 1} of {steps.length}: {steps[currentStep].description}
          </p>
        </CardHeader>

        <CardContent>
          {/* Step 0: Account */}
          {currentStep === 0 && (
            <Form {...accountForm}>
              <form onSubmit={accountForm.handleSubmit(onAccountSubmit)} className="space-y-4">
                <FormField
                  control={accountForm.control}
                  name="username"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Username</FormLabel>
                      <FormControl>
                        <Input placeholder="Choose a username" autoComplete="username" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={accountForm.control}
                  name="password"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Password</FormLabel>
                      <FormControl>
                        <Input type="password" placeholder="Choose a strong password" autoComplete="new-password" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={accountForm.control}
                  name="setup_token"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Setup token</FormLabel>
                      <FormControl>
                        <Input type="password" placeholder="From the API log or the server's data folder" autoComplete="off" {...field} />
                      </FormControl>
                      <p className="text-xs text-text-muted">
                        Only whoever runs the server can create the first account. Look for “FIRST-RUN SETUP” in
                        <code> journalctl -u quantfolio-api</code>, or read <code>setup_token</code> in the data folder.
                      </p>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <Button type="submit" className="w-full" disabled={register.isPending}>
                  {register.isPending ? "Creating account..." : "Next"}
                </Button>
              </form>
            </Form>
          )}

          {/* Step 1: Passkey */}
          {currentStep === 1 && (
            <div className="space-y-4">
              <p className="text-sm text-text-secondary">
                Passkeys provide passwordless, secure authentication. You can set one up now or skip this step.
              </p>
              <div className="flex gap-3">
                <Button
                  variant="outline"
                  className="flex-1"
                  onClick={onPasskeySkip}
                >
                  Skip for now
                </Button>
                <Button
                  className="flex-1"
                  onClick={onPasskeyRegister}
                  disabled={registerPasskey.isPending}
                >
                  <KeyRound className="h-4 w-4 mr-2" />
                  {registerPasskey.isPending ? "Setting up..." : "Register passkey"}
                </Button>
              </div>
            </div>
          )}

          {/* Step 2: LLM */}
          {currentStep === 2 && (
            <Form {...llmForm}>
              <form onSubmit={llmForm.handleSubmit(onLlmSubmit)} className="space-y-4">
                <FormField
                  control={llmForm.control}
                  name="llm_base_url"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>LLM Base URL</FormLabel>
                      <FormControl>
                        <Input placeholder="http://127.0.0.1:8080/v1" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={llmForm.control}
                  name="llm_model"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Model</FormLabel>
                      <FormControl>
                        <Input placeholder="qwen3.5-9b" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <Button type="submit" className="w-full" disabled={saveSettings.isPending}>
                  {saveSettings.isPending ? "Saving..." : "Next"}
                </Button>
              </form>
            </Form>
          )}

          {/* Step 3: DKB */}
          {currentStep === 3 && (
            <Form {...dkbForm}>
              <form onSubmit={dkbForm.handleSubmit(onDkbSubmit)} className="space-y-4">
                <p className="text-sm text-text-secondary">
                  Connect to your DKB account for automatic account syncing. This is optional.
                </p>
                <FormField
                  control={dkbForm.control}
                  name="dkb_username"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Username</FormLabel>
                      <FormControl>
                        <Input placeholder="Your DKB username" autoComplete="off" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={dkbForm.control}
                  name="dkb_pin"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>PIN</FormLabel>
                      <FormControl>
                        <Input type="password" placeholder="Your DKB PIN" autoComplete="off" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={dkbForm.control}
                  name="dkb_product_id"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>FinTS Product ID (optional)</FormLabel>
                      <FormControl>
                        <Input placeholder="Your registered FinTS product ID" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <Button type="submit" className="w-full" disabled={saveSettings.isPending}>
                  {saveSettings.isPending ? "Saving..." : "Next"}
                </Button>
              </form>
            </Form>
          )}

          {/* Step 4: Alpha Vantage */}
          {currentStep === 4 && (
            <Form {...alphaForm}>
              <form onSubmit={alphaForm.handleSubmit(onAlphaSubmit)} className="space-y-4">
                <p className="text-sm text-text-secondary">
                  Add an Alpha Vantage API key for market data. This is optional.
                </p>
                <FormField
                  control={alphaForm.control}
                  name="alphavantage_key"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>API Key</FormLabel>
                      <FormControl>
                        <Input placeholder="Your Alpha Vantage API key" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <Button type="submit" className="w-full" disabled={saveSettings.isPending}>
                  {saveSettings.isPending ? "Saving..." : "Next"}
                </Button>
              </form>
            </Form>
          )}

          {/* Step 5: Finnhub */}
          {currentStep === 5 && (
            <Form {...finnhubForm}>
              <form onSubmit={finnhubForm.handleSubmit(onFinnhubSubmit)} className="space-y-4">
                <p className="text-sm text-text-secondary">
                  Add a Finnhub API key for financial data. This is optional.
                </p>
                <FormField
                  control={finnhubForm.control}
                  name="finnhub_key"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>API Key</FormLabel>
                      <FormControl>
                        <Input placeholder="Your Finnhub API key" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <Button type="submit" className="w-full" disabled={saveSettings.isPending}>
                  {saveSettings.isPending ? "Saving..." : "Complete"}
                </Button>
              </form>
            </Form>
          )}

          {/* Step 6: Done */}
          {currentStep === 6 && (
            <div className="space-y-4 text-center">
              <div className="flex justify-center">
                <div className="flex h-16 w-16 items-center justify-center rounded-full bg-accent">
                  <Check className="h-8 w-8 text-accent-fg" />
                </div>
              </div>
              <h3 className="text-lg font-semibold">Setup complete!</h3>
              <p className="text-sm text-text-secondary">
                Your Quantfolio account is ready. Click below to go to the dashboard.
              </p>
              <Button onClick={onComplete} className="w-full" size="lg">
                Go to Dashboard
                <ChevronRight className="h-4 w-4 ml-2" />
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
