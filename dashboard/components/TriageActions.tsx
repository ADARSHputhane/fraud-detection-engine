"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import type { Label } from "@/lib/types";

const ANALYST_KEY = "fraud-review:analyst";

function readAnalyst(): string {
  try {
    return window.localStorage.getItem(ANALYST_KEY) ?? "";
  } catch {
    return "";
  }
}

function saveAnalyst(name: string) {
  try {
    window.localStorage.setItem(ANALYST_KEY, name);
  } catch {
    // Private mode or blocked storage: the name just isn't remembered.
  }
}

// F / L label the case and jump to the next one in the queue; U undoes; N skips.
export function TriageActions({ eventId, initial }: { eventId: string; initial: Label | null }) {
  const router = useRouter();
  const [label, setLabel] = useState<Label | null>(initial);
  const [note, setNote] = useState(initial?.note ?? "");
  const [analyst, setAnalyst] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setAnalyst(readAnalyst()), []);

  const next = useCallback(async () => {
    try {
      const res = await fetch("/api/decisions?view=review&labelled=no&sort=score&limit=2");
      const page = (await res.json()) as { items: { event_id: string }[] };
      const target = page.items.find((i) => i.event_id !== eventId);
      router.push(target ? `/events/${target.event_id}` : "/queue");
    } catch {
      router.push("/queue");
    }
  }, [eventId, router]);

  const submit = useCallback(
    async (isFraud: boolean) => {
      if (busy) return;
      setBusy(true);
      setError(null);
      const name = analyst.trim() || "analyst";
      try {
        const res = await fetch("/api/labels", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            event_id: eventId,
            is_fraud: isFraud,
            analyst: name,
            note: note.trim() || null,
          }),
        });
        if (!res.ok) throw new Error(`Saving failed (${res.status})`);
        setLabel((await res.json()) as Label);
        saveAnalyst(name);
        await next();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Saving failed");
      } finally {
        setBusy(false);
      }
    },
    [analyst, busy, eventId, next, note],
  );

  const undo = useCallback(async () => {
    setBusy(true);
    try {
      await fetch(`/api/labels/${eventId}`, { method: "DELETE" });
      setLabel(null);
      router.refresh();
    } finally {
      setBusy(false);
    }
  }, [eventId, router]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const target = e.target as HTMLElement;
      if (target.closest("input, textarea, select") || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "f") void submit(true);
      else if (e.key === "l") void submit(false);
      else if (e.key === "u" && label) void undo();
      else if (e.key === "n") void next();
      else if (e.key === "Escape") router.push("/queue");
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [label, next, router, submit, undo]);

  return (
    <section className="rounded-xl border border-hair bg-surface p-4" aria-label="Triage">
      <h2 className="text-sm font-semibold text-ink">Your verdict</h2>
      {label && (
        <p className="mt-2 rounded-md bg-raised px-3 py-2 text-sm text-ink">
          Labelled <strong>{label.is_fraud ? "fraud" : "legitimate"}</strong>
          {label.analyst ? ` by ${label.analyst}` : ""}.{" "}
          <button onClick={() => void undo()} className="text-accent hover:underline" disabled={busy}>
            Undo <kbd>U</kbd>
          </button>
        </p>
      )}
      <div className="mt-3 grid grid-cols-2 gap-2">
        <button
          onClick={() => void submit(true)}
          disabled={busy}
          className="whitespace-nowrap rounded-lg border border-hair bg-raised px-2 py-2.5 text-sm font-medium text-ink hover:border-[var(--critical)] disabled:opacity-50"
        >
          <span aria-hidden className="mr-1.5" style={{ color: "var(--critical)" }}>
            ●
          </span>
          Confirm fraud <kbd>F</kbd>
        </button>
        <button
          onClick={() => void submit(false)}
          disabled={busy}
          className="whitespace-nowrap rounded-lg border border-hair bg-raised px-2 py-2.5 text-sm font-medium text-ink hover:border-[var(--good)] disabled:opacity-50"
        >
          <span aria-hidden className="mr-1.5" style={{ color: "var(--good)" }}>
            ●
          </span>
          Legitimate <kbd>L</kbd>
        </button>
      </div>
      <label className="mt-3 block text-xs text-ink-2">
        Note (optional)
        <textarea
          value={note}
          onChange={(e) => setNote(e.target.value)}
          maxLength={500}
          rows={2}
          className="mt-1 w-full rounded-md border border-hair bg-raised px-2 py-1.5 text-sm text-ink"
          placeholder="e.g. customer confirmed by phone"
        />
      </label>
      <label className="mt-2 block text-xs text-ink-2">
        Analyst
        <input
          value={analyst}
          onChange={(e) => setAnalyst(e.target.value)}
          maxLength={64}
          className="mt-1 w-full rounded-md border border-hair bg-raised px-2 py-1.5 text-sm text-ink"
          placeholder="your name"
        />
      </label>
      {error && <p className="mt-2 text-sm text-ink">⚠ {error}</p>}
      <p className="mt-3 text-xs text-muted">
        <kbd>N</kbd> skip to next · <kbd>Esc</kbd> back to queue
      </p>
    </section>
  );
}
