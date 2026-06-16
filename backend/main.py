"""FastAPI application entry point — trading agent only."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.router_trades import router as trades_router
from backend.config import get_settings
from backend.database import create_all_tables
from backend.utils.logger import get_logger

logger = get_logger("backend.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("Starting trading agent backend (env=%s)", settings.app_env)

    create_all_tables()
    logger.info("Database ready")

    if settings.enable_scheduler:
        from backend.services.scheduler_service import get_scheduler_service
        get_scheduler_service().start()

    yield

    if settings.enable_scheduler:
        from backend.services.scheduler_service import get_scheduler_service
        get_scheduler_service().shutdown()

    from backend.services.okx_client import get_okx_client
    await get_okx_client().close()
    logger.info("Trading agent backend shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Trading Agent API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(trades_router)

    @app.get("/health")
    def health():
        return {"status": "ok", "env": settings.app_env}

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=settings.app_port,
        reload=False,
        log_level=settings.log_level.lower(),
    )
