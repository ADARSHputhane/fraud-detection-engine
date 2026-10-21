import { DecisionBadge } from "@/components/DecisionBadge";
import { band } from "@/lib/format";

const FILL = { ALLOW: "var(--good)", REVIEW: "var(--warning)", DENY: "var(--critical)" };

// A meter: the fill carries the severity band, the track is a lighter step of the same hue,
// and the two thresholds are marked so the score reads against them.
export function ScoreMeter({
  score,
  thresholds,
}: {
  score: number;
  thresholds: { review_at: number; deny_above: number };
}) {
  const b = band(score, thresholds);
  const color = FILL[b];
  return (
    <div>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-4xl font-semibold text-ink">{Math.round(score)}</span>
        <span className="text-sm text-ink-2">/ 100 model risk</span>
        <DecisionBadge decision={b} />
      </div>
      <div
        className="relative mt-3 h-2.5 rounded-full"
        style={{ background: `color-mix(in srgb, ${color} 20%, var(--surface))` }}
        role="meter"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(score)}
        aria-label="Model risk score"
      >
        <div
          className="absolute inset-y-0 left-0 rounded-full"
          style={{ width: `${Math.max(score, 1)}%`, background: color }}
        />
        {[thresholds.review_at, thresholds.deny_above].map((t) => (
          <div
            key={t}
            className="absolute -top-1 h-[18px] w-px bg-ink-2"
            style={{ left: `${t}%` }}
          />
        ))}
      </div>
      <div className="relative mt-1 h-4 text-[11px] text-muted">
        <span className="absolute -translate-x-1/2" style={{ left: `${thresholds.review_at}%` }}>
          review ≥ {thresholds.review_at}
        </span>
        <span className="absolute -translate-x-1/2" style={{ left: `${thresholds.deny_above}%` }}>
          deny &gt; {thresholds.deny_above}
        </span>
      </div>
    </div>
  );
}
