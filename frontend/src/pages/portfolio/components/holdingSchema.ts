import { z } from "zod";

export const holdingSchema = z.object({
  ticker: z.string().trim().min(1, "Ticker required"),
  isin: z.string().trim().regex(/^[A-Z]{2}[A-Z0-9]{9}\d$/, "Invalid ISIN").optional().or(z.literal("")),
  name: z.string().trim().min(1, "Name required"),
  asset_type: z.enum(["stock", "etf", "bond", "fund", "cash", "crypto", "other"]),
  quantity: z.coerce.number().positive("Must be > 0"),
  avg_buy_price: z.coerce.number().positive("Must be > 0"),
  currency: z.enum(["EUR", "USD", "GBP", "CHF", "JPY"]).default("EUR"),
  buy_date: z.string().optional(),
});

export type HoldingFormData = z.infer<typeof holdingSchema>;

export const holdingDefaults: HoldingFormData = {
  ticker: "",
  isin: "",
  name: "",
  asset_type: "stock",
  quantity: 1,
  avg_buy_price: 1,
  currency: "EUR",
  buy_date: "",
};
