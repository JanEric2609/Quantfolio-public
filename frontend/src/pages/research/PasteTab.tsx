import { useState, useRef } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type AnalysisReport } from "../../lib/api";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { Label } from "../../components/ui/label";
import { Textarea } from "../../components/ui/textarea";
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card";
import { UploadDropzone } from "./components/UploadDropzone";

export function PasteTab() {
  const queryClient = useQueryClient();
  const [title, setTitle] = useState("TradingAgents report");
  const [ticker, setTicker] = useState("EUNL.DE");
  const [horizon, setHorizon] = useState<"short" | "mid" | "long">("mid");
  const [content, setContent] = useState("");

  const createReport = useMutation({
    mutationFn: () => api<AnalysisReport>("/api/ai/reports", { method: "POST", body: JSON.stringify({ title, ticker, horizon, content }) }),
    onSuccess: () => {
      setTitle("TradingAgents report"); setTicker("EUNL.DE"); setHorizon("mid"); setContent("");
      queryClient.invalidateQueries({ queryKey: ["reports"] });
    },
  });

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
      <Card>
        <CardHeader><CardTitle>Paste Report</CardTitle></CardHeader>
        <CardContent>
          <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); createReport.mutate(); }}>
            <div className="space-y-2">
              <Label>Title</Label>
              <Input value={title} onChange={e => setTitle(e.target.value)} />
            </div>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
              <div className="space-y-2">
                <Label>Ticker</Label>
                <Input value={ticker} onChange={e => setTicker(e.target.value)} />
              </div>
              <div className="space-y-2">
                <Label>Horizon</Label>
                <select className="w-full h-10 rounded-md border border-border bg-surface-2 px-3 text-sm" value={horizon} onChange={e => setHorizon(e.target.value as "short" | "mid" | "long")}>
                  <option value="short">Short</option>
                  <option value="mid">Mid</option>
                  <option value="long">Long</option>
                </select>
              </div>
            </div>
            <div className="space-y-2">
              <Label>Content</Label>
              <Textarea className="min-h-48" value={content} onChange={e => setContent(e.target.value)} placeholder="Paste a TradingAgents Markdown/text report" />
            </div>
            <Button type="submit" disabled={createReport.isPending}>
              {createReport.isPending ? "Saving..." : "Save Report"}
            </Button>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Upload File</CardTitle></CardHeader>
        <CardContent>
          <UploadDropzone ticker={ticker} horizon={horizon} onSuccess={() => queryClient.invalidateQueries({ queryKey: ["reports"] })} />
        </CardContent>
      </Card>
    </div>
  );
}
