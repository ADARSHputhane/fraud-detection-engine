"use client";

import { useState } from "react";

import { TableView } from "@/components/TableView";

export interface HBarRow {
  key: string;
  label: string;
  sublabel?: string;
  value: number;
}

// One series, one colour: horizontal bars from a shared baseline, value at the tip.
export function HBarChart({
  rows,
  format = (v) => v.toLocaleString("en-US"),
  valueHeading = "Value",
}: {
  rows: HBarRow[];
  format?: (v: number) => string;
  valueHeading?: string;
}) {
  const [active, setActive] = useState<string | null>(null);
  const max = Math.max(...rows.map((r) => r.value), 1);
  return (
    <figure>
      <ul className="space-y-1" onMouseLeave={() => setActive(null)}>
        {rows.map((r) => {
          const w = (r.value / max) * 82;
          return (
            <li
              key={r.key}
              tabIndex={0}
              aria-label={`${r.label}: ${format(r.value)}`}
              onMouseEnter={() => setActive(r.key)}
              onFocus={() => setActive(r.key)}
              onBlur={() => setActive(null)}
              className="relative grid grid-cols-[minmax(0,13rem)_1fr] items-center gap-3 rounded-md px-1 outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              <div className="min-w-0 py-1 text-sm leading-snug text-ink">
                {r.label}
                {r.sublabel && <span className="ml-1.5 text-xs text-muted">{r.sublabel}</span>}
              </div>
              <div className="relative h-7">
                <div className="absolute inset-y-0 left-0 w-px bg-axis" />
                <div
                  className="absolute left-0 top-1/2 h-4 -translate-y-1/2"
                  style={{
                    width: `max(${w}%, 2px)`,
                    background: "var(--series-1)",
                    borderRadius: "0 4px 4px 0",
                    opacity: active === null || active === r.key ? 1 : 0.45,
                  }}
                />
                <span
                  className="tabular absolute top-1/2 -translate-y-1/2 text-xs text-ink-2"
                  style={{ left: `calc(${w}% + 6px)` }}
                >
                  {format(r.value)}
                </span>
              </div>
              {active === r.key && (
                <div
                  role="tooltip"
                  className="absolute left-[13.75rem] top-full z-20 mt-1 rounded-lg border border-hair bg-raised px-3 py-2 text-xs shadow-lg"
                >
                  <div className="tabular text-sm font-semibold text-ink">{format(r.value)}</div>
                  <div className="text-ink-2">{r.label}</div>
                </div>
              )}
            </li>
          );
        })}
      </ul>
      <TableView
        head={["", valueHeading]}
        rows={rows.map((r) => [r.sublabel ? `${r.label} (${r.sublabel})` : r.label, format(r.value)])}
      />
    </figure>
  );
}
