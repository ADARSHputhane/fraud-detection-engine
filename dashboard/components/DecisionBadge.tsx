import type { Decision } from "@/lib/types";

// Status colour lives on the icon only; the text stays in ink so it is always legible, and
// the label means the badge never relies on colour alone.
const STYLE: Record<Decision, { label: string; color: string; path: string }> = {
  ALLOW: { label: "Allow", color: "var(--good)", path: "M3.5 8.5l3 3 6-7" },
  REVIEW: { label: "Review", color: "var(--warning)", path: "M8 3.5v5.5M8 11.8v.7" },
  DENY: { label: "Deny", color: "var(--critical)", path: "M4.5 4.5l7 7M11.5 4.5l-7 7" },
};

export function DecisionBadge({ decision, large = false }: { decision: Decision; large?: boolean }) {
  const s = STYLE[decision];
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border border-hair bg-raised font-medium text-ink ${
        large ? "px-3 py-1 text-sm" : "px-2 py-0.5 text-xs"
      }`}
    >
      <svg
        aria-hidden="true"
        viewBox="0 0 16 16"
        className={large ? "h-4 w-4" : "h-3 w-3"}
        fill="none"
        stroke={s.color}
        strokeWidth={2.4}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d={s.path} />
      </svg>
      {s.label}
    </span>
  );
}
