import { useState, useRef } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, type AnalysisReport } from "../../../lib/api";
import { Upload } from "lucide-react";

interface UploadDropzoneProps {
  ticker: string;
  horizon: string;
  onSuccess?: () => void;
}

export function UploadDropzone({ ticker, horizon, onSuccess }: UploadDropzoneProps) {
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const upload = useMutation({
    mutationFn: (file: File) => {
      const body = new FormData();
      body.append("file", file);
      return api<AnalysisReport>(`/api/ai/reports/upload?ticker=${encodeURIComponent(ticker)}&horizon=${horizon}`, { method: "POST", body });
    },
    onSuccess: () => onSuccess?.(),
  });

  return (
    <div
      className={`relative flex flex-col items-center justify-center gap-3 rounded-md border-2 border-dashed p-12 text-center transition-colors ${dragging ? "border-accent bg-accent/5" : "border-border"}`}
      onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => { e.preventDefault(); setDragging(false); const f = e.dataTransfer.files[0]; if (f) upload.mutate(f); }}
    >
      <Upload className="h-8 w-8 text-text-muted" />
      <div className="text-sm text-text-secondary">
        <button type="button" className="text-accent hover:underline" onClick={() => inputRef.current?.click()}>Click to upload</button>
        {" or drag and drop"}
      </div>
      <p className="text-xs text-text-muted">.md or .txt files</p>
      <input ref={inputRef} type="file" accept=".md,.txt,text/markdown,text/plain" className="hidden" onChange={(e) => { const f = e.target.files?.[0]; if (f) upload.mutate(f); }} />
      {upload.isPending && <p className="text-xs text-text-secondary">Uploading...</p>}
      {upload.isSuccess && <p className="text-xs text-success">Uploaded successfully!</p>}
      {upload.isError && <p className="text-xs text-danger">Upload failed</p>}
    </div>
  );
}
