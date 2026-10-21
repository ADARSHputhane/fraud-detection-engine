"use client";

import { useState } from "react";

import { TableView } from "@/components/TableView";
import { featureLabel, featureValue } from "@/lib/format";
import type { Contribution } from "@/lib/types";

// Diverging bars around zero: red pushes the score towards fraud, blue towards legitimate.
// Bars are scaled to 40% of each half so the signed value always fits at the tip.
export function ShapChart({
  items,
  descriptions,
}: {
  items: Contribution[];
  descriptions: Record<string, string>;
}) {
  const [active, setActive] = useState<number | null>(null);
  const max = Math.max(...items.map((c) => Math.abs(c.contribution)), 1e-6);

  return (
    <figure>
      <figcaption className="mb-3 flex flex-wrap items-center gap-4 text-xs text-ink-2">
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2.5 w-2.5 rounded-sm" style={{ background: "var(--raises)" }} />
          Raises risk
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2.5 w-2.5 rounded-sm" style={{ background: "var(--lowers)" }} />
          Lowers risk
        </span>
        <span className="text-muted">SHAP contribution, log-odds</span>
      </figcaption>
      <ul className="space-y-1.5" onMouseLeave={() => setActive(null)}>
        {items.map((c, i) => {
          const w = (Math.abs(c.contribution) / max) * 40;
          const up = c.contribution > 0;
          const signed = `${up ? "+" : "−"}${Math.abs(c.contribution).toFixed(2)}`;
          return (
            <li
              key={c.feature}
              tabIndex={0}
              aria-label={`${featureLabel(c.feature)} is ${featureValue(c.value, c.feature)}; ${
                up ? "raises" : "lowers"
              } risk by ${Math.abs(c.contribution).toFixed(2)}`}
              onMouseEnter={() => setActive(i)}
              onFocus={() => setActive(i)}
              onBlur={() => setActive(null)}
              className="relative grid grid-cols-[minmax(0,13rem)_1fr] items-center gap-3 rounded-md px-1 outline-none focus-visible:ring-2 focus-visible:ring-accent"
            >
              <div className="min-w-0 py-0.5 leading-tight">
                <div className="text-sm text-ink">{featureLabel(c.feature)}</div>
                <div className="tabular text-xs text-ink-2">{featureValue(c.value, c.feature)}</div>
              </div>
              <div className="relative h-7">
                <div className="absolute inset-y-0 left-1/2 w-px bg-axis" />
                <div
                  className="absolute top-1/2 h-4 -translate-y-1/2 transition-opacity"
                  style={{
                    left: up ? "50%" : `${50 - w}%`,
                    width: `max(${w}%, 2px)`,
                    background: up ? "var(--raises)" : "var(--lowers)",
                    borderRadius: up ? "0 4px 4px 0" : "4px 0 0 4px",
                    opacity: active === null || active === i ? 1 : 0.45,
                  }}
                />
                <span
                  className="tabular absolute top-1/2 -translate-y-1/2 text-xs text-ink-2"
                  style={up ? { left: `calc(${50 + w}% + 6px)` } : { right: `calc(${50 + w}% + 6px)` }}
                >
                  {signed}
                </span>
              </div>
              {active === i && (
                <div
                  role="tooltip"
                  className="absolute left-[13.75rem] top-full z-20 mt-1 w-72 rounded-lg border border-hair bg-raised p-3 text-xs shadow-lg"
                >
                  <div className="tabular text-sm font-semibold text-ink">
                    {signed} {up ? "towards fraud" : "towards legitimate"}
                  </div>
                  <div className="mt-1 text-ink-2">
                    {descriptions[c.feature] ?? featureLabel(c.feature)}:{" "}
                    <span className="text-ink">{featureValue(c.value, c.feature)}</span>
                  </div>
                </div>
              )}
            </li>
          );
        })}
      </ul>
      <TableView
        head={["Feature", "Value", "Contribution"]}
        rows={items.map((c) => [
          featureLabel(c.feature),
          featureValue(c.value, c.feature),
          c.contribution.toFixed(3),
        ])}
      />
    </figure>
  );
}
