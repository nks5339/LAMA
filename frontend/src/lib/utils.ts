import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Merge Tailwind classes, with later conflicting utilities winning. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
