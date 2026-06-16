from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, Float
from sqlalchemy.orm import Mapped, mapped_column
from backend.database import Base


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    balance_usdt: Mapped[float] = mapped_column(Float)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True)
