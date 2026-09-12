# Secure TeraBox Telegram Bot

Includes per-user daily request/data quotas, hard per-file limits, HMAC-signed
resolver requests, rate/concurrency limiting, unique private temp files and cleanup.

Required:
BOT_TOKEN
TERABOX_RESOLVER_API
RESOLVER_SECRET

Optional:
DAILY_REQUEST_QUOTA=3
DAILY_BYTES_QUOTA=2147483648
MAX_FILE_BYTES=1992294400
MAX_CONCURRENT_DOWNLOADS=2
USER_RATE_LIMIT_SECONDS=10
DOWNLOAD_TIMEOUT=1800
RESOLVE_TIMEOUT=60

The bot supports public/authorized links only. It does not bypass login, CAPTCHA,
private access controls, expired links, or other provider restrictions.

The in-memory quota is process-local. For multiple replicas, use a shared datastore.
