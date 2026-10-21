export function ApiError({ message }: { message: string }) {
  return (
    <div className="rounded-xl border border-hair bg-surface p-6">
      <h2 className="font-semibold text-ink">The fraud engine isn&apos;t answering</h2>
      <p className="mt-1 text-sm text-ink-2">{message}</p>
      <p className="mt-3 text-sm text-ink-2">
        Start it with <code className="font-mono text-ink">docker compose up -d</code>, or point
        the dashboard elsewhere with <code className="font-mono text-ink">API_URL</code>.
      </p>
    </div>
  );
}
