from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler


def get_logger(name: str) -> logging.Logger:
    """Return a named logger with console + rotating file handlers."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured

    from backend.config import get_settings
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Rotating file handler
    os.makedirs("logs", exist_ok=True)
    log_file = os.path.join("logs", f"{name.split('.')[-1]}.log")
    fh = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logger.propagate = False
    return logger
