import { z } from "zod";

export const expenseSchema = z.object({
  date: z.string().min(1, "Date required"),
  amount: z.coerce.number().positive("Amount must be positive"),
  currency: z.enum(["EUR", "USD", "GBP", "CHF"]),
  description: z.string().trim().min(1, "Description required").max(250),
  category_id: z.string().optional(),
  notes: z.string().optional(),
});

export const expenseEditSchema = expenseSchema.extend({
  amount: z.coerce.number().refine((amount) => amount !== 0, "Amount cannot be zero"),
});

export type ExpenseFormData = z.infer<typeof expenseSchema>;

export const expenseDefaults: ExpenseFormData = {
  date: new Date().toISOString().slice(0, 10),
  amount: 0,
  currency: "EUR",
  description: "",
  category_id: "",
  notes: "",
};
