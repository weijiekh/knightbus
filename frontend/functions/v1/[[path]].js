// Cloudflare Pages Function: proxy /v1/* to the Knightbus API so the browser stays same-origin.
const TIMEOUT_MS = 10000;

export async function onRequest({ request, env }) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response(JSON.stringify({ detail: "method not allowed" }), {
      status: 405,
      headers: { "content-type": "application/json" },
    });
  }
  const incoming = new URL(request.url);
  const target = new URL(incoming.pathname + incoming.search, env.API_ORIGIN);
  try {
    const upstream = await fetch(new Request(target.toString(), { method: request.method }), {
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    return new Response(upstream.body, { status: upstream.status, headers: upstream.headers });
  } catch {
    return new Response(JSON.stringify({ detail: "backend unreachable" }), {
      status: 502,
      headers: { "content-type": "application/json" },
    });
  }
}
