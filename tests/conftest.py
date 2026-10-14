import io
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402

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
    from database import connect

    conn = connect(app.config["DATABASE"])
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def upload_files(app):
    return sorted(p.name for p in Path(app.config["UPLOAD_FOLDER"]).iterdir())
