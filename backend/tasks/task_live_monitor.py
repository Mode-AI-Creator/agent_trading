"""
Live trade monitor — runs every 5 minutes (only when TRADING_PAPER_MODE=false).

Syncs live trade lifecycle with OKX:
  1. pending_entry → open   (entry limit order filled on OKX)
  2. pending_entry → cancelled (order cancelled on OKX)
  3. open → closed          (TP/SL algo fired, position gone from OKX)
"""
from __future__ import annotations

from datetime import datetime, timezone

from backend.config import get_settings
from backend.database import db_session
from backend.models.trade import AutoTrade
from backend.utils.logger import get_logger

logger = get_logger("backend.tasks.live_monitor")

FEE = 0.12  # round-trip fee %


async def _get_close_price(trading_client, swap_symbol: str, direction: str) -> float | None:
    """Find the most recent closing fill for this symbol/direction."""
    fills = await trading_client.get_recent_fills(swap_symbol, limit=20)
    # Closing a long = sell side; closing a short = buy side
    close_side = "sell" if direction == "long" else "buy"
    for fill in fills:
        if fill.get("side") == close_side and fill.get("price", 0) > 0:
            return fill["price"]
    return None


async def run_live_monitor() -> None:
    settings = get_settings()
    if not settings.trading_enabled or settings.trading_paper_mode:
        return
    if not settings.okx_api_key:
        return

    from backend.services.okx_trading import get_trading_client
    trading_client = get_trading_client()
    now = datetime.now(timezone.utc)

    # ── Step 1: pending_entry → open / cancelled ──────────────────────────────
    with db_session() as db:
        pending = db.query(AutoTrade).filter(
            AutoTrade.is_paper == False,
            AutoTrade.status == "pending_entry",
            AutoTrade.okx_order_id.isnot(None),
        ).all()
        pending_rows = [
            {"id": t.id, "symbol": t.symbol, "order_id": t.okx_order_id}
            for t in pending
        ]

    for row in pending_rows:
        try:
            st = await trading_client.get_order_status(row["symbol"], row["order_id"])
            if st["status"] == "filled":
                with db_session() as db:
                    t = db.query(AutoTrade).filter(AutoTrade.id == row["id"]).first()
                    if t and t.status == "pending_entry":
                        t.status = "open"
                        t.opened_at = now
                        if st.get("avg_fill_px"):
                            t.entry_price = st["avg_fill_px"]
                logger.info("[LIVE] Trade #%d %s ENTERED @ %.4f",
                            row["id"], row["symbol"], st.get("avg_fill_px") or 0)

            elif st["status"] in ("canceled", "mmp_canceled"):
                with db_session() as db:
                    t = db.query(AutoTrade).filter(AutoTrade.id == row["id"]).first()
                    if t and t.status == "pending_entry":
                        t.status = "cancelled"
                        t.close_reason = "okx_cancelled"
                logger.info("[LIVE] Trade #%d cancelled by OKX", row["id"])

        except Exception as e:
            logger.error("[LIVE] Order check failed trade #%d: %s", row["id"], e)

    # ── Step 2: open → closed (position gone from OKX) ───────────────────────
    with db_session() as db:
        open_trades = db.query(AutoTrade).filter(
            AutoTrade.is_paper == False,
            AutoTrade.status == "open",
        ).all()
        open_rows = [
            {
                "id": t.id, "symbol": t.symbol, "direction": t.direction,
                "entry_price": t.entry_price, "leverage": t.leverage or 1,
                "margin_used": t.margin_used,
            }
            for t in open_trades
        ]

    if not open_rows:
        return

    try:
        live_positions = await trading_client.get_open_positions()
        live_symbols = {p["symbol"] for p in live_positions}
    except Exception as e:
        logger.error("[LIVE] get_open_positions failed: %s", e)
        return

    for row in open_rows:
        if row["symbol"] in live_symbols:
            continue  # still open on OKX

        # Position closed on OKX — find close price from fills
        close_price = await _get_close_price(trading_client, row["symbol"], row["direction"])
        if close_price is None:
            logger.warning("[LIVE] Trade #%d %s: position gone but no closing fill found",
                           row["id"], row["symbol"])
            continue

        entry = row["entry_price"]
        direction = row["direction"]
        lev = row["leverage"]
        gross = ((close_price - entry) if direction == "long" else (entry - close_price)) / entry * 100
        pnl = round((gross - FEE) * lev, 4)
        close_reason = "tp1_hit" if pnl > 0 else "sl_hit"

        with db_session() as db:
            t = db.query(AutoTrade).filter(AutoTrade.id == row["id"]).first()
            if t and t.status == "open":
                t.status = "closed"
                t.close_reason = close_reason
                t.pnl_pct = pnl
                t.closed_at = now
                if t.margin_used:
                    t.pnl_usdt = round(t.margin_used * pnl / 100, 2)

        emoji = "✅" if pnl > 0 else "❌"
        logger.info("[LIVE] Trade #%d %s CLOSED %s @ %.4f  pnl=%.2f%%",
                    row["id"], row["symbol"], emoji, close_price, pnl)
