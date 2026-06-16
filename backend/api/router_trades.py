"""Paper / live trade records API."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models.trade import AutoTrade

router = APIRouter(prefix="/api/trades", tags=["trades"])


# ── Response schemas ──────────────────────────────────────────────────────────

class TradeOut(BaseModel):
    id: int
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: Optional[float]
    size_pct: float
    leverage: int
    contracts: Optional[float]
    margin_used: Optional[float]
    status: str
    close_reason: Optional[str]
    pnl_pct: Optional[float]
    pnl_usdt: Optional[float]
    is_paper: bool
    partial_closed: bool
    peak_price: Optional[float]
    partial_pnl_pct: Optional[float]
    agent_reasoning: Optional[str]
    created_at: datetime
    opened_at: Optional[datetime]
    closed_at: Optional[datetime]

    class Config:
        from_attributes = True


class TradeSummary(BaseModel):
    total: int
    pending: int
    open: int
    closed: int
    wins: int
    losses: int
    win_rate_pct: Optional[float]
    avg_pnl_pct: Optional[float]
    total_pnl_pct: Optional[float]
    total_pnl_usdt: Optional[float]
    is_paper: bool


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/", response_model=List[TradeOut])
def list_trades(
    paper: bool = Query(True, description="True=模拟盘, False=实盘"),
    limit: int = Query(50, le=200),
    status: Optional[str] = Query(None, description="pending_entry | open | closed | failed"),
    db: Session = Depends(get_db),
):
    q = db.query(AutoTrade).filter(AutoTrade.is_paper == paper)
    if status:
        q = q.filter(AutoTrade.status == status)
    return q.order_by(desc(AutoTrade.created_at)).limit(limit).all()


@router.get("/summary", response_model=TradeSummary)
def get_summary(
    paper: bool = Query(True),
    db: Session = Depends(get_db),
):
    trades = db.query(AutoTrade).filter(AutoTrade.is_paper == paper).all()

    closed = [t for t in trades if t.status == "closed"]
    wins   = [t for t in closed if t.close_reason and "tp" in t.close_reason]
    losses = [t for t in closed if t.close_reason == "sl_hit"]

    pnl_values = [t.pnl_pct for t in closed if t.pnl_pct is not None]
    pnl_usdt   = [t.pnl_usdt for t in closed if t.pnl_usdt is not None]

    return TradeSummary(
        total=len(trades),
        pending=sum(1 for t in trades if t.status == "pending_entry"),
        open=sum(1 for t in trades if t.status == "open"),
        closed=len(closed),
        wins=len(wins),
        losses=len(losses),
        win_rate_pct=round(len(wins) / len(closed) * 100, 1) if closed else None,
        avg_pnl_pct=round(sum(pnl_values) / len(pnl_values), 2) if pnl_values else None,
        total_pnl_pct=round(sum(pnl_values), 2) if pnl_values else None,
        total_pnl_usdt=round(sum(pnl_usdt), 2) if pnl_usdt else None,
        is_paper=paper,
    )


@router.post("/run-agent-now")
async def trigger_agent_now():
    """立即触发一次 trading agent（无需等待定时器）。"""
    from backend.tasks.task_trading_agent import run_trading_agent_task
    asyncio.create_task(run_trading_agent_task())
    return {"status": "triggered", "message": "Agent started in background"}


@router.post("/run-paper-monitor-now")
async def trigger_paper_monitor():
    """立即触发一次 paper monitor（检查所有模拟仓位）。"""
    from backend.tasks.task_paper_monitor import run_paper_monitor
    asyncio.create_task(run_paper_monitor())
    return {"status": "triggered", "message": "Paper monitor started in background"}


@router.post("/run-position-review-now")
async def trigger_position_review():
    """立即触发一次持仓评审（>4h 持仓提交 agent 判断是否提前平仓）。"""
    from backend.tasks.task_position_review import run_position_review
    asyncio.create_task(run_position_review())
    return {"status": "triggered", "message": "Position review started in background"}


@router.get("/agent-log")
async def get_agent_log(limit: int = Query(30, le=100), db: Session = Depends(get_db)):
    """返回最近的 agent 决策日志 + 服务运行时长。"""
    from backend.services.trading_agent import _SERVER_START
    from backend.models.agent_run_log import AgentRunLog
    uptime = int((datetime.now(timezone.utc) - _SERVER_START).total_seconds())
    rows = db.query(AgentRunLog).order_by(desc(AgentRunLog.ts)).limit(limit).all()
    return {
        "uptime_seconds": uptime,
        "server_start": _SERVER_START.isoformat(),
        "log": [
            {"ts": r.ts.isoformat(), "symbol": r.symbol, "action": r.action, "reasoning": r.reasoning}
            for r in rows
        ],
    }


@router.get("/balance-history")
def get_balance_history(
    paper: bool = Query(True),
    hours: int = Query(24, ge=1, le=168),
    db: Session = Depends(get_db),
):
    """返回最近 N 小时的余额快照，用于绘制资产曲线。"""
    from backend.models.balance_snapshot import BalanceSnapshot
    from backend.config import get_settings
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = (
        db.query(BalanceSnapshot)
        .filter(BalanceSnapshot.is_paper == paper, BalanceSnapshot.ts >= cutoff)
        .order_by(BalanceSnapshot.ts)
        .all()
    )
    if paper:
        initial = get_settings().paper_initial_balance_usdt
    else:
        first = (
            db.query(BalanceSnapshot)
            .filter(BalanceSnapshot.is_paper == False)
            .order_by(BalanceSnapshot.ts)
            .first()
        )
        initial = first.balance_usdt if first else (rows[0].balance_usdt if rows else 0)
    return {
        "initial_balance": initial,
        "points": [
            {"ts": int(r.ts.timestamp()), "value": r.balance_usdt}
            for r in rows
        ],
    }


@router.get("/market-snapshot")
async def get_market_snapshot():
    """实时市场快照：BTC/ETH 资金费率、多空比、恐惧贪婪指数。"""
    OKX = "https://www.okx.com"
    FNG = "https://api.alternative.me/fng/?limit=1"

    async def fetch(client: httpx.AsyncClient, url: str, **params):
        try:
            r = await client.get(url, params=params, timeout=8.0)
            return r.json()
        except Exception:
            return {}

    async with httpx.AsyncClient() as c:
        btc_fr, eth_fr, btc_ls, eth_ls, fng_raw = await asyncio.gather(
            fetch(c, f"{OKX}/api/v5/public/funding-rate", instId="BTC-USDT-SWAP"),
            fetch(c, f"{OKX}/api/v5/public/funding-rate", instId="ETH-USDT-SWAP"),
            fetch(c, f"{OKX}/api/v5/rubik/stat/contracts/long-short-account-ratio-contract-top-trader",
                  instId="BTC-USDT-SWAP", period="1H"),
            fetch(c, f"{OKX}/api/v5/rubik/stat/contracts/long-short-account-ratio-contract-top-trader",
                  instId="ETH-USDT-SWAP", period="1H"),
            fetch(c, FNG),
        )

    def fr(raw):
        data = raw.get("data", [])
        if not data:
            return None
        return round(float(data[0].get("fundingRate", 0)) * 100, 4)

    def ls(raw):
        data = raw.get("data", [])
        if not data:
            return None
        try:
            return round(float(data[0][1]), 3)
        except Exception:
            return None

    fng_data = fng_raw.get("data", [])
    fng = int(fng_data[0]["value"]) if fng_data else None
    fng_label = fng_data[0].get("value_classification") if fng_data else None

    return {
        "btc": {"funding_rate_pct": fr(btc_fr), "long_short_ratio": ls(btc_ls)},
        "eth": {"funding_rate_pct": fr(eth_fr), "long_short_ratio": ls(eth_ls)},
        "fear_greed": fng,
        "fear_greed_label": fng_label,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


# ── Must be last — catches /{trade_id} after all named GET routes ─────────────
@router.get("/{trade_id}", response_model=TradeOut)
def get_trade(trade_id: int, db: Session = Depends(get_db)):
    from fastapi import HTTPException
    t = db.query(AutoTrade).filter(AutoTrade.id == trade_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Trade not found")
    return t
