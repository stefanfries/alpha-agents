import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection

from app.config import settings

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    _setup_logging()
    global _client
    if settings.db.mongodb_uri:
        _client = AsyncIOMotorClient(settings.db.mongodb_uri)
        await _ensure_indexes()
        logger.info("MongoDB connected — database: %s", settings.db.db_name)
    else:
        logger.warning("DB__MONGODB_URI not set — MongoDB unavailable; set it in .env")
    yield
    if _client:
        _client.close()
        _client = None


def _setup_logging() -> None:
    import logging.config
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "app": {"format": settings.log.format},
        },
        "handlers": {
            "console": {"class": "logging.StreamHandler", "formatter": "app"},
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "app",
                "filename": settings.log.file,
                "maxBytes": 10_000_000,
                "backupCount": 3,
                "encoding": "utf-8",
            },
        },
        "loggers": {
            "app": {
                "handlers": ["console", "file"],
                "level": settings.log.level,
                "propagate": False,
            },
        },
    })


def runs_collection() -> AsyncIOMotorCollection:
    if _client is None:
        raise RuntimeError("MongoDB client not initialised — set DB__MONGODB_URI in .env")
    return _client[settings.db.db_name]["pipeline_runs"]


async def update_stage_progress(run_id: str, stage: str, progress: dict | None) -> None:
    await runs_collection().update_one(
        {"run_id": run_id},
        {"$set": {f"stages.{stage}.progress": progress}},
    )


async def _ensure_indexes() -> None:
    coll = runs_collection()
    await coll.create_index("run_id", unique=True)
    await coll.create_index("created_at")
    await coll.create_index("status")
