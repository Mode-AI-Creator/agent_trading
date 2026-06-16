"""
Paper trade monitor — runs every 5 minutes.

For each open paper trade, fetches real 1m K-lines from OKX and uses
the kline_backtest logic to simulate whether entry / SL / TP was hit.
TP1 and TP2 both trigger a full close.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from backend.database import db_session
from backend.models.trade import AutoTrade
from backend.utils.logger import get_logger

logger = get_logger("backend.tasks.paper_monitor")

FEE = 0.12   # round-trip fee %

_SWAP_TO_SPOT = {
    "BTC-USDT-SWAP": "BTC-USDT",
    "ETH-USDT-SWAP": "ETH-USDT",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _simulate_trade_from_dict(info: dict) -> dict | None:
    from backend.utils.kline_backtest import run_signal_backtest

    spot_symbol = _SWAP_TO_SPOT.get(info["symbol"])
    if not spot_symbol:
        logger.warning("No spot mapping for %s", info["symbol"])
        return None

    signal_time = info["created_at"]
    if signal_time.tzinfo is None:
        signal_time = signal_time.replace(tzinfo=timezone.utc)

    try:
        return run_signal_backtest(
            symbol=spot_symbol,
            direction=info["direction"],
            entry_price=info["entry_price"],
            stop_loss=info["stop_loss"],
            take_profit_1=info["take_profit_1"],
            take_profit_2=info["take_profit_2"],
            signal_time=signal_time,
            end_time=datetime.now(timezone.utc),
            bar="1m",
            verbose=False,
        )
    except Exception as e:
        logger.error("kline backtest failed for trade %d: %s", info["id"], e)
        return None


async def _get_live_price(symbol: str) -> float | None:
    spot = _SWAP_TO_SPOT.get(symbol)
    if not spot:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(
                "https://www.okx.com/api/v5/market/ticker",
                params={"instId": spot},
            )
            return float(r.json()["data"][0]["last"])
    except Exception as e:
        logger.debug("Live ticker fetch failed: %s", e)
        return None


async def _live_ticker_check(
    symbol: str,
    direction: str,
    ep: float, sl: float, tp1: float, tp2: float | None,
) -> dict | None:
    last = await _get_live_price(symbol)
    if last is None:
        return None

    now = datetime.now(timezone.utc)

    def _closed(outcome: str, exit_price: float) -> dict:
        gross = ((exit_price - ep) if direction == "long" else (ep - exit_price)) / ep * 100
        return {
            "outcome": outcome,
            "entry_time": now,
            "exit_price": exit_price,
            "exit_time": now,
            "pnl_pct_gross": round(gross, 4),
        }

    if direction == "long":
        if last <= sl:
            return _closed("sl_hit", sl)
        if tp2 and last >= tp2:
            return _closed("tp2_hit", tp2)
        if last >= tp1:
            return _closed("tp1_hit", tp1)
        if last > ep:  # price hasn't reached limit entry yet
            return None
        return {"outcome": "pending", "entry_time": now}
    else:
        if last >= sl:
            return _closed("sl_hit", sl)
        if tp2 and last <= tp2:
            return _closed("tp2_hit", tp2)
        if last <= tp1:
            return _closed("tp1_hit", tp1)
        if last < ep:  # price hasn't reached limit entry yet
            return None
        return {"outcome": "pending", "entry_time": now}


# ── update helpers ────────────────────────────────────────────────────────────

def _apply_full_close(trade: AutoTrade, outcome: str, exit_price: float,
                      gross_pct: float, entry_time, exit_time) -> None:
    leverage = trade.leverage or 1
    pnl = round((gross_pct - FEE) * leverage, 4)

    if trade.status == "pending_entry" and entry_time is not None:
        trade.opened_at = entry_time.to_pydatetime() if hasattr(entry_time, "to_pydatetime") else entry_time

    trade.status = "closed"
    trade.close_reason = outcome
    trade.pnl_pct = pnl
    if trade.margin_used:
        trade.pnl_usdt = round(trade.margin_used * pnl / 100, 2)
    if exit_time is not None:
        trade.closed_at = exit_time.to_pydatetime() if hasattr(exit_time, "to_pydatetime") else exit_time

    emoji = "✅" if "tp" in outcome else "❌"
    logger.info("[PAPER] Trade %d %s CLOSED %s %s  pnl=%.2f%%",
                trade.id, trade.symbol, emoji, outcome.upper(), pnl)


def _update_paper_trade(trade: AutoTrade, result: dict) -> None:
    outcome    = result.get("outcome")
    entry_time = result.get("entry_time")
    exit_price = result.get("exit_price")
    exit_time  = result.get("exit_time")
    gross_pct  = result.get("pnl_pct_gross", 0.0)

    if outcome == "no_entry":
        return

    if outcome == "pending":
        if trade.status == "pending_entry" and entry_time is not None:
            trade.status = "open"
            trade.opened_at = entry_time.to_pydatetime() if hasattr(entry_time, "to_pydatetime") else entry_time
            logger.info("[PAPER] Trade %d %s entered @ %.4f",
                        trade.id, trade.symbol, trade.entry_price)
        return

    if outcome == "sl_hit":
        _apply_full_close(trade, outcome, exit_price or trade.stop_loss,
                          gross_pct, entry_time, exit_time)
        return

    if outcome == "tp2_hit":
        _apply_full_close(trade, outcome, exit_price or trade.take_profit_2,
                          gross_pct, entry_time, exit_time)
        return

    if outcome == "tp1_hit":
        _apply_full_close(trade, outcome, exit_price or trade.take_profit_1,
                          gross_pct, entry_time, exit_time)


# ── main task ─────────────────────────────────────────────────────────────────

async def run_paper_monitor() -> None:
    with db_session() as db:
        rows = (
            db.query(AutoTrade)
            .filter(
                AutoTrade.is_paper == True,
                AutoTrade.status.in_(["pending_entry", "open"]),
            )
            .all()
        )
        open_trades = [
            {
                "id": t.id, "symbol": t.symbol, "direction": t.direction,
                "entry_price": t.entry_price, "stop_loss": t.stop_loss,
                "take_profit_1": t.take_profit_1, "take_profit_2": t.take_profit_2,
                "created_at": t.created_at, "status": t.status,
                "margin_used": t.margin_used, "leverage": t.leverage,
            }
            for t in rows
        ]

    if not open_trades:
        return

    logger.info("[PAPER] Checking %d open paper trade(s)...", len(open_trades))

    for info in open_trades:
        result = await asyncio.get_event_loop().run_in_executor(
            None, _simulate_trade_from_dict, info
        )

        if result is None or result.get("outcome") == "no_entry":
            live = await _live_ticker_check(
                info["symbol"], info["direction"],
                info["entry_price"], info["stop_loss"],
                info["take_profit_1"], info["take_profit_2"],
            )
            if live is not None:
                logger.info("[PAPER] Trade %d live-ticker override: %s → %s",
                            info["id"], result and result.get("outcome"), live["outcome"])
                result = live

        if result is None:
            continue

        with db_session() as db:
            t = db.query(AutoTrade).filter(AutoTrade.id == info["id"]).first()
            if t and t.status in ("pending_entry", "open"):
                _update_paper_trade(t, result)
