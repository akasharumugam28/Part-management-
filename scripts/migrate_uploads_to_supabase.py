"""Upload the locally stored part images to the Supabase Storage bucket. Safe to rerun.

    python scripts/migrate_uploads_to_supabase.py --create-bucket --dry-run
    python scripts/migrate_uploads_to_supabase.py

* Uploads every image the database references (or every file with --all)
  from UPLOAD_FOLDER to the private bucket, under the same name, so the
  existing ``image_filename`` values keep working unchanged.
* Each file is checked with Pillow first; anything that is not a real
  JPEG/PNG/WebP image, or has an unsafe name, is skipped and reported.
* Never overwrites: a file already in the bucket with identical bytes is
  skipped; one with different bytes is reported as a conflict.
* Never deletes or changes local files.

Image names are read from DATABASE_URL (PostgreSQL) if set, else from the
SQLite database; --sqlite forces a SQLite file. SUPABASE_URL,
SUPABASE_SERVICE_ROLE_KEY and SUPABASE_BUCKET come from the environment / .env.
"""
import argparse
import io
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from PIL import Image, UnidentifiedImageError  # noqa: E402

import images  # noqa: E402
from database import connect, referenced_images  # noqa: E402

_PIL_EXT = {"JPEG": {"jpg", "jpeg"}, "PNG": {"png"}, "WEBP": {"webp"}}


def check_image(name, data):
    """Return the MIME type, or None if this is not an allowed image."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format
            img.verify()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError):
        return None
    if ext not in _PIL_EXT.get(fmt, ()):
        return None
    return images.MIME_TYPES[ext]


def make_storage(env=os.environ, session=None):
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise SystemExit("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (in .env or the environment).")
    return images.SupabaseStorage(url, key, env.get("SUPABASE_BUCKET") or "part-images",
                                  timeout=int(env.get("STORAGE_TIMEOUT") or 30), session=session)


def migrate(store, upload_folder, names, dry_run=False, out=print):
    """Upload ``names`` from ``upload_folder``. Returns {outcome: [names]}."""
    results = {k: [] for k in ("uploaded", "would upload", "already in bucket", "missing locally",
                               "invalid image", "unsafe name", "conflict", "failed")}
    for name in sorted(names):
        if not images.is_safe_name(name):
            outcome = "unsafe name"
        else:
            path = Path(upload_folder) / name
            data = path.read_bytes() if path.is_file() else None
            mime = check_image(name, data) if data is not None else None
            if data is None:
                outcome = "missing locally"
            elif mime is None:
                outcome = "invalid image"
            elif dry_run:
                outcome = "would upload"
            else:
                try:
                    store.save(name, data, mime)
                    outcome = "uploaded"
                except images.ObjectExists:
                    outcome = "already in bucket" if store.read(name) == data else "conflict"
                except images.StorageError:
                    outcome = "failed"
        results[outcome].append(name)
        out(f"  {outcome:<18} {name}")
    return results


def main(argv=None, session=None):
    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--upload-folder", type=Path,
                   default=Path(os.environ.get("UPLOAD_FOLDER") or ROOT / "instance" / "uploads"))
    p.add_argument("--sqlite", type=Path, help="read image names from this SQLite file")
    p.add_argument("--all", action="store_true", help="upload every file in the folder, referenced or not")
    p.add_argument("--create-bucket", action="store_true", help="create the private bucket if missing")
    p.add_argument("--dry-run", action="store_true", help="only report what would be uploaded")
    args = p.parse_args(argv)

    store = make_storage(session=session)
    if args.create_bucket and not args.dry_run:
        created = store.ensure_bucket()
        print(f"Bucket '{store.bucket}' {'created (private)' if created else 'already exists'}.")

    if args.all:
        folder = args.upload_folder
        names = {f.name for f in folder.iterdir() if f.is_file()} if folder.is_dir() else set()
    else:
        sqlite_path = args.sqlite or Path(os.environ.get("DATABASE_PATH")
                                          or ROOT / "instance" / "trolley_manager.db")
        target = None if args.sqlite else os.environ.get("DATABASE_URL")
        if target:
            db = connect(target)
        else:
            db = sqlite3.connect(f"file:{sqlite_path.resolve().as_posix()}?mode=ro", uri=True)
        try:
            names = referenced_images(db)
        finally:
            db.close()

    print(f"{len(names)} image(s) to check from {args.upload_folder} -> bucket '{store.bucket}'"
          + (" (DRY RUN)" if args.dry_run else ""))
    results = migrate(store, args.upload_folder, names, dry_run=args.dry_run)
    print()
    for outcome, items in results.items():
        if items:
            print(f"{outcome:<18} {len(items)}")
    bad = results["failed"] + results["conflict"]
    if results["missing locally"]:
        print("\nNote: 'missing locally' images were already missing before the migration; "
              "those parts will show the placeholder image.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
