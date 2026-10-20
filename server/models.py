"""Data models, table schemas and enum definitions for bilingual_book_maker web service."""

from enum import Enum
from typing import Optional
from dataclasses import dataclass


class TaskStatus(str, Enum):
    """Lifecycle states of a book translation task."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FileType(str, Enum):
    """Supported file formats."""

    EPUB = "epub"
    PDF = "pdf"
    TXT = "txt"
    SRT = "srt"


class BilingualStyle(str, Enum):
    """Layout style for bilingual output."""

    DEFAULT = "default"  # translation below original
    REVERSED = "reversed"  # translation above original
    ONLY = "only"  # monolingual translation only


# DDL statement for `tasks` table
CREATE_TASKS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    output_path TEXT,
    file_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    progress_current INTEGER DEFAULT 0,
    progress_total INTEGER DEFAULT 0,
    model_provider TEXT,
    target_language TEXT DEFAULT 'zh-hans',
    prompt_template TEXT,
    error_message TEXT,
    log_path TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
"""

# DDL statement for `app_settings` table (key-value store)
CREATE_APP_SETTINGS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


@dataclass
class TaskDBModel:
    id: str
    filename: str
    file_path: str
    output_path: Optional[str]
    file_type: str
    status: str
    progress_current: int
    progress_total: int
    model_provider: Optional[str]
    target_language: str
    prompt_template: Optional[str]
    error_message: Optional[str]
    log_path: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class AppSettingDBModel:
    key: str
    value: str
    updated_at: Optional[str] = None
