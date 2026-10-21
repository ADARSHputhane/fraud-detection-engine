// Server-side access to the FastAPI service. Browser code goes through /api/* instead.

export const API_URL = process.env.API_URL ?? "http://localhost:8000";

export class ApiUnavailable extends Error {}

export async function api<T>(path: string): Promise<T | null> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, { cache: "no-store" });
  } catch {
    throw new ApiUnavailable(`Cannot reach the fraud engine API at ${API_URL}.`);
  }
  if (res.status === 404) return null;
  if (!res.ok) throw new ApiUnavailable(`API ${path} returned ${res.status}.`);
  return (await res.json()) as T;
}
