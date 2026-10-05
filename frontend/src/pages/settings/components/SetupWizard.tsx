import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, CheckCircle2, Loader2, Wand2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "../../../components/ui/button";
import { Card, CardContent } from "../../../components/ui/card";
import { Checkbox } from "../../../components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "../../../components/ui/dialog";
import { Input } from "../../../components/ui/input";
import { Progress } from "../../../components/ui/progress";
import { MaskedInput } from "./MaskedInput";
import { StatusBadge } from "./StatusBadge";
import { api, testSettingsIntegration, updateSettings, type DkbDiagnostic, type IntegrationTestResponse } from "../../../lib/api";

type WizardDraft = {
  fintsUrl: string;
  blz: string;
  productId: string;
  username: string;
  pin: string;
  tanSecurityFunction: string;
  tanMedium: string;
};

const STEPS = [
  { title: "Prerequisites", description: "DKB FinTS is read-only AIS and requires DKB-App push-TAN approval." },
  { title: "Bank endpoint", description: "Confirm the DKB FinTS endpoint and BLZ." },
  { title: "Product ID", description: "Add your FinTS product ID (optional — not required by DKB)." },
  { title: "Credentials", description: "Store your DKB username and online banking PIN." },
  { title: "TAN method", description: "Optionally pin the DKB-App TAN method after discovery." },
  { title: "Test", description: "Save, validate the credential shape, then run the FinTS self-test." },
];

const INITIAL_DRAFT: WizardDraft = {
  fintsUrl: "https://fints.dkb.de/fints",
  blz: "12030000",
  productId: "",
  username: "",
  pin: "",
  tanSecurityFunction: "",
  tanMedium: "",
};

export function DkbSetupWizardButton({ className }: { className?: string }) {
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState(0);
  const [draft, setDraft] = useState<WizardDraft>(INITIAL_DRAFT);
  const [shapeResult, setShapeResult] = useState<IntegrationTestResponse>();
  const [selftestResult, setSelftestResult] = useState<DkbDiagnostic>();
  const [useTestProductId, setUseTestProductId] = useState(false);
  const queryClient = useQueryClient();

  const validation = useMemo(() => validateStep(step, draft), [draft, step]);
  const progress = ((step + 1) / STEPS.length) * 100;

  const runTest = useMutation({
    mutationFn: async () => {
      await updateSettings({
        settings: {
          dkb_fints_url: draft.fintsUrl,
          dkb_blz: draft.blz,
          dkb_provider: "fints",
          dkb_product_id: draft.productId,
          dkb_username: draft.username,
          dkb_tan_security_function: draft.tanSecurityFunction,
          dkb_tan_medium: draft.tanMedium,
        },
        integrations: {
          dkb: {
            secret: draft.pin,
            username: draft.username,
            product_id: draft.productId,
            tan_security_function: draft.tanSecurityFunction,
            tan_medium: draft.tanMedium,
          },
        },
      });
      const shape = await testSettingsIntegration({
        service: "dkb",
        value: draft.pin,
        meta: {
          provider: "fints",
          username: draft.username,
          product_id: draft.productId,
          tan_security_function: draft.tanSecurityFunction,
          tan_medium: draft.tanMedium,
        },
      });
      const qp = useTestProductId ? "?use_test_product_id=true" : "";
      const selftest = await api<DkbDiagnostic>(`/api/dkb/diagnostics/fints/selftest${qp}`, { method: "POST" });
      return { shape, selftest };
    },
    onSuccess: ({ shape, selftest }) => {
      setShapeResult(shape);
      setSelftestResult(selftest);
      queryClient.invalidateQueries({ queryKey: ["settings"] });
      queryClient.invalidateQueries({ queryKey: ["dkb-diagnostics"] });
      toast[shape.ok && selftest.status !== "failed" ? "success" : "warning"](selftest.summary);
    },
    onError: (error: any) => toast.error(`DKB setup test failed: ${error.message ?? error}`),
  });

  const update = (key: keyof WizardDraft, value: string) => setDraft((current) => ({ ...current, [key]: value }));

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button type="button" className={className}>
          <Wand2 className="mr-2 h-4 w-4" aria-hidden="true" />
          DKB setup wizard
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-xl flex flex-col max-h-[85vh]">
        <DialogHeader>
          <DialogTitle>DKB FinTS setup</DialogTitle>
          <DialogDescription>{STEPS[step].description}</DialogDescription>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto space-y-4 min-h-0">
          <div className="space-y-2">
            <div className="flex items-center justify-between text-xs text-text-muted">
              <span>
                Step {step + 1} of {STEPS.length}
              </span>
              <span>{STEPS[step].title}</span>
            </div>
            <Progress value={progress} />
          </div>

          <Card className="border-border bg-surface-2">
            <CardContent className="space-y-4 p-4 max-h-[70vh] overflow-y-auto">{renderStep(step, draft, update, shapeResult, selftestResult, useTestProductId, setUseTestProductId)}</CardContent>
          </Card>

          {validation ? <p className="text-sm text-warn">{validation}</p> : null}
        </div>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => setStep((current) => Math.max(0, current - 1))} disabled={step === 0 || runTest.isPending}>
            <ArrowLeft className="mr-2 h-4 w-4" aria-hidden="true" />
            Back
          </Button>
          {step < STEPS.length - 1 ? (
            <Button type="button" onClick={() => setStep((current) => current + 1)} disabled={!!validation}>
              Next
              <ArrowRight className="ml-2 h-4 w-4" aria-hidden="true" />
            </Button>
          ) : (
            <Button type="button" onClick={() => runTest.mutate()} disabled={!!validation || runTest.isPending}>
              {runTest.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" /> : <CheckCircle2 className="mr-2 h-4 w-4" aria-hidden="true" />}
              Save and test
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function renderStep(
  step: number,
  draft: WizardDraft,
  update: (key: keyof WizardDraft, value: string) => void,
  shapeResult?: IntegrationTestResponse,
  selftestResult?: DkbDiagnostic,
  useTestProductId?: boolean,
  setUseTestProductId?: (checked: boolean) => void,
) {
  if (step === 0) {
    return (
      <div className="space-y-3 text-sm text-text-secondary">
        <p>Use this only for read-only account information. QuantFolio never initiates payments through this integration.</p>
        <p>DKB currently uses decoupled push-TAN through the DKB app. Keep the app ready during the final self-test.</p>
        <p>A FinTS product ID is optional — DKB does not validate it. The built-in default (DKB_PUBLIC_TEST_PRODUCT_ID) works for all users.</p>
      </div>
    );
  }
  if (step === 1) {
    return (
      <div className="grid gap-3 sm:grid-cols-2">
        <LabeledInput label="FinTS URL" value={draft.fintsUrl} onChange={(value) => update("fintsUrl", value)} />
        <LabeledInput label="BLZ" value={draft.blz} onChange={(value) => update("blz", value)} />
      </div>
    );
  }
  if (step === 2) {
    return (
      <div className="space-y-3">
        <LabeledInput label="FinTS product ID (optional)" value={draft.productId} onChange={(value) => update("productId", value)} />
        <p className="text-xs text-text-muted">
          Leave blank to use the built-in test product ID. DKB does not validate product IDs against a registration list.
        </p>
      </div>
    );
  }
  if (step === 3) {
    return (
      <div className="grid gap-3 sm:grid-cols-2">
        <LabeledInput label="DKB username" value={draft.username} onChange={(value) => update("username", value)} />
        <MaskedInput label="Online banking PIN" value={draft.pin} onChange={(value) => update("pin", value)} ariaLabel="Online banking PIN" />
      </div>
    );
  }
  if (step === 4) {
    return (
      <div className="grid gap-3 sm:grid-cols-2">
        <LabeledInput label="TAN security function" value={draft.tanSecurityFunction} onChange={(value) => update("tanSecurityFunction", value)} placeholder="Auto-detect" />
        <LabeledInput label="TAN medium" value={draft.tanMedium} onChange={(value) => update("tanMedium", value)} placeholder="Auto-detect" />
        <p className="sm:col-span-2 text-sm text-text-secondary">Leave these blank unless diagnostics show multiple TAN methods. DKB-App push-TAN decoupled is the expected method.</p>
      </div>
    );
  }
  return (
    <div className="space-y-3">
      <p className="text-sm text-text-secondary">The wizard will save these values, validate the required credential shape, and run the live FinTS self-test.</p>
      <label className="flex items-start gap-2 rounded-md border border-border bg-surface p-3 text-sm cursor-pointer">
        <Checkbox
          checked={useTestProductId}
          onCheckedChange={(checked) => setUseTestProductId?.(checked === true)}
          className="mt-0.5"
        />
        <div>
          <div className="font-medium">Use known-working test product ID</div>
          <div className="mt-0.5 text-text-secondary">
            Override your product ID with DKB_PUBLIC_TEST_PRODUCT_ID for the self-test only (no stored change).
          </div>
        </div>
      </label>
      {shapeResult ? <ResultRow label="Credential shape" ok={shapeResult.ok} message={shapeResult.message} /> : null}
      {selftestResult ? <ResultRow label="FinTS self-test" ok={selftestResult.status !== "failed"} message={selftestResult.summary} /> : null}
    </div>
  );
}

function LabeledInput({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="space-y-1.5 text-sm">
      <span className="text-xs text-text-secondary">{label}</span>
      <Input value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} />
    </label>
  );
}

function ResultRow({ label, ok, message }: { label: string; ok: boolean; message: string }) {
  return (
    <div className="flex items-start justify-between gap-3 rounded-md border border-border bg-surface p-3 text-sm">
      <div>
        <div className="font-medium">{label}</div>
        <div className="mt-1 text-text-secondary">{message}</div>
      </div>
      <StatusBadge status={ok ? "connected" : "error"} />
    </div>
  );
}

function validateStep(step: number, draft: WizardDraft) {
  if (step === 1 && (!draft.fintsUrl || !draft.blz)) return "FinTS URL and BLZ are required.";
  if (step === 1 && draft.blz !== "12030000") return "DKB BLZ should be 12030000.";
  // Step 2 (product ID) has no hard requirement — empty = built-in default
  if (step === 3 && (!draft.username || !draft.pin)) return "DKB username and PIN are required.";
  return "";
}
