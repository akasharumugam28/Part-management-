"""Copy the local SQLite database into PostgreSQL (Supabase). Safe to rerun.

    python scripts/migrate_sqlite_to_postgres.py --dry-run      # check, write nothing
    python scripts/migrate_sqlite_to_postgres.py                # migrate
    python scripts/migrate_sqlite_to_postgres.py --verify-only  # compare only

* The SQLite file is opened READ-ONLY and is never changed. A backup copy is
  written to instance/backups/ before anything else happens.
* Creates the PostgreSQL tables if needed (db/schema_postgres.sql).
* Copies admin accounts (with their existing password hashes), parts and
  sub-parts with their original ids, timestamps, quantities and NULLs, then
  moves the id sequences past the highest id SQLite ever used.
* Rows already copied with identical values are skipped, so rerunning never
  creates duplicates. A row with the same id but different values stops the
  migration and nothing is written.
* Runs in a single transaction and verifies every row and the inventory
  totals before committing.

DATABASE_URL comes from the environment / .env unless --database-url is given.
From your PC, use Supabase's "Session pooler" string (port 5432, IPv4).
"""
import argparse
import hashlib
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

import psycopg  # noqa: E402
import psycopg.conninfo  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

import dbcopy  # noqa: E402
from database import connect_postgres, is_postgres_url  # noqa: E402


def describe(url):
    """host:port/dbname as user - never the password."""
    info = psycopg.conninfo.conninfo_to_dict(url)
    return f"{info.get('host', '?')}:{info.get('port', 5432)}/{info.get('dbname', '?')} as {info.get('user', '?')}"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_sqlite(src, backup_dir):
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"trolley_manager-pre-migration-{datetime.now():%Y%m%d-%H%M%S}.db"
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"Backup {target} failed its integrity check.")
    finally:
        dst.close()
    return target


def print_report(report):
    print()
    print(f"{'table':<12}{'in SQLite':>10}{'inserted':>10}{'already there':>15}{'conflicts':>11}")
    for table, r in report.tables.items():
        print(f"{table:<12}{r.source:>10}{r.inserted:>10}{r.already_present:>15}{len(r.conflicts):>11}")
    if report.source_totals:
        print()
        print(f"{'check':<30}{'SQLite':>10}{'PostgreSQL':>12}")
        for key, value in report.source_totals.items():
            other = report.target_totals.get(key)
            flag = "" if other == value else "   <- PostgreSQL also has rows not in SQLite"
            print(f"{key:<30}{value:>10}{other:>12}{flag}")
    for t, r in report.tables.items():
        if r.conflicts:
            print(f"\nCONFLICT in {t}: ids {r.conflicts[:50]} exist in PostgreSQL with different values.")
    if report.mismatches:
        print(f"\nMISMATCH: {', '.join(report.mismatches[:50])}")


def main(argv=None):
    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sqlite", type=Path,
                   default=Path(os.environ.get("DATABASE_PATH") or ROOT / "instance" / "trolley_manager.db"),
                   help="SQLite database to read (default: %(default)s)")
    p.add_argument("--database-url", default=os.environ.get("DATABASE_URL"),
                   help="PostgreSQL connection string (default: $DATABASE_URL)")
    p.add_argument("--backup-dir", type=Path, default=ROOT / "instance" / "backups")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="do everything, then roll back")
    mode.add_argument("--verify-only", action="store_true", help="only compare SQLite with PostgreSQL")
    args = p.parse_args(argv)

    if not args.sqlite.is_file():
        print(f"SQLite database not found: {args.sqlite}", file=sys.stderr)
        return 2
    if not is_postgres_url(args.database_url):
        print("Set DATABASE_URL (postgresql://...) or pass --database-url.", file=sys.stderr)
        return 2

    before = sha256(args.sqlite)
    src = sqlite3.connect(f"file:{args.sqlite.resolve().as_posix()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    print(f"Source:  {args.sqlite} (opened read-only)")
    print(f"Target:  {describe(args.database_url)}")
    try:
        if not args.verify_only:
            print(f"Backup:  {backup_sqlite(src, args.backup_dir)}")
        dst = connect_postgres(args.database_url)
        try:
            if args.verify_only:
                report = dbcopy.verify(src, dst)
            else:
                report = dbcopy.sqlite_to_postgres(src, dst, dry_run=args.dry_run)
                if not args.dry_run:
                    # Re-read after COMMIT: proves the data really landed.
                    after = dbcopy.verify(src, dst)
                    report.mismatches, report.target_totals = after.mismatches, after.target_totals
        finally:
            dst.close()
    except dbcopy.SchemaError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2
    except dbcopy.CopyConflict as exc:
        print_report(exc.report)
        print("\nNothing was written: the transaction was rolled back. Resolve the conflicting rows "
              "(e.g. they were edited in Supabase after an earlier migration) and rerun.", file=sys.stderr)
        return 1
    except (psycopg.Error, sqlite3.Error) as exc:
        print(f"\nDatabase error ({type(exc).__name__}): {exc}\nNothing was written.", file=sys.stderr)
        return 1
    finally:
        src.close()

    print_report(report)
    unchanged = sha256(args.sqlite) == before
    print(f"\nSQLite file unchanged: {'yes' if unchanged else 'NO'}")
    if args.verify_only:
        print("Verified: every SQLite row is in PostgreSQL unchanged." if report.verified
              else "NOT verified: see mismatches above.")
    elif args.dry_run:
        print("DRY RUN: the copy and verification succeeded, then everything was rolled back.")
    else:
        print("Migration committed and verified." if report.verified else "Verification FAILED.")
    return 0 if report.verified and unchanged else 1


if __name__ == "__main__":
    sys.exit(main())
