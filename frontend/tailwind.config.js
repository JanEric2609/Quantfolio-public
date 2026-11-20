import typography from "@tailwindcss/typography";

/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg:                "rgb(var(--c-bg) / <alpha-value>)",
        surface:           "rgb(var(--c-surface) / <alpha-value>)",
        "surface-2":       "rgb(var(--c-surface-2) / <alpha-value>)",
        "surface-3":       "rgb(var(--c-surface-3) / <alpha-value>)",
        border:            "rgb(var(--c-border) / <alpha-value>)",
        "border-strong":   "rgb(var(--c-border-strong) / <alpha-value>)",
        "text-primary":    "rgb(var(--c-text-primary) / <alpha-value>)",
        "text-secondary":  "rgb(var(--c-text-secondary) / <alpha-value>)",
        "text-muted":      "rgb(var(--c-text-muted) / <alpha-value>)",
        accent:            "rgb(var(--c-accent) / <alpha-value>)",
        "accent-hover":    "rgb(var(--c-accent-hover) / <alpha-value>)",
        "accent-fg":       "rgb(var(--c-accent-fg) / <alpha-value>)",
        success:           "rgb(var(--c-success) / <alpha-value>)",
        danger:            "rgb(var(--c-danger) / <alpha-value>)",
        warn:              "rgb(var(--c-warn) / <alpha-value>)",
        info:              "rgb(var(--c-info) / <alpha-value>)",
        // Legacy aliases — keep until all old pages are redesigned (Phase 9).
        // No `base` alias on purpose: it made `text-base` a colour utility instead of the
        // 1rem font size (and painted text in the page background colour). Use `bg-bg`.
        panel:   "rgb(var(--c-surface) / <alpha-value>)",
        panel2:  "rgb(var(--c-surface-2) / <alpha-value>)",
        line:    "rgb(var(--c-border) / <alpha-value>)",
        // shadcn-style aliases — several views were written against these
        // names; without them Tailwind silently drops the classes.
        foreground:  "rgb(var(--c-text-primary) / <alpha-value>)",
        background:  "rgb(var(--c-bg) / <alpha-value>)",
        muted: {
          DEFAULT:    "rgb(var(--c-surface-2) / <alpha-value>)",
          foreground: "rgb(var(--c-text-secondary) / <alpha-value>)",
        },
        primary: {
          DEFAULT:    "rgb(var(--c-accent) / <alpha-value>)",
          foreground: "rgb(var(--c-accent-fg) / <alpha-value>)",
        },
        destructive: "rgb(var(--c-danger) / <alpha-value>)",
        popover:     "rgb(var(--c-surface-2) / <alpha-value>)",
      },
      fontFamily: {
        sans:    ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "BlinkMacSystemFont", "sans-serif"],
        display: ['"Inter Tight"', "Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono:    ['"JetBrains Mono"', "ui-monospace", "monospace"],
      },
      borderRadius: {
        sm: "4px",
        md: "6px",
        lg: "8px",
        xl: "12px",
      },
      // Prose colors follow the data-theme CSS variables, so `prose` renders
      // correctly in both themes. The invert vars are mapped to the same
      // values: existing `prose-invert` usages stay theme-correct too.
      typography: () => {
        const vars = {
          "--tw-prose-body": "rgb(var(--c-text-secondary))",
          "--tw-prose-headings": "rgb(var(--c-text-primary))",
          "--tw-prose-lead": "rgb(var(--c-text-secondary))",
          "--tw-prose-links": "rgb(var(--c-accent))",
          "--tw-prose-bold": "rgb(var(--c-text-primary))",
          "--tw-prose-counters": "rgb(var(--c-text-muted))",
          "--tw-prose-bullets": "rgb(var(--c-text-muted))",
          "--tw-prose-hr": "rgb(var(--c-border))",
          "--tw-prose-quotes": "rgb(var(--c-text-primary))",
          "--tw-prose-quote-borders": "rgb(var(--c-border-strong))",
          "--tw-prose-captions": "rgb(var(--c-text-muted))",
          "--tw-prose-code": "rgb(var(--c-text-primary))",
          "--tw-prose-pre-code": "rgb(var(--c-text-secondary))",
          "--tw-prose-pre-bg": "rgb(var(--c-surface-2))",
          "--tw-prose-th-borders": "rgb(var(--c-border-strong))",
          "--tw-prose-td-borders": "rgb(var(--c-border))",
        };
        const invertVars = Object.fromEntries(
          Object.entries(vars).map(([k, v]) => [k.replace("--tw-prose-", "--tw-prose-invert-"), v]),
        );
        return { DEFAULT: { css: { ...vars, ...invertVars } } };
      },
    },
  },
  plugins: [
    typography,
    function densityPlugin({ addVariant }) {
      addVariant("compact", '[data-density="compact"] &');
      addVariant("comfy",   '[data-density="comfortable"] &');
    },
  ],
};
