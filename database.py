"""Database connection handling, schema, and database CLI commands.

Two backends share one interface:

* PostgreSQL (e.g. Supabase) when ``DATABASE_URL`` is set. Used in production.
* SQLite (``DATABASE``) otherwise, for local development and tests.

Queries are written once with ``?`` placeholders; the PostgreSQL connection
translates them to ``%s``. Rows support both ``row["col"]`` and ``row[0]``.
"""
import contextlib
import functools
import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import click
import psycopg
from flask import current_app, g

# Catch this instead of sqlite3.Error so both backends' failures are handled.
Error = (sqlite3.Error, psycopg.Error)
IntegrityError = (sqlite3.IntegrityError, psycopg.IntegrityError)

PG_SCHEMA_FILE = Path(__file__).resolve().parent / "db" / "schema_postgres.sql"

PARTS_TABLE = """
CREATE TABLE IF NOT EXISTS parts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    part_name      TEXT    NOT NULL CHECK (length(trim(part_name)) BETWEEN 1 AND 120),
    part_number    TEXT    NOT NULL CHECK (length(trim(part_number)) BETWEEN 1 AND 80),
    line_storage   TEXT    NOT NULL CHECK (length(trim(line_storage)) BETWEEN 1 AND 120),
    ols_storage    TEXT    NOT NULL CHECK (length(trim(ols_storage)) BETWEEN 1 AND 120),
    image_filename TEXT,
    trolley_qty    INTEGER CHECK (trolley_qty IS NULL OR trolley_qty BETWEEN 1 AND 9999),
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL,
    total_trolleys INTEGER CHECK (total_trolleys IS NULL OR total_trolleys BETWEEN 0 AND 9999),
    empty_trolleys INTEGER CHECK (empty_trolleys IS NULL OR empty_trolleys BETWEEN 0 AND 9999)
)
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS admin (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT    NOT NULL UNIQUE COLLATE NOCASE
                                 CHECK (length(trim(username)) BETWEEN 1 AND 50),
    password_hash        TEXT    NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),
    created_at           TEXT    NOT NULL
);

{parts_table}

CREATE INDEX IF NOT EXISTS idx_parts_name ON parts (part_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_parts_part_number ON parts (part_number COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS sub_parts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    part_id        INTEGER NOT NULL REFERENCES parts (id) ON DELETE CASCADE,
    part_name      TEXT    NOT NULL CHECK (length(trim(part_name)) BETWEEN 1 AND 120),
    part_number    TEXT    NOT NULL CHECK (length(trim(part_number)) BETWEEN 1 AND 80),
    trolley_qty    INTEGER CHECK (trolley_qty IS NULL OR trolley_qty BETWEEN 1 AND 9999),
    image_filename TEXT,
    created_at     TEXT    NOT NULL,
    updated_at     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sub_parts_part_id ON sub_parts (part_id);
""".format(parts_table=PARTS_TABLE.strip() + ";")

# Tables in foreign-key order, with their columns (shared by both backends).
TABLES = {
    "admin": ("id", "username", "password_hash", "must_change_password", "created_at"),
    "parts": ("id", "part_name", "part_number", "line_storage", "ols_storage",
              "image_filename", "trolley_qty", "created_at", "updated_at",
              "total_trolleys", "empty_trolleys"),
    "sub_parts": ("id", "part_id", "part_name", "part_number", "trolley_qty",
                  "image_filename", "created_at", "updated_at"),
}


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_postgres_url(target):
    return str(target or "").startswith(("postgres://", "postgresql://"))


def database_target(config):
    """The DATABASE_URL (PostgreSQL) if configured, else the SQLite file path."""
    return config.get("DATABASE_URL") or config["DATABASE"]


def uses_postgres(config):
    return is_postgres_url(config.get("DATABASE_URL"))


# ---------------------------------------------------------------- PostgreSQL

class Row:
    """A read-only row that behaves like sqlite3.Row: row["col"], row[0], keys()."""

    __slots__ = ("_names", "_index", "_values")

    def __init__(self, names, index, values):
        self._names, self._index, self._values = names, index, values

    def keys(self):
        return list(self._names)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._values[self._index[key]]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __eq__(self, other):
        if isinstance(other, Row):
            return self._names == other._names and tuple(self._values) == tuple(other._values)
        return NotImplemented

    def __repr__(self):
        return f"Row({dict(zip(self._names, self._values))!r})"


def _row_factory(cursor):
    if cursor.description is None:
        return None
    names = tuple(c.name for c in cursor.description)
    index = {n: i for i, n in enumerate(names)}
    return lambda values: Row(names, index, values)


@functools.lru_cache(maxsize=256)
def _pg_sql(sql):
    # Literal % must be doubled for psycopg; then ? placeholders become %s.
    # Our SQL never contains a literal "?" (values are always parameters).
    return sql.replace("%", "%%").replace("?", "%s")


class PostgresConnection:
    """Thin wrapper giving a psycopg connection the sqlite3-style API we use."""

    is_postgres = True

    def __init__(self, conn):
        self.raw = conn

    def execute(self, sql, params=()):
        return self.raw.execute(_pg_sql(sql), tuple(params))

    def executemany(self, sql, rows):
        with self.raw.cursor() as cur:
            cur.executemany(_pg_sql(sql), rows)

    def executescript(self, script):
        self.raw.execute(script)

    def close(self):
        self.raw.close()

    @contextlib.contextmanager
    def transaction(self):
        with self.raw.transaction():
            yield self


def connect_postgres(url, connect_timeout=10):
    # autocommit: each statement commits on its own, like the SQLite setup;
    # multi-statement writes use transaction().
    # prepare_threshold=None: required by Supabase's transaction pooler
    # (Supavisor, port 6543), which cannot keep prepared statements.
    conn = psycopg.connect(
        url, autocommit=True, prepare_threshold=None, row_factory=_row_factory,
        connect_timeout=connect_timeout, application_name="part-manager",
    )
    return PostgresConnection(conn)


def init_postgres(db):
    """Create the PostgreSQL tables (idempotent). Run via ``flask init-db``, not at startup."""
    with db.transaction():
        db.executescript(PG_SCHEMA_FILE.read_text(encoding="utf-8"))


def reset_sequences(db, sqlite_sequences=None):
    """Point each id sequence past the highest id ever used.

    ``sqlite_sequences`` (table -> last AUTOINCREMENT value) keeps ids of rows
    deleted in SQLite from being reused, matching SQLite's AUTOINCREMENT.
    """
    for table in TABLES:
        floor = (sqlite_sequences or {}).get(table, 0)
        top = db.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
        last = max(top, floor)
        if last:
            db.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), ?, true)", (last,))
        else:
            db.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), 1, false)")


# -------------------------------------------------------------------- shared

def connect(target, connect_timeout=10):
    if is_postgres_url(target):
        return connect_postgres(target, connect_timeout)
    # isolation_level=None: autocommit by default; multi-statement writes use
    # the explicit transaction() helper below.
    conn = sqlite3.connect(target, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def get_db():
    if "db" not in g:
        cfg = current_app.config
        g.db = connect(database_target(cfg), cfg["DB_CONNECT_TIMEOUT"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def is_postgres(db):
    return getattr(db, "is_postgres", False)


def like_operator(db):
    # SQLite's LIKE is case-insensitive for ASCII; PostgreSQL needs ILIKE.
    return "ILIKE" if is_postgres(db) else "LIKE"


def insert(db, sql, params):
    """Run an INSERT and return the new row's id."""
    if is_postgres(db):
        return db.execute(sql + " RETURNING id", params).fetchone()[0]
    return db.execute(sql, params).lastrowid


@contextlib.contextmanager
def transaction(db=None):
    """Run a block atomically: commit on success, roll back on any error."""
    db = db or get_db()
    if is_postgres(db):
        with db.transaction():
            yield db
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def init_db(target):
    if is_postgres_url(target):
        db = connect_postgres(target)
        try:
            init_postgres(db)
        finally:
            db.close()
        return
    path = target
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        _drop_trolleys(conn, path)
        conn.executescript(SCHEMA)
        _add_part_columns(conn)
    finally:
        conn.close()


# Columns added to parts after its first release, with their definitions.
ADDED_PART_COLUMNS = {
    "trolley_qty": "INTEGER CHECK (trolley_qty IS NULL OR trolley_qty BETWEEN 1 AND 9999)",
    "total_trolleys": "INTEGER CHECK (total_trolleys IS NULL OR total_trolleys BETWEEN 0 AND 9999)",
    "empty_trolleys": "INTEGER CHECK (empty_trolleys IS NULL OR empty_trolleys BETWEEN 0 AND 9999)",
}


def _add_part_columns(conn):
    """Add columns to parts tables created before those columns existed."""
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(parts)")}
    for name, definition in ADDED_PART_COLUMNS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE parts ADD COLUMN {name} {definition}")


def _drop_trolleys(conn, path):
    """One-time migration from the old trolley schema to standalone parts.

    Existing parts are kept (without their trolley link) and the trolleys
    table is dropped. A copy of the old database is written next to it first.
    """
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(parts)")}
    has_trolleys = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'trolleys'"
    ).fetchone()
    if "trolley_id" not in columns and not has_trolleys:
        return

    backup = sqlite3.connect(f"{path}.before-parts-only.bak")
    try:
        conn.backup(backup)
    finally:
        backup.close()

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with transaction(conn):
            if "trolley_id" in columns:
                conn.execute("ALTER TABLE parts RENAME TO parts_old")
                conn.execute(PARTS_TABLE)
                conn.execute(
                    "INSERT INTO parts (id, part_name, part_number, line_storage, ols_storage, "
                    "image_filename, created_at, updated_at) SELECT id, part_name, part_number, "
                    "line_storage, ols_storage, image_filename, created_at, updated_at "
                    "FROM parts_old ORDER BY id"
                )
                conn.execute("DROP TABLE parts_old")
            conn.execute("DROP TABLE IF EXISTS trolleys")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def backup_db(app, with_uploads=False):
    """Write a timestamped SQLite backup file (and optionally a zip of the images).

    For SQLite this uses the online backup API. For PostgreSQL the tables are
    copied into a new SQLite file with the same schema, which ``restore-db``
    (or scripts/migrate_sqlite_to_postgres.py) can load back.
    Returns the list of files written. Old backups beyond BACKUP_KEEP are pruned.
    """
    import dbcopy
    import images

    backup_dir = Path(app.config["BACKUP_FOLDER"])
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"trolley_manager-{stamp}.db"

    src = connect(database_target(app.config))
    try:
        if is_postgres(src):
            dbcopy.postgres_to_sqlite(src, target)
        else:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
            finally:
                dst.close()
    finally:
        src.close()
    written = [target]

    if with_uploads:
        archive = backup_dir / f"uploads-{stamp}.zip"
        with app.app_context():
            store = images.storage()
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                for name in sorted(store.list_names()):
                    data = store.read(name)
                    if data is not None:
                        zf.writestr(name, data)
        written.append(archive)

    keep = app.config["BACKUP_KEEP"]
    for pattern in ("trolley_manager-*.db", "uploads-*.zip"):
        old = sorted(backup_dir.glob(pattern), reverse=True)[keep:]
        for f in old:
            f.unlink(missing_ok=True)
    return written


def referenced_images(db):
    return {
        r[0]
        for r in db.execute(
            "SELECT image_filename FROM parts WHERE image_filename IS NOT NULL "
            "UNION SELECT image_filename FROM sub_parts WHERE image_filename IS NOT NULL"
        )
    }


def register(app):
    app.teardown_appcontext(close_db)

    @app.cli.command("init-db")
    def init_db_command():
        """Create the database tables (safe to run repeatedly)."""
        init_db(database_target(app.config))
        where = "PostgreSQL (DATABASE_URL)" if uses_postgres(app.config) else app.config["DATABASE"]
        click.echo(f"Database ready at {where}")

    @app.cli.command("create-admin")
    @click.option("--username", default="admin", show_default=True)
    @click.option(
        "--prompt-password",
        is_flag=True,
        help="Prompt for a password instead of using the default admin@123.",
    )
    @click.option("--reset", is_flag=True, help="Reset the password of an existing admin.")
    def create_admin_command(username, prompt_password, reset):
        """Seed the admin account (password is stored hashed)."""
        from models import create_admin, get_admin_by_username, reset_admin_password

        init_db(database_target(app.config))
        if prompt_password:
            password = click.prompt("Initial password", hide_input=True, confirmation_prompt=True)
            if len(password) < 8:
                raise click.ClickException("Password must be at least 8 characters.")
        else:
            password = "admin@123"

        with app.app_context():
            existing = get_admin_by_username(username)
            if existing and not reset:
                raise click.ClickException(
                    f"Admin '{username}' already exists. Use --reset to reset its password."
                )
            if existing:
                reset_admin_password(existing["id"], password)
                click.echo(f"Password for '{username}' reset.")
            else:
                create_admin(username, password)
                click.echo(f"Admin '{username}' created.")

    @app.cli.command("backup-db")
    @click.option("--with-uploads", is_flag=True, help="Also archive uploaded images.")
    def backup_db_command(with_uploads):
        """Write a timestamped backup to BACKUP_FOLDER."""
        for f in backup_db(app, with_uploads=with_uploads):
            click.echo(f"Backup written: {f}")

    @app.cli.command("cleanup-uploads")
    @click.option("--dry-run", is_flag=True, help="Only list the files that would be removed.")
    def cleanup_uploads_command(dry_run):
        """Delete uploaded images no part or sub-part references any more."""
        import images

        conn = connect(database_target(app.config))
        try:
            referenced = referenced_images(conn)
        finally:
            conn.close()
        with app.app_context():
            store = images.storage()
            orphans = sorted(n for n in store.list_names() if n not in referenced)
            if not dry_run:
                store.delete(orphans)
        for name in orphans:
            click.echo(f"{'Would remove' if dry_run else 'Removed'} {name}")
        click.echo(f"{len(orphans)} unreferenced file(s).")

    @app.cli.command("init-storage")
    def init_storage_command():
        """Create the private Supabase Storage bucket for images (safe to repeat)."""
        import images

        with app.app_context():
            store = images.storage()
            if not hasattr(store, "ensure_bucket"):
                raise click.ClickException("SUPABASE_URL is not set; images use the local folder.")
            created = store.ensure_bucket()
        bucket = app.config["SUPABASE_BUCKET"]
        click.echo(f"Bucket '{bucket}' {'created' if created else 'already exists'} (private).")

    @app.cli.command("restore-db")
    @click.argument("backup_file", type=click.Path(exists=True, dir_okay=False))
    @click.confirmation_option(prompt="This replaces the current database. Continue?")
    def restore_db_command(backup_file):
        """Replace the live database with a backup file (stop the server first)."""
        if uses_postgres(app.config):
            import dbcopy

            src = sqlite3.connect(f"file:{Path(backup_file).resolve().as_posix()}?mode=ro", uri=True)
            src.row_factory = sqlite3.Row
            dst = connect_postgres(app.config["DATABASE_URL"])
            try:
                report = dbcopy.sqlite_to_postgres(src, dst, replace=True)
            finally:
                src.close()
                dst.close()
            click.echo(f"Restored {backup_file} -> PostgreSQL ({report.summary()})")
            return
        db_path = Path(app.config["DATABASE"])
        for suffix in ("-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        shutil.copy2(backup_file, db_path)
        click.echo(f"Restored {backup_file} -> {db_path}")
