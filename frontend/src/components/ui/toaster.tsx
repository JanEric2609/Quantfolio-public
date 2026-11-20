import { Toaster as Sonner } from "sonner";
import { useState, useEffect } from "react";

function resolveTheme(raw: string | null): "dark" | "light" {
  if (raw === "system") {
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  return raw === "light" ? "light" : "dark";
}

function Toaster() {
  const [theme, setTheme] = useState<"dark" | "light">(
    typeof document !== "undefined"
      ? resolveTheme(document.documentElement.getAttribute("data-theme"))
      : "dark"
  );

  useEffect(() => {
    if (typeof document === "undefined") return;

    const update = () => {
      const raw = document.documentElement.getAttribute("data-theme");
      setTheme(resolveTheme(raw));
    };

    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });

    const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
    mediaQuery.addEventListener("change", update);

    return () => {
      observer.disconnect();
      mediaQuery.removeEventListener("change", update);
    };
  }, []);

  return (
    <Sonner
      theme={theme}
      richColors
      position="top-right"
      toastOptions={{
        style: {
          background: "rgb(var(--c-surface))",
          border: "1px solid rgb(var(--c-border))",
          color: "rgb(var(--c-text-primary))",
        },
      }}
    />
  );
}

export { Toaster };
