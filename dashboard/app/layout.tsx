import "./globals.css";

import type { Metadata } from "next";
import Link from "next/link";

import { api } from "@/lib/api";

export const metadata: Metadata = {
  title: "Fraud Review",
  description: "Analyst review queue for the payment fraud detection engine",
};

async function modelVersion(): Promise<string | null> {
  try {
    const m = await api<{ version: string }>("/models/current");
    return m?.version ?? null;
  } catch {
    return null;
  }
}

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const version = await modelVersion();
  return (
    <html lang="en">
      <body className="min-h-screen font-sans antialiased">
        <header className="border-b border-hair bg-surface">
          <nav className="mx-auto flex max-w-7xl items-center gap-6 px-4 py-3 sm:px-6">
            <Link href="/queue" className="font-semibold text-ink">
              Fraud Review
            </Link>
            <Link href="/queue" className="text-sm text-ink-2 hover:text-ink">
              Queue
            </Link>
            <Link href="/stats" className="text-sm text-ink-2 hover:text-ink">
              Stats
            </Link>
            <span className="ml-auto truncate text-xs text-muted">
              {version ? `model ${version}` : "rules only — no model promoted"}
            </span>
          </nav>
        </header>
        <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6">{children}</main>
      </body>
    </html>
  );
}
