"""تست: حد ضرر متحرک، فیلتر کارمزد، نگهبان (watchdog)، محدودکننده‌ی کد هدیه، قیف فروش، پیکربندی Render"""
import os, re, threading, time, unittest
import pandas as pd
import testkit  # noqa: F401
from testkit import B, make_ctx, make_update, reply_text
import backtest as bt
import smc
from config import ConfigError, load_config
from database import Database

TF = 900_000
HERE = os.path.dirname(os.path.abspath(__file__))
ENV = {"BOT_TOKEN": "t", "ADMIN_IDS": "1"}


class StopModeTests(unittest.TestCase):
    tps = [101, 102, 103, 104, 105]

    def adv(self, hs, ls, mode, hits=0, side="LONG", sl=99, tps=None):
        return smc.advance(side, sl, tps or self.tps, hits, hs, ls, entry=100, stop_mode=mode)

    def test_trail_moves_stop_up_each_target(self):
        # TP1 -> (کندل بعد) برگشت به ورود => be | TP1,TP2 -> برگشت به TP1 => trail
        h, ev, out, _ = self.adv([101.2, 100.5], [100, 99.9], "trail"); self.assertEqual((h, out), (1, "be"))
        h, ev, out, _ = self.adv([101.2, 102.2, 101.5], [100, 100.5, 100.9], "trail"); self.assertEqual((h, out), (2, "trail"))
        self.assertEqual(ev, [("tp", 1), ("tp", 2), ("trail", None)])
        h, ev, out, _ = self.adv([103.1, 103.5, 102.5], [101.5, 102.6, 101.9], "trail", hits=2); self.assertEqual((h, out), (3, "trail"))   # TP3 سپس برگشت به TP2

    def test_be_mode_keeps_stop_at_entry_even_after_tp2(self):
        h, ev, out, _ = self.adv([101.2, 102.2, 101.5], [100, 100.5, 100.9], "be"); self.assertIsNone(out)      # BE: ورود، نه TP1
        h, ev, out, _ = self.adv([101.2, 102.2, 101.5], [100, 100.5, 99.9], "be"); self.assertEqual(out, "be")

    def test_fixed_mode_and_legacy_flag_compat(self):
        _, _, out, _ = self.adv([101.2, 102.2, 101.5], [100, 100.5, 98.9], "fixed"); self.assertEqual(out, "sl")
        _, _, out, _ = smc.advance("LONG", 99, self.tps, 0, [101.2, 100.5], [100, 99.9], entry=100, be_after_tp1=True); self.assertEqual(out, "be")
        with self.assertRaises(ValueError): self.adv([101], [100], "weird")

    def test_short_side_trail(self):
        tps = [99, 98, 97, 96, 95]
        h, ev, out, _ = smc.advance("SHORT", 101, tps, 0, [100, 99.6, 99.1], [98.8, 97.9, 98.5][:0] or [98.8, 98.0, 98.5], entry=100, stop_mode="trail")
        # کندل1: TP1 | کندل2: TP2 (low 98.0) | کندل3: high 99.1 >= TP1(99) => trail
        self.assertEqual((h, out), (2, "trail"))

    def test_same_candle_uses_stop_level_at_candle_start(self):
        # در کندلی که TP1 لمس می‌شود حتی اگر قیمت زیر ورود هم برود، BE از کندل بعد اثر می‌کند
        h, ev, out, _ = self.adv([101.2], [99.5], "trail"); self.assertEqual((h, out), (1, None))

    def test_realized_r_values(self):
        self.assertAlmostEqual(smc.realized_r(1, "be", 100, 99, "LONG"), 0.2)
        self.assertAlmostEqual(smc.realized_r(2, "trail", 100, 99, "LONG"), 0.2 * 3 + 0.6 * 1.0)     # باقی‌مانده در TP1 = 1R
        self.assertAlmostEqual(smc.realized_r(3, "trail", 100, 99, "LONG"), 0.2 * 6 + 0.4 * 2.0)
        self.assertGreater(smc.realized_r(2, "trail", 100, 99, "LONG"), smc.realized_r(2, "be", 100, 99, "LONG"))

    def test_simulate_and_backtest_with_trail(self):
        sig = dict(idx=0, side="LONG", entry=100.0, sl=99.0, tps=self.tps)
        df = pd.DataFrame({"t": range(4), "o": [100] * 4, "h": [100, 101.2, 102.2, 101.6], "l": [100, 100, 100.5, 100.9], "c": [100, 101, 102, 101]})
        r = smc.simulate(sig, df, stop_mode="trail"); self.assertEqual((r["hits"], r["outcome"]), (2, "trail")); self.assertAlmostEqual(r["r"], 1.2)
        d = smc.synthetic_candles(6000, 9)
        a, b = bt.run_backtest(d, 1, 0.0), bt.run_backtest(d, 1, 0.0, stop_mode="trail")
        self.assertTrue(any(x["outcome"] == "trail" for x in b)); self.assertFalse(any(x["outcome"] == "trail" for x in a))
        self.assertTrue(all(x["net"] >= x["gross"] - 1e-9 for x in b))

    def test_config_stop_mode(self):
        self.assertEqual(load_config(ENV).stop_mode, "fixed"); self.assertEqual(load_config({**ENV, "STOP_MODE": "Trail"}).stop_mode, "trail")
        c = load_config({**ENV, "BE_AFTER_TP1": "1"}); self.assertEqual((c.stop_mode, c.be_after_tp1), ("be", True))
        self.assertEqual(load_config({**ENV, "BE_AFTER_TP1": "1", "STOP_MODE": "fixed"}).stop_mode, "fixed")
        with self.assertRaises(ConfigError): load_config({**ENV, "STOP_MODE": "x"})


class StopModeBotTests(unittest.IsolatedAsyncioTestCase):
    async def test_tracker_trail_flow_and_status_counts_as_closed(self):
        from test_bot import find_signal, cut_at, now_for
        ctx, cfg, db, bot = make_ctx({"STOP_MODE": "trail"}); db.grant(10, 5)
        df, sig = find_signal(); cut = cut_at(df, sig)
        await B.process_new_signals(ctx, {"BTCUSDT": cut}, now_ms=now_for(cut)); self.assertIn("حد ضرر متحرک", bot.texts(10)[0])
        e, t, long_ = sig["entry"], sig["tps"], sig["side"] == "LONG"; t0 = int(cut["t"].iloc[-1])
        mid = (e + t[0]) / 2                                             # بین ورود و TP1 (بدون لمس حد ضرر ورود)
        rows = [(t0 + TF, e, t[0] if long_ else e, e if long_ else t[0], e),            # TP1
                (t0 + 2 * TF, e, t[1] if long_ else mid, mid if long_ else t[1], e),    # TP2 بدون لمس ورود
                (t0 + 3 * TF, e, t[0] if long_ else t[0], t[0] if long_ else t[0], e)]  # برگشت به TP1 => حد ضرر متحرک
        d = pd.concat([cut, pd.DataFrame(rows, columns=["t", "o", "h", "l", "c"])], ignore_index=True)
        bot.sent.clear(); await B.track_open(ctx, {"BTCUSDT": d})
        row = db.conn.execute("SELECT status,hits,r FROM signals").fetchone()
        self.assertEqual((row["status"], row["hits"]), ("trail", 2)); self.assertAlmostEqual(row["r"], 1.2)
        self.assertIn("حد ضرر متحرک", " ".join(bot.texts(10))); self.assertEqual(db.performance()["n"], 1)


class FeeFilterTests(unittest.IsolatedAsyncioTestCase):
    def test_fee_cost_math(self):
        self.assertAlmostEqual(smc.fee_cost_r(100, 99, 0.0004), 0.08); self.assertAlmostEqual(smc.fee_cost_r(100, 98, 0.001), 0.1)
        self.assertEqual(smc.fee_cost_r(100, 100), float("inf"))

    def test_backtest_filter_removes_only_expensive_trades(self):
        d = smc.synthetic_candles(6000, 9); allt = bt.run_backtest(d, 1); fl = bt.run_backtest(d, 1, max_fee_r=0.10)
        self.assertGreater(len(allt), len(fl)); self.assertTrue(all(x["cost"] <= 0.10 + 1e-9 for x in fl))
        self.assertTrue(any(x["cost"] > 0.10 for x in allt))

    async def test_live_filter_and_fee_line_in_text(self):
        from test_bot import find_signal, cut_at, now_for
        df, sig = find_signal(); cut = cut_at(df, sig); cost = smc.fee_cost_r(sig["entry"], sig["sl"], 0.0004)
        ctx, cfg, db, bot = make_ctx({"MAX_FEE_R": str(round(cost / 2, 6))}); db.grant(10, 5)
        self.assertEqual(await B.process_new_signals(ctx, {"X": cut}, now_ms=now_for(cut)), 0)            # گران است -> رد
        ctx, cfg, db, bot = make_ctx({"MAX_FEE_R": str(round(cost * 2, 6))}); db.grant(10, 5)
        self.assertEqual(await B.process_new_signals(ctx, {"X": cut}, now_ms=now_for(cut)), 1)
        self.assertIn("کارمزد رفت‌وبرگشت", bot.texts(10)[0])

    def test_config_validation(self):
        for bad in ({"FEE_RATE": "x"}, {"FEE_RATE": "0.5"}, {"MAX_FEE_R": "-1"}, {"MAX_FEE_R": "9"}, {"WATCHDOG_SECONDS": "30"}):
            with self.assertRaises(ConfigError): load_config({**ENV, **bad})
        c = load_config(ENV); self.assertEqual((c.fee_rate, c.max_fee_r, c.watchdog_seconds), (0.0004, 0.0, 600))


class WatchdogTests(unittest.TestCase):
    def test_exits_when_heartbeat_stale(self):
        called = threading.Event(); codes = []
        t, stop = B.start_watchdog(lambda: time.time() - 1000, 60, lambda c: (codes.append(c), called.set()), interval=0.01)
        self.assertTrue(called.wait(2)); self.assertEqual(codes, [1]); t.join(1); self.assertFalse(t.is_alive())

    def test_does_not_exit_when_fresh_and_can_be_stopped(self):
        called = []; t, stop = B.start_watchdog(lambda: time.time(), 60, called.append, interval=0.01)
        time.sleep(0.2); self.assertEqual(called, []); self.assertTrue(t.is_alive()); stop.set(); t.join(1); self.assertFalse(t.is_alive())

    def test_disabled(self):
        self.assertEqual(B.start_watchdog(lambda: 0, 0), (None, None))

    def test_exit_function_called_once(self):
        calls = []; t, stop = B.start_watchdog(lambda: 0, 1, calls.append, interval=0.01); t.join(2); self.assertEqual(len(calls), 1)

    def test_heartbeat_job_updates_timestamp(self):
        import asyncio
        ctx, *_ = make_ctx(); asyncio.run(B.heartbeat_job(ctx)); self.assertLess(time.time() - ctx.bot_data["heartbeat"], 2)


class PromoLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocks_guessing_but_allows_valid_code_before_limit(self):
        ctx, cfg, db, bot = make_ctx(); db.create_promo("GOOD123", 3, 10)
        for i in range(5):
            ctx.args = [f"WRONG{i}"]; u = make_update(10); await B.promo(u, ctx); self.assertIn("نامعتبر", reply_text(u))
        ctx.args = ["GOOD123"]; u = make_update(10); await B.promo(u, ctx)
        self.assertIn("تعداد تلاش", reply_text(u)); self.assertFalse(db.is_active(10))                  # حتی کد درست هم موقتاً بسته است
        u = make_update(11); await B.promo(u, ctx); self.assertIn("🎁", reply_text(u))                   # کاربر دیگر مشکلی ندارد
        ctx.bot_data["promo_fail"][10] = [time.time() - 4000] * 5; u = make_update(10); await B.promo(u, ctx)   # بعد از یک ساعت آزاد می‌شود
        self.assertIn("🎁", reply_text(u))

    async def test_valid_use_does_not_count_as_failure(self):
        ctx, cfg, db, bot = make_ctx(); db.create_promo("OK1", 1, 99)
        ctx.args = ["OK1"]
        for uid in range(20, 30): await B.promo(make_update(uid), ctx)
        self.assertEqual(sum(len(v) for v in ctx.bot_data["promo_fail"].values()), 0)


class FunnelTests(unittest.IsolatedAsyncioTestCase):
    async def test_stats_funnel(self):
        ctx, cfg, db, bot = make_ctx(); now = 1_800_000_000
        for uid in (1, 2, 3, 4): db.upsert_user(uid, "u", now)
        db.start_trial(2, 1, now); db.start_trial(3, 1, now); db.record_payment("a", 3, 500, "month", 30, now=now)   # یک تریال تبدیل شد
        db.record_payment("b", 4, 500, "month", 30, now=now); db.upsert_user(5, "r", now); db.set_referrer(5, 4)
        s = db.stats(now); self.assertEqual((s["trials"], s["trial_paid"], s["payers"], s["referred"]), (2, 1, 2, 1))
        u = make_update(1); await B.stats(u, ctx); t = reply_text(u)
        self.assertIn("قیف فروش", t); self.assertIn("1/2", t); self.assertIn("50%", t)

    async def test_stats_with_no_data_has_no_zero_division(self):
        ctx, *_ = make_ctx(); u = make_update(1); await B.stats(u, ctx); self.assertIn("-", reply_text(u))

    async def test_refunded_payment_not_counted_in_funnel(self):
        db = Database(); now = 1_800_000_000; db.start_trial(1, 1, now); db.record_payment("r", 1, 500, "month", 30, now=now); db.mark_refunded("r", now)
        self.assertEqual(db.stats(now)["trial_paid"], 0)


class RenderConfigTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(HERE, "render.yaml"), encoding="utf-8") as f: self.y = f.read()

    def test_is_background_worker_with_paid_plan_and_disk(self):
        self.assertRegex(self.y, r"type:\s*worker"); self.assertRegex(self.y, r"runtime:\s*python")
        self.assertNotRegex(self.y, r"plan:\s*free")                                  # worker پلن رایگان ندارد و دیسک هم ندارد
        self.assertRegex(self.y, r"startCommand:\s*python signal_bot\.py"); self.assertIn("pip install -r requirements.txt", self.y)

    def test_region_is_not_us(self):
        self.assertNotRegex(self.y, r"region:\s*(oregon|ohio|virginia)")              # بایننس IP آمریکا را مسدود می‌کند
        self.assertRegex(self.y, r"region:\s*(frankfurt|singapore)")

    def test_db_path_is_on_the_mounted_disk(self):
        mount = re.search(r"mountPath:\s*(\S+)", self.y).group(1); db_path = re.search(r"key: DB_PATH\s+value:\s*(\S+)", self.y).group(1)
        self.assertEqual(os.path.dirname(db_path), mount)
        self.assertEqual(load_config({**ENV, "DB_PATH": db_path}).db_path, db_path)

    def test_secrets_are_not_in_file(self):
        for key in ("BOT_TOKEN", "ADMIN_IDS"):
            self.assertRegex(self.y, rf"key: {key}\s+sync: false")
        self.assertNotRegex(self.y, r"\d{6,}:[A-Za-z0-9_-]{20,}")                     # الگوی توکن تلگرام

    def test_all_env_keys_are_valid_for_config(self):
        env = dict(ENV)
        for k, v in re.findall(r"key: (\w+)\s+value:\s*\"?([^\"\n]+)\"?", self.y): env[k] = v.strip()
        cfg = load_config(env)
        self.assertEqual((cfg.exchange, cfg.timeframe, cfg.min_score), ("binance", "15m", 2)); self.assertEqual(cfg.plans[0].id, "test")

    def test_python_version_files_agree(self):
        with open(os.path.join(HERE, ".python-version")) as f: pv = f.read().strip()
        self.assertRegex(pv, r"^3\.\d+\.\d+$"); self.assertIn(f"value: {pv}", self.y)


class RestoreTests(unittest.IsolatedAsyncioTestCase):
    def make_backup(self, fill):
        import tempfile
        src = Database(os.path.join(tempfile.mkdtemp(), "src.db")); fill(src); path = os.path.join(tempfile.mkdtemp(), "b.db"); src.backup_to(path); return path

    def test_restore_replaces_content_and_keeps_working(self):
        path = self.make_backup(lambda d: (d.record_payment("old1", 7, 500, "month", 30, now=1_800_000_000), d.upsert_user(8, "x", 1)))
        target = Database(); target.record_payment("other", 99, 100, "week", 7); info = target.restore_from(path)
        self.assertEqual((info["payments"], info["users"]), (1, 2))
        self.assertIsNotNone(target.get_payment("old1")); self.assertIsNone(target.get_payment("other"))      # جایگزینی کامل
        self.assertEqual(target.record_payment("old1", 7, 500, "month", 30)["status"], "duplicate")           # پرداخت قدیمی دوباره تمدید نمی‌شود
        self.assertEqual(target.record_payment("new1", 7, 500, "month", 30)["status"], "ok")                  # دیتابیس بعد از ریستور سالم کار می‌کند

    def test_old_v1_backup_is_migrated_on_restore(self):
        import sqlite3, tempfile
        p = os.path.join(tempfile.mkdtemp(), "v1.db"); c = sqlite3.connect(p)
        c.executescript("CREATE TABLE subs(user_id INTEGER PRIMARY KEY, expires INTEGER); CREATE TABLE payments(charge_id TEXT PRIMARY KEY, user_id INTEGER, stars INTEGER, ts INTEGER);"
                        "CREATE TABLE signals(id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, candle INTEGER, side TEXT, entry REAL, sl REAL, tps TEXT, hits INTEGER DEFAULT 0, status TEXT DEFAULT 'open', UNIQUE(symbol,candle,side));"
                        "INSERT INTO subs VALUES(5, 4102444800); INSERT INTO payments VALUES('c',5,500,1700000000);"); c.commit(); c.close()
        db = Database(); db.restore_from(p); self.assertTrue(db.is_active(5)); self.assertEqual(db.get_payment("c")["refunded"], 0)
        self.assertIsNotNone(db.stats()["users"]); db.set_setting(5, "side", "LONG")                            # ستون‌های جدید موجودند

    def test_invalid_files_rejected_and_target_untouched(self):
        import tempfile, sqlite3
        d = tempfile.mkdtemp(); db = Database(); db.record_payment("keep", 1, 500, "month", 30)
        junk = os.path.join(d, "j.db")
        with open(junk, "wb") as fh: fh.write(b"not a database at all" * 50)
        wrong = os.path.join(d, "w.db"); c = sqlite3.connect(wrong); c.execute("CREATE TABLE foo(x)"); c.commit(); c.close()
        for p in (junk, wrong, os.path.join(d, "missing.db")):
            with self.assertRaises(ValueError): db.restore_from(p)
        self.assertIsNotNone(db.get_payment("keep"))

    async def send_restore(self, ctx, uid, caption, blob=b"", size=100):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        ctx.bot.files = {"fid": blob}
        u = make_update(uid); u.message.document = SimpleNamespace(file_id="fid", file_size=size); u.message.caption = caption
        await B.restore_handler(u, ctx); return u

    async def test_handler_requires_admin_and_confirm(self):
        ctx, cfg, db, bot = make_ctx(); path = self.make_backup(lambda d: d.record_payment("r1", 7, 500, "month", 30))
        with open(path, "rb") as fh: blob = fh.read()
        u = await self.send_restore(ctx, 555, "/restore CONFIRM", blob); self.assertFalse(u.message.reply_text.called); self.assertIsNone(db.get_payment("r1"))
        u = await self.send_restore(ctx, 1, "/restore", blob); self.assertIn("CONFIRM", reply_text(u)); self.assertIsNone(db.get_payment("r1"))
        u = await self.send_restore(ctx, 1, "/restore CONFIRM", blob)
        self.assertIn("✅", reply_text(u)); self.assertIsNotNone(db.get_payment("r1")); self.assertEqual(len(bot.documents), 1)   # پشتیبان قبلی فرستاده شد

    async def test_handler_rejects_bad_file_without_touching_db(self):
        ctx, cfg, db, bot = make_ctx(); db.record_payment("keep", 1, 500, "month", 30)
        u = await self.send_restore(ctx, 1, "/restore CONFIRM", b"garbage" * 100); self.assertIn("نامعتبر", reply_text(u))
        self.assertIsNotNone(db.get_payment("keep")); self.assertEqual(getattr(bot, "documents", []), [])
        u = await self.send_restore(ctx, 1, "/restore CONFIRM", b"x", size=50 * 1024 * 1024); self.assertIn("۲۰", reply_text(u))
        ctx.bot.files = {}; from types import SimpleNamespace
        u = make_update(1); u.message.document = SimpleNamespace(file_id="nope", file_size=10); u.message.caption = "/restore CONFIRM"
        await B.restore_handler(u, ctx); self.assertIn("ناموفق", reply_text(u))


if __name__ == "__main__":
    unittest.main()
