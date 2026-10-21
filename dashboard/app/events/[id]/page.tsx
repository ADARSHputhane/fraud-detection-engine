import Link from "next/link";
import { notFound } from "next/navigation";

import { ApiError } from "@/components/ApiError";
import { DecisionBadge } from "@/components/DecisionBadge";
import { ScoreMeter } from "@/components/ScoreMeter";
import { ShapChart } from "@/components/ShapChart";
import { TriageActions } from "@/components/TriageActions";
import { api, ApiUnavailable } from "@/lib/api";
import { featureValue, humanize, money, ms, when } from "@/lib/format";
import type { Case, FeatureValue } from "@/lib/types";

export const dynamic = "force-dynamic";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const GROUPS: Record<string, string> = {
  context: "Event",
  velocity: "Velocity",
  behaviour: "Behaviour vs. the user's baseline",
  account: "Account and device",
  geo: "Geo and network",
  instrument: "Card",
};

function str(v: unknown): string {
  return v === null || v === undefined || v === "" ? "—" : String(v);
}

function Detail({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4 border-b border-hair py-1.5 last:border-0">
      <dt className="text-ink-2">{label}</dt>
      <dd className="min-w-0 truncate text-right text-ink">{children}</dd>
    </div>
  );
}

export default async function CasePage({ params }: { params: { id: string } }) {
  if (!UUID.test(params.id)) notFound();
  let data: Case | null;
  try {
    data = await api<Case>(`/cases/${params.id}`);
  } catch (e) {
    return <ApiError message={e instanceof ApiUnavailable ? e.message : "Unexpected error."} />;
  }
  if (!data) notFound();

  const { event, decision, features, label, history } = data;
  const isPayment = decision.event_type === "payment";
  const geo = (event.geo ?? {}) as Record<string, unknown>;
  const descriptions = Object.fromEntries(features.map((f) => [f.name, f.description]));
  const grouped = features.reduce<Record<string, FeatureValue[]>>((acc, f) => {
    if (f.kind !== "str") (acc[f.group] ??= []).push(f);
    return acc;
  }, {});

  return (
    <div className="space-y-5">
      <Link href="/queue" className="text-sm text-ink-2 hover:text-ink">
        ← Queue
      </Link>

      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-ink">
            {isPayment
              ? `${money(str(event.amount), str(event.currency))} · ${humanize(str(event.merchant_category))}`
              : `Card ${str(event.action)} · ${str(event.card_type)}, issued in ${str(event.issuing_country)}`}
          </h1>
          <p className="mt-1 text-sm text-ink-2">
            {when(decision.occurred_at)} · user <span className="font-mono">{decision.user_id}</span>
            {isPayment && ` · ${str(event.status)}`}
          </p>
        </div>
        <div className="text-right">
          <DecisionBadge decision={decision.final_decision} large />
          <p className="mt-1 text-xs text-muted">
            decided by {decision.decided_by} in {ms(decision.latency_ms)}
          </p>
        </div>
      </header>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="min-w-0 space-y-5">
          <section className="rounded-xl border border-hair bg-surface p-5">
            <h2 className="text-sm font-semibold text-ink">Why it was flagged</h2>
            {decision.rules_fired.length ? (
              <ul className="mt-3 space-y-2">
                {decision.rules_fired.map((r) => (
                  <li key={r.id} className="flex gap-3 text-sm">
                    <span className="shrink-0">
                      <DecisionBadge decision={r.action} />
                    </span>
                    <span>
                      <span className="font-medium text-ink">
                        {r.id} · {r.name}
                      </span>
                      <span className="block text-ink-2">{r.reason}</span>
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-2 text-sm text-ink-2">No rule fired; the model score decided.</p>
            )}
          </section>

          <section className="rounded-xl border border-hair bg-surface p-5">
            <h2 className="text-sm font-semibold text-ink">Model</h2>
            {decision.ml_score !== null ? (
              <div className="mt-3 space-y-5">
                <ScoreMeter score={decision.ml_score} thresholds={decision.thresholds} />
                {decision.shap_top5 && decision.shap_top5.length > 0 && (
                  <div>
                    <h3 className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">
                      What moved the score
                    </h3>
                    <ShapChart items={decision.shap_top5} descriptions={descriptions} />
                  </div>
                )}
                <p className="text-xs text-muted">
                  {decision.model_version} · rules {decision.ruleset_version}
                </p>
              </div>
            ) : (
              <p className="mt-2 text-sm text-ink-2">No model was in use; rules decided alone.</p>
            )}
          </section>

          <section className="rounded-xl border border-hair bg-surface p-5">
            <h2 className="text-sm font-semibold text-ink">This user&apos;s previous activity</h2>
            {history.length ? (
              <div className="mt-2 overflow-x-auto">
                <table className="w-full min-w-[520px] text-left text-sm">
                  <thead className="text-xs text-muted">
                    <tr>
                      <th className="py-1.5 font-medium">When</th>
                      <th className="py-1.5 font-medium">What</th>
                      <th className="py-1.5 text-right font-medium">Amount</th>
                      <th className="py-1.5 pl-6 font-medium">Country</th>
                      <th className="py-1.5 text-right font-medium">Risk</th>
                      <th className="py-1.5 pl-3 font-medium">Decision</th>
                    </tr>
                  </thead>
                  <tbody className="tabular">
                    {history.map((h) => (
                      <tr key={h.event_id} className="border-t border-hair">
                        <td className="py-1.5 text-xs text-ink-2">
                          <Link href={`/events/${h.event_id}`} className="hover:underline">
                            {when(h.occurred_at)}
                          </Link>
                        </td>
                        <td className="py-1.5 text-ink">
                          {h.event_type === "payment" ? humanize(h.merchant_category) : "Card added"}
                        </td>
                        <td className="py-1.5 text-right text-ink">{h.amount ? money(h.amount) : "—"}</td>
                        <td className="py-1.5 pl-6 text-ink-2">{h.country ?? "—"}</td>
                        <td className="py-1.5 text-right text-ink-2">
                          {h.ml_score === null ? "—" : Math.round(h.ml_score)}
                        </td>
                        <td className="py-1.5 pl-3">
                          <DecisionBadge decision={h.final_decision} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="mt-2 text-sm text-ink-2">First event we have seen from this user.</p>
            )}
          </section>

          <details className="rounded-xl border border-hair bg-surface p-5">
            <summary className="cursor-pointer text-sm font-semibold text-ink">
              All {Object.values(grouped).flat().length} signals
            </summary>
            <div className="mt-3 grid gap-5 md:grid-cols-2">
              {Object.entries(grouped).map(([group, items]) => (
                <div key={group}>
                  <h3 className="mb-1 text-xs font-medium uppercase tracking-wide text-muted">
                    {GROUPS[group] ?? group}
                  </h3>
                  <dl className="text-sm">
                    {items.map((f) => (
                      <div key={f.name} className="flex justify-between gap-3 border-b border-hair py-1 last:border-0">
                        <dt className="text-ink-2" title={f.name}>
                          {f.description}
                        </dt>
                        <dd className="tabular shrink-0 text-ink">{featureValue(f.value, f.name)}</dd>
                      </div>
                    ))}
                  </dl>
                </div>
              ))}
            </div>
          </details>
        </div>

        <aside className="space-y-5 lg:sticky lg:top-4 lg:self-start">
          <TriageActions eventId={decision.event_id} initial={label} />
          <section className="rounded-xl border border-hair bg-surface p-4">
            <h2 className="mb-1 text-sm font-semibold text-ink">Event</h2>
            <dl className="text-sm">
              <Detail label="Card">
                <span className="font-mono text-xs">{str(event.card_id)}</span>
              </Detail>
              <Detail label="Device">
                <span className="font-mono text-xs">{str(event.device_id)}</span>
              </Detail>
              <Detail label="IP address">
                <span className="font-mono text-xs">{str(event.ip_address)}</span>
              </Detail>
              <Detail label="Location">
                {geo.city ? `${str(geo.city)}, ${str(geo.country)}` : str(geo.country)}
              </Detail>
              {isPayment ? (
                <>
                  <Detail label="Merchant">{str(event.merchant_id)}</Detail>
                  <Detail label="Channel">{humanize(str(event.channel))}</Detail>
                  {event.decline_reason ? (
                    <Detail label="Decline reason">{humanize(str(event.decline_reason))}</Detail>
                  ) : null}
                </>
              ) : (
                <>
                  <Detail label="BIN">{str(event.bin)}</Detail>
                  <Detail label="Name matches account">
                    {event.name_matches_account === false ? "No" : event.name_matches_account ? "Yes" : "—"}
                  </Detail>
                </>
              )}
              <Detail label="Event id">
                <span className="font-mono text-xs">{decision.event_id.slice(0, 8)}…</span>
              </Detail>
            </dl>
          </section>
        </aside>
      </div>
    </div>
  );
}
