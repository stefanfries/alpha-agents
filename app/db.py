import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection

from app.config import settings

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None
_startup_tasks: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    _setup_logging()
    global _client
    if settings.db.mongodb_uri:
        _client = AsyncIOMotorClient(settings.db.mongodb_uri)
        await _ensure_indexes()
        await _resume_running_executions()
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


def warrant_underlying_map_collection() -> AsyncIOMotorCollection:
    if _client is None:
        raise RuntimeError("MongoDB client not initialised — set DB__MONGODB_URI in .env")
    return _client[settings.db.db_name]["warrant_underlying_map"]


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
    await warrant_underlying_map_collection().create_index("warrant_isin", sparse=True)
    await warrant_underlying_map_collection().create_index("warrant_wkn", sparse=True)
    await warrant_underlying_map_collection().create_index("checked_at")


async def _resume_running_executions() -> None:
    """Resume in-flight execution stages after app restart.

    Without this recovery, executions that were marked as running before shutdown
    stay stuck forever because no background task is re-scheduled on startup.
    """
    from app.orchestrator import get_pipeline

    coll = executions_collection()
    pipeline = get_pipeline()
    resumed = 0

    async for run in coll.find(
        {"status": "running"},
        {
            "_id": 0,
            "execution_id": 1,
            "current_stage": 1,
            "stages": 1,
        },
    ):
        execution_id = run.get("execution_id")
        stage = run.get("current_stage")
        if not execution_id or not stage:
            continue

        stage_status = (run.get("stages", {}).get(stage) or {}).get("status")
        if stage_status != "running":
            continue

        logger.warning(
            "Resuming execution %s at stage %s after startup recovery",
            execution_id,
            stage,
        )
        task = asyncio.create_task(pipeline.run_stage(execution_id, stage))
        _startup_tasks.add(task)
        task.add_done_callback(_startup_tasks.discard)
        resumed += 1

    if resumed:
        logger.info("Startup recovery resumed %d running execution(s)", resumed)
