import os, re, uuid, hmac, hashlib, time, asyncio, logging, tempfile
from pathlib import Path
from urllib.parse import urlparse, urlencode
import aiohttp
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN")
RESOLVER_API = os.getenv("TERABOX_RESOLVER_API")
RESOLVER_SECRET = os.getenv("RESOLVER_SECRET")

DAILY_REQUEST_QUOTA = int(os.getenv("DAILY_REQUEST_QUOTA", "3"))
DAILY_BYTES_QUOTA = int(os.getenv("DAILY_BYTES_QUOTA", str(2 * 1024**3)))
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", str(1900 * 1024 * 1024)))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "1800"))
RESOLVE_TIMEOUT = int(os.getenv("RESOLVE_TIMEOUT", "60"))
MAX_CONCURRENT_DOWNLOADS = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "2"))
USER_RATE_LIMIT_SECONDS = int(os.getenv("USER_RATE_LIMIT_SECONDS", "10"))
SIGNED_REQUEST_TTL = int(os.getenv("SIGNED_REQUEST_TTL", "60"))

USER_AGENT = "Mozilla/5.0 (compatible; TeraBoxTelegramBot/2.1)"
SUPPORTED_DOMAINS = {
    "terabox.com","www.terabox.com","1024terabox.com","www.1024terabox.com",
    "terabox.app","www.terabox.app","teraboxshare.com","www.teraboxshare.com",
    "teraboxlink.com","www.teraboxlink.com","terasharefile.com","www.terasharefile.com",
    "terafileshare.com","www.terafileshare.com","terasharelink.com","www.terasharelink.com",
}
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
USER_LAST_REQUEST = {}
QUOTA_LOCK = asyncio.Lock()
USER_QUOTA = {}
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger("terabox-bot")

def validate_config():
    missing = [k for k,v in {"BOT_TOKEN":BOT_TOKEN,"TERABOX_RESOLVER_API":RESOLVER_API,
                              "RESOLVER_SECRET":RESOLVER_SECRET}.items() if not v]
    if missing:
        raise RuntimeError("Missing environment variables: " + ", ".join(missing))

def extract_url(text):
    m = re.search(r"https?://[^\s<>\"]+", text or "")
    return m.group(0).rstrip(".,);]>") if m else None

def is_supported_url(url):
    try:
        p = urlparse(url)
        return p.scheme.lower() in {"http","https"} and (p.hostname or "").lower() in SUPPORTED_DOMAINS
    except Exception:
        return False

def sanitize_filename(name):
    name = re.sub(r'[\\/:*?"<>|]', "_", str(name or "").strip())
    return (re.sub(r"\s+", " ", name) or "terabox_file")[:180]

def utc_day():
    return time.strftime("%Y-%m-%d", time.gmtime())

def user_is_rate_limited(user_id):
    now = time.monotonic()
    prev = USER_LAST_REQUEST.get(user_id)
    if prev and now - prev < USER_RATE_LIMIT_SECONDS:
        return True
    USER_LAST_REQUEST[user_id] = now
    return False

async def reserve_request_quota(user_id):
    async with QUOTA_LOCK:
        q = USER_QUOTA.get(user_id)
        if not q or q["day"] != utc_day():
            q = {"day": utc_day(), "requests": 0, "bytes": 0}
            USER_QUOTA[user_id] = q
        if q["requests"] >= DAILY_REQUEST_QUOTA:
            return False
        q["requests"] += 1
        return True

async def reserve_bytes_quota(user_id, size):
    async with QUOTA_LOCK:
        q = USER_QUOTA.get(user_id)
        if not q or q["day"] != utc_day():
            q = {"day": utc_day(), "requests": 0, "bytes": 0}
            USER_QUOTA[user_id] = q
        if q["bytes"] + size > DAILY_BYTES_QUOTA:
            return False
        q["bytes"] += size
        return True

def sign_request(url):
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    canonical = f"GET\n{urlencode({'url': url})}\n{ts}\n{nonce}"
    sig = hmac.new(RESOLVER_SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return ts, nonce, sig

async def resolve_terabox(url):
    ts, nonce, sig = sign_request(url)
    timeout = aiohttp.ClientTimeout(total=RESOLVE_TIMEOUT)
    headers = {
        "User-Agent": USER_AGENT,
        "X-Resolver-Timestamp": ts,
        "X-Resolver-Nonce": nonce,
        "X-Resolver-Signature": sig,
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(RESOLVER_API, params={"url": url}) as r:
            if r.status != 200:
                logger.warning("resolver HTTP %s", r.status)
                return []
            try:
                data = await r.json(content_type=None)
            except Exception:
                return []
    if not isinstance(data, dict) or data.get("success") is False:
        return []
    result = []
    for item in data.get("files") or []:
        if not isinstance(item, dict):
            continue
        durl = item.get("download_url") or item.get("download_link") or item.get("direct_link") or item.get("dlink")
        if not durl or not str(durl).startswith(("http://","https://")):
            continue
        result.append({
            "url": str(durl),
            "filename": sanitize_filename(item.get("file_name") or item.get("filename") or "terabox_file"),
            "size": item.get("size"),
        })
    return result

def parse_size(value):
    if value is None: return None
    try:
        if isinstance(value, (int,float)): return int(value)
        s = re.sub(r"[^0-9.]", "", str(value))
        return int(float(s)) if s else None
    except Exception:
        return None

async def download_file(url, path, expected_size, progress):
    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT, connect=30, sock_connect=30, sock_read=120)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent":USER_AGENT,"Accept":"*/*"}) as session:
        async with session.get(url, allow_redirects=True) as r:
            if r.status >= 400:
                raise RuntimeError(f"download HTTP {r.status}")
            total = parse_size(r.headers.get("Content-Length")) or parse_size(expected_size)
            if total and total > MAX_FILE_BYTES:
                raise RuntimeError("file exceeds maximum size")
            fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            downloaded = 0
            try:
                with os.fdopen(fd, "wb") as f:
                    async for chunk in r.content.iter_chunked(1024*1024):
                        downloaded += len(chunk)
                        if downloaded > MAX_FILE_BYTES:
                            raise RuntimeError("file exceeds maximum size")
                        f.write(chunk)
                        await progress(downloaded, total or 0)
            except Exception:
                try: os.unlink(path)
                except FileNotFoundError: pass
                raise
    if downloaded == 0:
        raise RuntimeError("empty download")
    return downloaded

async def progress_message(msg, done, total, state):
    now = time.monotonic()
    if now-state["last"] < 4: return
    state["last"] = now
    text = (f"⬇️ Downloading...\n\nProgress: {min(100,int(done*100/total))}%\n"
            f"Size: {done/1024/1024:.1f} / {total/1024/1024:.1f} MB") if total else \
           f"⬇️ Downloading...\n\nDownloaded: {done/1024/1024:.1f} MB"
    try: await msg.edit_text(text)
    except Exception: pass

async def start(update, context):
    await update.message.reply_text("📥 Send a supported TeraBox/TeraShare link.")

async def help_command(update, context):
    await update.message.reply_text(
        f"Daily quota: {DAILY_REQUEST_QUOTA} requests / {DAILY_BYTES_QUOTA/1024**3:.2f} GiB\n"
        f"Maximum file: {MAX_FILE_BYTES/1024**2:.0f} MB"
    )

async def handle_message(update, context):
    if not update.message or not update.message.text: return
    uid = update.effective_user.id
    if user_is_rate_limited(uid):
        await update.message.reply_text("⏳ Please wait a few seconds.")
        return
    url = extract_url(update.message.text)
    if not url or not is_supported_url(url):
        await update.message.reply_text("❌ Unsupported or invalid TeraBox/TeraShare link.")
        return
    if not await reserve_request_quota(uid):
        await update.message.reply_text("🚫 Daily request quota reached. Try again tomorrow.")
        return

    status = await update.message.reply_text("🔎 Resolving link...")
    temp_dir = tempfile.TemporaryDirectory(prefix=f"tb_{uid}_")
    try:
        async with DOWNLOAD_SEMAPHORE:
            files = await resolve_terabox(url)
            if not files:
                await status.edit_text(
                    "❌ Could not resolve the link. It may be expired, deleted, restricted, "
                    "or the resolver may not support this link."
                )
                return

            item = files[0]
            advertised = parse_size(item.get("size"))
            if advertised and advertised > MAX_FILE_BYTES:
                await status.edit_text("⚠️ File is larger than the configured maximum.")
                return
            if advertised and not await reserve_bytes_quota(uid, advertised):
                await status.edit_text("🚫 Daily data quota reached.")
                return

            out = Path(temp_dir.name) / f"{uuid.uuid4().hex}_{item['filename']}"
            await status.edit_text(f"📦 {item['filename']}\n\n⬇️ Starting download...")
            state = {"last": 0}
            async def cb(done,total):
                await progress_message(status,done,total,state)

            actual = await download_file(item["url"], str(out), advertised, cb)
            if not advertised and not await reserve_bytes_quota(uid, actual):
                await status.edit_text("🚫 Daily data quota reached.")
                return

            await status.edit_text(f"✅ Download complete.\n📄 {item['filename']}\n📦 {actual/1024/1024:.2f} MB\n\n📤 Uploading...")
            await update.message.chat.send_action(action=ChatAction.UPLOAD_DOCUMENT)
            with open(out, "rb") as f:
                await update.message.reply_document(document=f, filename=item["filename"], caption="✅ Download complete")
            await status.edit_text("✅ Done.")
    except Exception as e:
        logger.exception("request failed")
        try: await status.edit_text(f"❌ Download failed.\n\nReason: {str(e)[:400]}")
        except Exception: pass
    finally:
        temp_dir.cleanup()

async def error_handler(update, context):
    logger.exception("Unhandled bot exception", exc_info=context.error)

def main():
    validate_config()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
