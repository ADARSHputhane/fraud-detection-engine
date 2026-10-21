"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { DecisionBadge } from "@/components/DecisionBadge";
import { featureLabel, humanize, money, when } from "@/lib/format";
import type { QueueItem } from "@/lib/types";

function reason(item: QueueItem): string {
  if (item.top_rule) return item.top_rule.reason;
  if (item.top_feature) {
    return `Model: “${featureLabel(item.top_feature.feature)}” raised the risk most`;
  }
  return "—";
}

// J / K move the selection, Enter opens it: an analyst can work the queue without a mouse.
export function QueueTable({ items }: { items: QueueItem[] }) {
  const router = useRouter();
  const [selected, setSelected] = useState(0);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const target = e.target as HTMLElement;
      if (target.closest("input, textarea, select") || e.metaKey || e.ctrlKey) return;
      if (e.key === "j") setSelected((s) => Math.min(s + 1, items.length - 1));
      else if (e.key === "k") setSelected((s) => Math.max(s - 1, 0));
      else if (e.key === "Enter" && items[selected]) router.push(`/events/${items[selected].event_id}`);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items, selected, router]);

  if (!items.length) {
    return (
      <div className="rounded-xl border border-hair bg-surface p-10 text-center text-sm text-ink-2">
        Nothing here. The queue is clear.
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-xl border border-hair bg-surface">
      <table className="w-full min-w-[860px] text-left text-sm">
        <thead className="border-b border-hair text-xs text-muted">
          <tr>
            <th className="px-4 py-2.5 font-medium">Risk</th>
            <th className="px-4 py-2.5 font-medium">Decision</th>
            <th className="px-4 py-2.5 text-right font-medium">Amount</th>
            <th className="px-4 py-2.5 font-medium">What</th>
            <th className="px-4 py-2.5 font-medium">Main reason</th>
            <th className="px-4 py-2.5 font-medium">When</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item, i) => (
            <tr
              key={item.event_id}
              onClick={() => router.push(`/events/${item.event_id}`)}
              onMouseEnter={() => setSelected(i)}
              className={`cursor-pointer border-b border-hair last:border-0 ${
                i === selected ? "bg-raised" : ""
              }`}
              aria-selected={i === selected}
            >
              <td className="px-4 py-3">
                <div className="flex items-center gap-2">
                  <span className="tabular w-7 text-right font-semibold text-ink">
                    {item.ml_score == null ? "—" : Math.round(item.ml_score)}
                  </span>
                  <span className="h-1.5 w-14 rounded-full bg-grid">
                    <span
                      className="block h-1.5 rounded-full"
                      style={{
                        width: `${Math.max(item.ml_score ?? 0, 2)}%`,
                        background: "var(--series-1)",
                      }}
                    />
                  </span>
                </div>
              </td>
              <td className="px-4 py-3">
                <div className="flex items-center gap-2">
                  <DecisionBadge decision={item.final_decision} />
                  {item.analyst_label !== null && (
                    <span className="text-xs text-ink-2">
                      ✓ {item.analyst_label ? "fraud" : "legit"}
                    </span>
                  )}
                </div>
              </td>
              <td className="tabular px-4 py-3 text-right text-ink">
                {item.event_type === "payment" ? money(item.amount, item.currency) : "Card added"}
              </td>
              <td className="px-4 py-3 text-ink-2">
                <Link
                  href={`/events/${item.event_id}`}
                  className="text-ink hover:underline"
                  onClick={(e) => e.stopPropagation()}
                >
                  {item.event_type === "payment" ? humanize(item.merchant_category) : "Instrument"}
                </Link>
                <div className="font-mono text-xs text-muted">{item.user_id}</div>
              </td>
              <td className="max-w-[22rem] px-4 py-3 text-ink-2">
                <span className="line-clamp-2">{reason(item)}</span>
              </td>
              <td className="whitespace-nowrap px-4 py-3 text-xs text-ink-2">{when(item.occurred_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
