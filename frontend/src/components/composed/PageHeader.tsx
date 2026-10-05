import { Link } from "react-router-dom";
import { Breadcrumb, BreadcrumbList, BreadcrumbItem, BreadcrumbPage, BreadcrumbSeparator } from "../ui/breadcrumb";

/**
 * The page's title and its one h1. A page nested inside a layout that already
 * renders a PageHeader passes `level={2}` so the document keeps a single h1.
 */
export function PageHeader({ title, subtitle, icon, breadcrumb, actions, level = 1 }: {
  title: string;
  subtitle?: string;
  icon?: React.ReactNode;
  breadcrumb?: { label: string; href?: string }[];
  actions?: React.ReactNode;
  level?: 1 | 2;
}) {
  const Heading = level === 1 ? "h1" : "h2";
  return (
    <header className="flex flex-wrap items-center justify-between gap-x-4 gap-y-3 border-b border-border pb-4">
      <div className="flex min-w-0 items-center gap-3">
        {icon && <div className="shrink-0 text-text-secondary">{icon}</div>}
        <div className="min-w-0">
          {breadcrumb && (
            <Breadcrumb className="mb-1">
              <BreadcrumbList>
                {breadcrumb.map((b, i) => (
                  <BreadcrumbItem key={i}>
                    {b.href
                      ? <Link to={b.href} className="transition-colors hover:text-text-primary">{b.label}</Link>
                      : <BreadcrumbPage>{b.label}</BreadcrumbPage>}
                    {i < breadcrumb.length - 1 && <BreadcrumbSeparator />}
                  </BreadcrumbItem>
                ))}
              </BreadcrumbList>
            </Breadcrumb>
          )}
          <Heading className="font-display text-2xl font-semibold text-text-primary sm:text-3xl">{title}</Heading>
          {subtitle && <p className="mt-1 text-sm text-text-secondary">{subtitle}</p>}
        </div>
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}
