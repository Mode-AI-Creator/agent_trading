from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import String, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from backend.database import Base


class AgentRunLog(Base):
    __tablename__ = "agent_run_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    symbol: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(20))   # trade | hold | error | skipped
    reasoning: Mapped[str | None] = mapped_column(Text)
