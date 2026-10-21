import Link from "next/link";

export default function NotFound() {
  return (
    <div className="rounded-xl border border-hair bg-surface p-8">
      <h1 className="text-lg font-semibold text-ink">Not found</h1>
      <p className="mt-1 text-sm text-ink-2">
        There is no decision for that event. It may not have been scored yet.
      </p>
      <Link href="/queue" className="mt-4 inline-block text-sm text-accent hover:underline">
        Back to the queue
      </Link>
    </div>
  );
}
