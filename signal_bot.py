"""
ربات سیگنال SMC با اشتراک Telegram Stars  (TP1..TP5)

نصب:   pip install "python-telegram-bot[job-queue]>=21.7" httpx pandas numpy
اجرا:  BOT_TOKEN=xxx ADMIN_ID=123456 python signal_bot.py
فایل smc.py باید کنار این فایل باشد.
"""
import os, time, json, sqlite3, asyncio, logging
import httpx
import pandas as pd
from telegram import Update, LabeledPrice
from telegram.error import Forbidden, BadRequest
from telegram.ext import (Application, CommandHandler, PreCheckoutQueryHandler,
                          MessageHandler, ContextTypes, filters)
import smc

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
PRICE_STARS = int(os.getenv("PRICE_STARS", "500"))
SUB_DAYS = int(os.getenv("SUB_DAYS", "30"))
TIMEFRAME = os.getenv("TIMEFRAME", "15m")
MIN_SCORE = int(os.getenv("MIN_SCORE", "2"))   # 1=فقط OB ... 4=همه تاییدیه‌ها
SCAN_EVERY = 300
DB_PATH = os.getenv("DB_PATH", "bot.db")
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
           "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "SUIUSDT"]
DISCLAIMER = ("⚠️ توصیه مالی نیست. فیوچرز با اهرم ریسک بالایی دارد و ممکن است کل سرمایه "
              "از بین برود. ریسک هر معامله را حداکثر ۱ تا ۲٪ سرمایه نگه دارید.")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS subs(user_id INTEGER PRIMARY KEY, expires INTEGER);
CREATE TABLE IF NOT EXISTS payments(charge_id TEXT PRIMARY KEY, user_id INTEGER, stars INTEGER, ts INTEGER);
CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, candle INTEGER,
  side TEXT, entry REAL, sl REAL, tps TEXT, hits INTEGER DEFAULT 0, status TEXT DEFAULT 'open',
  UNIQUE(symbol, candle, side));
""")
db.commit()


def is_active(uid):
    r = db.execute("SELECT expires FROM subs WHERE user_id=?", (uid,)).fetchone()
    return bool(r and r[0] > time.time())


def extend(uid, days):
    r = db.execute("SELECT expires FROM subs WHERE user_id=?", (uid,)).fetchone()
    base = max(time.time(), r[0]) if r else time.time()
    db.execute("INSERT OR REPLACE INTO subs VALUES(?,?)", (uid, int(base + days * 86400)))
    db.commit()


def active_users():
    return [r[0] for r in db.execute("SELECT user_id FROM subs WHERE expires>?", (time.time(),))]


async def fetch(client, symbol):
    r = await client.get("https://fapi.binance.com/fapi/v1/klines",
                         params={"symbol": symbol, "interval": TIMEFRAME, "limit": 500})
    r.raise_for_status()
    df = pd.DataFrame(r.json(), columns=["t", "o", "h", "l", "c", "v", "ct", "q", "n", "tb", "tq", "i"])
    for col in ["o", "h", "l", "c"]:
        df[col] = df[col].astype(float)
    return df.iloc[:-1].reset_index(drop=True)   # فقط کندل بسته‌شده


def fmt(p):  # قیمت با دقت مناسب
    return f"{p:.2f}" if p >= 100 else f"{p:.4f}" if p >= 1 else f"{p:.6f}"


def signal_text(sym, s):
    emoji = "🟢" if s["side"] == "LONG" else "🔴"
    tags = [s["kind"]] + (["FVG"] if s["fvg"] else []) + (["Liquidity Sweep"] if s["swept"] else [])
    tps = "\n".join(f"🎯 TP{i+1}: {fmt(x)}" for i, x in enumerate(s["tps"]))
    return (f"{emoji} {s['side']} | {sym} | {TIMEFRAME}\n"
            f"🧠 SMC: {' + '.join(tags)} + Order Block\n"
            f"⭐ قدرت سیگنال: {s['score']}/4\n\n"
            f"📍 ورود: {fmt(s['entry'])}\n🛑 حد ضرر: {fmt(s['sl'])}\n{tps}\n\n"
            f"💡 در هر TP حدود ۲۰٪ حجم ببندید.\n{DISCLAIMER}")


async def broadcast(context, text):
    ok = 0
    for uid in active_users():
        try:
            await context.bot.send_message(uid, text); ok += 1
        except (Forbidden, BadRequest):
            pass
        await asyncio.sleep(0.05)
    return ok


async def track_open(context, client):
    """پیگیری سیگنال‌های باز و اطلاع‌رسانی TP/SL"""
    for sid, sym, candle, side, sl, tps, hits in db.execute(
            "SELECT id,symbol,candle,side,sl,tps,hits FROM signals WHERE status='open'").fetchall():
        try:
            df = await fetch(client, sym)
        except Exception:
            continue
        tps, long_ = json.loads(tps), side == "LONG"
        for _, row in df[df["t"] > candle].iterrows():
            if (long_ and row["l"] <= sl) or (not long_ and row["h"] >= sl):
                db.execute("UPDATE signals SET status='sl' WHERE id=?", (sid,)); db.commit()
                await broadcast(context, f"🛑 {sym} {side}: حد ضرر فعال شد (بعد از {hits} TP).")
                break
            new = hits
            while new < 5 and ((long_ and row["h"] >= tps[new]) or (not long_ and row["l"] <= tps[new])):
                new += 1
            if new > hits:
                hits = new
                done = hits == 5
                db.execute("UPDATE signals SET hits=?, status=? WHERE id=?", (hits, "done" if done else "open", sid)); db.commit()
                await broadcast(context, f"✅ {sym} {side}: TP{hits} لمس شد!" + (" 🏆 همه‌ی اهداف کامل شد" if done else ""))
                if done:
                    break


async def scan(context: ContextTypes.DEFAULT_TYPE):
    async with httpx.AsyncClient(timeout=15) as client:
        for sym in SYMBOLS:
            try:
                df = await fetch(client, sym)
            except Exception as e:
                logging.warning("scan %s failed: %s", sym, e); continue
            last = len(df) - 1
            for s in smc.analyze(df):
                if s["idx"] != last or s["score"] < MIN_SCORE:
                    continue
                cur = db.execute("INSERT OR IGNORE INTO signals(symbol,candle,side,entry,sl,tps) VALUES(?,?,?,?,?,?)",
                                 (sym, s["candle"], s["side"], s["entry"], s["sl"], json.dumps(s["tps"])))
                db.commit()
                if cur.rowcount:      # جلوگیری از ارسال تکراری
                    await broadcast(context, signal_text(sym, s))
        await track_open(context, client)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام 👋\nسیگنال‌های فیوچرز بر پایه‌ی Smart Money Concepts (BOS/CHoCH، Order Block، FVG، Liquidity Sweep) "
        f"با ۵ هدف (TP1–TP5).\n\n💳 اشتراک {SUB_DAYS} روزه: {PRICE_STARS} ⭐\n"
        "خرید: /buy\nوضعیت: /status\nپشتیبانی پرداخت: /paysupport\n\n" + DISCLAIMER)


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_invoice(
        title=f"اشتراک سیگنال {SUB_DAYS} روزه", description="سیگنال‌های SMC فیوچرز با TP1 تا TP5",
        payload=f"sub_{SUB_DAYS}d", currency="XTR", prices=[LabeledPrice("اشتراک", PRICE_STARS)], provider_token="")


async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    await q.answer(ok=q.invoice_payload.startswith("sub_"), error_message="پرداخت نامعتبر")


async def paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p, uid = update.message.successful_payment, update.effective_user.id
    db.execute("INSERT OR IGNORE INTO payments VALUES(?,?,?,?)",
               (p.telegram_payment_charge_id, uid, p.total_amount, int(time.time())))
    extend(uid, SUB_DAYS)
    await update.message.reply_text(f"✅ پرداخت موفق. اشتراک شما {SUB_DAYS} روز فعال شد.")
    await context.bot.send_message(ADMIN_ID, f"💰 {p.total_amount}⭐ از کاربر {uid} | charge: {p.telegram_payment_charge_id}")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = db.execute("SELECT expires FROM subs WHERE user_id=?", (update.effective_user.id,)).fetchone()
    if r and r[0] > time.time():
        await update.message.reply_text(f"✅ اشتراک فعال — {(r[0]-time.time())/86400:.1f} روز باقی مانده")
    else:
        await update.message.reply_text("❌ اشتراک فعال ندارید. /buy")


async def paysupport(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("مشکل پرداخت یا بازگشت وجه: @YOUR_SUPPORT_USERNAME")


def admin_only(fn):
    async def w(update, context):
        if update.effective_user.id == ADMIN_ID:
            await fn(update, context)
    return w


@admin_only
async def signal_cmd(update, context):
    text = " ".join(context.args)
    if not text:
        return await update.message.reply_text("استفاده: /signal متن")
    await update.message.reply_text(f"ارسال شد برای {await broadcast(context, text + chr(10)*2 + DISCLAIMER)} نفر")


@admin_only
async def grant(update, context):
    extend(int(context.args[0]), int(context.args[1])); await update.message.reply_text("انجام شد")


@admin_only
async def stats(update, context):
    s, n = db.execute("SELECT COALESCE(SUM(stars),0), COUNT(*) FROM payments").fetchone()
    sg = db.execute("SELECT COUNT(*), SUM(status='sl'), SUM(hits>=1), SUM(hits=5) FROM signals").fetchone()
    await update.message.reply_text(
        f"مشترکان فعال: {len(active_users())}\nپرداخت‌ها: {n} | مجموع {s}⭐\n"
        f"سیگنال‌ها: {sg[0]} | SL: {sg[1] or 0} | حداقل TP1: {sg[2] or 0} | کامل TP5: {sg[3] or 0}")


@admin_only
async def refund(update, context):
    uid = int(context.args[0])
    await context.bot.refund_star_payment(uid, context.args[1])
    db.execute("DELETE FROM subs WHERE user_id=?", (uid,)); db.commit()
    await update.message.reply_text("بازگشت وجه انجام شد")


def main():
    app = Application.builder().token(TOKEN).build()
    for name, fn in [("start", start), ("buy", buy), ("status", status), ("paysupport", paysupport),
                     ("signal", signal_cmd), ("grant", grant), ("stats", stats), ("refund", refund)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, paid))
    app.job_queue.run_repeating(scan, interval=SCAN_EVERY, first=10)
    app.run_polling()


if __name__ == "__main__":
    main()
