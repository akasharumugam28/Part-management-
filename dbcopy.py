"""Copy the app's tables between SQLite and PostgreSQL.

Used by scripts/migrate_sqlite_to_postgres.py (SQLite -> Supabase),
``flask backup-db`` (PostgreSQL -> SQLite file) and ``flask restore-db``.

Rules for SQLite -> PostgreSQL:
* ids, timestamps, quantities and NULLs are copied exactly;
* a row whose id already exists with identical values is skipped, so a rerun
  never duplicates anything;
* a row whose id exists with *different* values is a conflict: the whole
  copy is rolled back and nothing is overwritten (unless ``replace=True``,
  which is only used by ``restore-db`` after an explicit confirmation);
* everything happens in one transaction.
"""
import sqlite3
from dataclasses import dataclass, field

from database import SCHEMA, TABLES, init_postgres, reset_sequences


class SchemaError(Exception):
    pass


class CopyConflict(Exception):
    def __init__(self, report):
        self.report = report
        super().__init__(
            "Rows with the same id but different values already exist in PostgreSQL: "
            + "; ".join(f"{t} ids {r.conflicts[:20]}" for t, r in report.tables.items() if r.conflicts)
        )


class _Rollback(Exception):
    pass


@dataclass
class TableReport:
    source: int = 0
    inserted: int = 0
    already_present: int = 0
    conflicts: list = field(default_factory=list)


@dataclass
class Report:
    tables: dict = field(default_factory=dict)
    source_totals: dict = field(default_factory=dict)
    target_totals: dict = field(default_factory=dict)
    mismatches: list = field(default_factory=list)
    dry_run: bool = False

    @property
    def has_conflicts(self):
        return any(r.conflicts for r in self.tables.values())

    @property
    def verified(self):
        return not self.mismatches and not self.has_conflicts

    def summary(self):
        return "; ".join(
            f"{t}: {r.source} in source, {r.inserted} inserted, {r.already_present} already present"
            + (f", {len(r.conflicts)} CONFLICTS" if r.conflicts else "")
            for t, r in self.tables.items()
        )


def check_sqlite_schema(src):
    """Refuse databases that are not on the current schema."""
    tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "trolleys" in tables:
        raise SchemaError(
            "This SQLite database still has the old 'trolleys' schema. Upgrade it first by "
            "starting the app locally once (or `flask --app app init-db`), which keeps a backup."
        )
    for table, cols in TABLES.items():
        have = {r[1] for r in src.execute(f"PRAGMA table_info({table})")}
        if not have:
            raise SchemaError(f"SQLite database has no '{table}' table.")
        missing = set(cols) - have
        if missing:
            raise SchemaError(f"Table '{table}' is missing columns: {', '.join(sorted(missing))}.")


def sqlite_sequences(src):
    try:
        return {name: seq for name, seq in src.execute("SELECT name, seq FROM sqlite_sequence")}
    except sqlite3.OperationalError:  # no AUTOINCREMENT table has been written yet
        return {}


def read_rows(conn, table):
    cols = ", ".join(TABLES[table])
    return [tuple(r) for r in conn.execute(f"SELECT {cols} FROM {table} ORDER BY id")]


def totals(conn):
    """Counts and inventory totals used to verify a copy."""
    def one(sql):
        return conn.execute(sql).fetchone()[0]

    return {
        "admin rows": one("SELECT COUNT(*) FROM admin"),
        "parts rows": one("SELECT COUNT(*) FROM parts"),
        "sub_parts rows": one("SELECT COUNT(*) FROM sub_parts"),
        "parts trolley_qty total": one("SELECT COALESCE(SUM(trolley_qty), 0) FROM parts"),
        "sub_parts trolley_qty total": one("SELECT COALESCE(SUM(trolley_qty), 0) FROM sub_parts"),
        "images referenced": one(
            "SELECT (SELECT COUNT(*) FROM parts WHERE image_filename IS NOT NULL)"
            " + (SELECT COUNT(*) FROM sub_parts WHERE image_filename IS NOT NULL)"
        ),
    }


def _existing(dst, table, ids):
    cols = ", ".join(TABLES[table])
    rows = dst.execute(f"SELECT {cols} FROM {table} WHERE id = ANY(?)", (list(ids),)).fetchall()
    return {r[0]: tuple(r) for r in rows}


def _verify(src, dst, report):
    """Every source row must exist in PostgreSQL with identical values."""
    for table in TABLES:
        rows = read_rows(src, table)
        found = _existing(dst, table, [r[0] for r in rows]) if rows else {}
        for row in rows:
            if found.get(row[0]) != row:
                report.mismatches.append(f"{table} id={row[0]}")
    report.source_totals = totals(src)
    report.target_totals = totals(dst)


def verify(src, dst):
    """Read-only check that every SQLite row is in PostgreSQL unchanged."""
    check_sqlite_schema(src)
    report = Report()
    for table in TABLES:
        report.tables[table] = TableReport(source=len(read_rows(src, table)))
    _verify(src, dst, report)
    return report


def sqlite_to_postgres(src, dst, *, replace=False, dry_run=False):
    """Copy every table from a SQLite connection into PostgreSQL. Returns a Report.

    With ``dry_run`` everything (including the schema) runs inside the
    transaction and is then rolled back, so constraint errors still surface.
    """
    check_sqlite_schema(src)
    report = Report(dry_run=dry_run)
    try:
        with dst.transaction():
            init_postgres(dst)
            if replace:
                dst.execute("TRUNCATE sub_parts, parts, admin RESTART IDENTITY CASCADE")
            for table, cols in TABLES.items():
                rows = read_rows(src, table)
                tr = report.tables[table] = TableReport(source=len(rows))
                existing = _existing(dst, table, [r[0] for r in rows]) if rows else {}
                new_rows = []
                for row in rows:
                    if row[0] not in existing:
                        new_rows.append(row)
                    elif existing[row[0]] == row:
                        tr.already_present += 1
                    else:
                        tr.conflicts.append(row[0])
                if new_rows:
                    marks = ", ".join("?" * len(cols))
                    dst.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})", new_rows)
                tr.inserted = len(new_rows)
            if report.has_conflicts:
                raise CopyConflict(report)
            reset_sequences(dst, sqlite_sequences(src))
            _verify(src, dst, report)
            if report.mismatches:
                raise CopyConflict(report)
            if dry_run:
                raise _Rollback
    except _Rollback:
        pass
    return report


def postgres_to_sqlite(src, target_path):
    """Write every PostgreSQL table into a new SQLite file with the app's schema."""
    dst = sqlite3.connect(target_path, isolation_level=None)
    try:
        dst.executescript(SCHEMA)
        dst.execute("BEGIN")
        for table, cols in TABLES.items():
            marks = ", ".join("?" * len(cols))
            dst.executemany(
                f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})", read_rows(src, table)
            )
            # Carry the sequence over so ids of deleted rows are not reused after a restore.
            last = src.execute(
                f"SELECT pg_sequence_last_value(pg_get_serial_sequence('{table}', 'id')::regclass)"
            ).fetchone()[0]
            if last:
                updated = dst.execute(
                    "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?", (last, table)
                ).rowcount
                if not updated:
                    dst.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, last))
        dst.execute("COMMIT")
    except BaseException:
        if dst.in_transaction:
            dst.execute("ROLLBACK")
        raise
    finally:
        dst.close()
