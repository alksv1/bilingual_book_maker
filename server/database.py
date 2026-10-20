"""Database management and asynchronous SQLite connection helper."""

import logging
from collections.abc import AsyncGenerator
from pathlib import Path
import aiosqlite

from server import config
from server.models import CREATE_TASKS_TABLE_SQL, CREATE_APP_SETTINGS_TABLE_SQL

logger = logging.getLogger("server.database")


def ensure_directories():
    """Ensure all required persistent data directories exist."""
    for directory in [
        config.DATA_DIR,
        config.CONFIG_DIR,
        config.UPLOADS_DIR,
        config.OUTPUT_DIR,
        config.LOGS_DIR,
        config.STATIC_DIR,
    ]:
        directory.mkdir(parents=True, exist_ok=True)


async def init_db(db_path: Path | None = None) -> None:
    """Initialize SQLite database with required tables."""
    ensure_directories()
    target_db = db_path or config.DB_PATH

    logger.info("Initializing database at: %s", target_db)
    async with aiosqlite.connect(target_db) as db:
        # Enable Write-Ahead Logging (WAL) mode for better concurrency
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.executescript(CREATE_TASKS_TABLE_SQL)
        await db.executescript(CREATE_APP_SETTINGS_TABLE_SQL)
        await db.commit()
    logger.info("Database initialized successfully.")


async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Dependency for obtaining an asynchronous SQLite connection with Row factory."""
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        yield db
