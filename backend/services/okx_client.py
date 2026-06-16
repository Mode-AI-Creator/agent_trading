"""OKX Exchange REST + WebSocket client."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import base64
import json
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import httpx
import pandas as pd
import websockets

from backend.config import get_settings
from backend.utils.logger import get_logger
from backend.utils.rate_limiter import okx_rest_limiter

logger = get_logger("backend.services.okx_client")

# OKX bar codes used in API vs. human-readable labels
BAR_MAP = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1H": "1H", "2H": "2H", "4H": "4H", "6H": "6H", "12H": "12H",
    "1D": "1D", "1W": "1W",
}

KLINE_COLUMNS = [
    "timestamp", "open", "high", "low", "close",
    "volume", "volCcy", "volCcyQuote", "confirm",
]


class OKXClient:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._base_url = self._settings.okx_base_url
        self._client = httpx.AsyncClient(timeout=15.0)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        msg = f"{timestamp}{method}{path}{body}"
        sig = hmac.new(
            self._settings.okx_secret_key.encode(),
            msg.encode(),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(sig).decode()

    def _auth_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        return {
            "OK-ACCESS-KEY": self._settings.okx_api_key,
            "OK-ACCESS-SIGN": self._sign(ts, method, path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self._settings.okx_passphrase,
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: Dict | None = None) -> dict:
        await okx_rest_limiter.acquire()
        url = f"{self._base_url}{path}"
        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def _get_auth(self, path: str, params: Dict | None = None) -> dict:
        await okx_rest_limiter.acquire()
        url = f"{self._base_url}{path}"
        headers = self._auth_headers("GET", path)
        resp = await self._client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()

    # ── Public Market Data ────────────────────────────────────────────────────

    async def get_klines(
        self,
        symbol: str,
        bar: str = "4H",
        limit: int = 200,
    ) -> pd.DataFrame:
        """Fetch OHLCV candlestick data.

        Args:
            symbol: e.g. 'BTC-USDT'
            bar: timeframe e.g. '4H', '1D'
            limit: number of candles (max 300 per OKX docs)

        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume, ...
        """
        bar = BAR_MAP.get(bar, bar)
        data = await self._get(
            "/api/v5/market/candles",
            params={"instId": symbol, "bar": bar, "limit": str(min(limit, 300))},
        )
        rows = data.get("data", [])
        if not rows:
            logger.warning("No kline data returned for %s %s", symbol, bar)
            return pd.DataFrame(columns=KLINE_COLUMNS)

        df = pd.DataFrame(rows, columns=KLINE_COLUMNS)
        df = df.astype({
            "open": float, "high": float, "low": float, "close": float,
            "volume": float, "volCcy": float, "volCcyQuote": float,
        })
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="ms", utc=True)
        df = df.sort_values("timestamp").reset_index(drop=True)
        # Drop unconfirmed (in-progress) candle
        df = df[df["confirm"] == "1"].copy()
        df = df.drop(columns=["confirm"])
        return df

    async def get_ticker(self, symbol: str) -> dict:
        """Fetch latest ticker for a symbol."""
        data = await self._get("/api/v5/market/ticker", params={"instId": symbol})
        tickers = data.get("data", [])
        if not tickers:
            raise ValueError(f"No ticker data for {symbol}")
        t = tickers[0]
        return {
            "symbol": symbol,
            "last": float(t["last"]),
            "bid": float(t["bidPx"]) if t["bidPx"] else None,
            "ask": float(t["askPx"]) if t["askPx"] else None,
            "open_24h": float(t["open24h"]),
            "high_24h": float(t["high24h"]),
            "low_24h": float(t["low24h"]),
            "vol_24h": float(t["vol24h"]),
            "change_pct_24h": (
                (float(t["last"]) - float(t["open24h"])) / float(t["open24h"]) * 100
                if float(t["open24h"]) else 0.0
            ),
            "ts": int(t["ts"]),
        }

    # ── Affiliate (requires auth) ─────────────────────────────────────────────

    async def get_affiliate_stats(self) -> dict:
        """Fetch OKX affiliate program summary stats."""
        if not self._settings.okx_api_key:
            return {}
        try:
            data = await self._get_auth("/api/v5/affiliate/invitee/detail")
            return data.get("data", {})
        except Exception as exc:
            logger.warning("Affiliate stats fetch failed: %s", exc)
            return {}

    # ── WebSocket ─────────────────────────────────────────────────────────────

    async def subscribe_tickers(
        self,
        symbols: List[str],
        callback: Callable[[dict], None],
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Subscribe to real-time ticker updates for given symbols.

        Runs indefinitely until stop_event is set (or exception).
        Reconnects automatically with exponential backoff.
        """
        ws_url = "wss://ws.okx.com:8443/ws/v5/public"
        args = [{"channel": "tickers", "instId": sym} for sym in symbols]
        subscribe_msg = json.dumps({"op": "subscribe", "args": args})

        backoff = 5
        while True:
            try:
                async with websockets.connect(ws_url, ping_interval=20) as ws:
                    await ws.send(subscribe_msg)
                    logger.info("OKX WebSocket subscribed: %s", symbols)
                    backoff = 5  # reset on successful connect
                    async for raw in ws:
                        if stop_event and stop_event.is_set():
                            return
                        try:
                            msg = json.loads(raw)
                            if msg.get("event") == "error":
                                logger.error("OKX WS error: %s", msg)
                                continue
                            if "data" in msg:
                                for item in msg["data"]:
                                    callback(item)
                        except Exception as exc:
                            logger.debug("WS message parse error: %s", exc)
            except Exception as exc:
                logger.warning("OKX WebSocket disconnected: %s — reconnecting in %ds", exc, backoff)
                if stop_event and stop_event.is_set():
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    async def close(self) -> None:
        await self._client.aclose()


# Module-level singleton
_client: OKXClient | None = None


def get_okx_client() -> OKXClient:
    global _client
    if _client is None:
        _client = OKXClient()
    return _client
