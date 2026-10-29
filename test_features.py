"""تست قابلیت‌های جدید: صرافی‌ها (Binance/Bybit)، BE بعد از TP1، تنظیمات شخصی، کد هدیه، پیکربندی Railway"""
import json, os, time, unittest
import pandas as pd
import testkit  # noqa: F401
import httpx
from testkit import B, make_ctx, make_update, reply_text
import backtest as bt
import exchange as ex
import smc
from config import ConfigError, load_config
from database import Database, DAY

TF = 900_000


class ExchangeTests(unittest.TestCase):
    def test_binance_request_shape(self):
        url, p = ex.build_request("binance", "https://fapi.binance.com", "BTCUSDT", "15m", 100, 200, 9999)
        self.assertTrue(url.endswith("/fapi/v1/klines")); self.assertEqual((p["startTime"], p["endTime"], p["limit"]), (100, 200, 1500))

    def test_bybit_request_shape(self):
        url, p = ex.build_request("bybit", "https://api.bybit.com", "ETHUSDT", "1h", 100, 200, 5000)
        self.assertTrue(url.endswith("/v5/market/kline"))
        self.assertEqual((p["category"], p["interval"], p["start"], p["end"], p["limit"]), ("linear", "60", 100, 200, 1000))
        with self.assertRaises(ValueError): ex.build_request("kraken", "x", "BTCUSDT", "15m")

    def test_bybit_normalize_reverses_and_adds_close_time(self):
        payload = {"retCode": 0, "result": {"list": [[str(2 * TF), "3", "4", "2", "3.5", "9", "0"], [str(TF), "2", "3", "1", "2.5", "8", "0"]]}}
        rows = ex.normalize("bybit", payload, "15m")
        self.assertEqual([r[0] for r in rows], [TF, 2 * TF]); self.assertEqual(rows[0][6], TF + TF - 1)

    def test_bybit_unclosed_candle_dropped(self):
        payload = {"retCode": 0, "result": {"list": [[str(2 * TF), "3", "4", "2", "3.5", "9", "0"], [str(TF), "2", "3", "1", "2.5", "8", "0"]]}}
        df = smc.klines_to_df(ex.normalize("bybit", payload, "15m"), now_ms=2 * TF + 5000)    # کندل دوم هنوز باز است
        self.assertEqual(list(df["t"]), [TF])

    def test_bad_payloads_raise(self):
        for payload in ({"retCode": 10001, "result": {}}, {"retCode": 0, "result": {"list": "x"}}, [], None,
                        {"retCode": 0, "result": {"list": [["a"]]}}):
            with self.assertRaises(ValueError): ex.normalize("bybit", payload, "15m")
        with self.assertRaises(ValueError): ex.normalize("binance", {"code": -1}, "15m")


class BybitEndToEndTests(unittest.IsolatedAsyncioTestCase):
    def bybit_client(self, df):
        class C:
            async def get(c, url, params=None, timeout=None):
                assert "/v5/market/kline" in url and params["category"] == "linear"
                lo, hi = params.get("start", 0), params.get("end", 10 ** 18)
                rows = [[str(int(t)), str(o), str(h), str(l), str(cl), "1", "0"] for t, o, h, l, cl in df[["t", "o", "h", "l", "c"]].itertuples(index=False) if lo <= t <= hi]
                rows = rows[::-1][: params["limit"]]
                class R:
                    def raise_for_status(s): pass
                    def json(s): return {"retCode": 0, "result": {"list": rows}}
                return R()
        return C()

    async def test_live_fetch_and_history_via_bybit(self):
        df = smc.synthetic_candles(2500, 3); client = self.bybit_client(df)
        ctx, cfg, db, bot = make_ctx({"EXCHANGE": "bybit"}); self.assertEqual(cfg.exchange_url, "https://api.bybit.com")
        out = await B.fetch_klines(client, "BTCUSDT", cfg)            # آخرین ۵۰۰ کندل بسته‌شده (همه‌ی داده‌ی تست قدیمی است)
        self.assertEqual(len(out), 500); self.assertTrue((out["t"].diff().dropna() == TF).all())
        hist = await bt.fetch_history(client, "BTCUSDT", "15m", 40, now_ms=int(df["t"].iloc[-1]) + TF + 1, exchange="bybit")
        self.assertEqual(len(hist), 2500)


class BreakevenTests(unittest.TestCase):
    base = dict(side="LONG", sl=99.0, tps=[101, 102, 103, 104, 105])

    def test_returns_to_entry_after_tp1(self):
        # کندل۱: TP1 | کندل۲: برگشت به ۱۰۰ => با BE بدون ضرر، بدون BE حد ضرر نمی‌خورد (قیمت ۱۰۰ > ۹۹)
        h, ev, out, used = smc.advance("LONG", 99, self.base["tps"], 0, [101.5, 100.5], [100, 99.9], entry=100, be_after_tp1=True)
        self.assertEqual((h, out, used), (1, "be", 2)); self.assertEqual(ev[-1], ("be", None))
        self.assertAlmostEqual(smc.realized_r(1, "be", 100, 99, "LONG"), 0.2)
        h, ev, out, used = smc.advance("LONG", 99, self.base["tps"], 0, [101.5, 100.5], [100, 99.9], entry=100, be_after_tp1=False)
        self.assertIsNone(out)

    def test_be_not_applied_before_tp1_or_in_same_candle(self):
        _, _, out, _ = smc.advance("LONG", 99, self.base["tps"], 0, [100.5], [99.5], entry=100, be_after_tp1=True)
        self.assertIsNone(out)                                         # قبل از TP1 همان SL اصلی است
        _, ev, out, _ = smc.advance("LONG", 99, self.base["tps"], 0, [101.5], [99.9], entry=100, be_after_tp1=True)
        self.assertEqual(ev, [("tp", 1)]); self.assertIsNone(out)      # در همان کندلِ TP1، BE فرض نمی‌شود

    def test_short_side_and_simulate(self):
        sig = dict(idx=0, side="SHORT", entry=100.0, sl=101.0, tps=[99, 98, 97, 96, 95])
        df = pd.DataFrame({"t": range(3), "o": [100] * 3, "h": [100, 100, 100.5], "l": [100, 98.9, 99.6], "c": [100, 99, 100]})
        r = smc.simulate(sig, df, be_after_tp1=True)
        self.assertEqual((r["hits"], r["outcome"]), (1, "be")); self.assertAlmostEqual(r["r"], 0.2)
        r2 = smc.simulate(sig, df, be_after_tp1=False); self.assertEqual(r2["outcome"], "expired")

    def test_backtest_flag_changes_results_consistently(self):
        df = smc.synthetic_candles(6000, 9)
        a, b = bt.run_backtest(df, 1, 0.0), bt.run_backtest(df, 1, 0.0, be=True)
        self.assertGreater(len(a), 10); self.assertTrue(any(x["outcome"] == "be" for x in b))
        self.assertFalse(any(x["outcome"] == "be" for x in a))


class BreakevenBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_tracker_announces_breakeven(self):
        from test_bot import find_signal, cut_at, now_for
        ctx, cfg, db, bot = make_ctx({"BE_AFTER_TP1": "1"}); db.grant(10, 5)
        df, sig = find_signal(); cut = cut_at(df, sig)
        await B.process_new_signals(ctx, {"BTCUSDT": cut}, now_ms=now_for(cut))
        self.assertIn("🔒", bot.texts(10)[0])                          # یادآوری BE در خود سیگنال
        e, tp1, long_ = sig["entry"], sig["tps"][0], sig["side"] == "LONG"
        t0 = int(cut["t"].iloc[-1])
        rows = [(t0 + TF, e, max(e, tp1) if long_ else e, e if long_ else min(e, tp1), e),
                (t0 + 2 * TF, e, e + (sig["tps"][0] - e) * 0.01 if long_ else e, e if long_ else e - (e - sig["tps"][0]) * 0.01, e)]
        # کندل دوم: دقیقاً به ورود برمی‌گردد (low <= entry برای LONG / high >= entry برای SHORT)
        rows[1] = (t0 + 2 * TF, e, e if long_ else e, e if long_ else e, e)
        d = pd.concat([cut, pd.DataFrame(rows, columns=["t", "o", "h", "l", "c"])], ignore_index=True)
        bot.sent.clear(); await B.track_open(ctx, {"BTCUSDT": d})
        row = db.conn.execute("SELECT status,hits,r FROM signals").fetchone()
        self.assertEqual((row["status"], row["hits"]), ("be", 1)); self.assertAlmostEqual(row["r"], 0.2)
        self.assertIn("بدون ضرر", " ".join(bot.texts(10)))
        self.assertEqual(db.performance()["n"], 1)


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    def test_db_validation_and_defaults(self):
        db = Database(); self.assertEqual(db.get_settings(1), {"min_score": None, "side": "ALL", "progress": True})
        self.assertTrue(db.set_setting(1, "min_score", 3)); self.assertTrue(db.set_setting(1, "side", "LONG")); self.assertTrue(db.set_setting(1, "progress", False))
        self.assertEqual(db.get_settings(1), {"min_score": 3, "side": "LONG", "progress": False})
        for bad in (("min_score", 9), ("min_score", "x"), ("side", "BOTH"), ("hack", 1)): self.assertFalse(db.set_setting(1, *bad))
        self.assertEqual(db.get_settings(1)["min_score"], 3)

    def test_recipient_filtering(self):
        db = Database()
        for uid in (1, 2, 3, 4, 5): db.grant(uid, 5)
        db.set_setting(2, "min_score", 3); db.set_setting(3, "side", "SHORT"); db.set_setting(4, "progress", False); db.set_blocked(5)
        self.assertEqual(sorted(db.recipients_for_signal(2, "LONG")), [1, 4])        # 2: امتیاز کم، 3: فقط SHORT، 5: بلاک
        self.assertEqual(sorted(db.recipients_for_signal(4, "SHORT")), [1, 2, 3, 4])
        self.assertEqual(sorted(db.recipients_for_signal(4, "SHORT", progress=True)), [1, 2, 3])   # 4 اعلان خاموش
        self.assertEqual(db.recipients_for_signal(4, "SHORT", now=time.time() + 30 * DAY), [])   # منقضی

    async def test_buttons_update_settings_and_reject_garbage(self):
        ctx, cfg, db, bot = make_ctx(); u = make_update(10); await B.settings_cmd(u, ctx)
        self.assertIn("تنظیمات", reply_text(u))
        for data in ("set:ms:3", "set:side:SHORT", "set:prog:0"):
            q = make_update(10, data=data); await B.settings_button(q, ctx); self.assertTrue(q.callback_query.edit_message_text.called)
        self.assertEqual(db.get_settings(10), {"min_score": 3, "side": "SHORT", "progress": False})
        for data in ("set:ms:9", "set:side:XX", "set:prog:7", "set:ms", "evil:ms:1", "set:ms:abc"):
            q = make_update(10, data=data); await B.settings_button(q, ctx); self.assertFalse(q.callback_query.edit_message_text.called, data)
        self.assertEqual(db.get_settings(10)["min_score"], 3)

    async def test_signal_respects_personal_filters(self):
        from test_bot import find_signal, cut_at, now_for
        ctx, cfg, db, bot = make_ctx()
        df, sig = find_signal(); cut = cut_at(df, sig); other = "SHORT" if sig["side"] == "LONG" else "LONG"
        for uid in (10, 20, 30): db.grant(uid, 5)
        db.set_setting(20, "side", other); db.set_setting(30, "min_score", 4 if sig["score"] < 4 else 1)
        await B.process_new_signals(ctx, {"BTCUSDT": cut}, now_ms=now_for(cut))
        self.assertEqual(len(bot.texts(10)), 1); self.assertEqual(bot.texts(20), [])
        self.assertEqual(len(bot.texts(30)), 0 if sig["score"] < 4 else 1)


class PromoTests(unittest.IsolatedAsyncioTestCase):
    def test_redeem_rules(self):
        db = Database(); now = 1_800_000_000
        self.assertTrue(db.create_promo("welcome7", 7, 2, now + 10 * DAY, now)); self.assertFalse(db.create_promo("WELCOME7", 7, 2, None, now))
        r = db.redeem_promo("WeLcOmE7", 1, now); self.assertEqual((r["status"], r["expires"]), ("ok", now + 7 * DAY))
        self.assertEqual(db.redeem_promo("WELCOME7", 1, now)["status"], "already")
        self.assertEqual(db.redeem_promo("WELCOME7", 2, now)["status"], "ok")
        self.assertEqual(db.redeem_promo("WELCOME7", 3, now)["status"], "exhausted"); self.assertFalse(db.is_active(3, now))
        self.assertEqual(db.redeem_promo("NOPE", 3, now)["status"], "not_found")
        db.create_promo("OLD", 5, 5, now - 1, now - DAY); self.assertEqual(db.redeem_promo("OLD", 4, now)["status"], "expired")

    def test_stacks_on_active_subscription(self):
        db = Database(); now = 1_800_000_000; db.grant(1, 10, now); db.create_promo("PLUS5", 5, 9, None, now)
        self.assertEqual(db.redeem_promo("PLUS5", 1, now + DAY)["expires"], now + 15 * DAY)

    async def test_commands(self):
        ctx, cfg, db, bot = make_ctx(args=["WELCOME7", "7", "1", "30"])
        u = make_update(555); await B.newpromo(u, ctx); self.assertFalse(u.message.reply_text.called)       # فقط ادمین
        u = make_update(1); await B.newpromo(u, ctx); self.assertIn("✅", reply_text(u))
        u = make_update(1); await B.newpromo(u, ctx); self.assertIn("قبلاً", reply_text(u))
        for args in ([], ["x", "7", "1"], ["OK1", "0", "1"], ["OK2", "7", "0"], ["bad code", "7", "1"], ["OK3", "7", "1", "0"]):
            ctx.args = args; u = make_update(1); await B.newpromo(u, ctx); self.assertIn("استفاده", reply_text(u))
        ctx.args = ["welcome7"]; u = make_update(10); await B.promo(u, ctx)
        self.assertIn("🎁", reply_text(u)); self.assertTrue(db.is_active(10))
        u = make_update(11); await B.promo(u, ctx); self.assertIn("تمام", reply_text(u)); self.assertFalse(db.is_active(11))
        ctx.args = ["x!"]; u = make_update(12); await B.promo(u, ctx); self.assertIn("استفاده", reply_text(u))
        ctx.args = []; u = make_update(1); await B.promos(u, ctx); self.assertIn("WELCOME7", reply_text(u)); self.assertIn("1/1", reply_text(u))


class RailwayConfigTests(unittest.TestCase):
    ENV = {"BOT_TOKEN": "t", "ADMIN_IDS": "1"}

    def test_db_path_priority(self):
        self.assertEqual(load_config(self.ENV).db_path, "bot.db")
        self.assertEqual(load_config({**self.ENV, "RAILWAY_VOLUME_MOUNT_PATH": "/data/"}).db_path, "/data/bot.db")
        self.assertEqual(load_config({**self.ENV, "RAILWAY_VOLUME_MOUNT_PATH": "/data", "DB_PATH": "/x/y.db"}).db_path, "/x/y.db")

    def test_exchange_and_be_options(self):
        c = load_config({**self.ENV, "EXCHANGE": "Bybit", "BE_AFTER_TP1": "true"})
        self.assertEqual((c.exchange, c.exchange_url, c.be_after_tp1), ("bybit", "https://api.bybit.com", True))
        self.assertEqual(load_config({**self.ENV, "EXCHANGE": "bybit", "EXCHANGE_URL": "https://proxy.example/"}).exchange_url, "https://proxy.example")
        self.assertFalse(load_config(self.ENV).be_after_tp1)
        with self.assertRaises(ConfigError): load_config({**self.ENV, "EXCHANGE": "ftx"})

    def test_railway_json_is_valid_and_uses_start_command(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "railway.json")) as f:
            cfg = json.load(f)
        self.assertEqual(cfg["deploy"]["startCommand"], "python signal_bot.py")
        self.assertEqual(cfg["deploy"]["restartPolicyType"], "ON_FAILURE")
        self.assertNotIn("healthcheckPath", cfg["deploy"])             # ربات وب‌سرور ندارد؛ healthcheck باعث شکست دیپلوی می‌شود
        self.assertNotIn("BOT_TOKEN", json.dumps(cfg))                 # هیچ رازی در فایل نیست


if __name__ == "__main__":
    unittest.main()
