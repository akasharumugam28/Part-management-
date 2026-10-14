"""SQLite connection handling, schema, and database CLI commands."""
import contextlib
import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import click
from flask import current_app, g

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
    updated_at     TEXT    NOT NULL
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


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path):
    # isolation_level=None: autocommit by default; multi-statement writes use
    # the explicit transaction() helper below.
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextlib.contextmanager
def transaction(db=None):
    """Run a block atomically: commit on success, roll back on any error."""
    db = db or get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
    except BaseException:
        db.execute("ROLLBACK")
        raise
    else:
        db.execute("COMMIT")


def init_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        _drop_trolleys(conn, path)
        conn.executescript(SCHEMA)
        _add_trolley_qty(conn)
    finally:
        conn.close()


def _add_trolley_qty(conn):
    """Add the trolley_qty column to databases created before it existed."""
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(parts)")}
    if "trolley_qty" not in columns:
        conn.execute(
            "ALTER TABLE parts ADD COLUMN trolley_qty INTEGER "
            "CHECK (trolley_qty IS NULL OR trolley_qty BETWEEN 1 AND 9999)"
        )


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
    """Create a consistent online backup using SQLite's backup API.

    Returns the list of files written. Old backups beyond BACKUP_KEEP are pruned.
    """
    backup_dir = Path(app.config["BACKUP_FOLDER"])
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"trolley_manager-{stamp}.db"

    src = connect(app.config["DATABASE"])
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    written = [target]

    if with_uploads:
        archive = backup_dir / f"uploads-{stamp}.zip"
        upload_dir = Path(app.config["UPLOAD_FOLDER"])
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            if upload_dir.exists():
                for f in sorted(upload_dir.iterdir()):
                    if f.is_file():
                        zf.write(f, f.name)
        written.append(archive)

    keep = app.config["BACKUP_KEEP"]
    for pattern in ("trolley_manager-*.db", "uploads-*.zip"):
        old = sorted(backup_dir.glob(pattern), reverse=True)[keep:]
        for f in old:
            f.unlink(missing_ok=True)
    return written


def register(app):
    app.teardown_appcontext(close_db)

    @app.cli.command("init-db")
    def init_db_command():
        """Create the database tables (safe to run repeatedly)."""
        init_db(app.config["DATABASE"])
        click.echo(f"Database ready at {app.config['DATABASE']}")

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

        init_db(app.config["DATABASE"])
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
        upload_dir = Path(app.config["UPLOAD_FOLDER"])
        conn = connect(app.config["DATABASE"])
        try:
            referenced = {
                r[0]
                for r in conn.execute(
                    "SELECT image_filename FROM parts WHERE image_filename IS NOT NULL "
                    "UNION SELECT image_filename FROM sub_parts WHERE image_filename IS NOT NULL"
                )
            }
        finally:
            conn.close()
        orphans = [f for f in upload_dir.iterdir() if f.is_file() and f.name not in referenced]
        for f in orphans:
            if not dry_run:
                f.unlink(missing_ok=True)
            click.echo(f"{'Would remove' if dry_run else 'Removed'} {f.name}")
        click.echo(f"{len(orphans)} unreferenced file(s).")

    @app.cli.command("restore-db")
    @click.argument("backup_file", type=click.Path(exists=True, dir_okay=False))
    @click.confirmation_option(prompt="This replaces the current database. Continue?")
    def restore_db_command(backup_file):
        """Replace the live database with a backup file (stop the server first)."""
        db_path = Path(app.config["DATABASE"])
        for suffix in ("-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        shutil.copy2(backup_file, db_path)
        click.echo(f"Restored {backup_file} -> {db_path}")
