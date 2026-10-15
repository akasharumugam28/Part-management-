# Supabase + Vercel setup and cutover

How Part Manager runs in production: **Vercel** hosts the Flask app,
**Supabase PostgreSQL** stores the data and a **private Supabase Storage
bucket** stores the images. Local development still works with no Supabase
at all (SQLite file + `instance/uploads/`).

## How the app picks its backends

| Setting present | Database | Images | Login rate-limit counters |
|---|---|---|---|
| nothing (local dev) | SQLite `instance/trolley_manager.db` | `instance/uploads/` | memory |
| `DATABASE_URL` | PostgreSQL | (see next row) | PostgreSQL table `rate_limits` |
| `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` | (see above) | private bucket, signed URLs | (see above) |

On Vercel the app **refuses to start** unless `DATABASE_URL`, `SUPABASE_URL`
and `SUPABASE_SERVICE_ROLE_KEY` are set. Without them every part and image
would be lost on the next cold start.

What stays the same: every page, route, form, the PWA, CSRF, the session
cookie, password hashing (werkzeug scrypt; existing hashes are copied as-is),
and image validation/re-encoding. `image_filename` still holds the same
`<uuid>.<ext>` name; it is now the object's name in the bucket.

## 1. Create the Supabase project

1. <https://supabase.com/dashboard> → **New project**. Pick the region closest
   to your Vercel region (Vercel default `iad1` = US East; for India choose
   Mumbai on both: Supabase `ap-south-1`, Vercel `bom1`).
2. Save the database password in your password manager.
3. **Connect** (top bar) → copy two connection strings and put the password in:
   * **Transaction pooler**, port **6543**: this goes to Vercel as `DATABASE_URL`.
   * **Session pooler**, port **5432**: used from your PC for the migration
     and backups (works over IPv4; the "Direct connection" may be IPv6-only).
   Append `?sslmode=require` to both.
4. **Project Settings → API**: copy the **Project URL** (`SUPABASE_URL`) and
   the **service_role** secret (`SUPABASE_SERVICE_ROLE_KEY`). Never use the
   service-role key in browser code; this app only uses it on the server.

## 2. Create the tables and the bucket (from your PC)

```powershell
cd "C:\Users\ELCOT\Desktop\part management"
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Session-pooler string (port 5432) for this PowerShell window only:
$env:DATABASE_URL = "postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require"
$env:SUPABASE_URL = "https://<ref>.supabase.co"
$env:SUPABASE_SERVICE_ROLE_KEY = "<service-role-key>"

flask --app app init-db        # creates tables, indexes, RLS (safe to repeat)
flask --app app init-storage   # creates the PRIVATE bucket "part-images"
```

`init-db` runs [db/schema_postgres.sql](../db/schema_postgres.sql); you can
also paste that file into the Supabase **SQL editor**. It enables Row Level
Security with no policies and revokes the `anon`/`authenticated` roles, so
Supabase's public Data API cannot read the tables. The app's own connection
(the table owner) is unaffected.

If you prefer the dashboard for the bucket: **Storage → New bucket**, name
`part-images`, **Public: off**, allowed MIME types `image/jpeg, image/png,
image/webp`.

## 3. Migrate the existing data

Do this when nobody is using the local app, so no edits are missed.

```powershell
# same PowerShell window as above (session-pooler DATABASE_URL set)
python scripts\migrate_sqlite_to_postgres.py --dry-run   # check, writes nothing
python scripts\migrate_sqlite_to_postgres.py             # copy + verify + commit
python scripts\migrate_sqlite_to_postgres.py --verify-only

python scripts\migrate_uploads_to_supabase.py --dry-run
python scripts\migrate_uploads_to_supabase.py
```

The database script:
* opens `instance/trolley_manager.db` **read-only** and checks its SHA-256
  is unchanged afterwards; writes a backup copy to `instance/backups/` first;
* copies `admin`, `parts`, `sub_parts` with their original ids, timestamps,
  quantities, NULLs and password hashes, then moves the id sequences past the
  highest id SQLite ever used;
* skips rows already copied unchanged (rerunning never duplicates anything)
  and **stops without writing anything** if an id exists with different values;
* prints row counts and trolley-qty totals for both sides.

The image script uploads every referenced file under the same name, never
overwrites a different file already in the bucket, and never touches local files.

It does **not** create any admin account. Your existing admin and password
come across unchanged.

## 4. Configure and deploy Vercel

**Vercel → Project → Settings → Environment Variables** (Production; add to
Preview too if you use preview deployments, ideally with a separate Supabase project):

| Name | Value |
|---|---|
| `SECRET_KEY` | new random value: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DATABASE_URL` | **transaction pooler** string (port 6543) + `?sslmode=require` |
| `SUPABASE_URL` | `https://<ref>.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | service_role secret |
| `SUPABASE_BUCKET` | `part-images` (optional, this is the default) |

Leave `RATELIMIT_STORAGE_URI`, `TRUST_PROXY`, `LOG_TO_FILE` unset; the
serverless defaults are right. Mark the keys as **Sensitive**.

[vercel.json](../vercel.json) routes every request to `wsgi.py`;
[.vercelignore](../.vercelignore) keeps `.env`, `instance/` (the SQLite
file and local images) and tests out of the upload.

Deploy by pushing to the connected Git branch, or with the CLI:

```powershell
npm i -g vercel
vercel login
vercel link                 # once, picks the existing project
vercel --prod
vercel logs <deployment-url>
```

## 5. Optional: least-privilege database role

[db/app_role.sql](../db/app_role.sql) creates `part_manager_app`, which can
only read/write rows in the app's four tables. Run it in the SQL editor
(after changing the password) and set Vercel's `DATABASE_URL` user to
`part_manager_app.<ref>`. Keep the `postgres` role for `init-db`,
`restore-db` and the migration script.

## 6. Backups

Check your Supabase plan's backup coverage (the free plan has historically
had none, and Storage files are never in database backups). Either way you
can keep your own copies. From your PC (session-pooler `DATABASE_URL` and the
Supabase variables set):

```powershell
flask --app app backup-db --with-uploads
```

This writes a SQLite copy of all tables plus a zip of the bucket to
`instance/backups/`. Restore one with
`flask --app app restore-db instance\backups\trolley_manager-<stamp>.db`
(replaces all rows in PostgreSQL after a confirmation prompt).

## Limits on Vercel

* **Request body 4.5 MB.** The image limit defaults to 4 MB there (`MAX_IMAGE_MB`).
  Whatever is uploaded is stored at 2 MB or less (`MAX_STORED_IMAGE_MB`).
* **Cold starts.** The first request after idle time takes a few seconds.
* Each request opens one short database connection through Supabase's pooler
  and closes it at the end. Nothing is cached on the instance.
* Logs go to stdout, so view them with Vercel → Deployment → **Logs**.

## Rollback

The SQLite file and `instance/uploads/` are never modified by the migration.
To go back, remove `DATABASE_URL` and `SUPABASE_*` locally and run the app as
before. Data entered in Supabase after the cutover can be pulled down with
`flask --app app backup-db --with-uploads`.
