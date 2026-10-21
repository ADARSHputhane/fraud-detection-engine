import { ApiError } from "@/components/ApiError";
import { ColumnChart } from "@/components/ColumnChart";
import { HBarChart } from "@/components/HBarChart";
import { api, ApiUnavailable } from "@/lib/api";
import { count, ms, pct } from "@/lib/format";
import type { Stats } from "@/lib/types";

export const dynamic = "force-dynamic";

function Tile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="rounded-xl border border-hair bg-surface p-4">
      <div className="text-xs text-ink-2">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-ink">{value}</div>
      {note && <div className="mt-0.5 text-xs text-muted">{note}</div>}
    </div>
  );
}

function Card({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <section className="min-w-0 rounded-xl border border-hair bg-surface p-5">
      <h2 className={`text-sm font-semibold text-ink ${subtitle ? "" : "mb-3"}`}>{title}</h2>
      {subtitle && <p className="mb-3 text-xs text-ink-2">{subtitle}</p>}
      {children}
    </section>
  );
}

export default async function StatsPage() {
  let s: Stats | null;
  try {
    s = await api<Stats>("/stats");
  } catch (e) {
    return <ApiError message={e instanceof ApiUnavailable ? e.message : "Unexpected error."} />;
  }
  if (!s) return <ApiError message="No statistics yet." />;

  const gt = s.ground_truth;
  const days = s.by_day.map((d) => d.day.slice(5));
  const decidedTotal = Math.max(s.decisions.total, 1);

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink">Engine statistics</h1>
        <p className="text-sm text-ink-2">Everything the worker has decided so far.</p>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <Tile label="Events scored" value={count(s.decisions.total)} />
        <Tile label="Waiting for review" value={count(s.review_queue)} />
        <Tile
          label="Fraud flagged"
          value={pct(gt?.recall)}
          note={
            gt
              ? `of ${count(gt.fraud_events)} known fraud, incl. the model's training period`
              : "no ground truth loaded"
          }
        />
        <Tile label="False-positive rate" value={pct(gt?.false_positive_rate, 2)} note="genuine events flagged" />
        <Tile
          label="Decision time, p95"
          value={ms(s.latency_ms.processing.p95)}
          note={`incl. queue wait: ${ms(s.latency_ms.end_to_end.p95)}`}
        />
        <Tile
          label="Analyst labels"
          value={count(s.analyst.labelled)}
          note={
            s.analyst.agreement_with_ground_truth == null
              ? undefined
              : `${pct(s.analyst.agreement_with_ground_truth, 0)} agree with ground truth`
          }
        />
      </div>

      <Card
        title="Flagged events per day"
        subtitle={`Reviews and denials by event date, last 30 days. Allowed events (${count(s.decisions.ALLOW)} in total) are not shown.`}
      >
        <ColumnChart
          ariaLabel="Flagged events per day, stacked by decision"
          categories={days}
          series={[
            { key: "REVIEW", label: "Review", color: "var(--series-1)", values: s.by_day.map((d) => d.REVIEW) },
            { key: "DENY", label: "Deny", color: "var(--series-2)", values: s.by_day.map((d) => d.DENY) },
          ]}
        />
      </Card>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card
          title="Model risk scores"
          subtitle="Events per score band. Below 40 is allowed, 40–75 reviewed, above 75 denied."
        >
          <ColumnChart
            ariaLabel="Number of events in each risk score band"
            categories={s.score_histogram.map((b) => `${b.from}–${b.to}`)}
            series={[
              {
                key: "events",
                label: "Events",
                color: "var(--series-1)",
                values: s.score_histogram.map((b) => b.count),
              },
            ]}
            capLabels
          />
        </Card>

        <Card title="Rule hits" subtitle="Several rules can fire on one event.">
          {s.rule_hits.length ? (
            <HBarChart
              valueHeading="Hits"
              rows={[...s.rule_hits]
                .sort((a, b) => b.hits - a.hits)
                .map((r) => ({ key: r.id, label: r.name, sublabel: r.id, value: r.hits }))}
            />
          ) : (
            <p className="text-sm text-ink-2">No rule has fired yet.</p>
          )}
        </Card>
      </div>

      <div className="grid gap-5 md:grid-cols-2">
        <Card title="Who decided">
          <dl className="grid grid-cols-3 gap-3 text-sm">
            {(
              [
                ["A rule", s.decided_by.rule],
                ["The model", s.decided_by.model],
                ["Default allow", s.decided_by.default],
              ] as const
            ).map(([label, n]) => (
              <div key={label}>
                <dt className="text-xs text-ink-2">{label}</dt>
                <dd className="text-lg font-semibold text-ink">{count(n)}</dd>
                <dd className="text-xs text-muted">{pct(n / decidedTotal)}</dd>
              </div>
            ))}
          </dl>
        </Card>
        <Card title="Model in use">
          {s.model ? (
            <dl className="grid grid-cols-3 gap-3 text-sm">
              <div className="col-span-3 min-w-0">
                <dt className="text-xs text-ink-2">Version</dt>
                <dd className="truncate font-mono text-ink">{s.model.version}</dd>
              </div>
              <div>
                <dt className="text-xs text-ink-2">AUC-ROC</dt>
                <dd className="text-lg font-semibold text-ink">{s.model.auc_roc?.toFixed(3) ?? "—"}</dd>
              </div>
              <div>
                <dt className="text-xs text-ink-2">PR-AUC</dt>
                <dd className="text-lg font-semibold text-ink">{s.model.pr_auc?.toFixed(3) ?? "—"}</dd>
              </div>
              <div>
                <dt className="text-xs text-ink-2">Kind</dt>
                <dd className="text-lg font-semibold text-ink">{s.model.kind === "xgboost" ? "XGBoost" : "Isolation Forest"}</dd>
              </div>
            </dl>
          ) : (
            <p className="text-sm text-ink-2">No model promoted; decisions come from rules alone.</p>
          )}
        </Card>
      </div>
    </div>
  );
}
