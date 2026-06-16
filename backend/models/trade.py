"""Auto-trade tracking model — one row per agent-initiated order."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class AutoTrade(Base):
    __tablename__ = "auto_trade"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Trade parameters
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)   # BTC-USDT-SWAP
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # long | short
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit_1: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit_2: Mapped[float | None] = mapped_column(Float)

    # Position sizing
    size_pct: Mapped[float] = mapped_column(Float, default=10.0)   # % of balance used
    leverage: Mapped[int] = mapped_column(Integer, default=3)
    contracts: Mapped[float | None] = mapped_column(Float)          # number of contracts
    margin_used: Mapped[float | None] = mapped_column(Float)        # USDT

    # OKX order IDs
    okx_order_id: Mapped[str | None] = mapped_column(String(64))    # entry limit order
    okx_algo_id: Mapped[str | None] = mapped_column(String(64))     # SL/TP algo order

    # Lifecycle
    # pending_entry → open → closed | cancelled | failed
    status: Mapped[str] = mapped_column(String(20), default="pending_entry")
    close_reason: Mapped[str | None] = mapped_column(String(20))    # sl_hit | tp1_hit | tp2_hit | manual | expired | error

    # P&L (filled after close)
    pnl_pct: Mapped[float | None] = mapped_column(Float)
    pnl_usdt: Mapped[float | None] = mapped_column(Float)

    # Reserved columns (not currently written; kept for schema compatibility)
    partial_closed: Mapped[bool] = mapped_column(Boolean, default=False)
    peak_price: Mapped[float | None] = mapped_column(Float)
    partial_pnl_pct: Mapped[float | None] = mapped_column(Float)

    # Agent reasoning snapshot
    agent_reasoning: Mapped[str | None] = mapped_column(Text)

    # Paper trading flag
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True)   # True=模拟, False=实盘

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime)    # entry order filled
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
