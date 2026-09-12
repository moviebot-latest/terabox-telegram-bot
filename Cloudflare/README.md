# Cloudflare Resolver Proxy

This Worker verifies HMAC-SHA256 requests from the bot and forwards verified requests
to an authorized resolver backend.

Set secrets:
wrangler secret put RESOLVER_SECRET

Set the backend URL as a non-secret variable if appropriate:
RESOLVER_BACKEND=https://your-authorized-resolver.example/api

Backend response contract:
{
  "success": true,
  "files": [
    {"file_name":"video.mp4","size":123456789,"download_url":"https://..."}
  ]
}

Do not use this proxy to bypass access controls or authentication.
