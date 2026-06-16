"""
Auto-trading agent using DeepSeek tool-calling (OpenAI-compatible API).

Flow per run:
  1. Risk gates: daily loss limit, max open positions.
  2. For BTC and ETH, run DeepSeek agent loop:
     - Agent calls market-data tools to gather raw data.
     - Agent returns final decision: trade | hold.
  3. If TRADING_PAPER_MODE=true  → record paper trade in DB, skip OKX.
     If TRADING_PAPER_MODE=false → place real limit order on OKX.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

import httpx
import pandas as pd
from openai import AsyncOpenAI

from backend.config import get_settings
from backend.database import db_session
from backend.models.trade import AutoTrade
from backend.services.okx_trading import get_trading_client
from backend.utils.logger import get_logger

logger = get_logger("backend.services.trading_agent")

_SERVER_START = datetime.now(timezone.utc)

_DEEPSEEK_BASE = "https://api.deepseek.com"

_SYMBOLS = [
    ("BTC-USDT", "BTC-USDT-SWAP"),
    ("ETH-USDT", "ETH-USDT-SWAP"),
]

_OKX_BASE = "https://www.okx.com"

# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM_TEMPLATE = """You are an autonomous crypto trading agent for BTC/USDT and ETH/USDT perpetual futures on OKX.

Your job: analyze raw market data for ONE symbol at a time, then make ONE decision:
  A) TRADE — provide exact entry, stop-loss, take-profit levels.
  B) HOLD  — no trade right now.

Rules:
- Only trade if risk:reward ≥ 1:2 (risk 1% → target at least 2%).
- Prefer waiting for clean setups; most runs should be HOLD.
- High positive funding rate → bias short; very negative → bias long.
- Large recent liquidations on one side → reduced edge for entries in that direction.
- Always set stop-loss. No entries without stop-loss.
- The system enforces max open positions and daily loss limits separately.

Position size guidance (size_pct = % of total balance used as margin, range 1–20):
- Weak setup / low conviction → 3–5%
- Standard setup / moderate conviction → 8–12%
- Strong setup / high conviction + tight stop → 15–20%
- Never risk more than you're willing to lose on a single trade.

Leverage guidance (max allowed: {max_leverage}x):
- Choose leverage based on your confidence level and setup quality.
- Low confidence / unclear structure → 1–2x
- Moderate confidence / decent R:R → 2–3x
- High confidence / strong setup + clear invalidation level → 4–{max_leverage}x
- Never exceed {max_leverage}x.

Use the provided tools to fetch raw market data. You may call multiple tools across different timeframes. When ready, call submit_decision exactly once.

submit_decision fields:
  action:        "trade" | "hold"
  direction:     "long" | "short"   (required if trade)
  entry_price:   float              (current market price, for reference only — order is market)
  stop_loss:     float
  take_profit_1: float
  take_profit_2: float              (optional, higher target)
  size_pct:      float              (1–20, % of total balance as margin; agent decides)
  leverage:      int                (1–{max_leverage}, based on confidence; agent decides)
  reasoning:     string             (1–3 sentences)
"""

# ── Tool definitions (OpenAI function-calling format) ─────────────────────────

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_klines",
            "description": (
                "Fetch recent OHLCV candlestick data from OKX. "
                "bar options: 1m 5m 15m 30m 1H 4H 1D. limit max 300."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "e.g. BTC-USDT"},
                    "bar":    {"type": "string", "description": "1m 5m 15m 30m 1H 4H 1D"},
                    "limit":  {"type": "integer", "description": "Number of candles (max 300)", "default": 100},
                },
                "required": ["symbol", "bar"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ticker",
            "description": "Get current price, 24h high/low/volume and change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "e.g. BTC-USDT"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_funding_rate",
            "description": "Get current funding rate for a perpetual swap. Positive = longs pay shorts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "swap_symbol": {"type": "string", "description": "e.g. BTC-USDT-SWAP"},
                },
                "required": ["swap_symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_open_interest",
            "description": "Get open interest (USD billions) for a perpetual swap.",
            "parameters": {
                "type": "object",
                "properties": {
                    "swap_symbol": {"type": "string", "description": "e.g. BTC-USDT-SWAP"},
                },
                "required": ["swap_symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_long_short_ratio",
            "description": "Get top-trader long/short position ratio. >1 = more longs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "swap_symbol": {"type": "string", "description": "e.g. BTC-USDT-SWAP"},
                    "period":      {"type": "string", "description": "1H or 4H or 1D", "default": "1H"},
                },
                "required": ["swap_symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_liquidations",
            "description": "Get liquidation volumes (past 1 hour, USD millions) for a perpetual swap.",
            "parameters": {
                "type": "object",
                "properties": {
                    "swap_symbol": {"type": "string", "description": "e.g. BTC-USDT-SWAP"},
                },
                "required": ["swap_symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_orderbook",
            "description": "Get order book snapshot. bid_ask_ratio > 1 = more buy pressure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "e.g. BTC-USDT"},
                    "depth":  {"type": "integer", "description": "Levels (max 20)", "default": 5},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fear_greed",
            "description": "Get Crypto Fear & Greed Index (0=extreme fear, 100=extreme greed).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_open_positions",
            "description": "Get your currently open OKX perpetual swap positions and available balance.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_bollinger_bands",
            "description": (
                "Calculate Bollinger Bands for a symbol on a given timeframe. "
                "Returns upper/middle/lower band values for recent candles plus the current %B and bandwidth."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol":   {"type": "string", "description": "e.g. BTC-USDT"},
                    "bar":      {"type": "string", "description": "1m 5m 15m 30m 1H 4H 1D"},
                    "period":   {"type": "integer", "description": "SMA period (default 20)", "default": 20},
                    "std_dev":  {"type": "number",  "description": "Standard deviation multiplier (default 2.0)", "default": 2.0},
                    "limit":    {"type": "integer", "description": "Number of result candles to return (default 50)", "default": 50},
                },
                "required": ["symbol", "bar"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_macd",
            "description": (
                "Calculate MACD (Moving Average Convergence Divergence) for a symbol on a given timeframe. "
                "Returns MACD line, signal line, and histogram for recent candles."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "e.g. BTC-USDT"},
                    "bar":    {"type": "string", "description": "1m 5m 15m 30m 1H 4H 1D"},
                    "fast":   {"type": "integer", "description": "Fast EMA period (default 12)", "default": 12},
                    "slow":   {"type": "integer", "description": "Slow EMA period (default 26)", "default": 26},
                    "signal": {"type": "integer", "description": "Signal EMA period (default 9)", "default": 9},
                    "limit":  {"type": "integer", "description": "Number of result candles to return (default 50)", "default": 50},
                },
                "required": ["symbol", "bar"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_decision",
            "description": "Submit your final trading decision. Call this exactly once when ready.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action":        {"type": "string", "enum": ["trade", "hold"]},
                    "direction":     {"type": "string", "enum": ["long", "short"]},
                    "entry_price":   {"type": "number"},
                    "stop_loss":     {"type": "number"},
                    "take_profit_1": {"type": "number"},
                    "take_profit_2": {"type": "number"},
                    "size_pct":      {"type": "number", "description": "1–20, default 10"},
                    "leverage":      {"type": "integer", "description": "1–max_leverage, based on confidence; default 2"},
                    "reasoning":     {"type": "string"},
                },
                "required": ["action", "reasoning", "direction", "entry_price", "stop_loss", "take_profit_1"],
            },
        },
    },
]

# ── Tool execution ─────────────────────────────────────────────────────────────

async def _exec_tool(name: str, inp: dict, trading_client) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:

        if name == "get_klines":
            symbol = inp["symbol"]
            bar = inp.get("bar", "1H")
            limit = min(int(inp.get("limit", 100)), 300)
            r = await client.get(
                f"{_OKX_BASE}/api/v5/market/candles",
                params={"instId": symbol, "bar": bar, "limit": str(limit)},
            )
            r.raise_for_status()
            candles = r.json().get("data", [])
            rows = [{"t": c[0], "o": c[1], "h": c[2], "l": c[3], "c": c[4], "vol": c[5]}
                    for c in reversed(candles)]
            return {"symbol": symbol, "bar": bar, "candles": rows[-limit:]}

        elif name == "get_ticker":
            symbol = inp["symbol"]
            r = await client.get(f"{_OKX_BASE}/api/v5/market/ticker", params={"instId": symbol})
            r.raise_for_status()
            t = r.json()["data"][0]
            last = float(t["last"])
            open24 = float(t["open24h"])
            return {
                "symbol": symbol,
                "last": last,
                "open_24h": open24,
                "high_24h": float(t["high24h"]),
                "low_24h": float(t["low24h"]),
                "vol_24h_usdt": float(t["volCcy24h"]),
                "change_24h_pct": round((last - open24) / open24 * 100, 2) if open24 else 0,
            }

        elif name == "get_funding_rate":
            swap_symbol = inp["swap_symbol"]
            r = await client.get(f"{_OKX_BASE}/api/v5/public/funding-rate",
                                 params={"instId": swap_symbol})
            r.raise_for_status()
            d = r.json()["data"][0]
            rate = float(d["fundingRate"])
            return {
                "symbol": swap_symbol,
                "funding_rate": rate,
                "funding_rate_pct": f"{rate * 100:.4f}%",
                "annualized_pct": round(rate * 3 * 365 * 100, 2),
                "next_funding_time": d.get("nextFundingTime"),
            }

        elif name == "get_open_interest":
            swap_symbol = inp["swap_symbol"]
            r = await client.get(f"{_OKX_BASE}/api/v5/public/open-interest",
                                 params={"instType": "SWAP", "instId": swap_symbol})
            r.raise_for_status()
            d = r.json()["data"][0]
            return {
                "symbol": swap_symbol,
                "open_interest_usd_billions": round(float(d["oiUsd"]) / 1e9, 3),
                "open_interest_contracts": float(d["oi"]),
            }

        elif name == "get_long_short_ratio":
            swap_symbol = inp["swap_symbol"]
            period = inp.get("period", "1H")
            r = await client.get(
                f"{_OKX_BASE}/api/v5/rubik/stat/contracts/long-short-account-ratio-contract-top-trader",
                params={"instId": swap_symbol, "period": period},
            )
            r.raise_for_status()
            entries = r.json().get("data", [])
            if not entries:
                return {"symbol": swap_symbol, "long_short_ratio": None}
            ratio = round(float(entries[0][1]), 3)
            return {
                "symbol": swap_symbol,
                "period": period,
                "long_short_ratio": ratio,
                "interpretation": (
                    "longs dominate" if ratio > 1.2
                    else "shorts dominate" if ratio < 0.85
                    else "balanced"
                ),
            }

        elif name == "get_liquidations":
            swap_symbol = inp["swap_symbol"]
            uly = swap_symbol.replace("-SWAP", "")
            r = await client.get(
                f"{_OKX_BASE}/api/v5/public/liquidation-orders",
                params={"instType": "SWAP", "uly": uly, "state": "filled", "limit": "100"},
            )
            r.raise_for_status()
            now_ts = datetime.now(timezone.utc).timestamp() * 1000
            one_hour_ago = now_ts - 3_600_000
            liq_long = liq_short = 0.0
            for entry in r.json().get("data", []):
                for detail in entry.get("details", []):
                    try:
                        if float(detail.get("ts", 0)) < one_hour_ago:
                            continue
                        val = float(detail.get("sz", 0)) * float(detail.get("bkPx", 0)) / 1e6
                        if detail.get("posSide") == "long":
                            liq_long += val
                        elif detail.get("posSide") == "short":
                            liq_short += val
                    except Exception:
                        continue
            return {
                "symbol": swap_symbol,
                "liq_long_usd_millions": round(liq_long, 2),
                "liq_short_usd_millions": round(liq_short, 2),
                "total_liq_usd_millions": round(liq_long + liq_short, 2),
            }

        elif name == "get_orderbook":
            symbol = inp["symbol"]
            depth = min(int(inp.get("depth", 5)), 20)
            r = await client.get(f"{_OKX_BASE}/api/v5/market/books",
                                 params={"instId": symbol, "sz": str(depth)})
            r.raise_for_status()
            d = r.json()["data"][0]
            bids = [[float(b[0]), float(b[1])] for b in d["bids"][:depth]]
            asks = [[float(a[0]), float(a[1])] for a in d["asks"][:depth]]
            bid_vol = sum(b[1] for b in bids)
            ask_vol = sum(a[1] for a in asks)
            return {
                "symbol": symbol,
                "best_bid": bids[0][0] if bids else None,
                "best_ask": asks[0][0] if asks else None,
                "spread": round(asks[0][0] - bids[0][0], 4) if bids and asks else None,
                "bid_volume": round(bid_vol, 4),
                "ask_volume": round(ask_vol, 4),
                "bid_ask_ratio": round(bid_vol / ask_vol, 3) if ask_vol else None,
                "bids": bids,
                "asks": asks,
            }

        elif name == "get_fear_greed":
            r = await client.get("https://api.alternative.me/fng/?limit=2", timeout=10.0)
            r.raise_for_status()
            entries = r.json().get("data", [])
            result: dict = {}
            if entries:
                result["value"] = int(entries[0]["value"])
                result["label"] = entries[0]["value_classification"]
            if len(entries) > 1:
                result["yesterday_value"] = int(entries[1]["value"])
                result["delta"] = result["value"] - result["yesterday_value"]
            return result

        elif name == "get_open_positions":
            positions = await trading_client.get_open_positions()
            balance = await trading_client.get_balance()
            return {"positions": positions, "available_balance_usdt": round(balance, 2)}

        elif name == "get_bollinger_bands":
            symbol = inp["symbol"]
            bar    = inp.get("bar", "1H")
            period = int(inp.get("period", 20))
            std_dev = float(inp.get("std_dev", 2.0))
            limit  = min(int(inp.get("limit", 50)), 300)
            fetch  = min(period * 3 + limit, 300)
            r = await client.get(
                f"{_OKX_BASE}/api/v5/market/candles",
                params={"instId": symbol, "bar": bar, "limit": str(fetch)},
            )
            r.raise_for_status()
            candles = list(reversed(r.json().get("data", [])))
            closes = pd.Series([float(c[4]) for c in candles])
            sma    = closes.rolling(period).mean()
            std    = closes.rolling(period).std(ddof=0)
            upper  = sma + std_dev * std
            lower  = sma - std_dev * std
            bw     = (upper - lower) / sma  # bandwidth
            pct_b  = (closes - lower) / (upper - lower)  # %B
            rows = []
            for i in range(max(0, len(closes) - limit), len(closes)):
                if pd.isna(sma.iloc[i]):
                    continue
                rows.append({
                    "t":       candles[i][0],
                    "close":   round(float(closes.iloc[i]), 4),
                    "upper":   round(float(upper.iloc[i]), 4),
                    "middle":  round(float(sma.iloc[i]), 4),
                    "lower":   round(float(lower.iloc[i]), 4),
                    "pct_b":   round(float(pct_b.iloc[i]), 4),
                    "bandwidth": round(float(bw.iloc[i]), 4),
                })
            return {"symbol": symbol, "bar": bar, "period": period, "std_dev": std_dev, "bands": rows}

        elif name == "get_macd":
            symbol = inp["symbol"]
            bar    = inp.get("bar", "1H")
            fast   = int(inp.get("fast", 12))
            slow   = int(inp.get("slow", 26))
            signal = int(inp.get("signal", 9))
            limit  = min(int(inp.get("limit", 50)), 300)
            fetch  = min(slow * 3 + signal + limit, 300)
            r = await client.get(
                f"{_OKX_BASE}/api/v5/market/candles",
                params={"instId": symbol, "bar": bar, "limit": str(fetch)},
            )
            r.raise_for_status()
            candles = list(reversed(r.json().get("data", [])))
            closes  = pd.Series([float(c[4]) for c in candles])
            ema_fast   = closes.ewm(span=fast, adjust=False).mean()
            ema_slow   = closes.ewm(span=slow, adjust=False).mean()
            macd_line  = ema_fast - ema_slow
            signal_line = macd_line.ewm(span=signal, adjust=False).mean()
            histogram  = macd_line - signal_line
            rows = []
            for i in range(max(0, len(closes) - limit), len(closes)):
                rows.append({
                    "t":         candles[i][0],
                    "close":     round(float(closes.iloc[i]), 4),
                    "macd":      round(float(macd_line.iloc[i]), 4),
                    "signal":    round(float(signal_line.iloc[i]), 4),
                    "histogram": round(float(histogram.iloc[i]), 4),
                })
            return {"symbol": symbol, "bar": bar, "fast": fast, "slow": slow, "signal": signal, "macd": rows}

        else:
            return {"error": f"Unknown tool: {name}"}


# ── Agent loop (DeepSeek via OpenAI-compatible client) ────────────────────────


async def _run_agent_for_symbol(
    spot_symbol: str,
    swap_symbol: str,
    trading_client,
    settings,
    is_paper: bool,
) -> Optional[dict]:
    """
    Run the DeepSeek tool-calling agent for one symbol.
    Returns the submit_decision payload if action=trade, else None.
    """
    ai_client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=_DEEPSEEK_BASE,
        timeout=120.0,
    )

    messages = [
        {"role": "system", "content": _SYSTEM_TEMPLATE.format(max_leverage=settings.trading_max_leverage)},
        {
            "role": "user",
            "content": (
                f"Analyze {spot_symbol} (perpetual swap: {swap_symbol}) and decide: trade or hold?\n"
                f"Current UTC time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}\n"
                f"Mode: {'PAPER (simulation)' if settings.trading_paper_mode else 'LIVE'}\n"
                + "\nUse tools to gather data, then call submit_decision."
            ),
        },
    ]

    decision = None

    for _ in range(20):
        response = await ai_client.chat.completions.create(
            model=settings.trading_agent_model,
            messages=messages,
            tools=_TOOLS,
            tool_choice="auto",
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}},
        )

        choice = response.choices[0]
        msg = choice.message
        messages.append(msg)

        if choice.finish_reason == "stop" or not msg.tool_calls:
            break

        # Execute all tool calls in this turn
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except Exception:
                args = {}

            logger.info("[%s] Tool call: %s(%s)", spot_symbol, name, list(args.keys()))

            if name == "submit_decision":
                decision = args
                tool_result = {"status": "recorded", "action": args.get("action")}
            else:
                try:
                    tool_result = await _exec_tool(name, args, trading_client)
                except Exception as e:
                    tool_result = {"error": str(e)}
                    logger.error("[%s] Tool %s error: %s", spot_symbol, name, e)

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(tool_result, default=str),
            })

        if decision is not None:
            break

    return decision


# ── Position review (early close) ────────────────────────────────────────────

# Same market-data tools, but without trade-action tools
_REVIEW_TOOLS = [t for t in _TOOLS if t["function"]["name"] not in (
    "submit_decision", "cancel_pending_order"
)] + [
    {
        "type": "function",
        "function": {
            "name": "submit_position_review",
            "description": "Submit your early-close review decision. Call exactly once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action":    {"type": "string", "enum": ["close", "hold"]},
                    "reasoning": {"type": "string"},
                },
                "required": ["action", "reasoning"],
            },
        },
    },
]

_REVIEW_SYSTEM = """You are reviewing an open perpetual futures position to decide if it should be closed early.

YOUR DEFAULT IS HOLD. Only recommend "close" if you are highly confident.

Early close is justified ONLY when BOTH conditions are met:
1. Current price structure makes it VERY UNLIKELY the position reaches TP1 in the next 4-8 hours
   (e.g., key support/resistance broken in the wrong direction, clear trend reversal confirmed on 1H+)
2. Market conditions have FUNDAMENTALLY REVERSED from the original entry thesis

Do NOT close early because:
- The position is in a small loss or small profit
- Price is ranging or consolidating (ranging is NOT a reversal)
- You think there might be a better entry later
- Funding rate or sentiment changed slightly

Gather current market data with the tools provided, then call submit_position_review once.
"""


async def _run_position_review_for_trade(
    info: dict,
    hours_open: float,
    trading_client,
    settings,
) -> Optional[dict]:
    """Ask the DeepSeek agent whether to close one open position early."""
    ai_client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=_DEEPSEEK_BASE,
        timeout=120.0,
    )

    user_content = (
        f"Review this open position and decide: HOLD or CLOSE EARLY?\n\n"
        f"Symbol: {info['symbol']}\n"
        f"Direction: {info['direction'].upper()}\n"
        f"Entry price: {info['entry_price']}\n"
        f"Stop loss: {info['stop_loss']}\n"
        f"TP1: {info['take_profit_1']}  TP2: {info.get('take_profit_2', 'N/A')}\n"
        f"Leverage: {info['leverage']}x\n"
        f"Open for: {hours_open:.1f} hours\n"
        f"Original reasoning: {info.get('agent_reasoning') or 'N/A'}\n\n"
        f"Current UTC time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}\n"
        "Use market data tools to assess current conditions, then call submit_position_review."
    )

    messages = [
        {"role": "system", "content": _REVIEW_SYSTEM},
        {"role": "user",   "content": user_content},
    ]

    decision = None

    for _ in range(15):
        response = await ai_client.chat.completions.create(
            model=settings.trading_agent_model,
            messages=messages,
            tools=_REVIEW_TOOLS,
            tool_choice="auto",
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}},
        )

        choice = response.choices[0]
        msg = choice.message
        messages.append(msg)

        if choice.finish_reason == "stop" or not msg.tool_calls:
            break

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except Exception:
                args = {}

            logger.info("[REVIEW][%s] Tool: %s", info["symbol"], name)

            if name == "submit_position_review":
                decision = args
                tool_result = {"status": "recorded", "action": args.get("action")}
            else:
                try:
                    tool_result = await _exec_tool(name, args, trading_client)
                except Exception as e:
                    tool_result = {"error": str(e)}

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(tool_result, default=str),
            })

        if decision is not None:
            break

    return decision


# ── Risk gates ─────────────────────────────────────────────────────────────────

def _check_daily_loss(settings) -> tuple[bool, float]:
    try:
        with db_session() as db:
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            closed_today = (
                db.query(AutoTrade)
                .filter(
                    AutoTrade.closed_at >= today_start,
                    AutoTrade.status == "closed",
                    AutoTrade.pnl_usdt.isnot(None),
                    AutoTrade.is_paper == (settings.trading_paper_mode),
                )
                .all()
            )
            total_loss = sum(t.pnl_usdt for t in closed_today if t.pnl_usdt and t.pnl_usdt < 0)
            if abs(total_loss) >= settings.trading_daily_loss_limit_usdt:
                return False, total_loss
            return True, total_loss
    except Exception as e:
        logger.error("_check_daily_loss: %s", e)
        return True, 0.0


def _count_open_for_mode(is_paper: bool) -> int:
    try:
        with db_session() as db:
            return db.query(AutoTrade).filter(
                AutoTrade.status.in_(["pending_entry", "open"]),
                AutoTrade.is_paper == is_paper,
            ).count()
    except Exception as e:
        logger.error("_count_open_for_mode: %s", e)
        return 0




# ── DB logging ────────────────────────────────────────────────────────────────

def _log_run(symbol: str, action: str, reasoning: str | None) -> None:
    """Persist one agent decision to the DB."""
    from backend.models.agent_run_log import AgentRunLog
    try:
        with db_session() as db:
            db.add(AgentRunLog(symbol=symbol, action=action, reasoning=reasoning))
    except Exception as e:
        logger.error("_log_run DB write failed: %s", e)


# ── Main entry point ──────────────────────────────────────────────────────────

async def run_trading_agent() -> None:
    settings = get_settings()

    if not settings.trading_enabled:
        logger.info("Trading disabled (TRADING_ENABLED=false), skipping.")
        return

    if not settings.deepseek_api_key:
        logger.error("DEEPSEEK_API_KEY not set — trading agent cannot run.")
        return

    is_paper = settings.trading_paper_mode
    mode_label = "PAPER" if is_paper else "LIVE"

    # In live mode we also need OKX credentials
    if not is_paper and not settings.okx_api_key:
        logger.error("OKX_API_KEY not set and TRADING_PAPER_MODE=false — aborting.")
        return

    ok, daily_loss = _check_daily_loss(settings)
    if not ok:
        logger.warning("[%s] Daily loss limit reached (%.2f USDT). Skipping.", mode_label, abs(daily_loss))
        return

    open_count = _count_open_for_mode(is_paper)
    if open_count >= settings.trading_max_open_positions:
        logger.info("[%s] Max open positions (%d/%d). Skipping.", mode_label, open_count, settings.trading_max_open_positions)
        _log_run("ALL", "skipped", f"Max open positions ({open_count}/{settings.trading_max_open_positions})")
        return

    trading_client = get_trading_client()

    for spot_symbol, swap_symbol in _SYMBOLS:
        if _count_open_for_mode(is_paper) >= settings.trading_max_open_positions:
            break

        logger.info("=== [%s] Running agent for %s ===", mode_label, spot_symbol)

        try:
            decision = await _run_agent_for_symbol(spot_symbol, swap_symbol, trading_client, settings, is_paper)
        except Exception as e:
            logger.error("[%s] Agent error: %s", spot_symbol, e, exc_info=True)
            _log_run(spot_symbol, "error", str(e))
            continue

        if not decision or decision.get("action") != "trade":
            reasoning = decision.get("reasoning", "") if decision else "no decision returned"
            logger.info("[%s][%s] HOLD — %s", mode_label, spot_symbol, reasoning)
            _log_run(spot_symbol, "hold", reasoning)
            continue

        # Validate required fields
        missing = [k for k in ("direction", "entry_price", "stop_loss", "take_profit_1") if k not in decision]
        if missing:
            logger.error("[%s] Decision missing: %s", spot_symbol, missing)
            continue

        direction    = decision["direction"]
        entry_price  = float(decision["entry_price"])
        stop_loss    = float(decision["stop_loss"])
        tp1          = float(decision["take_profit_1"])

        # Validate SL/TP are on the correct side of entry
        if direction == "long" and (stop_loss >= entry_price or tp1 <= entry_price):
            logger.error("[%s] Long SL/TP invalid: entry=%.4f sl=%.4f tp1=%.4f — skipping",
                         spot_symbol, entry_price, stop_loss, tp1)
            continue
        if direction == "short" and (stop_loss <= entry_price or tp1 >= entry_price):
            logger.error("[%s] Short SL/TP invalid: entry=%.4f sl=%.4f tp1=%.4f — skipping",
                         spot_symbol, entry_price, stop_loss, tp1)
            continue
        tp2          = float(decision["take_profit_2"]) if decision.get("take_profit_2") else None
        size_pct     = max(1.0, min(20.0, float(decision.get("size_pct", settings.trading_position_size_pct))))
        leverage     = max(1, min(settings.trading_max_leverage, int(decision.get("leverage", 2))))
        reasoning    = decision.get("reasoning", "")

        logger.info(
            "[%s][%s] TRADE %s entry=%.4f sl=%.4f tp1=%.4f tp2=%s size=%.1f%% lev=%dx",
            mode_label, spot_symbol, direction, entry_price, stop_loss, tp1,
            f"{tp2:.4f}" if tp2 else "None", size_pct, leverage,
        )
        logger.info("[%s][%s] Reasoning: %s", mode_label, spot_symbol, reasoning)
        _log_run(spot_symbol, "trade", reasoning)

        if is_paper:
            # Paper mode — just record, paper monitor task will simulate outcome
            with db_session() as db:
                closed = db.query(AutoTrade).filter(
                    AutoTrade.is_paper == True,
                    AutoTrade.status == "closed",
                    AutoTrade.pnl_usdt.isnot(None),
                ).all()
                cumulative_pnl = sum(t.pnl_usdt for t in closed if t.pnl_usdt is not None)
                current_balance = settings.paper_initial_balance_usdt + cumulative_pnl
                margin = round(current_balance * size_pct / 100, 2)

                db.add(AutoTrade(
                    symbol=swap_symbol,
                    direction=direction,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit_1=tp1,
                    take_profit_2=tp2,
                    size_pct=size_pct,
                    leverage=leverage,
                    margin_used=margin,
                    status="pending_entry",
                    is_paper=True,
                    agent_reasoning=reasoning,
                ))
            logger.info("[PAPER][%s] Trade recorded. lev=%dx margin=%.2f USDT", swap_symbol, leverage, margin)

        else:
            # Live mode — place real OKX order
            try:
                result = await trading_client.place_trade(
                    swap_symbol=swap_symbol,
                    direction=direction,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit_1=tp1,
                    take_profit_2=tp2,
                    size_pct=size_pct,
                    leverage=leverage,
                )
            except Exception as e:
                logger.error("[LIVE][%s] Order placement failed: %s", swap_symbol, e, exc_info=True)
                with db_session() as db:
                    db.add(AutoTrade(
                        symbol=swap_symbol,
                        direction=direction,
                        entry_price=entry_price,
                        stop_loss=stop_loss,
                        take_profit_1=tp1,
                        take_profit_2=tp2,
                        size_pct=size_pct,
                        leverage=leverage,
                        status="failed",
                        close_reason="error",
                        is_paper=False,
                        agent_reasoning=reasoning,
                    ))
                continue

            with db_session() as db:
                db.add(AutoTrade(
                    symbol=swap_symbol,
                    direction=direction,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit_1=tp1,
                    take_profit_2=tp2,
                    size_pct=size_pct,
                    leverage=leverage,
                    contracts=result["contracts"],
                    margin_used=result["margin_used"],
                    okx_order_id=result["order_id"],
                    okx_algo_id=result.get("algo_id"),
                    status="pending_entry",
                    is_paper=False,
                    agent_reasoning=reasoning,
                ))
            logger.info(
                "[LIVE][%s] Order placed ✓ order_id=%s contracts=%s margin=%.2f USDT",
                swap_symbol, result["order_id"], result["contracts"], result["margin_used"],
            )

        await asyncio.sleep(1)
