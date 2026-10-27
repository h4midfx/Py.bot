import numpy as np, pandas as pd
from smc import analyze, simulate, _find_fvg


def synth(n=3000, seed=0, drift_switch=120):
    """قیمت مصنوعی با رژیم‌های صعودی/نزولی و نوسان متغیر"""
    rng = np.random.default_rng(seed)
    price, rows, drift = 100.0, [], 0.0
    for i in range(n):
        if i % drift_switch == 0:
            drift = rng.normal(0, 0.0015)
        vol = 0.004 * (1 + 0.5 * np.sin(i / 200))
        o = price
        c = o * (1 + drift + rng.normal(0, vol))
        h = max(o, c) * (1 + abs(rng.normal(0, vol / 2)))
        l = min(o, c) * (1 - abs(rng.normal(0, vol / 2)))
        rows.append((i * 900_000, o, h, l, c))
        price = c
    return pd.DataFrame(rows, columns=["t", "o", "h", "l", "c"])


def test_fvg_helper():
    h = [10, 11, 12, 15]; l = [9, 10, 11, 13]
    assert _find_fvg(h, l, 0, 3, "LONG")       # l[3]=13 > h[1]=11
    h2 = [10, 11, 12, 13]; l2 = [9, 9.5, 10, 11]
    assert not _find_fvg(h2, l2, 0, 3, "LONG")
    print("fvg helper ok")


def test_handmade_bullish_ob():
    """ساختار دست‌ساز: سقف و کف مشخص، بعد شکست سقف، بعد برگشت به OB"""
    # n=2 برای کوتاه بودن
    pts = [  # (o,h,l,c)
        (100, 101, 99, 100.5), (100.5, 102, 100, 101.5), (101.5, 103, 101, 102.5),   # صعود تا سقف
        (102.5, 105, 102, 104), (104, 104.5, 101.5, 102), (102, 102.5, 99.5, 100),   # سقف 105 ، ریزش
        (100, 100.5, 97, 98), (98, 98.5, 96, 97),                                     # کف 96
        (97, 99, 96.5, 98.5), (98.5, 100, 98, 99.5), (99.5, 101, 99, 100.5),         # بازگشت
        (100.5, 102, 100, 101), (101, 101.5, 100, 100.2),                             # کندل نزولی (OB) نزدیک
        (100.2, 104, 100, 103.8), (103.8, 107, 103.5, 106.5),                         # شکست 105 => BOS/CHoCH
        (106.5, 107, 105, 106), (106, 106.2, 102.5, 103),                             # ریزش
        (103, 103.2, 100.0, 100.8),                                                   # تماس با OB
        (100.8, 103, 100.5, 102.5), (102.5, 104, 102, 103.5), (103.5, 105, 103, 104.5),
    ]
    df = pd.DataFrame([(i, *p) for i, p in enumerate(pts)], columns=["t", "o", "h", "l", "c"])
    sigs = analyze(df, n=2, require_pd=False)
    print("handmade signals:", [(s["side"], s["kind"], round(s["entry"], 2), round(s["sl"], 2)) for s in sigs])
    assert any(s["side"] == "LONG" for s in sigs), "باید حداقل یک سیگنال LONG پیدا شود"
    s = [s for s in sigs if s["side"] == "LONG"][0]
    assert s["sl"] < s["entry"] < s["tps"][0] < s["tps"][4]
    assert len(s["tps"]) == 5
    r = abs(s["entry"] - s["sl"])
    assert abs((s["tps"][4] - s["entry"]) - 5 * r) < 1e-9


def test_no_lookahead():
    """سیگنال‌ها نباید با اضافه‌شدن کندل‌های آینده تغییر کنند (no repaint)"""
    df = synth(2500, seed=3)
    full = {(s["idx"], s["side"], round(s["entry"], 8)) for s in analyze(df)}
    for cut in (800, 1400, 2000):
        part = {(s["idx"], s["side"], round(s["entry"], 8)) for s in analyze(df.iloc[:cut].reset_index(drop=True))}
        in_full = {x for x in full if x[0] < cut}
        assert part == in_full, f"repaint در cut={cut}: {part ^ in_full}"
    print("no-lookahead ok, total signals:", len(full))


def test_tp_geometry_and_sides():
    sigs = analyze(synth(4000, seed=7))
    assert sigs, "روی داده‌ی مصنوعی باید سیگنال تولید شود"
    for s in sigs:
        if s["side"] == "LONG":
            assert s["sl"] < s["entry"] < s["tps"][0] < s["tps"][1] < s["tps"][2] < s["tps"][3] < s["tps"][4]
        else:
            assert s["sl"] > s["entry"] > s["tps"][0] > s["tps"][1] > s["tps"][2] > s["tps"][3] > s["tps"][4]
    print("geometry ok:", len(sigs), "signals,",
          sum(s["side"] == "LONG" for s in sigs), "long /", sum(s["side"] == "SHORT" for s in sigs), "short")


def test_simulate_known_paths():
    base = dict(idx=0, side="LONG", entry=100.0, sl=99.0, tps=[101, 102, 103, 104, 105])
    # همه TPها
    df = pd.DataFrame({"t": range(3), "o": [100] * 3, "h": [100, 106, 106], "l": [100, 100, 100], "c": [100, 105, 105]})
    r = simulate(base, df); assert r["hits"] == 5 and abs(r["r"] - 3.0) < 1e-9, r
    # SL مستقیم
    df = pd.DataFrame({"t": range(2), "o": [100] * 2, "h": [100, 100.5], "l": [100, 98.9], "c": [100, 99]})
    r = simulate(base, df); assert r["stopped"] and abs(r["r"] + 1.0) < 1e-9, r
    # TP1 و TP2 بعد SL  => 0.2*1+0.2*2 - 0.2*3 = 0
    df = pd.DataFrame({"t": range(3), "o": [100] * 3, "h": [100, 102.5, 102], "l": [100, 100, 98.9], "c": [100, 102, 99]})
    r = simulate(base, df); assert r["hits"] == 2 and r["stopped"] and abs(r["r"]) < 1e-9, r
    # کندلی که هم SL و هم TP را لمس کند => SL اول
    df = pd.DataFrame({"t": range(2), "o": [100] * 2, "h": [100, 106], "l": [100, 98], "c": [100, 100]})
    r = simulate(base, df); assert r["stopped"] and r["hits"] == 0, r
    print("simulate ok")


def test_backtest_stats():
    allr, hits = [], np.zeros(6, int)
    for seed in range(8):
        df = synth(4000, seed=seed)
        for s in analyze(df):
            res = simulate(s, df)
            allr.append(res["r"]); hits[res["hits"]] += 1
    allr = np.array(allr)
    print(f"[داده مصنوعی/تصادفی] تعداد سیگنال={len(allr)}  میانگین R={allr.mean():+.3f}  "
          f"وین‌ریت(R>0)={(allr>0).mean():.1%}")
    print("توزیع تعداد TP لمس‌شده (0..5):", hits.tolist())
    # روی داده‌ی تصادفی نباید ادعای سود کنیم؛ فقط بررسی می‌کنیم کد سالم اجرا می‌شود
    assert len(allr) > 20


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn(); print("PASS", name)
            except AssertionError as e:
                print("FAIL", name, "->", e)
