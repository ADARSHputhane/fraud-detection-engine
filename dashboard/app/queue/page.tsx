import Link from "next/link";

import { ApiError } from "@/components/ApiError";
import { QueueTable } from "@/components/QueueTable";
import { api, ApiUnavailable } from "@/lib/api";
import { count } from "@/lib/format";
import type { DecisionPage } from "@/lib/types";

export const dynamic = "force-dynamic";

const PAGE_SIZE = 50;
const VIEWS = {
  review: { label: "Needs review", query: "view=review&labelled=no&sort=score" },
  deny: { label: "Denied", query: "view=deny&labelled=any&sort=score" },
  labelled: { label: "Labelled", query: "view=flagged&labelled=yes&sort=recent" },
  all: { label: "All decisions", query: "view=all&labelled=any&sort=recent" },
} as const;
type View = keyof typeof VIEWS;

export default async function QueuePage({
  searchParams,
}: {
  searchParams: { view?: string; page?: string };
}) {
  const view: View = searchParams.view && searchParams.view in VIEWS ? (searchParams.view as View) : "review";
  const page = Math.max(1, Number(searchParams.page ?? 1) || 1);

  let data: DecisionPage | null;
  let pending: DecisionPage | null;
  try {
    [data, pending] = await Promise.all([
      api<DecisionPage>(`/decisions?${VIEWS[view].query}&limit=${PAGE_SIZE}&offset=${(page - 1) * PAGE_SIZE}`),
      api<DecisionPage>(`/decisions?${VIEWS.review.query}&limit=1`),
    ]);
  } catch (e) {
    return <ApiError message={e instanceof ApiUnavailable ? e.message : "Unexpected error."} />;
  }
  const total = data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink">Review queue</h1>
          <p className="text-sm text-ink-2">
            Riskiest first. <kbd>J</kbd> <kbd>K</kbd> to move, <kbd>Enter</kbd> to open.
          </p>
        </div>
        <nav className="flex flex-wrap gap-1 rounded-lg border border-hair bg-surface p-1 text-sm">
          {(Object.keys(VIEWS) as View[]).map((key) => (
            <Link
              key={key}
              href={`/queue?view=${key}`}
              aria-current={key === view ? "page" : undefined}
              className={`rounded-md px-3 py-1.5 ${
                key === view ? "bg-raised font-medium text-ink shadow-sm" : "text-ink-2 hover:text-ink"
              }`}
            >
              {VIEWS[key].label}
              {key === "review" && (
                <span className="tabular ml-1.5 text-xs text-muted">{count(pending?.total ?? 0)}</span>
              )}
            </Link>
          ))}
        </nav>
      </div>

      <QueueTable items={data?.items ?? []} />

      {pages > 1 && (
        <div className="flex items-center justify-between text-sm text-ink-2">
          <span className="tabular">
            {count(total)} events · page {page} of {pages}
          </span>
          <div className="flex gap-2">
            {page > 1 && (
              <Link href={`/queue?view=${view}&page=${page - 1}`} className="rounded-md border border-hair px-3 py-1 hover:text-ink">
                Previous
              </Link>
            )}
            {page < pages && (
              <Link href={`/queue?view=${view}&page=${page + 1}`} className="rounded-md border border-hair px-3 py-1 hover:text-ink">
                Next
              </Link>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
