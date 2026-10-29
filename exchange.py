"""
منبع داده‌ی بازار: Binance Futures (پیش‌فرض) یا Bybit (linear USDT).
هر دو به یک قالب مشترک تبدیل می‌شوند: [open_time, o, h, l, c, volume, close_time] (صعودی).
کندل‌های هنوز بسته‌نشده را smc.klines_to_df با close_time حذف می‌کند.

⚠️ آداپتور Bybit طبق مستندات رسمی v5 نوشته و با پاسخ ساختگی تست شده؛ با سرور زنده تست نشده است.
"""
from __future__ import annotations

import smc

EXCHANGES = ("binance", "bybit")
DEFAULT_URLS = {"binance": "https://fapi.binance.com", "bybit": "https://api.bybit.com"}
PAGE = {"binance": 1500, "bybit": 1000}
BYBIT_INTERVALS = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "2h": "120", "4h": "240"}


def build_request(exchange: str, base_url: str, symbol: str, timeframe: str,
                  start_ms: int | None = None, end_ms: int | None = None, limit: int = 500):
    """-> (url, params)"""
    if exchange not in EXCHANGES:
        raise ValueError(f"exchange ناشناخته: {exchange}")
    limit = min(limit, PAGE[exchange])
    if exchange == "binance":
        p = {"symbol": symbol, "interval": timeframe, "limit": limit}
        if start_ms is not None:
            p["startTime"] = int(start_ms)
        if end_ms is not None:
            p["endTime"] = int(end_ms)
        return f"{base_url}/fapi/v1/klines", p
    if exchange == "bybit":
        p = {"category": "linear", "symbol": symbol, "interval": BYBIT_INTERVALS[timeframe], "limit": limit}
        if start_ms is not None:
            p["start"] = int(start_ms)
        if end_ms is not None:
            p["end"] = int(end_ms)
        return f"{base_url}/v5/market/kline", p
    raise ValueError(f"exchange ناشناخته: {exchange}")


def normalize(exchange: str, payload, timeframe: str) -> list:
    """پاسخ خام -> ردیف‌های صعودی با قالب مشترک. ساختار نامعتبر = ValueError."""
    tf = smc.TF_MS[timeframe]
    if exchange == "binance":
        if not isinstance(payload, list):
            raise ValueError("پاسخ Binance نامعتبر")
        return payload
    if exchange == "bybit":
        if not isinstance(payload, dict) or payload.get("retCode") != 0:
            raise ValueError("پاسخ Bybit نامعتبر یا خطا")
        lst = (payload.get("result") or {}).get("list")
        if not isinstance(lst, list):
            raise ValueError("پاسخ Bybit بدون list")
        rows = []
        try:
            for r in reversed(lst):                      # Bybit: جدید به قدیم
                t = int(r[0])
                rows.append([t, r[1], r[2], r[3], r[4], r[5] if len(r) > 5 else "0", t + tf - 1])
        except (IndexError, TypeError, ValueError) as e:
            raise ValueError("ردیف Bybit نامعتبر") from e
        return rows
    raise ValueError(f"exchange ناشناخته: {exchange}")
