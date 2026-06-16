"""Scheduled task: run the trading agent every 15 minutes."""
from __future__ import annotations

import asyncio

from backend.utils.logger import get_logger

logger = get_logger("backend.tasks.trading_agent")


async def run_trading_agent_task() -> None:
    from backend.services.trading_agent import run_trading_agent
    try:
        logger.info("Trading agent task starting...")
        await run_trading_agent()
        logger.info("Trading agent task complete.")
    except Exception as e:
        logger.error("Trading agent task failed: %s", e, exc_info=True)
