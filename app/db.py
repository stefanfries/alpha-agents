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


def executions_collection() -> AsyncIOMotorCollection:
    if _client is None:
        raise RuntimeError("MongoDB client not initialised — set DB__MONGODB_URI in .env")
    return _client[settings.db.db_name]["executions"]


async def update_stage_progress(execution_id: str, stage: str, progress: dict | None) -> None:
    await executions_collection().update_one(
        {"execution_id": execution_id},
        {"$set": {f"stages.{stage}.progress": progress}},
    )


def quant_systems_collection() -> AsyncIOMotorCollection:
    if _client is None:
        raise RuntimeError("MongoDB client not initialised — set DB__MONGODB_URI in .env")
    return _client[settings.db.db_name]["quant_systems"]


def virtual_depots_collection() -> AsyncIOMotorCollection:
    if _client is None:
        raise RuntimeError("MongoDB client not initialised — set DB__MONGODB_URI in .env")
    return _client[settings.db.db_name]["virtual_depots"]


def virtual_depot_snapshots_collection() -> AsyncIOMotorCollection:
    if _client is None:
        raise RuntimeError("MongoDB client not initialised — set DB__MONGODB_URI in .env")
    return _client[settings.db.db_name]["virtual_depot_snapshots"]


def virtual_depot_transactions_collection() -> AsyncIOMotorCollection:
    if _client is None:
        raise RuntimeError("MongoDB client not initialised — set DB__MONGODB_URI in .env")
    return _client[settings.db.db_name]["virtual_depot_transactions"]


def warrant_availability_collection() -> AsyncIOMotorCollection:
    if _client is None:
        raise RuntimeError("MongoDB client not initialised — set DB__MONGODB_URI in .env")
    return _client[settings.db.db_name]["warrant_availability"]


def finance_db():  # type: ignore[return]
    """Read-only access to the finance database (written by comdirect_api)."""
    if _client is None:
        raise RuntimeError("MongoDB client not initialised — set DB__MONGODB_URI in .env")
    return _client[settings.db.finance_db_name]


async def _ensure_indexes() -> None:
    await executions_collection().create_index("execution_id", unique=True)
    await executions_collection().create_index("created_at")
    await executions_collection().create_index("status")
    await executions_collection().create_index("quant_system_id")

    await quant_systems_collection().create_index("quant_system_id", unique=True)
    await quant_systems_collection().create_index("name", unique=True)
    await quant_systems_collection().create_index("status")

    await virtual_depots_collection().create_index("depot_id", unique=True)
    await virtual_depots_collection().create_index("name", unique=True)

    await virtual_depot_snapshots_collection().create_index([("depot_id", 1), ("recorded_at", -1)])
    await virtual_depot_transactions_collection().create_index("transaction_id", unique=True)
    await virtual_depot_transactions_collection().create_index([("depot_id", 1), ("booking_date", -1)])

    await warrant_availability_collection().create_index("override_isin", sparse=True)
