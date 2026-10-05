import { Outlet } from "react-router-dom";
import { Star, Library, Sparkles, ClipboardPaste } from "lucide-react";
import { PageHeader } from "../../components/composed/PageHeader";
import { TabNav } from "../../components/composed/TabNav";

// The watchlist and the research written about it. News is its own page.
const WATCHLIST_TABS = [
  { to: "/research/watchlist", label: "Watchlist", icon: Star },
  { to: "/research/library", label: "Library", icon: Library },
  { to: "/research/generate", label: "Generate", icon: Sparkles },
  { to: "/research/paste", label: "Paste", icon: ClipboardPaste },
];

export function ResearchLayout() {
  return (
    <div className="space-y-6">
      <PageHeader title="Watchlist" />
      <TabNav tabs={WATCHLIST_TABS} ariaLabel="Watchlist sections" />
      <Outlet />
    </div>
  );
}
