"""Application configuration.

All secrets and deployment-specific values come from environment variables
(optionally loaded from a local ``.env`` file, which must never be committed).
"""
import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name, default):
    value = os.environ.get(name)
    return int(value) if value else default


class Config:
    # Required in production. create_app() refuses to start without it unless
    # running in debug/testing mode.
    SECRET_KEY = os.environ.get("SECRET_KEY")

    INSTANCE_DIR = Path(os.environ.get("INSTANCE_DIR", BASE_DIR / "instance"))
    DATABASE = os.environ.get("DATABASE_PATH", str(INSTANCE_DIR / "trolley_manager.db"))
    # Uploaded images live outside /static so they are only served to the
    # logged-in admin. Point this at a persistent disk/volume in production.
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", str(INSTANCE_DIR / "uploads"))
    BACKUP_FOLDER = os.environ.get("BACKUP_FOLDER", str(INSTANCE_DIR / "backups"))
    BACKUP_KEEP = _env_int("BACKUP_KEEP", 14)
    LOG_FOLDER = os.environ.get("LOG_FOLDER", str(INSTANCE_DIR / "logs"))

    # Image limits (per file). These are upload limits, not part-count limits.
    MAX_IMAGE_BYTES = _env_int("MAX_IMAGE_MB", 5) * 1024 * 1024
    MAX_IMAGE_DIMENSION = _env_int("MAX_IMAGE_DIMENSION", 1600)
    ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

    # Whole-request limits. They reflect hosting capacity only.
    MAX_CONTENT_LENGTH = _env_int("MAX_REQUEST_MB", 200) * 1024 * 1024
    MAX_FORM_MEMORY_SIZE = _env_int("MAX_FORM_MEMORY_MB", 20) * 1024 * 1024
    MAX_FORM_PARTS = _env_int("MAX_FORM_PARTS", 20000)

    # Sessions / cookies
    SESSION_COOKIE_NAME = "tm_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", True)
    PERMANENT_SESSION_LIFETIME = timedelta(hours=_env_int("SESSION_HOURS", 8))

    # CSRF (Flask-WTF)
    WTF_CSRF_TIME_LIMIT = None  # token valid for the life of the session

    # Login rate limiting (Flask-Limiter). Use a shared backend such as
    # redis://localhost:6379 when running several worker processes.
    LOGIN_RATE_LIMIT = os.environ.get("LOGIN_RATE_LIMIT", "5 per minute;30 per hour")
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_ENABLED = _env_bool("RATELIMIT_ENABLED", True)

    # Set when running behind a reverse proxy (nginx, Caddy, Render, ...).
    TRUST_PROXY = _env_bool("TRUST_PROXY", False)

    # Bump to force browsers / the service worker to fetch fresh static assets.
    ASSET_VERSION = "1.4.0"
