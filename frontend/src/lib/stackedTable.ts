/**
 * Class names that turn a plain table into a stack of labelled cards below the `sm` breakpoint
 * (640 px), so a wide table never squeezes its columns or hides the last one off-screen on a phone.
 * Give every `<td>` a `data-label` (the column name) and the `td` class below.
 */
export const stacked = {
  table: "w-full text-sm max-sm:block",
  thead: "max-sm:sr-only",
  tbody: "max-sm:block",
  tr: "max-sm:block max-sm:border-t max-sm:border-border/50 max-sm:py-2",
  td: "max-sm:flex max-sm:justify-between max-sm:gap-3 max-sm:py-0.5 max-sm:before:shrink-0 max-sm:before:text-muted-foreground max-sm:before:content-[attr(data-label)]",
} as const;
