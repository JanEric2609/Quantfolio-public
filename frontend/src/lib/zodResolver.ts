import { zodResolver as _zodResolver } from "@hookform/resolvers/zod";
import type { Resolver } from "react-hook-form";
import type { z } from "zod/v4";

/**
 * Wraps @hookform/resolvers zodResolver with output-typed resolver.
 *
 * Zod 4's z.coerce.number() produces `input: unknown`, which causes
 * the raw zodResolver to return `Resolver<z.input<T>>` — incompatible
 * with `useForm<z.output<T>>`. This wrapper re-types the resolver to
 * match the output type that useForm expects, since zod 4 coercion
 * already handles the runtime conversion.
 */
export function zodResolver<T extends z.ZodType<any, any, any>>(
  schema: T,
): Resolver<z.output<T>, any, z.output<T>> {
  const resolver: Resolver<z.output<T>, any, z.output<T>> = _zodResolver(
    schema,
  ) as unknown as Resolver<z.output<T>, any, z.output<T>>;
  return resolver;
}
