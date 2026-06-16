from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ──────────────────────────────────────────────────────────
    app_env: str = "development"
    app_port: int = 8000
    log_level: str = "INFO"

    # ── OKX ──────────────────────────────────────────────────────────────────
    okx_api_key: str = ""
    okx_secret_key: str = ""
    okx_passphrase: str = ""
    okx_referral_code: str = ""
    okx_base_url: str = "https://www.okx.com"

    # ── DeepSeek ──────────────────────────────────────────────────────────────
    deepseek_api_key: str = ""
    deepseek_default_model: str = "deepseek-chat"      # daily analysis
    deepseek_reasoner_model: str = "deepseek-reasoner"  # deep reasoning

    # ── Symbols ───────────────────────────────────────────────────────────────
    target_symbols: str = "BTC-USDT,ETH-USDT,SOL-USDT,BNB-USDT,XRP-USDT"

    # ── Scheduler ─────────────────────────────────────────────────────────────
    enable_scheduler: bool = True
    scheduler_timezone: str = "Asia/Shanghai"

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "sqlite:///./data/kol.db"

    # ── Auto Trading ──────────────────────────────────────────────────────────
    trading_enabled: bool = False               # master kill switch
    trading_paper_mode: bool = True             # True=paper (no orders), False=live
    trading_agent_model: str = "deepseek-chat"  # DeepSeek model used by the agent
    trading_max_leverage: int = 5               # max leverage agent may use (1–N)
    trading_position_size_pct: float = 10.0     # fallback; agent decides 1–20% per trade
    trading_daily_loss_limit_usdt: float = 100.0  # stop trading if daily loss exceeds this
    trading_max_open_positions: int = 10        # max simultaneous open positions
    paper_initial_balance_usdt: float = 10000.0  # starting equity for the paper curve

    # ── Derived helpers ───────────────────────────────────────────────────────
    @property
    def symbols_list(self) -> List[str]:
        return [s.strip() for s in self.target_symbols.split(",") if s.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
