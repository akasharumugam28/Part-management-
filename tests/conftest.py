import io
import os
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402

# Run the suite against PostgreSQL by pointing this at a disposable database
# (its name must contain "test"; every table in it is emptied before each test):
#   $env:TEST_DATABASE_URL = "postgresql://postgres@127.0.0.1:5432/part_manager_test"
# Unset, the suite uses a fresh SQLite file per test. It never uses
# DATABASE_URL / SUPABASE_* from .env, so production data is never touched.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or None
POSTGRES = TEST_DATABASE_URL is not None


def _check_test_database_url(url):
    import psycopg.conninfo

    name = psycopg.conninfo.conninfo_to_dict(url).get("dbname", "")
    if "test" not in name.lower():
        raise pytest.UsageError(
            f"TEST_DATABASE_URL database name {name!r} does not contain 'test'. Refusing to "
            "empty it: use a separate, disposable test database."
        )
    if url == os.environ.get("DATABASE_URL"):
        raise pytest.UsageError("TEST_DATABASE_URL must not be the same as DATABASE_URL.")


def pytest_configure(config):
    if POSTGRES:
        _check_test_database_url(TEST_DATABASE_URL)


def pytest_collection_modifyitems(config, items):
    skip_pg = pytest.mark.skip(reason="SQLite-specific (TEST_DATABASE_URL is set)")
    skip_sqlite = pytest.mark.skip(reason="needs TEST_DATABASE_URL (PostgreSQL)")
    for item in items:
        if POSTGRES and "sqlite_only" in item.keywords:
            item.add_marker(skip_pg)
        if not POSTGRES and "postgres_only" in item.keywords:
            item.add_marker(skip_sqlite)


def reset_postgres(url):
    from database import connect_postgres, init_postgres

    db = connect_postgres(url)
    try:
        init_postgres(db)
        db.execute("TRUNCATE sub_parts, parts, admin, rate_limits RESTART IDENTITY CASCADE")
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _fresh_postgres():
    if POSTGRES:
        reset_postgres(TEST_DATABASE_URL)
    yield


def make_config(tmp_path, **extra):
    cfg = {
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "DATABASE": str(tmp_path / "trolley_manager.db"),
        "UPLOAD_FOLDER": str(tmp_path / "uploads"),
        "BACKUP_FOLDER": str(tmp_path / "backups"),
        "LOG_FOLDER": str(tmp_path / "logs"),
        "WTF_CSRF_ENABLED": False,
        "RATELIMIT_ENABLED": False,
        "SESSION_COOKIE_SECURE": False,
        "DATABASE_URL": TEST_DATABASE_URL,
        # Never talk to a real Supabase project from tests, whatever .env says.
        "SUPABASE_URL": None,
        "SUPABASE_SERVICE_ROLE_KEY": None,
        "RATELIMIT_STORAGE_URI": None,
    }
    cfg.update(extra)
    return cfg


@pytest.fixture
def app(tmp_path):
    app = create_app(make_config(tmp_path))
    result = app.test_cli_runner().invoke(args=["create-admin"])
    assert result.exit_code == 0, result.output
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, username="admin", password="admin@123"):
    return client.post("/login", data={"username": username, "password": password})


@pytest.fixture
def auth_client(client):
    """A client logged in as admin."""
    assert login(client).status_code == 302
    return client


def image_bytes(fmt="PNG", size=(40, 30), color=(13, 148, 136)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, fmt)
    buf.seek(0)
    return buf


def upload(name="photo.png", fmt="PNG", **kw):
    return (image_bytes(fmt, **kw), name)


def part_dict(i=1, **overrides):
    p = {
        "part_name": f"Bolt {i}",
        "part_number": f"PN-{i:04d}",
        "line_storage": f"L-{i}",
        "ols_storage": f"O-{i}",
    }
    p.update(overrides)
    return p


def create_part(client, i=1, files=None, **overrides):
    """POST /parts/create the way the Create Part form does."""
    data = part_dict(i, **overrides)
    if files:
        data.update(files)
    return client.post("/parts/create", data=data, content_type="multipart/form-data")


def db_rows(app, sql, params=()):
    from database import connect, database_target

    conn = connect(database_target(app.config))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def upload_files(app):
    return sorted(p.name for p in Path(app.config["UPLOAD_FOLDER"]).iterdir())
