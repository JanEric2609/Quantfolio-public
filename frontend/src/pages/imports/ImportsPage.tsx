import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "../../components/ui/tabs";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { PageHeader } from "../../components/composed/PageHeader";
import { Button } from "../../components/ui/button";
import { Upload, FileUp, CheckCircle, AlertCircle } from "lucide-react";
import { toast } from "sonner";
import { api, ImportValidationResponse, ImportCommitResponse } from "../../lib/api";

interface ValidationState {
  valid: boolean;
  format?: string | null;
  message: string;
  columns?: string[] | null;
  row_count?: number | null;
}

interface PreviewState {
  valid: boolean;
  format?: string | null;
  columns?: string[] | null;
  row_count?: number | null;
}

export function ImportsPage() {
  const queryClient = useQueryClient();
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [isValidating, setIsValidating] = useState(false);
  const [isCommitting, setIsCommitting] = useState(false);
  const [validationState, setValidationState] = useState<ValidationState | null>(null);
  const [previewState, setPreviewState] = useState<PreviewState | null>(null);
  const [createdIds, setCreatedIds] = useState<string[]>([]);

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files?.[0]) {
      setSelectedFile(e.target.files[0]);
      setValidationState(null);
      setPreviewState(null);
      setCreatedIds([]);
    }
  };

  const handleValidate = async () => {
    if (!selectedFile) {
      toast.error("Please select a file");
      return;
    }
    setIsValidating(true);
    try {
      const formData = new FormData();
      formData.append("file", selectedFile);
      const result = await api<ImportValidationResponse>("/api/imports/validate", {
        method: "POST",
        body: formData,
      });
      setValidationState({
        valid: result.valid,
        format: result.format,
        message: result.message,
        columns: result.columns,
        row_count: result.row_count,
      });
      if (result.valid) {
        setPreviewState({
          valid: true,
          format: result.format,
          columns: result.columns,
          row_count: result.row_count,
        });
        toast.success(`Detected format: ${result.format} (${result.row_count} rows)`);
      } else {
        toast.error(result.message);
      }
    } catch (err) {
      toast.error(`Validation failed: ${err}`);
      setValidationState({ valid: false, message: String(err) });
    } finally {
      setIsValidating(false);
    }
  };

  const handleCommit = async () => {
    if (!selectedFile || !previewState?.valid) {
      toast.error("Please validate the file first");
      return;
    }
    setIsCommitting(true);
    try {
      const formData = new FormData();
      formData.append("file", selectedFile);
      formData.append("format_type", previewState.format || "dkb");
      const result = await api<ImportCommitResponse>("/api/imports/commit", {
        method: "POST",
        body: formData,
      });
      setCreatedIds(result.created_ids);
      if (result.errors.length > 0) {
        toast.warning(`Imported ${result.created_ids.length} entries with ${result.errors.length} errors`);
      } else {
        toast.success(`Successfully imported ${result.created_ids.length} transactions`);
      }
      queryClient.invalidateQueries();
    } catch (err) {
      toast.error(`Commit failed: ${err}`);
    } finally {
      setIsCommitting(false);
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader title="Imports & Reconciliation" />
      <Tabs defaultValue="imports" className="w-full">
        <TabsList>
          <TabsTrigger value="imports">Imports</TabsTrigger>
          <TabsTrigger value="reconciliation">Reconciliation</TabsTrigger>
        </TabsList>

        <TabsContent value="imports" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Import Broker Statements</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="border-2 border-dashed border-border rounded-lg p-8 text-center">
                <Upload className="h-12 w-12 mx-auto mb-4 text-text-secondary" />
                <p className="text-sm text-text-secondary mb-4">Drag and drop a CSV file here, or click to select</p>
                <input
                  type="file"
                  accept=".csv,.txt"
                  onChange={handleFileSelect}
                  className="hidden"
                  id="file-input"
                />
                <label htmlFor="file-input">
                  <Button asChild variant="outline" className="cursor-pointer">
                    <span>Select File</span>
                  </Button>
                </label>
              </div>

              {selectedFile && (
                <div className="flex items-center gap-2 p-3 bg-surface-2 rounded-md">
                  <FileUp size={16} />
                  <span className="text-sm">{selectedFile.name}</span>
                </div>
              )}

              {selectedFile && !validationState && (
                <Button onClick={handleValidate} disabled={isValidating} className="w-full">
                  {isValidating ? "Validating..." : "Step 1: Validate Format"}
                </Button>
              )}

              {validationState && (
                <div className={`p-4 rounded-md flex items-start gap-3 ${validationState.valid ? "bg-success/20" : "bg-danger/20"}`}>
                  {validationState.valid ? (
                    <CheckCircle className="h-5 w-5 text-success flex-shrink-0 mt-0.5" />
                  ) : (
                    <AlertCircle className="h-5 w-5 text-danger flex-shrink-0 mt-0.5" />
                  )}
                  <div className="flex-1">
                    <p className="text-sm font-medium">{validationState.message}</p>
                    {validationState.valid && (
                      <p className="text-xs text-text-secondary mt-1">
                        Format: <strong>{validationState.format}</strong> | Rows: <strong>{validationState.row_count}</strong>
                      </p>
                    )}
                  </div>
                </div>
              )}

              {previewState?.valid && !createdIds.length && (
                <Button onClick={handleCommit} disabled={isCommitting} className="w-full bg-success hover:bg-success/90">
                  {isCommitting ? "Committing..." : "Step 2: Commit Transactions"}
                </Button>
              )}

              {createdIds.length > 0 && (
                <div className="p-4 rounded-md bg-success/20 flex items-start gap-3">
                  <CheckCircle className="h-5 w-5 text-success flex-shrink-0 mt-0.5" />
                  <div>
                    <p className="text-sm font-medium">Successfully imported {createdIds.length} transactions</p>
                    <p className="text-xs text-text-secondary mt-1">All entries marked for review before posting</p>
                  </div>
                </div>
              )}

              <div className="mt-6 space-y-2 text-sm text-text-secondary">
                <p className="font-medium">Supported formats:</p>
                <ul className="list-disc list-inside space-y-1">
                  <li>DKB CSV (German banking statements)</li>
                  <li>Comdirect (coming soon)</li>
                  <li>Trade Republic (coming soon)</li>
                </ul>
              </div>
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="reconciliation" className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Reconciliation</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <p className="text-sm text-text-secondary">
                Reconciliation allows you to match imported transactions with existing records to identify discrepancies.
              </p>
              <div className="text-center py-8">
                <p className="text-text-secondary">No pending reconciliations</p>
              </div>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}
