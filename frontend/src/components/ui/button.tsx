import * as React from "react";
import { Slot, Slottable } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "../../lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center whitespace-nowrap rounded-md text-sm font-medium ring-offset-bg transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        default: "bg-accent text-accent-fg hover:bg-accent-hover",
        secondary: "bg-surface-2 text-text-primary hover:bg-surface-3",
        outline: "border border-border bg-transparent text-text-primary hover:bg-surface-2",
        ghost: "hover:bg-surface-2 text-text-secondary hover:text-text-primary",
        destructive: "bg-danger text-white hover:bg-[#dc2626]",
        link: "text-accent underline-offset-4 hover:underline",
        accent: "bg-accent text-accent-fg hover:bg-accent-hover",
      },
      size: {
        // Phones get 44 px touch targets (WCAG 2.5.5 / Apple HIG); desktop density is unchanged from `sm` up.
        default: "h-11 px-4 py-2 sm:h-9",
        sm: "h-11 rounded-md px-3 text-xs sm:h-8",
        lg: "h-11 rounded-md px-8 sm:h-10",
        icon: "h-11 w-11 sm:h-9 sm:w-9",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        {...props}
      />
    );
  }
);
Button.displayName = "Button";

export { Button, buttonVariants };
