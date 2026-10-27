"""
موتور Smart Money Concepts (SMC)

منطق:
  1) پیوت‌های سوینگ (های/لو) با تایید n کندل بعد -> بدون نگاه به آینده
  2) شکست ساختار: BOS (ادامه‌دهنده) و CHoCH (تغییر روند) با «بسته‌شدن کندل»
  3) Order Block = آخرین کندل مخالف قبل از حرکت شکننده‌ی ساختار
  4) FVG و Liquidity Sweep به‌عنوان تاییدیه (confluence)
  5) فیلتر Premium/Discount: خرید فقط در نیمه‌ی پایین رنج، فروش در نیمه‌ی بالا
  6) سیگنال: اولین برگشت قیمت به OB معتبر  ->  ورود، SL پشت OB، TP1..TP5
"""
from __future__ import annotations
import numpy as np
import pandas as pd

TP_MULTIPLES = (1.0, 2.0, 3.0, 4.0, 5.0)  # TP1..TP5 بر حسب R


def _atr(df: pd.DataFrame, period: int = 14) -> np.ndarray:
    c = df["c"]
    tr = pd.concat([df["h"] - df["l"], (df["h"] - c.shift()).abs(), (df["l"] - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean().to_numpy()


def _find_fvg(h, l, start: int, end: int, side: str) -> bool:
    """آیا بین start..end یک FVG هم‌جهت وجود دارد؟"""
    for k in range(max(start + 2, 2), end + 1):
        if side == "LONG" and l[k] > h[k - 2]:
            return True
        if side == "SHORT" and h[k] < l[k - 2]:
            return True
    return False


def analyze(df: pd.DataFrame, n: int = 5, max_age: int = 40, require_pd: bool = True,
            tp_multiples=TP_MULTIPLES) -> list[dict]:
    """
    روی تمام کندل‌های بسته‌شده اجرا می‌شود و همه‌ی سیگنال‌ها را برمی‌گرداند.
    برای استفاده‌ی زنده فقط سیگنال‌هایی که idx == آخرین کندل هستند مهم‌اند.
    """
    o, h, l, c = (df[x].to_numpy(dtype=float) for x in ("o", "h", "l", "c"))
    t = df["t"].to_numpy()
    atr = _atr(df)
    N = len(df)

    trend = 0
    last_sh = last_sl = prev_sl = prev_sh = None  # (idx, price)
    sh_broken = sl_broken = True
    zones: list[dict] = []
    signals: list[dict] = []

    for i in range(N):
        # --- تایید پیوت در ایندکس p=i-n (فقط داده‌ی تا i استفاده می‌شود) ---
        p = i - n
        if p >= n:
            win_h, win_l = h[p - n:p + n + 1], l[p - n:p + n + 1]
            if h[p] >= win_h.max() and (win_h == h[p]).sum() == 1:
                prev_sh, last_sh, sh_broken = last_sh, (p, h[p]), False
            if l[p] <= win_l.min() and (win_l == l[p]).sum() == 1:
                prev_sl, last_sl, sl_broken = last_sl, (p, l[p]), False

        # --- شکست ساختار صعودی ---
        if last_sh and not sh_broken and c[i] > last_sh[1]:
            kind = "BOS" if trend == 1 else ("CHoCH" if trend == -1 else "BOS")
            trend, sh_broken = 1, True
            start = last_sl[0] if last_sl else max(0, i - 25)
            ob = next((j for j in range(i - 1, start - 1, -1) if c[j] < o[j]), None)
            if ob is not None and last_sl:
                rng_hi, rng_lo = h[start:i + 1].max(), last_sl[1]
                zones.append(dict(
                    side="LONG", top=h[ob], bottom=l[ob], created=i, ob_idx=ob, kind=kind,
                    fvg=_find_fvg(h, l, start, i, "LONG"),
                    swept=bool(prev_sl and last_sl[1] < prev_sl[1]),
                    eq=(rng_hi + rng_lo) / 2, used=False))

        # --- شکست ساختار نزولی ---
        if last_sl and not sl_broken and c[i] < last_sl[1]:
            kind = "BOS" if trend == -1 else ("CHoCH" if trend == 1 else "BOS")
            trend, sl_broken = -1, True
            start = last_sh[0] if last_sh else max(0, i - 25)
            ob = next((j for j in range(i - 1, start - 1, -1) if c[j] > o[j]), None)
            if ob is not None and last_sh:
                rng_lo, rng_hi = l[start:i + 1].min(), last_sh[1]
                zones.append(dict(
                    side="SHORT", top=h[ob], bottom=l[ob], created=i, ob_idx=ob, kind=kind,
                    fvg=_find_fvg(h, l, start, i, "SHORT"),
                    swept=bool(prev_sh and last_sh[1] > prev_sh[1]),
                    eq=(rng_hi + rng_lo) / 2, used=False))

        # --- بررسی برگشت به OB (فقط زون‌های ساخته‌شده قبل از این کندل) ---
        for z in zones:
            if z["used"] or z["created"] >= i:
                continue
            if i - z["created"] > max_age:
                z["used"] = True
                continue
            long_ = z["side"] == "LONG"
            if (long_ and c[i] < z["bottom"]) or (not long_ and c[i] > z["top"]):
                z["used"] = True  # OB با بسته‌شدن کندل شکسته شد -> نامعتبر
                continue
            touched = (l[i] <= z["top"]) if long_ else (h[i] >= z["bottom"])
            if not touched:
                continue
            z["used"] = True  # فقط اولین تماس
            mid = (z["top"] + z["bottom"]) / 2
            if require_pd and ((long_ and mid > z["eq"]) or (not long_ and mid < z["eq"])):
                continue  # OB در ناحیه‌ی اشتباه (Premium/Discount)
            entry = c[i]
            sl = (z["bottom"] - 0.1 * atr[i]) if long_ else (z["top"] + 0.1 * atr[i])
            risk = abs(entry - sl)
            if not (0.3 * atr[i] <= risk <= 4 * atr[i]):
                continue
            m = 1 if long_ else -1
            score = 1 + int(z["fvg"]) + int(z["swept"]) + int(z["kind"] == "CHoCH")
            signals.append(dict(
                idx=i, candle=int(t[i]), side=z["side"], entry=float(entry), sl=float(sl),
                tps=[float(entry + m * risk * x) for x in tp_multiples],
                kind=z["kind"], fvg=z["fvg"], swept=z["swept"], score=score,
                ob_top=float(z["top"]), ob_bottom=float(z["bottom"])))
    return signals


def simulate(sig: dict, df: pd.DataFrame, max_hold: int = 150) -> dict:
    """
    بررسی نتیجه‌ی یک سیگنال روی کندل‌های بعدی.
    محافظه‌کارانه: اگر در یک کندل هم SL و هم TP لمس شود، SL اول حساب می‌شود.
    خروجی بر حسب R با خروج ۲۰٪ در هر TP.
    """
    h, l, c = (df[x].to_numpy(dtype=float) for x in ("h", "l", "c"))
    long_ = sig["side"] == "LONG"
    hits, stopped = 0, False
    end = min(len(df), sig["idx"] + 1 + max_hold)
    for j in range(sig["idx"] + 1, end):
        if (long_ and l[j] <= sig["sl"]) or (not long_ and h[j] >= sig["sl"]):
            stopped = True
            break
        while hits < 5 and ((long_ and h[j] >= sig["tps"][hits]) or (not long_ and l[j] <= sig["tps"][hits])):
            hits += 1
        if hits == 5:
            break
    risk = abs(sig["entry"] - sig["sl"])
    r = sum(0.2 * TP_MULTIPLES[k] for k in range(hits))
    rest = 0.2 * (5 - hits)
    if stopped:
        r -= rest
    elif hits < 5:  # تایم‌اوت: باقی‌مانده با قیمت بسته‌شدن
        last = c[end - 1]
        r += rest * ((last - sig["entry"]) / risk if long_ else (sig["entry"] - last) / risk)
    return dict(hits=hits, stopped=stopped, r=r)
