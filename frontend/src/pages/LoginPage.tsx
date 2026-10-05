import { useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound } from "lucide-react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { zodResolver } from "../lib/zodResolver";
import { api, type AuthMeResponse } from "../lib/api";
import { credentialToJSON, prepareRequestOptions } from "../lib/webauthn";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Form, FormField, FormItem, FormLabel, FormControl, FormMessage } from "../components/ui/form";
import { Accordion, AccordionItem, AccordionTrigger, AccordionContent } from "../components/ui/accordion";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "../components/ui/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "../components/ui/dialog";
import { toast } from "sonner";

const loginSchema = z.object({
  username: z.string().min(1, "Username required"),
  password: z.string().min(1, "Password required"),
});

const registerSchema = z.object({
  username: z.string().min(2, "At least 2 characters").max(80),
  password: z.string().min(8, "At least 8 characters"),
  confirm: z.string().min(1, "Please confirm password"),
  setup_token: z.string().trim().min(1, "Enter the setup token from the server log"),
}).refine((d) => d.password === d.confirm, {
  message: "Passwords do not match",
  path: ["confirm"],
});

const resetSchema = z.object({
  username: z.string().min(2, "Username required"),
  reset_token: z.string().min(1, "Reset token required"),
  new_password: z.string().min(8, "At least 8 characters"),
});

type LoginFormValues = z.infer<typeof loginSchema>;
type RegisterFormValues = z.infer<typeof registerSchema>;
type ResetFormValues = z.infer<typeof resetSchema>;

export function LoginPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [resetOpen, setResetOpen] = useState(false);

  const { data: authStatus } = useQuery({
    queryKey: ["auth-status"],
    queryFn: () => api<{ initialized: boolean }>("/api/auth/status"),
    retry: false,
  });

  // If the user already has a valid session, redirect them into the app.
  const { isSuccess: alreadyLoggedIn } = useQuery({
    queryKey: ["me"],
    queryFn: () => api<AuthMeResponse>("/api/auth/me"),
    retry: false,
  });

  const loginForm = useForm<LoginFormValues>({
    resolver: zodResolver(loginSchema),
    defaultValues: { username: "", password: "" },
  });

  const registerForm = useForm<RegisterFormValues>({
    resolver: zodResolver(registerSchema),
    defaultValues: { username: "", password: "", confirm: "", setup_token: "" },
  });

  const resetForm = useForm<ResetFormValues>({
    resolver: zodResolver(resetSchema),
    defaultValues: { username: "", reset_token: "", new_password: "" },
  });

  const passwordLogin = useMutation({
    mutationFn: (data: LoginFormValues) =>
      api("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ username: data.username, password: data.password }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["me"] });
      toast.success("Signed in successfully");
      navigate("/", { replace: true });
    },
    onError: (err) => {
      toast.error(err.message || "Sign in failed");
    },
  });

  const register = useMutation({
    mutationFn: (data: RegisterFormValues) =>
      api("/api/auth/register", {
        method: "POST",
        body: JSON.stringify({ username: data.username, password: data.password, setup_token: data.setup_token.trim() }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["me"] });
      queryClient.invalidateQueries({ queryKey: ["auth-status"] });
      toast.success("Account created. Welcome!");
      navigate("/", { replace: true });
    },
    onError: (err) => {
      toast.error(err.message || "Registration failed");
    },
  });

  const passkeyLogin = useMutation({
    mutationFn: async () => {
      if (!window.PublicKeyCredential) throw new Error("This browser does not support passkeys.");
      const options = await api<any>("/api/auth/passkey/authenticate/options", {
        method: "POST",
        body: JSON.stringify({}),
      });
      const credential = await navigator.credentials.get({ publicKey: prepareRequestOptions(options) });
      if (!credential) throw new Error("Passkey authentication cancelled");
      const credentialJson = credentialToJSON(credential);
      return api("/api/auth/passkey/authenticate/verify", {
        method: "POST",
        body: JSON.stringify({
          credential_id: credentialJson.id,
          challenge: options.challenge,
          credential: credentialJson,
        }),
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["me"] });
      toast.success("Signed in with passkey");
      navigate("/", { replace: true });
    },
    onError: (err) => {
      toast.error(err.message || "Passkey sign in failed");
    },
  });

  const [tokenMinted, setTokenMinted] = useState(false);

  const passwordResetRequest = useMutation({
    mutationFn: (data: { username: string; owner_token: string }) =>
      api<{ message: string; reset_token: string }>("/api/auth/password-reset/request", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    onSuccess: (data) => {
      toast.success("Token minted. Now set your new password.");
      resetForm.setValue("reset_token", data.reset_token);
      setTokenMinted(true);
    },
    onError: (err) => {
      toast.error(err.message || "Password reset request failed");
    },
  });

  const passwordReset = useMutation({
    mutationFn: (data: ResetFormValues) =>
      api("/api/auth/password-reset", {
        method: "POST",
        body: JSON.stringify({ username: data.username, reset_token: data.reset_token, new_password: data.new_password }),
      }),
    onSuccess: () => {
      toast.success("Password reset. Sign in with the new password.");
      loginForm.setValue("username", resetForm.getValues("username"));
      resetForm.reset();
      setResetOpen(false);
      setTokenMinted(false);
    },
    onError: (err) => {
      toast.error(err.message || "Password reset failed");
    },
  });

  const initialized = authStatus?.initialized;

  // All hooks above run unconditionally; only redirect after they're declared.
  if (alreadyLoggedIn) {
    return <Navigate to="/" replace />;
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-bg to-surface flex items-center justify-center p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="space-y-1">
          <CardTitle className="text-2xl font-display">Quantfolio</CardTitle>
          {initialized === false && (
            <CardDescription>Create your account to get started.</CardDescription>
          )}
        </CardHeader>

        <CardContent className="space-y-6">
          {initialized === false ? (
            /* ── First-run registration ── */
            <Form {...registerForm}>
              <form
                onSubmit={registerForm.handleSubmit((d) => register.mutate(d))}
                className="space-y-4"
              >
                <FormField
                  control={registerForm.control}
                  name="username"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Username</FormLabel>
                      <FormControl>
                        <Input placeholder="Choose a username" autoFocus autoComplete="username" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={registerForm.control}
                  name="password"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Password</FormLabel>
                      <FormControl>
                        <Input type="password" placeholder="At least 8 characters" autoComplete="new-password" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={registerForm.control}
                  name="confirm"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Confirm password</FormLabel>
                      <FormControl>
                        <Input type="password" placeholder="Repeat password" autoComplete="new-password" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={registerForm.control}
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
                  {register.isPending ? "Creating account..." : "Create account"}
                </Button>
              </form>
            </Form>
          ) : (
            /* ── Normal login ── */
            <>
              <Button
                type="button"
                variant="accent"
                size="lg"
                className="w-full"
                onClick={() => passkeyLogin.mutate()}
                disabled={passkeyLogin.isPending}
              >
                <KeyRound className="h-5 w-5 mr-2" />
                {passkeyLogin.isPending ? "Signing in..." : "Continue with passkey"}
              </Button>

              <Accordion type="single" collapsible className="border border-border rounded-md">
                <AccordionItem value="password" className="border-none">
                  <AccordionTrigger className="px-4 py-2 text-sm font-medium text-text-secondary hover:text-text-primary">
                    Password sign-in
                  </AccordionTrigger>
                  <AccordionContent className="px-4 pb-4">
                    <Form {...loginForm}>
                      <form
                        onSubmit={loginForm.handleSubmit((d) => passwordLogin.mutate(d))}
                        className="space-y-4"
                      >
                        <FormField
                          control={loginForm.control}
                          name="username"
                          render={({ field }) => (
                            <FormItem>
                              <FormLabel>Username</FormLabel>
                              <FormControl>
                                <Input placeholder="Enter username" autoComplete="username" {...field} />
                              </FormControl>
                              <FormMessage />
                            </FormItem>
                          )}
                        />
                        <FormField
                          control={loginForm.control}
                          name="password"
                          render={({ field }) => (
                            <FormItem>
                              <FormLabel>Password</FormLabel>
                              <FormControl>
                                <Input type="password" placeholder="Enter password" autoComplete="current-password" {...field} />
                              </FormControl>
                              <FormMessage />
                            </FormItem>
                          )}
                        />
                        <Button type="submit" className="w-full" disabled={passwordLogin.isPending}>
                          {passwordLogin.isPending ? "Signing in..." : "Sign in"}
                        </Button>
                      </form>
                    </Form>
                  </AccordionContent>
                </AccordionItem>
              </Accordion>

              <div className="text-center">
                <Button
                  type="button"
                  variant="link"
                  className="text-sm text-text-secondary"
                  onClick={() => {
                    resetForm.setValue("username", loginForm.getValues("username"));
                    setResetOpen(true);
                  }}
                >
                  Forgot password?
                </Button>
              </div>
            </>
          )}
        </CardContent>
      </Card>

      {/* ── Password reset dialog ── */}
      <Dialog open={resetOpen} onOpenChange={(open) => {
        if (!open) { setTokenMinted(false); resetForm.reset(); }
        setResetOpen(open);
      }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Reset password</DialogTitle>
            <DialogDescription>
              Enter your server&apos;s <code>PASSWORD_RESET_TOKEN</code> to get a
              single-use reset token, then set a new password.
            </DialogDescription>
          </DialogHeader>
          <Form {...resetForm}>
            {!tokenMinted ? (
              <div className="space-y-4">
                <FormField
                  control={resetForm.control}
                  name="username"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Username</FormLabel>
                      <FormControl>
                        <Input placeholder="Your username" autoComplete="username" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={resetForm.control}
                  name="reset_token"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Owner token</FormLabel>
                      <FormControl>
                        <Input type="password" placeholder="Value of PASSWORD_RESET_TOKEN" autoComplete="one-time-code" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <Button
                  type="button"
                  className="w-full"
                  disabled={passwordResetRequest.isPending}
                  onClick={resetForm.handleSubmit((d) =>
                    passwordResetRequest.mutate({ username: d.username, owner_token: d.reset_token })
                  )}
                >
                  {passwordResetRequest.isPending ? "Requesting..." : "Request reset token"}
                </Button>
              </div>
            ) : (
              <div className="space-y-4">
                <FormField
                  control={resetForm.control}
                  name="new_password"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>New password</FormLabel>
                      <FormControl>
                        <Input type="password" placeholder="At least 8 characters" autoComplete="new-password" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <Button
                  type="button"
                  className="w-full"
                  disabled={passwordReset.isPending}
                  onClick={resetForm.handleSubmit((d) => passwordReset.mutate(d))}
                >
                  {passwordReset.isPending ? "Resetting..." : "Set new password"}
                </Button>
              </div>
            )}
          </Form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
