import {
  Car,
  GraduationCap,
  HeartPulse,
  Home,
  MoreHorizontal,
  PartyPopper,
  Plane,
  Repeat,
  ShoppingCart,
  Tag,
  Utensils,
  Zap,
  type LucideIcon,
} from "lucide-react";

const CATEGORY_ICONS: Record<string, LucideIcon> = {
  "shopping-cart": ShoppingCart,
  utensils: Utensils,
  home: Home,
  bolt: Zap,
  car: Car,
  "graduation-cap": GraduationCap,
  "heart-pulse": HeartPulse,
  repeat: Repeat,
  "party-popper": PartyPopper,
  plane: Plane,
  "more-horizontal": MoreHorizontal,
  tag: Tag,
};

export function categoryIcon(name: string | undefined): LucideIcon {
  return (name && CATEGORY_ICONS[name]) || Tag;
}
