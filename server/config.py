"""Server package configuration and path utilities."""

import os
from pathlib import Path

# Base directories
BASE_DIR = Path(__file__).resolve().parent.parent

# Persistent data directory (configurable via BBM_DATA_DIR)
DATA_DIR = Path(os.getenv("BBM_DATA_DIR", BASE_DIR / "data")).resolve()

# Subdirectories for data storage
CONFIG_DIR = DATA_DIR / "config"
UPLOADS_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "output"
LOGS_DIR = DATA_DIR / "logs"

# Database & configuration file paths
DB_PATH = DATA_DIR / "bbm_tasks.db"
PROVIDERS_CONFIG_PATH = CONFIG_DIR / "bbm_providers.json"
GENERAL_CONFIG_PATH = CONFIG_DIR / "general.json"

# Static files directory
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Dual-mode authentication settings
# When AUTH_PASSWORD is empty or unset, the server runs in open (unprotected) mode.
AUTH_PASSWORD = os.getenv("AUTH_PASSWORD", "").strip()
SESSION_COOKIE_NAME = "bbm_session"


def is_protected_mode() -> bool:
    """Check if the server is running in protected mode (AUTH_PASSWORD set)."""
    return bool(AUTH_PASSWORD)


# Server runtime host and port
HOST = os.getenv("BBM_HOST", "0.0.0.0")
PORT = int(os.getenv("BBM_PORT", "8080"))

# CORS settings (comma-separated list or '*' for wildcard)
CORS_ORIGINS_STR = os.getenv("CORS_ORIGINS", "*")
CORS_ORIGINS = [
    origin.strip() for origin in CORS_ORIGINS_STR.split(",") if origin.strip()
]
