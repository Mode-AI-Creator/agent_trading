"""每分钟抓取一次账户余额快照，用于绘制资产曲线。

Paper 模式：权益 = 初始资金 + 所有已平仓模拟单的累计盈亏（USDT）
Live  模式：读取 OKX 账户总权益 totalEq（含未实现盈亏）
"""
from __future__ import annotations

from backend.config import get_settings
from backend.database import db_session
from backend.models.balance_snapshot import BalanceSnapshot
from backend.models.trade import AutoTrade
from backend.utils.logger import get_logger

logger = get_logger("backend.tasks.balance_snapshot")


async def run_balance_snapshot() -> None:
    settings = get_settings()
    is_paper = settings.trading_paper_mode

    try:
        if is_paper:
            with db_session() as db:
                closed = (
                    db.query(AutoTrade)
                    .filter(
                        AutoTrade.is_paper == True,
                        AutoTrade.status == "closed",
                        AutoTrade.pnl_usdt.isnot(None),
                    )
                    .all()
                )
                cumulative_pnl = sum(t.pnl_usdt for t in closed if t.pnl_usdt is not None)
                equity = settings.paper_initial_balance_usdt + cumulative_pnl
                db.add(BalanceSnapshot(balance_usdt=round(equity, 4), is_paper=True))
        else:
            from backend.services.okx_trading import get_trading_client
            client = get_trading_client()
            equity = await client.get_total_equity()
            with db_session() as db:
                db.add(BalanceSnapshot(balance_usdt=round(equity, 4), is_paper=False))

    except Exception as e:
        logger.error("Balance snapshot failed: %s", e)
