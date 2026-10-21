// Same-origin proxy from the browser to the FastAPI service. API_URL is read at request
// time, so one image works locally and in Docker; only the paths the dashboard needs are
// forwarded.

import { NextRequest, NextResponse } from "next/server";

import { API_URL } from "@/lib/api";

export const dynamic = "force-dynamic";

const ALLOWED = /^(decisions|cases|labels|stats)(\/[A-Za-z0-9-]+)*$/;

async function proxy(req: NextRequest, { params }: { params: { path: string[] } }) {
  const path = params.path.join("/");
  if (!ALLOWED.test(path)) {
    return NextResponse.json({ detail: "not found" }, { status: 404 });
  }
  const hasBody = req.method !== "GET" && req.method !== "HEAD";
  let upstream: Response;
  try {
    upstream = await fetch(`${API_URL}/${path}${req.nextUrl.search}`, {
      method: req.method,
      headers: hasBody ? { "content-type": "application/json" } : undefined,
      body: hasBody ? await req.text() : undefined,
      cache: "no-store",
    });
  } catch {
    return NextResponse.json({ detail: "fraud engine API unreachable" }, { status: 502 });
  }
  if (upstream.status === 204) return new NextResponse(null, { status: 204 });
  return new NextResponse(await upstream.text(), {
    status: upstream.status,
    headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" },
  });
}

export { proxy as DELETE, proxy as GET, proxy as POST };
