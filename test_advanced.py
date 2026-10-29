"""تست قابلیت‌های پیشرفته: فیلتر HTF، سقف ریسک، توقف خودکار، پایش سلامت، منحنی سرمایه"""
import os, tempfile, time, unittest
from unittest.mock import AsyncMock
import numpy as np
import pandas as pd
import testkit  # noqa: F401
from testkit import B, make_ctx, make_update, reply_text, NetworkError
import backtest as bt
import smc
from config import ConfigError, load_config
from database import Database

TF, H1 = 900_000, 3_600_000


def trend_df(n=400, drift=0.001, seed=0):
    rng = np.random.default_rng(seed); p, rows = 100.0, []
    for i in range(n):
        o = p; c = o * (1 + drift + rng.normal(0, 0.0005))
        rows.append((i * TF, o, max(o, c) * 1.0003, min(o, c) * 0.9997, c)); p = c
    return pd.DataFrame(rows, columns=["t", "o", "h", "l", "c"])


def find_signals_where(pred, seeds=range(80), min_idx=300):
    for seed in seeds:
        df = smc.synthetic_candles(3000, seed)
        for s in smc.analyze(df):
            if s["idx"] > min_idx and pred(s):
                return df.iloc[: s["idx"] + 1].reset_index(drop=True), s
    raise AssertionError("signal not found")


def now_for(df): return int(df["t"].iloc[-1]) + TF + 1000


class HtfBiasTests(unittest.TestCase):
    def test_invalid_arguments(self):
        df = trend_df(50)
        for tf, htf in ((TF, TF), (TF, TF // 2), (TF, TF * 3 // 2 + 1)):
            with self.assertRaises(ValueError): smc.htf_bias_series(df, tf, htf)

    def test_uptrend_downtrend_and_flat(self):
        up = smc.htf_bias_series(trend_df(400, 0.001), TF, H1); dn = smc.htf_bias_series(trend_df(400, -0.001), TF, H1)
        i = np.arange(800); c = 100 + 3 * np.sin(i / 12)                # بازار نوسانی بدون روند
        o = np.r_[c[0], c[:-1]]
        sine = pd.DataFrame({"t": i * TF, "o": o, "h": np.maximum(o, c) + 0.05, "l": np.minimum(o, c) - 0.05, "c": c})
        osc = smc.htf_bias_series(sine, TF, H1)
        self.assertEqual(set(up[100:]), {1}); self.assertEqual(set(dn[100:]), {-1})
        self.assertTrue({-1, 1} <= set(osc[100:]))                     # در بازار نوسانی سوگیری بین صعودی/نزولی عوض می‌شود (برخلاف روند پایدار)
        self.assertTrue((up[:8] == 0).all())                           # قبل از کامل‌شدن چند کندل HTF، خنثی

    def test_no_lookahead(self):
        df = smc.synthetic_candles(1500, 4); full = smc.htf_bias_series(df, TF, H1)
        for cut in (333, 777, 1111):
            self.assertTrue((smc.htf_bias_series(df.iloc[:cut].reset_index(drop=True), TF, H1) == full[:cut]).all(), cut)

    def test_incomplete_htf_candle_not_used(self):
        # قیمت در وسط کندل HTF جهش می‌کند؛ تا قبل از بسته‌شدن HTF سوگیری نباید عوض شود
        df = trend_df(200, 0.0005); b0 = smc.htf_bias_series(df, TF, H1)
        j = df.copy(); mid = 101                                       # کندل اول یک ساعت جدید (101 % 4 == 1)
        j.loc[mid:, ["o", "h", "l", "c"]] = j.loc[mid:, ["o", "h", "l", "c"]] * 0.5
        b1 = smc.htf_bias_series(j, TF, H1)
        self.assertTrue((b0[:mid + 2] == b1[:mid + 2]).all())          # تا قبل از کامل‌شدن آن ساعت یکسان


class HtfFilterTests(unittest.TestCase):
    def test_filter_only_removes_counter_trend_and_keeps_rest(self):
        df = smc.synthetic_candles(5000, 8); bias = smc.htf_bias_series(df, TF, H1)
        base = {(s["idx"], s["side"]) for s in smc.analyze(df)}
        filt = smc.analyze(df, htf_ms=H1, tf_ms=TF); got = {(s["idx"], s["side"]) for s in filt}
        self.assertTrue(got <= base); self.assertGreater(len(base), len(got))
        for s in filt:
            self.assertGreaterEqual(s["htf"], 0)                       # هرگز خلاف روند HTF
            self.assertEqual(s["htf"], bias[s["idx"]] * (1 if s["side"] == "LONG" else -1))
        removed = base - got
        for idx, side in removed: self.assertEqual(bias[idx], -1 if side == "LONG" else 1)

    def test_filter_has_no_lookahead(self):
        df = smc.synthetic_candles(2500, 5); key = lambda s: (s["idx"], s["side"], round(s["entry"], 8))
        full = {key(s) for s in smc.analyze(df, htf_ms=H1, tf_ms=TF)}
        for cut in (900, 1700):
            part = {key(s) for s in smc.analyze(df.iloc[:cut].reset_index(drop=True), htf_ms=H1, tf_ms=TF)}
            self.assertEqual(part, {x for x in full if x[0] < cut})

    def test_without_filter_htf_key_is_none(self):
        self.assertTrue(all(s["htf"] is None for s in smc.analyze(smc.synthetic_candles(2000, 1))))

    def test_backtest_accepts_htf(self):
        df = smc.synthetic_candles(5000, 8)
        a, b = bt.run_backtest(df, 1), bt.run_backtest(df, 1, htf_ms=H1, tf_ms=TF)
        self.assertGreater(len(a), 5); self.assertTrue(all(x["closed"] > x["idx"] for x in b))

    def test_config_validation(self):
        env = {"BOT_TOKEN": "t", "ADMIN_IDS": "1", "TIMEFRAME": "15m"}
        self.assertEqual(load_config({**env, "HTF": "1h"}).htf_ms, H1); self.assertIsNone(load_config({**env, "HTF": ""}).htf_ms)
        self.assertIsNone(load_config({**env, "HTF": "off"}).htf_ms)
        for bad in ("15m", "5m", "7m", "xx"):
            with self.assertRaises(ConfigError): load_config({**env, "HTF": bad})
        with self.assertRaises(ConfigError): load_config({**env, "TIMEFRAME": "1h", "HTF": "2h", "MAX_OPEN_SIGNALS": "-1"})

    def test_signal_text_shows_htf_tag(self):
        s = dict(smc.analyze(smc.synthetic_candles(3000, 3))[0]); s["htf"] = 1
        self.assertIn("HTF ✅", B.signal_text("X", s, "15m")); s["htf"] = 0; self.assertNotIn("HTF", B.signal_text("X", s, "15m"))


class ExposureLimitTests(unittest.IsolatedAsyncioTestCase):
    async def run_scan(self, env, dfs_by_symbol):
        ctx, cfg, db, bot = make_ctx(env); db.grant(10, 5)
        now = max(now_for(d) for d in dfs_by_symbol.values())
        n = await B.process_new_signals(ctx, dfs_by_symbol, now_ms=now); return n, db, bot

    async def test_max_open_cap(self):
        cut, _ = find_signals_where(lambda s: True)
        n, db, bot = await self.run_scan({"MAX_OPEN_SIGNALS": "2"}, {f"S{i}": cut for i in range(5)})
        self.assertEqual((n, db.count_open(), len(bot.texts(10))), (2, 2, 2))

    async def test_daily_cap_and_unlimited(self):
        cut, _ = find_signals_where(lambda s: True)
        n, db, bot = await self.run_scan({"MAX_SIGNALS_PER_DAY": "1", "MAX_OPEN_SIGNALS": "0"}, {f"S{i}": cut for i in range(4)})
        self.assertEqual(n, 1)
        n, db, bot = await self.run_scan({"MAX_SIGNALS_PER_DAY": "0", "MAX_OPEN_SIGNALS": "0"}, {f"S{i}": cut for i in range(4)})
        self.assertEqual(n, 4)

    async def test_strongest_signal_wins_when_capped(self):
        low, ls = find_signals_where(lambda s: s["score"] == 1); high, hs = find_signals_where(lambda s: s["score"] >= 3)
        n, db, bot = await self.run_scan({"MAX_OPEN_SIGNALS": "1"}, {"LOWSYM": low, "HIGHSYM": high})   # LOW اول در دیکشنری
        self.assertEqual(n, 1); self.assertEqual(db.conn.execute("SELECT symbol FROM signals").fetchone()[0], "HIGHSYM")

    async def test_cap_counts_existing_open_signals(self):
        cut, _ = find_signals_where(lambda s: True); ctx, cfg, db, bot = make_ctx({"MAX_OPEN_SIGNALS": "1"}); db.grant(10, 5)
        db.insert_signal("OLD", 1, "LONG", 100, 99, [1] * 5, 2, "BOS", "15m")
        self.assertEqual(await B.process_new_signals(ctx, {"NEW": cut}, now_ms=now_for(cut)), 0)


class CircuitBreakerTests(unittest.IsolatedAsyncioTestCase):
    def close(self, db, n, r, base_ts=1_800_000_000):
        for i in range(n):
            sid = db.insert_signal(f"X{db.max_signal_id()}", 1000 + db.max_signal_id(), "LONG", 100, 99, [1] * 5, 2, "BOS", "15m", base_ts)
            db.update_signal(sid, 0, 1, "sl", r, now=base_ts + db.max_signal_id())

    def test_consecutive_losses_logic(self):
        db = Database(); self.close(db, 2, 1.0); self.close(db, 3, -1.0); self.assertEqual(db.consecutive_losses(), 3)
        self.close(db, 1, 0.5); self.assertEqual(db.consecutive_losses(), 0)            # یک برد شمارش را صفر می‌کند
        self.close(db, 2, -1.0); self.assertEqual(db.consecutive_losses(), 2)
        self.assertEqual(db.consecutive_losses(after_id=db.max_signal_id()), 0)

    async def test_auto_pause_notify_once_and_block_signals(self):
        ctx, cfg, db, bot = make_ctx({"PAUSE_AFTER_LOSSES": "3"}); db.grant(10, 5)
        self.close(db, 2, -1.0); self.assertFalse(await B.check_breaker(ctx)); self.assertFalse(B.is_paused(db))
        self.close(db, 1, -1.0); self.assertTrue(await B.check_breaker(ctx)); self.assertTrue(B.is_paused(db))
        self.assertTrue(any("توقف خودکار" in t for t in bot.texts(1)))
        n_msgs = len(bot.texts(1)); self.assertFalse(await B.check_breaker(ctx)); self.assertEqual(len(bot.texts(1)), n_msgs)   # دوباره اعلام نمی‌شود
        cut, _ = find_signals_where(lambda s: True)
        self.assertEqual(await B.process_new_signals(ctx, {"BTCUSDT": cut}, now_ms=now_for(cut)), 0)
        self.assertEqual(bot.texts(10), [])

    async def test_disabled_by_default_and_resume_resets_counting(self):
        ctx, cfg, db, bot = make_ctx(); self.close(db, 10, -1.0); self.assertFalse(await B.check_breaker(ctx))
        ctx, cfg, db, bot = make_ctx({"PAUSE_AFTER_LOSSES": "2"}); self.close(db, 2, -1.0); await B.check_breaker(ctx)
        u = make_update(555); await B.resume_cmd(u, ctx); self.assertTrue(B.is_paused(db)); self.assertFalse(u.message.reply_text.called)   # فقط ادمین
        await B.resume_cmd(make_update(1), ctx); self.assertFalse(B.is_paused(db))
        self.assertFalse(await B.check_breaker(ctx))                   # باخت‌های قبلی دوباره شمرده نمی‌شوند
        self.close(db, 2, -1.0); self.assertTrue(await B.check_breaker(ctx))

    async def test_manual_pause_and_persistence(self):
        path = os.path.join(tempfile.mkdtemp(), "p.db"); ctx, cfg, db, bot = make_ctx({"DB_PATH": path}); ctx.bot_data["db"] = Database(path)
        await B.pause_cmd(make_update(1), ctx); self.assertTrue(B.is_paused(Database(path)))        # بعد از ری‌استارت هم متوقف می‌ماند
        u = make_update(555); await B.pause_cmd(u, ctx); self.assertFalse(u.message.reply_text.called)

    async def test_tracking_continues_while_paused(self):
        from test_bot import find_signal, cut_at
        ctx, cfg, db, bot = make_ctx(); db.grant(10, 5)
        df, sig = find_signal(); cut = cut_at(df, sig)
        await B.process_new_signals(ctx, {"BTCUSDT": cut}, now_ms=now_for(cut)); await B.pause_cmd(make_update(1), ctx)
        e, tp = sig["entry"], sig["tps"][0]; long_ = sig["side"] == "LONG"; t0 = int(cut["t"].iloc[-1])
        d = pd.concat([cut, pd.DataFrame([(t0 + TF, e, max(e, tp) if long_ else e, e if long_ else min(e, tp), e)], columns=["t", "o", "h", "l", "c"])], ignore_index=True)
        bot.sent.clear(); self.assertEqual(await B.track_open(ctx, {"BTCUSDT": d}), 1); self.assertTrue(any("TP1" in t for t in bot.texts(10)))


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_alert_after_three_total_failures_then_recovery(self):
        ctx, cfg, db, bot = make_ctx(); dead = {"A": None, "B": None}; alive = {"A": smc.synthetic_candles(100), "B": None}
        for _ in range(2): await B.record_scan_health(ctx, dead)
        self.assertEqual(bot.texts(1), [])
        await B.record_scan_health(ctx, dead); self.assertEqual(len(bot.texts(1)), 1); self.assertIn("مسدودی", bot.texts(1)[0])
        await B.record_scan_health(ctx, dead); self.assertEqual(len(bot.texts(1)), 1)                  # هشدار تکراری نیست
        await B.record_scan_health(ctx, alive); self.assertIn("برقرار", bot.texts(1)[-1])
        n = len(bot.texts(1)); await B.record_scan_health(ctx, alive); self.assertEqual(len(bot.texts(1)), n)

    async def test_partial_failure_does_not_alert(self):
        ctx, cfg, db, bot = make_ctx(); partial = {"A": smc.synthetic_candles(100), "B": None}
        for _ in range(6): await B.record_scan_health(ctx, partial)
        self.assertEqual(bot.texts(1), [])

    async def test_health_command_admin_only_and_content(self):
        ctx, cfg, db, bot = make_ctx({"PAUSE_AFTER_LOSSES": "5", "HTF": "1h"}); db.grant(10, 5)
        await B.record_scan_health(ctx, {"A": smc.synthetic_candles(100), "B": None})
        u = make_update(555); await B.health_cmd(u, ctx); self.assertFalse(u.message.reply_text.called)
        u = make_update(1); await B.health_cmd(u, ctx); t = reply_text(u)
        for needle in ("آپ‌تایم", "1/2", "binance", "60m", "فعال", "0/5", "مشترک فعال: 1"): self.assertIn(needle, t)
        self.assertNotIn("TESTTOKEN", t)                                                              # هیچ رازی در خروجی نیست

    async def test_scan_job_end_to_end_and_never_raises(self):
        cut, _ = find_signals_where(lambda s: True); ctx, cfg, db, bot = make_ctx(); db.grant(10, 5)
        async def fake_all(client, cfg_): return {"BTCUSDT": cut}
        orig = B.fetch_all; B.fetch_all = fake_all
        try:
            real_now = B.time.time
            B.time.time = lambda: now_for(cut) / 1000                                                 # ساعت را به لحظه‌ی کندل می‌بریم
            try: await B.scan_job(ctx)
            finally: B.time.time = real_now
            self.assertEqual(len(bot.texts(10)), 1); self.assertEqual(ctx.bot_data["health"]["ok_symbols"], 1)
            async def boom(client, cfg_): raise RuntimeError("x")
            B.fetch_all = boom; await B.scan_job(ctx)                                                 # استثنا بالا نمی‌آید
        finally: B.fetch_all = orig


class SparklineTests(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(B.sparkline([]), ""); up = B.sparkline([1, 1, 1, 1, 1])
        self.assertEqual(up, "".join(sorted(up))); self.assertEqual((up[0], up[-1]), ("▁", "█"))
        self.assertEqual(len(set(B.sparkline([0, 0, 0]))), 1)
        dn = B.sparkline([-1, -1, -1]); self.assertEqual((dn[0], dn[-1]), ("█", "▁"))
        v = B.sparkline([2, -2, 2, -2]); self.assertEqual(len(v), 4)

    def test_perf_text_includes_curve_only_with_enough_data(self):
        db = Database()
        for i, r in enumerate([1.0, -0.5, 2.0]):
            sid = db.insert_signal("X", i, "LONG", 100, 99, [1] * 5, 2, "BOS", "15m", 1000 + i); db.update_signal(sid, 1, i, "sl", r, now=2000 + i)
        t = B.perf_text(db.performance()); self.assertIn("منحنی سرمایه", t)
        db2 = Database(); sid = db2.insert_signal("X", 1, "LONG", 100, 99, [1] * 5, 2, "BOS", "15m"); db2.update_signal(sid, 1, 1, "sl", 1.0)
        self.assertNotIn("منحنی", B.perf_text(db2.performance()))


class BacktestStreakTests(unittest.TestCase):
    def test_max_streak_in_summary_and_report(self):
        mk = lambda t, r: dict(t=t, idx=t, closed=t, side="LONG", score=2, hits=0, outcome="sl", gross=r, net=r, cost=0)
        sm = bt.summarize([mk(1, 1), mk(2, -1), mk(3, -1), mk(4, -1), mk(5, 1), mk(6, -1)])
        self.assertEqual(sm["max_streak"], 3); self.assertIn("باخت پیاپی: 3", bt.format_report("T", sm, 0.0004))


if __name__ == "__main__":
    unittest.main()
