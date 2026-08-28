/**
 * Egress proxy for the restock monitor.
 *
 * Retailer bot protection scores datacenter IP ranges harshly, so a request
 * from the VPS can be refused while the identical request from elsewhere
 * succeeds. This Worker re-issues the request from Cloudflare's edge, giving
 * the monitor a second network identity for the specific hosts that block it.
 *
 * It is deliberately NOT a general-purpose proxy. An open relay on a public URL
 * gets found and abused within days, so every request must carry the shared
 * key, and the destination must be on the allowlist this Worker is deployed
 * with. Both are required — neither alone is enough.
 */

const MAX_BYTES = 5 * 1024 * 1024;

// Headers worth carrying to the origin. Anything else (cookies, auth) is
// dropped rather than forwarded blindly.
const FORWARD = [
  "user-agent",
  "accept",
  "accept-language",
  "if-none-match",
  "if-modified-since",
];

// Sent back to the monitor. ETag and Last-Modified matter most: without them
// conditional requests stop working and every poll re-downloads in full.
const RETURN = [
  "content-type",
  "etag",
  "last-modified",
  "retry-after",
  "cache-control",
];

function deny(status, message) {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function allowed(hostname, allowlist) {
  return allowlist.some(
    (entry) => hostname === entry || hostname.endsWith("." + entry),
  );
}

export default {
  async fetch(request, env) {
    if (request.method !== "GET") {
      return deny(405, "only GET is proxied");
    }

    // Constant-ish comparison; the key is a bearer secret, not a password.
    const presented = request.headers.get("X-Proxy-Key") || "";
    if (!env.PROXY_KEY || presented !== env.PROXY_KEY) {
      return deny(403, "bad or missing X-Proxy-Key");
    }

    const target = new URL(request.url).searchParams.get("url");
    if (!target) return deny(400, "missing ?url=");

    let parsed;
    try {
      parsed = new URL(target);
    } catch {
      return deny(400, "unparseable url");
    }
    if (parsed.protocol !== "https:") {
      return deny(400, "https only");
    }

    const allowlist = (env.ALLOWED_HOSTS || "")
      .split(",")
      .map((h) => h.trim().toLowerCase())
      .filter(Boolean);
    if (!allowlist.length) {
      return deny(503, "ALLOWED_HOSTS is not configured");
    }
    if (!allowed(parsed.hostname.toLowerCase(), allowlist)) {
      return deny(403, `${parsed.hostname} is not on this proxy's allowlist`);
    }

    const headers = new Headers();
    for (const name of FORWARD) {
      const value = request.headers.get(name);
      if (value) headers.set(name, value);
    }

    let upstream;
    try {
      upstream = await fetch(parsed.toString(), {
        method: "GET",
        headers,
        redirect: "follow",
        // Serving a cached body would defeat the point: the monitor is asking
        // what the origin says right now.
        cf: { cacheTtl: 0, cacheEverything: false },
      });
    } catch (err) {
      return deny(502, `upstream fetch failed: ${err}`);
    }

    // 304 carries no body and must be passed through intact, or the monitor's
    // conditional requests silently degrade into full fetches.
    if (upstream.status === 304) {
      const out = new Headers();
      for (const name of RETURN) {
        const value = upstream.headers.get(name);
        if (value) out.set(name, value);
      }
      return new Response(null, { status: 304, headers: out });
    }

    const body = await upstream.arrayBuffer();
    if (body.byteLength > MAX_BYTES) {
      return deny(502, "upstream response too large");
    }

    const out = new Headers();
    for (const name of RETURN) {
      const value = upstream.headers.get(name);
      if (value) out.set(name, value);
    }
    out.set("X-Proxied-By", "restock-monitor-worker");

    return new Response(body, { status: upstream.status, headers: out });
  },
};
