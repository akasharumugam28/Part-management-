"""Database-layer behaviour. Tests marked postgres_only need TEST_DATABASE_URL."""
import threading

import pytest

import database
import models
from app import create_app
from conftest import TEST_DATABASE_URL, create_part, db_rows, login, make_config
from database import Row, _pg_sql

postgres_only = pytest.mark.postgres_only


# ------------------------------------------------------------ both backends

def test_row_behaves_like_sqlite_row():
    row = Row(("id", "name"), {"id": 0, "name": 1}, (7, "Bolt"))
    assert row["name"] == "Bolt" and row[0] == 7 and row[-1] == "Bolt"
    assert row.keys() == ["id", "name"] and "name" in row.keys()
    assert tuple(row) == (7, "Bolt") and dict(row) == {"id": 7, "name": "Bolt"}
    with pytest.raises(KeyError):
        row["missing"]


def test_placeholders_translated_and_percent_escaped():
    assert _pg_sql("SELECT * FROM t WHERE a = ? AND b LIKE '100%'") == (
        "SELECT * FROM t WHERE a = %s AND b LIKE '100%%'"
    )


def test_transaction_rolls_back_everything(app):
    with app.app_context():
        db = database.get_db()
        with pytest.raises(RuntimeError):
            with database.transaction(db):
                models.create_part({"part_name": "A", "part_number": "1", "line_storage": "L",
                                    "ols_storage": "O"})
                raise RuntimeError("boom")
        assert models.count_parts() == 0
        with database.transaction(db):
            models.create_part({"part_name": "A", "part_number": "1", "line_storage": "L",
                                "ols_storage": "O"})
        assert models.count_parts() == 1


def test_username_is_case_insensitive(app, client):
    with app.app_context():
        with pytest.raises(database.IntegrityError):
            models.create_admin("ADMIN", "another-password")
    assert login(client, username="ADMIN").status_code == 302


def test_search_is_case_insensitive_and_escapes_wildcards(auth_client):
    create_part(auth_client, 1, part_name="Hex Bolt", part_number="AB_100%")
    create_part(auth_client, 2, part_name="Nut", part_number="AB1100")
    page = auth_client.get("/?q=hex bOLT").get_data(as_text=True)
    assert "AB_100%" in page and "AB1100" not in page
    page = auth_client.get("/?q=AB_1").get_data(as_text=True)  # "_" is literal, not a wildcard
    assert "AB_100%" in page and "AB1100" not in page


def test_insert_returns_new_ids(app):
    with app.app_context():
        data = {"part_name": "A", "part_number": "1", "line_storage": "L", "ols_storage": "O"}
        first, second = models.create_part(data), models.create_part(data)
        assert second == first + 1
        sub = models.create_sub_part(first, {"part_name": "S", "part_number": "2"})
        assert models.get_sub_part(first, sub)["part_id"] == first


# ------------------------------------------------------------ PostgreSQL only

@postgres_only
def test_postgres_schema_matches_sqlite_schema(app):
    for table, cols in database.TABLES.items():
        rows = db_rows(app, "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = current_schema() AND table_name = ?", (table,))
        assert {r[0] for r in rows} == set(cols)
    indexes = {r[0] for r in db_rows(app, "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")}
    assert {"idx_parts_name", "idx_parts_part_number", "idx_sub_parts_part_id",
            "admin_username_lower_key"} <= indexes
    rls = dict(db_rows(app, "SELECT relname, relrowsecurity FROM pg_class WHERE relname = ANY(?)",
                       (["admin", "parts", "sub_parts", "rate_limits"],)))
    assert rls == {"admin": True, "parts": True, "sub_parts": True, "rate_limits": True}


@postgres_only
def test_schema_is_idempotent(app):
    database.init_db(TEST_DATABASE_URL)
    database.init_db(TEST_DATABASE_URL)
    assert db_rows(app, "SELECT COUNT(*) FROM admin")[0][0] == 1  # data untouched


@postgres_only
def test_startup_does_not_create_schema(tmp_path):
    db = database.connect_postgres(TEST_DATABASE_URL)
    try:
        db.execute("DROP TABLE sub_parts, parts, admin, rate_limits")
        create_app(make_config(tmp_path))
        exists = db.execute("SELECT to_regclass('parts')").fetchone()[0]
    finally:
        database.init_postgres(db)
        db.close()
    assert exists is None


@postgres_only
def test_cascade_delete_and_foreign_keys(app, auth_client):
    create_part(auth_client, 1)
    auth_client.post("/parts/1/sub-parts/create", data={"part_name": "W", "part_number": "S1"})
    with pytest.raises(database.IntegrityError):
        db_rows(app, "INSERT INTO sub_parts (part_id, part_name, part_number, created_at, updated_at) "
                     "VALUES (999, 'x', 'y', 'c', 'u')")
    auth_client.post("/parts/1/delete")
    assert db_rows(app, "SELECT COUNT(*) FROM sub_parts")[0][0] == 0


@postgres_only
def test_connections_are_closed_after_each_request(app, auth_client):
    def open_connections():
        db = database.connect_postgres(TEST_DATABASE_URL)
        try:
            return db.execute(
                "SELECT COUNT(*) FROM pg_stat_activity WHERE application_name = 'part-manager' "
                "AND pid <> pg_backend_pid()"
            ).fetchone()[0]
        finally:
            db.close()

    before = open_connections()
    for _ in range(10):
        assert auth_client.get("/").status_code == 200
    create_part(auth_client, 1)
    assert open_connections() == before


def test_static_assets_do_not_touch_the_database(app, auth_client, monkeypatch):
    def no_db():
        raise AssertionError("database used for a static asset")

    monkeypatch.setattr(database, "get_db", no_db)
    monkeypatch.setattr("models.get_db", no_db)
    for path in ("/static/css/style.css", "/manifest.webmanifest", "/service-worker.js"):
        assert auth_client.get(path).status_code == 200


@postgres_only
def test_login_rate_limit_is_shared_between_instances(tmp_path):
    """Two app instances (= two Vercel instances) count attempts in the same table."""
    cfg = make_config(tmp_path, RATELIMIT_ENABLED=True, LOGIN_RATE_LIMIT="3 per minute")
    app1 = create_app(cfg)
    app1.test_cli_runner().invoke(args=["create-admin"])
    app2 = create_app(cfg)
    assert app1.config["RATELIMIT_STORAGE_URI"] == "pglimits://"
    c1, c2 = app1.test_client(), app2.test_client()
    assert login(c1, password="bad").status_code == 401
    assert login(c2, password="bad").status_code == 401
    assert login(c1, password="bad").status_code == 401
    assert login(c2).status_code == 429  # 4th attempt, on the other instance
    assert db_rows(app1, "SELECT SUM(count) FROM rate_limits")[0][0] >= 3


@postgres_only
def test_rate_limit_counter_is_atomic_under_concurrency():
    from ratelimit_pg import PostgresStorage

    store = PostgresStorage(dsn=TEST_DATABASE_URL)
    errors = []

    def hit():
        try:
            for _ in range(5):
                store.incr("login/1.2.3.4", 60)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=hit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert store.get("login/1.2.3.4") == 40
    assert store.check()
    store.clear("login/1.2.3.4")
    assert store.get("login/1.2.3.4") == 0


@postgres_only
def test_expired_rate_limit_window_restarts():
    from ratelimit_pg import PostgresStorage

    store = PostgresStorage(dsn=TEST_DATABASE_URL)
    assert store.incr("k", 60) == 1 and store.incr("k", 60) == 2
    db = database.connect_postgres(TEST_DATABASE_URL)
    try:
        db.execute("UPDATE rate_limits SET expires_at = 0 WHERE key = 'k'")
    finally:
        db.close()
    assert store.get("k") == 0
    assert store.incr("k", 60) == 1
