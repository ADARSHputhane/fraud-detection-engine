"use client";

import { useLayoutEffect, useRef, useState } from "react";

import { TableView } from "@/components/TableView";

export interface ColumnSeries {
  key: string;
  label: string;
  color: string;
  values: number[];
}

const MARGIN = { top: 20, right: 8, bottom: 28, left: 44 };
const GAP = 2; // surface gap between stacked segments

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const exp = 10 ** Math.floor(Math.log10(v));
  const step = [1, 2, 2.5, 5, 10].find((m) => m * exp >= v / 4) ?? 10;
  return Math.ceil(v / (step * exp)) * step * exp;
}

function compact(v: number): string {
  return new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(v);
}

// Rounded data end at the top, square at the baseline.
function topRounded(x: number, y: number, w: number, h: number, r = 4): string {
  const rr = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + rr}A${rr},${rr} 0 0 1 ${x + rr},${y}H${x + w - rr}A${rr},${rr} 0 0 1 ${x + w},${y + rr}V${y + h}Z`;
}

// Columns (stacked when given several series) on one axis, sized to the container's real
// width so text is never scaled. Each column's whole slot is its hover/focus target.
export function ColumnChart({
  categories,
  series,
  height = 240,
  capLabels = false,
  ariaLabel,
}: {
  categories: string[];
  series: ColumnSeries[];
  height?: number;
  capLabels?: boolean;
  ariaLabel: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  const [active, setActive] = useState<number | null>(null);

  // Measure before the first paint, then follow resizes. The server render (and any
  // environment without layout callbacks) falls back to the viewBox scaling down to fit.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setWidth(Math.max(280, Math.floor(el.getBoundingClientRect().width)));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const totals = categories.map((_, i) => series.reduce((s, ser) => s + ser.values[i], 0));
  const yMax = niceMax(Math.max(...totals, 0));
  const plotW = width - MARGIN.left - MARGIN.right;
  const plotH = height - MARGIN.top - MARGIN.bottom;
  const slot = plotW / Math.max(categories.length, 1);
  const barW = Math.min(24, slot * 0.62);
  const y = (v: number) => MARGIN.top + plotH - (v / yMax) * plotH;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * yMax);
  const longest = Math.max(...categories.map((c) => c.length), 1);
  const every = Math.max(1, Math.ceil((longest * 6.5 + 8) / slot));

  const tip = active === null ? null : active;
  const tipLeft = tip === null ? 0 : Math.min(Math.max(MARGIN.left + tip * slot + slot / 2, 90), width - 90);

  return (
    <figure>
      {series.length > 1 && (
        <figcaption className="mb-2 flex flex-wrap gap-4 text-xs text-ink-2">
          {series.map((s) => (
            <span key={s.key} className="inline-flex items-center gap-1.5">
              <span className="h-2.5 w-2.5 rounded-sm" style={{ background: s.color }} />
              {s.label}
            </span>
          ))}
        </figcaption>
      )}
      <div ref={ref} className="relative" onMouseLeave={() => setActive(null)}>
        <svg
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-label={ariaLabel}
          className="block h-auto max-w-full"
        >
          {ticks.map((t) => (
            <g key={t}>
              <line
                x1={MARGIN.left}
                x2={width - MARGIN.right}
                y1={y(t)}
                y2={y(t)}
                stroke={t === 0 ? "var(--axis)" : "var(--grid)"}
                strokeWidth={1}
              />
              <text
                x={MARGIN.left - 8}
                y={y(t)}
                dy="0.32em"
                textAnchor="end"
                fontSize={11}
                fill="var(--muted)"
                className="tabular"
              >
                {compact(t)}
              </text>
            </g>
          ))}
          {categories.map((c, i) => {
            const x = MARGIN.left + i * slot + (slot - barW) / 2;
            let base = 0;
            const visible = series.filter((s) => s.values[i] > 0);
            return (
              <g
                key={c}
                tabIndex={0}
                role="graphics-symbol"
                aria-label={`${c}: ${series.map((s) => `${s.label} ${s.values[i]}`).join(", ")}`}
                onMouseEnter={() => setActive(i)}
                onFocus={() => setActive(i)}
                onBlur={() => setActive(null)}
                className="outline-none"
                opacity={active === null || active === i ? 1 : 0.55}
              >
                <rect x={MARGIN.left + i * slot} y={MARGIN.top} width={slot} height={plotH} fill="transparent" />
                {visible.map((s, k) => {
                  const v = s.values[i];
                  const top = y(base + v);
                  const bottom = y(base) - (k > 0 ? GAP : 0);
                  base += v;
                  const h = Math.max(bottom - top, 1);
                  const isTop = k === visible.length - 1;
                  return isTop ? (
                    <path key={s.key} d={topRounded(x, top, barW, h)} fill={s.color} />
                  ) : (
                    <rect key={s.key} x={x} y={top} width={barW} height={h} fill={s.color} />
                  );
                })}
                {capLabels && totals[i] > 0 && slot > 26 && (
                  <text
                    x={x + barW / 2}
                    y={y(totals[i]) - 6}
                    textAnchor="middle"
                    fontSize={11}
                    fill="var(--ink-2)"
                    className="tabular"
                  >
                    {compact(totals[i])}
                  </text>
                )}
                {i % every === 0 && (
                  <text
                    x={MARGIN.left + i * slot + slot / 2}
                    y={height - 8}
                    textAnchor="middle"
                    fontSize={11}
                    fill="var(--muted)"
                  >
                    {c}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
        {tip !== null && (
          <div
            role="tooltip"
            className="pointer-events-none absolute top-0 z-20 -translate-x-1/2 rounded-lg border border-hair bg-raised px-3 py-2 text-xs shadow-lg"
            style={{ left: tipLeft }}
          >
            <div className="mb-1 text-ink-2">{categories[tip]}</div>
            {series.map((s) => (
              <div key={s.key} className="flex items-center gap-2">
                <span className="h-0.5 w-3" style={{ background: s.color }} />
                <span className="tabular font-semibold text-ink">
                  {s.values[tip].toLocaleString("en-US")}
                </span>
                <span className="text-ink-2">{s.label}</span>
              </div>
            ))}
          </div>
        )}
      </div>
      <TableView
        head={["", ...series.map((s) => s.label)]}
        rows={categories.map((c, i) => [c, ...series.map((s) => s.values[i].toLocaleString("en-US"))])}
      />
    </figure>
  );
}
