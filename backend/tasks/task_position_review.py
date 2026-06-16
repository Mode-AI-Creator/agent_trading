"""
Position review task — runs every 15 minutes.

For each open paper trade held longer than REVIEW_AFTER_HOURS, asks the
trading agent whether to close early. The agent is strongly biased toward
HOLD and may only recommend early close if:
  1. The price structure makes TP very unlikely to be reached, AND
  2. Market conditions have fundamentally reversed from the original entry thesis.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from backend.config import get_settings
from backend.database import db_session
from backend.models.trade import AutoTrade
from backend.utils.logger import get_logger

logger = get_logger("backend.tasks.position_review")

REVIEW_AFTER_HOURS = 4
FEE = 0.12


async def run_position_review() -> None:
    settings = get_settings()
    if not settings.trading_enabled or not settings.deepseek_api_key:
        return

    from backend.services.trading_agent import _run_position_review_for_trade
    from backend.services.okx_trading import get_trading_client
    from backend.tasks.task_paper_monitor import _get_live_price

    is_paper = settings.trading_paper_mode
    now = datetime.now(timezone.utc)

    with db_session() as db:
        rows = db.query(AutoTrade).filter(
            AutoTrade.is_paper == is_paper,
            AutoTrade.status == "open",
        ).all()
        candidates = [
            {
                "id": t.id, "symbol": t.symbol, "direction": t.direction,
                "entry_price": t.entry_price, "stop_loss": t.stop_loss,
                "take_profit_1": t.take_profit_1, "take_profit_2": t.take_profit_2,
                "leverage": t.leverage or 1, "margin_used": t.margin_used,
                "opened_at": t.opened_at, "agent_reasoning": t.agent_reasoning,
                "partial_closed": t.partial_closed, "partial_pnl_pct": t.partial_pnl_pct,
            }
            for t in rows
            if t.opened_at and (
                now - (t.opened_at.replace(tzinfo=timezone.utc)
                       if t.opened_at.tzinfo is None else t.opened_at)
            ).total_seconds() >= REVIEW_AFTER_HOURS * 3600
        ]

    if not candidates:
        return

    logger.info("[REVIEW] %d position(s) eligible for early-close review", len(candidates))
    trading_client = get_trading_client()

    for info in candidates:
        opened_at = info["opened_at"]
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        hours_open = (now - opened_at).total_seconds() / 3600

        logger.info("[REVIEW] Reviewing trade #%d %s %s (open %.1fh)",
                    info["id"], info["symbol"], info["direction"], hours_open)

        try:
            decision = await _run_position_review_for_trade(
                info, hours_open, trading_client, settings
            )
        except Exception as e:
            logger.error("[REVIEW] Trade #%d review failed: %s", info["id"], e, exc_info=True)
            continue

        reasoning = decision.get("reasoning", "") if decision else "no decision"
        if not decision or decision.get("action") != "close":
            logger.info("[REVIEW] Trade #%d HOLD — %s", info["id"], reasoning)
            continue

        # Agent decided to close early — send close order for live trades first
        if not settings.trading_paper_mode:
            ok = await trading_client.close_position(info["symbol"], info["direction"])
            if not ok:
                logger.error("[REVIEW] Trade #%d: OKX close_position failed, skipping DB update",
                             info["id"])
                continue
            logger.info("[REVIEW] Trade #%d: OKX position closed", info["id"])

        # Fetch live price then update DB
        live_price = await _get_live_price(info["symbol"])
        if live_price is None:
            logger.warning("[REVIEW] Trade #%d: could not get live price, skipping", info["id"])
            continue

        entry = info["entry_price"]
        lev   = info["leverage"]
        gross = ((live_price - entry) if info["direction"] == "long"
                 else (entry - live_price)) / entry * 100

        with db_session() as db:
            t = db.query(AutoTrade).filter(AutoTrade.id == info["id"]).first()
            if not t or t.status != "open":
                continue

            total_pnl = round((gross - FEE) * lev, 4)

            t.status = "closed"
            t.close_reason = "agent_early_close"
            t.pnl_pct = total_pnl
            if t.margin_used:
                t.pnl_usdt = round(t.margin_used * total_pnl / 100, 2)
            t.closed_at = now

            emoji = "✅" if total_pnl > 0 else "❌"
            logger.info(
                "[REVIEW] Trade #%d %s EARLY CLOSED %s @ %.4f  pnl=%.2f%%  reason: %s",
                t.id, t.symbol, emoji, live_price, total_pnl, reasoning[:120],
            )

        await asyncio.sleep(2)
