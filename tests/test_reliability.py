"""Persistence across restarts, failed DB operations, backups, schema integrity, migration."""
import sqlite3
from pathlib import Path

import pytest

import models
from app import create_app
from conftest import create_part, db_rows, login, make_config, upload, upload_files


def test_data_persists_after_restart(tmp_path):
    cfg = make_config(tmp_path)
    app1 = create_app(cfg)
    app1.test_cli_runner().invoke(args=["create-admin"])
    c1 = app1.test_client()
    login(c1)
    create_part(c1, 1, {"image": upload()})
    create_part(c1, 2)
    del app1, c1

    app2 = create_app(cfg)  # simulated restart: new app, same database file
    c2 = app2.test_client()
    assert login(c2).location == "/"
    page = c2.get("/").get_data(as_text=True)
    assert "PN-0001" in page and "PN-0002" in page
    image = db_rows(app2, "SELECT image_filename FROM parts WHERE id = 1")[0][0]
    assert c2.get(f"/uploads/{image}").status_code == 200


def test_failed_create_shows_generic_error(app, auth_client, monkeypatch):
    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(models, "create_part", boom)
    r = create_part(auth_client, 1, {"image": upload()})
    assert r.status_code == 500
    html = r.get_data(as_text=True)
    assert "Something went wrong. Please try again." in html
    assert "database is locked" not in html  # no internal details leaked
    assert "created successfully" not in html
    assert db_rows(app, "SELECT COUNT(*) FROM parts")[0][0] == 0
    assert upload_files(app) == []  # saved image cleaned up


@pytest.mark.parametrize(
    "func,path,data",
    [("update_part", "/parts/1/update", {"part_name": "Nut", "part_number": "P", "line_storage": "L",
                                         "ols_storage": "O"}),
     ("delete_part", "/parts/1/delete", {})],
)
def test_failed_edit_delete_show_generic_error(app, auth_client, monkeypatch, func, path, data):
    create_part(auth_client, 1)

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(models, func, boom)
    html = auth_client.post(path, data=data, follow_redirects=True).get_data(as_text=True)
    assert "Something went wrong. Please try again." in html
    assert "database is locked" not in html
    assert "updated successfully" not in html and "deleted successfully" not in html
    assert db_rows(app, "SELECT part_name FROM parts")[0][0] == "Bolt 1"


def test_unexpected_error_returns_generic_500(app, auth_client, monkeypatch):
    app.config["PROPAGATE_EXCEPTIONS"] = False

    def boom(*_a, **_k):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(models, "list_parts", boom)
    r = auth_client.get("/")
    assert r.status_code == 500
    assert b"Something went wrong. Please try again." in r.data
    assert b"secret internal detail" not in r.data


def test_check_constraints_enforced(app):
    from database import IntegrityError, connect, database_target

    conn = connect(database_target(app.config))
    with pytest.raises(IntegrityError):  # CHECK constraint on blank values
        conn.execute(
            "INSERT INTO parts (part_name, part_number, line_storage, ols_storage, created_at, updated_at) "
            "VALUES ('  ', 'b', 'c', 'd', 'x', 'x')"
        )
    conn.close()


@pytest.mark.sqlite_only
def test_schema_tables_and_indexes(app):
    cols = {r["name"] for r in db_rows(app, "PRAGMA table_info(parts)")}
    assert cols == {"id", "part_name", "part_number", "line_storage", "ols_storage",
                    "image_filename", "trolley_qty", "created_at", "updated_at",
                    "total_trolleys", "empty_trolleys"}
    cols = {r["name"] for r in db_rows(app, "PRAGMA table_info(admin)")}
    assert cols == {"id", "username", "password_hash", "must_change_password", "created_at"}
    tables = {r["name"] for r in db_rows(app, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "trolleys" not in tables
    indexes = {r["name"] for r in db_rows(app, "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"idx_parts_name", "idx_parts_part_number"} <= indexes


OLD_SCHEMA = """
CREATE TABLE trolleys (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trolley_number TEXT NOT NULL UNIQUE,
    trolley_name TEXT NOT NULL, line_storage TEXT NOT NULL, ols_storage TEXT NOT NULL,
    image_filename TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trolley_id INTEGER NOT NULL REFERENCES trolleys (id) ON DELETE CASCADE,
    part_name TEXT NOT NULL, part_number TEXT NOT NULL, line_storage TEXT NOT NULL,
    ols_storage TEXT NOT NULL, image_filename TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX idx_parts_trolley_id ON parts (trolley_id);
CREATE INDEX idx_parts_part_number ON parts (part_number);
INSERT INTO trolleys VALUES (1, 'T1', 'Trolley', 'L', 'O', 't.png', 'c', 'u');
INSERT INTO parts VALUES (5, 1, 'Bolt', 'PN-5', 'L-5', 'O-5', 'p.png', '2026-01-01T00:00:00Z', 'u');
INSERT INTO parts VALUES (9, 1, 'Nut', 'PN-9', 'L-9', 'O-9', NULL, '2026-01-02T00:00:00Z', 'u');
"""


@pytest.mark.sqlite_only
def test_old_trolley_database_is_migrated(tmp_path):
    cfg = make_config(tmp_path)
    conn = sqlite3.connect(cfg["DATABASE"])
    conn.executescript(OLD_SCHEMA)
    conn.close()

    app = create_app(cfg)
    rows = db_rows(app, "SELECT * FROM parts ORDER BY id")
    assert [tuple(r) for r in rows] == [
        (5, "Bolt", "PN-5", "L-5", "O-5", "p.png", None, "2026-01-01T00:00:00Z", "u", None, None),
        (9, "Nut", "PN-9", "L-9", "O-9", None, None, "2026-01-02T00:00:00Z", "u", None, None),
    ]
    tables = {r["name"] for r in db_rows(app, "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "trolleys" not in tables and "parts_old" not in tables
    assert db_rows(app, "PRAGMA foreign_key_check") == []

    backup = sqlite3.connect(cfg["DATABASE"] + ".before-parts-only.bak")
    assert backup.execute("SELECT COUNT(*) FROM trolleys").fetchone()[0] == 1
    backup.close()

    create_app(cfg)  # running again is a no-op
    assert len(db_rows(app, "SELECT * FROM parts")) == 2


def test_backup_command(app, auth_client):
    create_part(auth_client, 1, {"image": upload()})
    result = app.test_cli_runner().invoke(args=["backup-db", "--with-uploads"])
    assert result.exit_code == 0, result.output
    backups = list(Path(app.config["BACKUP_FOLDER"]).glob("trolley_manager-*.db"))
    assert len(backups) == 1
    conn = sqlite3.connect(backups[0])
    assert conn.execute("SELECT part_number FROM parts").fetchone()[0] == "PN-0001"
    conn.close()
    assert len(list(Path(app.config["BACKUP_FOLDER"]).glob("uploads-*.zip"))) == 1


def test_secret_key_required_in_production(tmp_path, monkeypatch):
    monkeypatch.delenv("FLASK_DEBUG", raising=False)
    cfg = make_config(tmp_path, SECRET_KEY=None, TESTING=False)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app(cfg)


def test_cleanup_uploads_removes_only_orphans(app, auth_client):
    create_part(auth_client, 1, {"image": upload()})
    create_part(auth_client, 2, {"image": upload()})
    orphan = Path(app.config["UPLOAD_FOLDER"]) / "orphan.png"
    orphan.write_bytes(b"x")
    result = app.test_cli_runner().invoke(args=["cleanup-uploads"])
    assert result.exit_code == 0, result.output
    assert "Removed orphan.png" in result.output
    assert not orphan.exists()
    assert len(upload_files(app)) == 2
