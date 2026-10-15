"""SQLite -> PostgreSQL data migration, and backup / restore on PostgreSQL."""
import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

import database
from app import create_app
from conftest import ROOT, TEST_DATABASE_URL, create_part, db_rows, login, make_config

sys.path.insert(0, str(ROOT / "scripts"))
import migrate_sqlite_to_postgres as mig  # noqa: E402

postgres_only = pytest.mark.postgres_only

PASSWORD = "Migrated-Pa55word!"
SPECIAL = "Bolt 'M8' \"quoted\" \\ back % under_score — ünïcödé ✓"


def make_source(path):
    """A SQLite database on the current schema with awkward data."""
    conn = sqlite3.connect(path)
    conn.executescript(database.SCHEMA)
    conn.execute("INSERT INTO admin VALUES (1, 'admin', ?, 0, '2026-10-13T15:12:11Z')",
                 (generate_password_hash(PASSWORD),))
    conn.executemany("INSERT INTO parts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
        (2, SPECIAL, "PN-2", "L-2", "O-2", "0123456789abcdef0123456789abcdef.png", 20,
         "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", 12, 4),
        (5, "Nut", "PN-5", "L-5", "O-5", None, None, "2026-02-01T00:00:00Z", "2026-02-01T00:00:00Z",
         None, None),
        (9, "Gone", "PN-9", "L", "O", None, 1, "x", "x", 0, 0),
    ])
    conn.executemany("INSERT INTO sub_parts VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
        (3, 2, "Washer", "SP-3", 40, None, "2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z"),
        (4, 5, SPECIAL, "SP-4", None, "fedcba9876543210fedcba9876543210.webp", "c", "u"),
    ])
    conn.execute("DELETE FROM parts WHERE id = 9")  # sqlite_sequence stays at 9
    conn.commit()
    conn.close()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(source, tmp_path, *extra):
    return mig.main(["--sqlite", str(source), "--database-url", TEST_DATABASE_URL,
                     "--backup-dir", str(tmp_path / "backups"), *extra])


def pg_rows(table):
    db = database.connect_postgres(TEST_DATABASE_URL)
    try:
        return [tuple(r) for r in db.execute(
            f"SELECT {', '.join(database.TABLES[table])} FROM {table} ORDER BY id")]
    finally:
        db.close()


def sqlite_rows(path, table):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT {', '.join(database.TABLES[table])} FROM {table} ORDER BY id").fetchall()
    finally:
        conn.close()


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.db"
    make_source(path)
    return path


@pytest.fixture
def empty_pg():
    db = database.connect_postgres(TEST_DATABASE_URL)
    try:
        db.execute("DROP TABLE IF EXISTS sub_parts, parts, admin, rate_limits")
    finally:
        db.close()


def test_missing_database_url_is_reported(source, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "")
    assert mig.main(["--sqlite", str(source), "--database-url", ""]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_missing_sqlite_file_is_reported(tmp_path, capsys):
    assert mig.main(["--sqlite", str(tmp_path / "nope.db"), "--database-url", "postgresql://x/y"]) == 2


@postgres_only
def test_dry_run_writes_nothing(source, tmp_path, empty_pg, capsys):
    before = sha(source)
    assert run(source, tmp_path, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "SQLite file unchanged: yes" in out
    db = database.connect_postgres(TEST_DATABASE_URL)
    try:
        assert db.execute("SELECT to_regclass('parts')").fetchone()[0] is None  # even the schema rolled back
    finally:
        db.close()
    assert sha(source) == before
    assert len(list((tmp_path / "backups").glob("trolley_manager-pre-migration-*.db"))) == 1


@postgres_only
def test_migration_copies_everything_exactly(source, tmp_path, empty_pg, capsys):
    before = sha(source)
    assert run(source, tmp_path) == 0
    out = capsys.readouterr().out
    assert "Migration committed and verified." in out
    for table in database.TABLES:
        assert pg_rows(table) == sqlite_rows(source, table)
    assert sha(source) == before
    backup = next((tmp_path / "backups").glob("trolley_manager-pre-migration-*.db"))
    assert sqlite_rows(backup, "parts") == sqlite_rows(source, "parts")

    # The app runs on the migrated data: same password, same ids, totals intact.
    app = create_app(make_config(tmp_path / "app"))
    client = app.test_client()
    assert login(client, password=PASSWORD).status_code == 302
    page = client.get("/").get_data(as_text=True)
    assert "ünïcödé ✓" in page and "PN-5" in page
    assert client.get("/parts/2").status_code == 200
    assert db_rows(app, "SELECT SUM(trolley_qty) FROM sub_parts")[0][0] == 40
    # Ids continue after the highest id SQLite ever used (9 was deleted, not reused).
    create_part(client, 10)
    assert db_rows(app, "SELECT MAX(id) FROM parts")[0][0] == 10
    client.post("/parts/2/sub-parts/create", data={"part_name": "N", "part_number": "S"})
    assert db_rows(app, "SELECT MAX(id) FROM sub_parts")[0][0] == 5


@postgres_only
def test_rerun_creates_no_duplicates(source, tmp_path, empty_pg, capsys):
    assert run(source, tmp_path) == 0
    capsys.readouterr()
    assert run(source, tmp_path) == 0
    out = capsys.readouterr().out
    assert "parts                2         0              2          0" in out
    assert len(pg_rows("parts")) == 2 and len(pg_rows("admin")) == 1


@postgres_only
def test_new_sqlite_rows_are_added_on_rerun(source, tmp_path, empty_pg):
    assert run(source, tmp_path) == 0
    conn = sqlite3.connect(source)
    conn.execute("INSERT INTO parts VALUES (12, 'New', 'PN-12', 'L', 'O', NULL, 3, 'c', 'u', NULL, NULL)")
    conn.commit()
    conn.close()
    assert run(source, tmp_path) == 0
    assert [r[0] for r in pg_rows("parts")] == [2, 5, 12]


@postgres_only
def test_conflict_aborts_without_writing(source, tmp_path, empty_pg, capsys):
    assert run(source, tmp_path) == 0
    db = database.connect_postgres(TEST_DATABASE_URL)
    try:
        db.execute("UPDATE parts SET trolley_qty = 99 WHERE id = 5")  # edited in Supabase later
    finally:
        db.close()
    conn = sqlite3.connect(source)
    conn.execute("INSERT INTO parts VALUES (12, 'New', 'PN-12', 'L', 'O', NULL, 3, 'c', 'u', NULL, NULL)")
    conn.commit()
    conn.close()
    capsys.readouterr()
    assert run(source, tmp_path) == 1
    captured = capsys.readouterr()
    assert "CONFLICT in parts: ids [5]" in captured.out and "Nothing was written" in captured.err
    rows = pg_rows("parts")
    assert [r[0] for r in rows] == [2, 5]  # part 12 was NOT inserted: all rolled back
    assert rows[1][6] == 99  # and the edited row was not overwritten


@postgres_only
def test_verify_only_detects_differences(source, tmp_path, empty_pg, capsys):
    assert run(source, tmp_path) == 0
    assert run(source, tmp_path, "--verify-only") == 0
    db = database.connect_postgres(TEST_DATABASE_URL)
    try:
        db.execute("DELETE FROM sub_parts WHERE id = 4")
    finally:
        db.close()
    capsys.readouterr()
    assert run(source, tmp_path, "--verify-only") == 1
    assert "MISMATCH: sub_parts id=4" in capsys.readouterr().out


@postgres_only
def test_old_trolley_schema_is_refused(tmp_path, capsys):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE trolleys (id INTEGER PRIMARY KEY); CREATE TABLE parts (id INTEGER);")
    conn.close()
    assert run(path, tmp_path) == 2
    assert "old 'trolleys' schema" in capsys.readouterr().err


@postgres_only
def test_backup_and_restore_on_postgres(app, auth_client):
    create_part(auth_client, 1, trolley_qty="20")
    create_part(auth_client, 2)
    runner = app.test_cli_runner()
    result = runner.invoke(args=["backup-db"])
    assert result.exit_code == 0, result.output
    backup = next(Path(app.config["BACKUP_FOLDER"]).glob("trolley_manager-*.db"))
    assert [r[2] for r in sqlite_rows(backup, "parts")] == ["PN-0001", "PN-0002"]

    auth_client.post("/parts/1/delete")
    create_part(auth_client, 3)  # id 3
    result = runner.invoke(args=["restore-db", str(backup), "--yes"])
    assert result.exit_code == 0, result.output
    assert [r[0] for r in db_rows(app, "SELECT id FROM parts ORDER BY id")] == [1, 2]
    assert db_rows(app, "SELECT trolley_qty FROM parts WHERE id = 1")[0][0] == 20
    assert login(app.test_client()).status_code == 302  # admin restored with its hash
