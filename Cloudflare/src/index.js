const enc = new TextEncoder();

async function hmacHex(secret, message) {
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return [...new Uint8Array(sig)].map(x => x.toString(16).padStart(2, "0")).join("");
}

function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

export default {
  async fetch(request, env) {
    const u = new URL(request.url);
    if (u.pathname !== "/api") return new Response("OK");
    if (request.method !== "GET") return new Response("Method Not Allowed", {status:405});

    const target = u.searchParams.get("url");
    const ts = request.headers.get("X-Resolver-Timestamp");
    const nonce = request.headers.get("X-Resolver-Nonce");
    const provided = request.headers.get("X-Resolver-Signature");

    if (!target || !ts || !nonce || !provided || !env.RESOLVER_SECRET)
      return Response.json({success:false,error:"unauthorized"}, {status:401});

    const age = Math.abs(Math.floor(Date.now()/1000) - Number(ts));
    if (!Number.isFinite(age) || age > 60 || nonce.length < 16)
      return Response.json({success:false,error:"expired request"}, {status:401});

    const canonical = `GET\n${new URLSearchParams({url:target}).toString()}\n${ts}\n${nonce}`;
    const expected = await hmacHex(env.RESOLVER_SECRET, canonical);
    if (!safeEqual(provided, expected))
      return Response.json({success:false,error:"bad signature"}, {status:401});

    // Forward only after signature verification.
    // RESOLVER_BACKEND must be an authorized resolver that returns the documented JSON.
    if (!env.RESOLVER_BACKEND)
      return Response.json({success:false,error:"resolver_backend_not_configured"}, {status:501});

    const backend = new URL(env.RESOLVER_BACKEND);
    backend.searchParams.set("url", target);

    const r = await fetch(backend.toString(), {
      headers: {"User-Agent":"TeraBoxResolverProxy/1.0"}
    });

    return new Response(r.body, {
      status: r.status,
      headers: {"content-type": r.headers.get("content-type") || "application/json"}
    });
  }
};
