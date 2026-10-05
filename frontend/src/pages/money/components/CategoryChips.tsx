import { categoryIcon } from "./categoryIcons";

export interface CategoryChipItem {
  id: string;
  name: string;
  color: string;
  icon: string;
}

export function CategoryChips({ categories, disabled, onSelect }: {
  categories: CategoryChipItem[];
  disabled?: boolean;
  onSelect: (categoryId: string) => void;
}) {
  return (
    <div className="grid grid-cols-3 gap-2" role="group" aria-label="Categories">
      {categories.map((category) => {
        const Icon = categoryIcon(category.icon);
        return (
          <button
            key={category.id}
            type="button"
            disabled={disabled}
            onClick={() => onSelect(category.id)}
            className="flex flex-col items-center gap-1 rounded-lg border border-border bg-surface-2 p-3 text-xs transition-colors hover:border-accent disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Icon className="h-5 w-5" style={{ color: category.color }} />
            <span className="text-center leading-tight text-text-secondary">{category.name}</span>
          </button>
        );
      })}
    </div>
  );
}
