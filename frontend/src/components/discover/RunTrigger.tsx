import { useState, useRef, useEffect } from "react";
import { Link } from "react-router-dom";
import { Sparkles, Loader2, Settings2, Square, ChevronDown, Zap } from "lucide-react";
import { Button } from "../../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../../components/ui/card";
import { Textarea } from "../../components/ui/textarea";
import { Badge } from "../../components/ui/badge";
import { formatCurrency } from "../../lib/format";

interface RunTriggerProps {
  /** Public settings (GET /api/settings); only the investor-profile keys are read. */
  profile: {
    discover_investor_horizon?: string;
    discover_investor_risk_appetite?: string;
    monthly_contribution_eur?: number;
    discover_investor_exclusions?: string;
  } | null;
  onStart: (description: string) => void;
  isStarting: boolean;
  activeRunId: string | null;
  runStatus: string | null;
  onCancel: (mode: "graceful" | "immediate") => void;
  isCancelling: boolean;
}

export function RunTrigger({ profile, onStart, isStarting, activeRunId, runStatus, onCancel, isCancelling }: RunTriggerProps) {
  const [description, setDescription] = useState("");
  const [showDropdown, setShowDropdown] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setShowDropdown(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  const chips: string[] = [];
  if (profile?.discover_investor_horizon) chips.push(`Horizon: ${profile.discover_investor_horizon}`);
  if (profile?.discover_investor_risk_appetite) chips.push(`Risk: ${profile.discover_investor_risk_appetite}`);
  if (profile?.monthly_contribution_eur) chips.push(`Monthly: ${formatCurrency(profile.monthly_contribution_eur, "EUR", { digits: 0 })}`);

  const isRunning = activeRunId && (runStatus === "queued" || runStatus === "running" || runStatus === "cancellation_requested");

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <div>
            <CardTitle>Discover New Opportunities</CardTitle>
            <CardDescription>
              Run the discovery pipeline to find candidates matching your investor profile.
            </CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        <Textarea
          placeholder="Optional focus description (e.g., 'dividend-focused European large caps')"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          className="min-h-[80px]"
        />

        {chips.length > 0 && (
          <div className="flex flex-wrap items-center gap-2">
            {chips.map((chip) => (
              <Badge key={chip} variant="secondary" className="text-xs">
                {chip}
              </Badge>
            ))}
          </div>
        )}

        <div className="flex flex-wrap items-center gap-3">
          {isRunning ? (
            <div className="relative" ref={dropdownRef}>
              <div className="flex">
                <Button
                  variant="destructive"
                  size="lg"
                  disabled={isCancelling}
                  onClick={() => onCancel("graceful")}
                  className="rounded-r-none"
                >
                  {isCancelling ? (
                    <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                  ) : (
                    <Square className="w-4 h-4 mr-2" />
                  )}
                  {isCancelling ? "Stopping..." : "Stop"}
                </Button>
                <Button
                  variant="destructive"
                  size="icon"
                  className="h-11 w-11 rounded-l-none border-l border-destructive-foreground/20 sm:h-10 sm:w-10"
                  disabled={isCancelling}
                  aria-label="More stop options"
                  aria-haspopup="menu"
                  aria-expanded={showDropdown}
                  onClick={() => setShowDropdown((prev) => !prev)}
                >
                  <ChevronDown className="w-4 h-4" />
                </Button>
              </div>
              {showDropdown && (
                <div className="absolute right-0 top-full mt-1 z-50 w-48 rounded-md border bg-popover p-1 shadow-md">
                  <button
                    type="button"
                    role="menuitem"
                    className="flex min-h-11 w-full items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent sm:min-h-0"
                    onClick={() => {
                      onCancel("immediate");
                      setShowDropdown(false);
                    }}
                  >
                    <Zap className="w-4 h-4" />
                    Immediate kill
                  </button>
                </div>
              )}
            </div>
          ) : (
            <Button
              onClick={() => onStart(description)}
              disabled={isStarting}
              size="lg"
            >
              {isStarting ? (
                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
              ) : (
                <Sparkles className="w-4 h-4 mr-2" />
              )}
              {isStarting ? "Starting..." : "Discover new opportunities"}
            </Button>
          )}
          <Button variant="outline" size="sm" asChild>
            <Link to="/settings/profile">
              <Settings2 className="w-3.5 h-3.5 mr-1.5" />
              Edit profile
            </Link>
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
