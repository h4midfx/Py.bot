"""تست منطق ربات با تلگرام و httpx شبیه‌سازی‌شده (چون این محیط اینترنت ندارد)"""
import sys, types, asyncio, os, json, time
from unittest.mock import MagicMock, AsyncMock

# --- stubهای ماژول‌های تلگرام ---
tg = types.ModuleType("telegram"); tg.Update = object
class LabeledPrice:
    def __init__(self, label, amount): self.label, self.amount = label, amount
tg.LabeledPrice = LabeledPrice
err = types.ModuleType("telegram.error"); err.Forbidden = type("Forbidden", (Exception,), {}); err.BadRequest = type("BadRequest", (Exception,), {})
ext = types.ModuleType("telegram.ext")
for n in ["Application", "CommandHandler", "PreCheckoutQueryHandler", "MessageHandler"]: setattr(ext, n, MagicMock())
ext.ContextTypes = MagicMock(); ext.filters = MagicMock()
sys.modules.update({"telegram": tg, "telegram.error": err, "telegram.ext": ext, "httpx": MagicMock()})
os.environ.update(BOT_TOKEN="x", ADMIN_ID="1", DB_PATH=":memory:")

import signal_bot as B
import pandas as pd
from test_smc import synth
import smc

sent = []  # (uid, text)
class Bot:
    async def send_message(self, uid, text):
        if uid == 999: raise err.Forbidden()   # کاربری که ربات را بلاک کرده
        sent.append((uid, text))
ctx = types.SimpleNamespace(bot=Bot(), args=[])
async def noop(*_): pass
B.asyncio.sleep = noop

def upd(uid):
    u = MagicMock(); u.effective_user.id = uid
    u.message.reply_invoice = AsyncMock(); u.message.reply_text = AsyncMock()
    return u

async def main():
    # 1) /buy
    u = upd(10); await B.buy(u, ctx)
    kw = u.message.reply_invoice.call_args.kwargs
    assert kw["currency"] == "XTR" and kw["provider_token"] == "" and kw["prices"][0].amount == B.PRICE_STARS, kw
    print("PASS buy: فاکتور XTR با provider_token خالی و مبلغ", kw["prices"][0].amount)

    # 2) pre_checkout
    u = upd(10); u.pre_checkout_query = MagicMock(invoice_payload="sub_30d"); u.pre_checkout_query.answer = AsyncMock()
    await B.precheckout(u, ctx); assert u.pre_checkout_query.answer.call_args.kwargs["ok"] is True
    u.pre_checkout_query.invoice_payload = "evil"; await B.precheckout(u, ctx)
    assert u.pre_checkout_query.answer.call_args.kwargs["ok"] is False
    print("PASS precheckout: payload معتبر تایید، نامعتبر رد")

    # 3) پرداخت موفق
    u = upd(10); u.message.successful_payment = MagicMock(telegram_payment_charge_id="ch1", total_amount=B.PRICE_STARS)
    assert not B.is_active(10)
    await B.paid(u, ctx); assert B.is_active(10)
    assert any(uid == 1 and "ch1" in t for uid, t in sent), "ادمین باید خبر پرداخت بگیرد"
    exp1 = B.db.execute("SELECT expires FROM subs WHERE user_id=10").fetchone()[0]
    await B.paid(u, ctx)  # پرداخت دوم => تمدید، charge تکراری دوبار ثبت نشود
    exp2 = B.db.execute("SELECT expires FROM subs WHERE user_id=10").fetchone()[0]
    assert exp2 - exp1 >= B.SUB_DAYS * 86400 - 1
    assert B.db.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
    print("PASS paid: اشتراک فعال، ادمین مطلع، تمدید جمع می‌شود، charge تکراری ثبت نمی‌شود")

    # 4) فقط مشترک فعال پیام می‌گیرد
    B.extend(20, 30); B.extend(999, 30)                       # 999 ربات را بلاک کرده
    B.db.execute("INSERT OR REPLACE INTO subs VALUES(30,?)", (int(time.time()) - 10,)); B.db.commit()  # منقضی
    sent.clear(); n = await B.broadcast(ctx, "hello")
    got = {uid for uid, _ in sent}
    assert got == {10, 20} and n == 2, got
    print("PASS broadcast: فقط مشترکان فعال؛ منقضی دریافت نکرد؛ کاربر بلاک‌کرده کرش نداد")

    # 5) اسکن: یک سیگنال واقعی SMC را در انتهای داده پیدا و ارسال کن
    df, sig, sym = None, None, "BTCUSDT"
    for seed in range(50):
        d = synth(3000, seed)
        ss = [s for s in smc.analyze(d) if s["score"] >= B.MIN_SCORE and s["idx"] > 500]
        if ss: df, sig = d, ss[0]; break
    assert sig, "سیگنالی پیدا نشد"
    cut = sig["idx"] + 1
    state = {"df": df.iloc[:cut].reset_index(drop=True)}
    async def fake_fetch(client, s): return state["df"]
    B.fetch = fake_fetch; B.SYMBOLS = [sym]
    B.httpx.AsyncClient = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(return_value=None), __aexit__=AsyncMock(return_value=False)))
    sent.clear(); await B.scan(types.SimpleNamespace(bot=Bot()))
    msgs = [t for uid, t in sent if uid == 10]
    assert len(msgs) == 1 and all(f"TP{i}" in msgs[0] for i in range(1, 6)), msgs
    assert not any(uid == 30 for uid, _ in sent)
    print("PASS scan: سیگنال", sig["side"], "ارسال شد با TP1..TP5 (قدرت", sig["score"], ")")
    print("----- نمونه پیام -----\n" + msgs[0] + "\n----------------------")
    sent.clear(); await B.scan(types.SimpleNamespace(bot=Bot()))
    assert not [1 for _, t in sent if "SMC" in t], "سیگنال تکراری نباید ارسال شود"
    print("PASS dedupe: اسکن دوباره سیگنال تکراری نفرستاد")

    # 6) ردیابی: قیمت به TPها می‌رسد
    long_ = sig["side"] == "LONG"; t0 = int(state["df"]["t"].iloc[-1]); px = sig["entry"]
    rows = []
    for k in range(1, 6):
        tp = sig["tps"][k - 1]; rows.append((t0 + k * 900000, px, tp * (1.0001 if long_ else 0.9999) if long_ else tp * 0.9999, px if long_ else tp * 0.9999, px))
    # ساخت کندل‌هایی که high/low به TPها برسد
    rows = []
    for k in range(1, 6):
        tp = sig["tps"][k - 1]
        rows.append((t0 + k * 900000, px, max(px, tp) if long_ else px, px if long_ else min(px, tp), px))
    state["df"] = pd.concat([state["df"], pd.DataFrame(rows, columns=["t", "o", "h", "l", "c"])], ignore_index=True)
    sent.clear(); await B.track_open(types.SimpleNamespace(bot=Bot()), None)
    txt = [t for uid, t in sent if uid == 10]
    assert any("TP5" in t and "کامل" in t for t in txt), txt
    print("PASS track: پیام‌های TP1..TP5 ارسال شد:", [t.split(":")[1].strip().split()[0] for t in txt])
    assert B.db.execute("SELECT status,hits FROM signals").fetchone() == ("done", 5)

    # 7) ادمین
    u = upd(1); await B.stats(u, ctx); print("PASS stats:", u.message.reply_text.call_args.args[0].replace(chr(10), " | "))
    u = upd(555); await B.stats(u, ctx); assert not u.message.reply_text.called
    print("PASS admin_only: کاربر عادی به /stats دسترسی ندارد")

asyncio.run(main())
