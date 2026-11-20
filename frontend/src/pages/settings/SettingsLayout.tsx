import { Outlet } from "react-router-dom";
import { PageHeader } from "../../components/composed/PageHeader";
import { SettingsNav, SettingsPagePicker } from "./components/SettingsNav";

export function SettingsLayout() {
  return (
    <div className="space-y-6">
      <PageHeader title="Control Center" subtitle="Your preferences, connections, engine tuning and system health." />
      <div className="lg:hidden">
        <SettingsPagePicker />
      </div>
      <div className="grid gap-10 lg:grid-cols-[16.5rem_minmax(0,1fr)]">
        <aside className="hidden lg:block">
          <div className="sticky top-4">
            <SettingsNav />
          </div>
        </aside>
        <div className="min-w-0 max-w-4xl pb-16">
          <Outlet />
        </div>
      </div>
    </div>
  );
}
