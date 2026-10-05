import { Button } from "../ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from "../ui/dropdown-menu";
import { useUiStore } from "../../lib/store";
import { api } from "../../lib/api";
import { useNavigate } from "react-router-dom";
import { clearApiCache } from "../../lib/pwa";

export function UserMenu() {
  const density = useUiStore((s) => s.density);
  const setDensity = useUiStore((s) => s.setDensity);
  const navigate = useNavigate();

  const handleLogout = async () => {
    try { await api("/api/auth/logout", { method: "POST" }); } catch { /* ignore */ }
    await clearApiCache();
    navigate("/login");
  };

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label="User menu" className="h-11 w-11 rounded-full bg-surface-2 font-display text-text-primary sm:h-8 sm:w-8">
          U
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-48">
        <DropdownMenuLabel className="text-sm text-text-secondary">User</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuLabel className="text-xs">Density</DropdownMenuLabel>
        <DropdownMenuItem onClick={() => setDensity("compact")} className={density === "compact" ? "bg-surface-2" : ""}>Compact</DropdownMenuItem>
        <DropdownMenuItem onClick={() => setDensity("comfortable")} className={density === "comfortable" ? "bg-surface-2" : ""}>Comfortable</DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={() => navigate("/settings")}>Settings</DropdownMenuItem>
        <DropdownMenuItem onClick={handleLogout} className="text-danger">Logout</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
