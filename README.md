# Part Manager

A mobile-first Progressive Web App for managing parts and their **Line Storage** and **OLS Storage** locations.

Built with **Python Flask, Jinja2, SQLite, HTML, CSS and vanilla JavaScript**. It uses no frontend frameworks.

- One admin account. There is no public sign-up. After login the admin goes straight to the dashboard; the password can be changed any time from the header.
- The dashboard lists every part with search, and **Create Part** adds a new one (Part Name, Part Number, Line Storage, OLS Storage, optional image). **Save & Add Another** keeps the form open for the next part.
- Every part can be viewed, edited (including replacing or removing its image) and deleted from the dashboard row or its details page. Delete asks for confirmation.
- Optional part images (JPG, PNG, WebP). Each upload is validated server-side and re-encoded under a random filename.
- Installable PWA with an offline fallback page. While offline, write actions are disabled.

---

## 1. Project structure

```
app.py                 Application factory, security headers, error handlers, /uploads route
config.py              Configuration (all secrets from environment variables)
database.py            SQLite connection, schema, transactions, CLI commands
models.py              Data-access functions (parameterized SQL only)
auth.py                Login, logout, login_required
exports.py             Excel and PDF downloads of the parts list
part_routes.py         Dashboard, Create Part, part details, edit and delete
pwa.py                 /service-worker.js, /manifest.webmanifest, /offline
images.py              Upload validation (Pillow), storage, cleanup
validation.py          Server-side field validation
extensions.py          CSRF (Flask-WTF) and rate limiter (Flask-Limiter)
wsgi.py                WSGI entry point for gunicorn / waitress
gunicorn.conf.py       Production gunicorn settings
deploy/                nginx and systemd examples
templates/             Jinja2 templates
static/css/style.css   Teal-and-white, mobile-first styles
static/js/app.js       Toasts, confirm dialogs, loading states, offline mode, SW + install prompt
static/manifest.webmanifest, static/service-worker.js, static/icons/
scripts/generate_icons.py
tests/                 pytest suite; tests/e2e/browser_e2e.py = real-browser checks
instance/              Created at runtime: trolley_manager.db, uploads/, backups/, logs/  (git-ignored)
```

## 2. Installation

Requires Python 3.10+ (developed and tested on Python 3.14.3).

**Windows (PowerShell)**

```powershell
cd "C:\path\to\trolley-manager"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"   # paste into SECRET_KEY in .env
```

**Linux / macOS**

```bash
cd /path/to/trolley-manager
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"    # paste into SECRET_KEY in .env
```

For local development over plain `http`, set `SESSION_COOKIE_SECURE=false` in `.env`. Keep it `true` in production.

### Create the database and the admin account

```bash
flask --app app init-db         # creates instance/trolley_manager.db (safe to re-run)
flask --app app create-admin    # seeds username "admin", password "admin@123" (hashed)
```

- The password is stored as a Werkzeug hash (scrypt), never in plaintext.
- Logging in goes straight to the dashboard. There is no in-app password change; to set a new password run `flask --app app create-admin --reset --prompt-password`.
- Each part can hold **sub-parts**: open a part and use **Add Sub-part** (name, number, optional Trolley Qty and image). Sub-parts can be edited and deleted there; deleting a part deletes its sub-parts too. Searching for a sub-part's name or number finds its part.
- **Download All** on the dashboard downloads every part with its sub-parts in one Excel or PDF file. Each part row (and the part page) also has a **Download** button for just that part and its sub-parts. Both ask for a file name first. Sub-parts are listed under their part as 1.1, 1.2, ...
- Each part can have an optional **Trolley Qty** (parts per trolley). On the Create/Edit form, tap **20** or **40** or type any whole number from 1 to 9999. It is shown next to the part image in the list, on the details page, and in the Excel/PDF downloads.
- `flask --app app create-admin --prompt-password` sets a different initial password interactively instead of the default.
- `flask --app app create-admin --reset` resets a forgotten password back to `admin@123`.

## 3. Local development

```bash
flask --app app run --debug
```

Open <http://localhost:5000> and log in as `admin` / `admin@123`.

Service workers and PWA installation need a **secure context**: HTTPS, or `http://localhost`. On the development machine, `localhost` works. To test on a phone, use one of these:

- Use an HTTPS tunnel (for example `cloudflared tunnel --url http://localhost:5000` or `ngrok http 5000`) and open the `https://` URL on the phone. Set `TRUST_PROXY=true`.
- Or use Chrome on Android with USB debugging and `chrome://inspect` → *Port forwarding* (`5000 → localhost:5000`), then open `http://localhost:5000` on the phone.

Over a plain LAN address such as `http://192.168.x.x:5000`, the app works but it **cannot be installed** and has no service worker. You also need `SESSION_COOKIE_SECURE=false` there.

## 4. Database

Production: **Supabase PostgreSQL** when `DATABASE_URL` is set (schema: `db/schema_postgres.sql`). Local development: SQLite file `instance/trolley_manager.db`, or `DATABASE_PATH` (schema in `database.py`). Both have the same tables and columns. See [docs/SUPABASE_VERCEL.md](docs/SUPABASE_VERCEL.md).

| Table | Columns |
|---|---|
| `admin` | id, username (unique, case-insensitive), password_hash, must_change_password, created_at |
| `parts` | id, part_name, part_number, line_storage, ols_storage, image_filename, trolley_qty, created_at, updated_at |
| `sub_parts` | id, part_id (→ parts, cascade delete), part_name, part_number, trolley_qty, image_filename, created_at, updated_at |

- `PRAGMA foreign_keys = ON` is set on every connection. WAL journal mode is enabled.
- Indexes: `parts(part_name)`, `parts(part_number)`.
- CHECK constraints reject blank or over-length values even if the app layer is bypassed.
- If saving a part fails, its uploaded image is deleted so no orphan file is left.
- **Upgrading from the trolley version:** on first start the app copies the database to `trolley_manager.db.before-parts-only.bak`, keeps every existing part (dropping only its trolley link) and drops the `trolleys` table. Old trolley images are no longer referenced; remove them with `flask --app app cleanup-uploads`.
- Only image **filenames** are stored. The image files themselves live in `UPLOAD_FOLDER`.

### Deletion policy

- **Deleting a part** removes the record and its image file, after the database commit succeeds.
- **Replacing or removing an image** deletes the old file after the update commits. If the update fails, the newly uploaded file is deleted instead.
- If a file can't be deleted (for example because it is locked on Windows), the failure is logged. `flask --app app cleanup-uploads` removes any image that no record references; add `--dry-run` to only list them.

## 5. Images

- Allowed formats: JPG, JPEG, PNG and WebP. The browser shows a preview and checks the type, size and that the file actually decodes.
- The server never trusts the filename or the browser:
  - It checks the extension and size (`MAX_IMAGE_MB`, default 5 MB).
  - It decodes the file with Pillow and requires the real format to be JPEG, PNG or WebP. Decompression bombs are rejected.
  - It re-encodes the image. This strips EXIF/GPS metadata and caps dimensions at `MAX_IMAGE_DIMENSION`, default 1600 px.
  - It saves the result under a random `uuid4` filename.
- Uploads are stored in a **private Supabase Storage bucket** when `SUPABASE_URL` is set; pages get short-lived signed URLs, and `/uploads/<file>` redirects to one **for the logged-in admin only**. Without Supabase they are stored in `instance/uploads/`, outside `static/`, and served by `/uploads/<file>` to the logged-in admin only.
- If there is no image, a placeholder is shown.
- **Production:** `UPLOAD_FOLDER` and the database must live on **persistent storage**, such as a VPS disk or a mounted volume. Many PaaS hosts (Heroku, and Render or Railway without a disk) have an *ephemeral* filesystem that is wiped on redeploy. Do not keep inventory images there.

## 6. Backups

```bash
flask --app app backup-db                  # instance/backups/trolley_manager-YYYYmmdd-HHMMSS.db
flask --app app backup-db --with-uploads   # also uploads-YYYYmmdd-HHMMSS.zip of all images
```

- Backups use SQLite's online backup API, so they are consistent while the app is running.
- The newest `BACKUP_KEEP` backups are kept (default 14).
- Put `BACKUP_FOLDER` on a different disk, or sync it off-site.

**Schedule daily backups**

Linux (cron, as the app user):

```
15 2 * * * cd /opt/trolley-manager && .venv/bin/flask --app app backup-db --with-uploads >> instance/logs/backup.log 2>&1
```

Windows (Task Scheduler, run daily):

```
Program:   C:\path\to\trolley-manager\.venv\Scripts\flask.exe
Arguments: --app app backup-db --with-uploads
Start in:  C:\path\to\trolley-manager
```

**Restore**: stop the server, then run:

```bash
flask --app app restore-db instance/backups/trolley_manager-20260101-021500.db
```

If you restore from a zip, also unzip the matching `uploads-*.zip` into `UPLOAD_FOLDER`.

## 7. Production deployment

Never use `flask run` or `app.run()` in production.

Checklist:

1. Set a strong `SECRET_KEY` in `.env`. The app refuses to start in production mode without one. Keep `.env` out of source control.
2. Serve over **HTTPS**. Secure cookies, PWA installation and service workers all need it.
3. Set `TRUST_PROXY=true` when running behind nginx, Caddy or a platform load balancer.
4. Put `DATABASE_PATH`, `UPLOAD_FOLDER`, `BACKUP_FOLDER` and `LOG_FOLDER` on persistent storage.
5. If you run more than one worker process, set `RATELIMIT_STORAGE_URI=redis://localhost:6379` so the login rate limit is shared between them.
6. Schedule backups (section 6).

**Linux: gunicorn + nginx + systemd**

```bash
sudo useradd --system --home /opt/trolley-manager trolley
sudo -u trolley git clone <repo> /opt/trolley-manager   # or copy the files
cd /opt/trolley-manager
sudo -u trolley python3 -m venv .venv
sudo -u trolley .venv/bin/pip install -r requirements.txt
sudo -u trolley cp .env.example .env && sudo -u trolley nano .env   # SECRET_KEY, TRUST_PROXY=true, paths
sudo -u trolley .venv/bin/flask --app app init-db
sudo -u trolley .venv/bin/flask --app app create-admin
sudo cp deploy/trolley-manager.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now trolley-manager
sudo cp deploy/nginx.conf /etc/nginx/sites-available/trolley-manager
sudo ln -s /etc/nginx/sites-available/trolley-manager /etc/nginx/sites-enabled/
sudo certbot --nginx -d trolley.example.com && sudo systemctl reload nginx
```

**Windows: waitress**

```powershell
.\.venv\Scripts\waitress-serve --host=127.0.0.1 --port=8000 wsgi:app
```

Put IIS, Caddy or nginx in front of it for HTTPS, and run it as a service, for example with NSSM.

**Updating the app:** when CSS or JS changes, bump `ASSET_VERSION` in `config.py` **and** `VERSION` in `static/service-worker.js` to the same value. Open clients then show a "new version available — Reload" banner. A test checks that the two values match.

**Vercel + Supabase:** see [docs/SUPABASE_VERCEL.md](docs/SUPABASE_VERCEL.md) for creating the tables and bucket, migrating the SQLite data and images (`scripts/migrate_sqlite_to_postgres.py`, `scripts/migrate_uploads_to_supabase.py`), the environment variables and deployment.

## 8. Installing on an Android phone

1. Open the HTTPS URL of your deployment in **Chrome** on the phone.
2. Log in. If Chrome offers it, tap **Install** in the app header, or the *Install app* banner.
3. Otherwise open Chrome's **⋮** menu and choose **Install app** (or **Add to Home screen**).
4. Part Manager appears on the home screen and opens full-screen (standalone) with the teal theme.

Installation needs HTTPS, the manifest, and a registered service worker. All three are in place; see section 3 for testing without a deployment.

## 9. Offline behaviour

- Cached: CSS, JS, icons, placeholders, the manifest, and an anonymous `/offline` page.
- **Never cached:** inventory pages, uploaded images, and any POST. Every authenticated response is sent with `Cache-Control: no-store`.
- When the connection drops, a banner reads **"You are offline. Reconnect to save changes."** and all save buttons are disabled. If you open a page while offline, the offline page is shown.
- The app never reports a save that the server did not confirm. A failed **Create Part** keeps the entered details on screen so you can retry.
- There is no offline queue or sync. Changes must be made while online.

## 10. Testing

```bash
pip install -r requirements.txt
python -m pytest                     # SQLite + local images
$env:TEST_DATABASE_URL = "postgresql://postgres@127.0.0.1:5432/part_manager_test"
python -m pytest                     # the same suite on a disposable PostgreSQL database
```

The pytest suite covers:
- admin seeding and hashing
- valid and invalid login
- logout (POST only) and every protected GET and POST route
- open-redirect protection, cookie flags, login rate limiting, and CSRF rejection and acceptance
- create, view, search, edit and delete for parts; all trolley routes return 404
- part image upload, replace and remove, invalid and oversize images, and orphan cleanup
- required fields and length limits
- migration of an old trolley database (parts kept, backup written)
- persistence across an app restart
- rollback on database failure, with no leaked internals and no orphaned files
- CHECK constraints, schema and indexes
- backups
- manifest, icons, service worker headers and caching policy, offline page, security headers and CSP compliance

**Real-browser end-to-end checks** (optional) use Playwright with your installed Chrome or Edge; no browser download is needed:

```bash
pip install playwright
python tests/e2e/browser_e2e.py          # Chrome
python tests/e2e/browser_e2e.py msedge   # Edge
```

The script starts the app under waitress on localhost and checks the full flow at phone (390×844) and desktop (1366×900) sizes:
- login straight to the dashboard
- adding 52 pending parts with images, and editing and deleting pending parts
- double-submit prevention
- part CRUD with confirmation dialogs
- service worker registration and precache contents
- offline banner, disabled buttons and the offline page
- Chrome's installability check (`Page.getInstallabilityErrors`)
- no horizontal scrolling at either size
- no CSP violations or JavaScript errors

Screenshots are saved to `tests/e2e/screenshots/`.

## 11. Configuration reference (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | — (required) | Session signing key |
| `SESSION_COOKIE_SECURE` | `true` | Send the cookie only over HTTPS |
| `SESSION_HOURS` | `8` | Session lifetime |
| `INSTANCE_DIR`, `DATABASE_PATH`, `UPLOAD_FOLDER`, `BACKUP_FOLDER`, `LOG_FOLDER` | `instance/…` | Storage locations |
| `BACKUP_KEEP` | `14` | Backups retained |
| `MAX_IMAGE_MB` / `MAX_IMAGE_DIMENSION` | `5` / `1600` | Per-image limits |
| `MAX_REQUEST_MB` | `200` | Total size of one request |
| `LOGIN_RATE_LIMIT` | `5 per minute;30 per hour` | Failed and successful login POSTs per IP |
| `RATELIMIT_STORAGE_URI` | PostgreSQL if `DATABASE_URL`, else `memory://` | Where login attempt counters are kept |
| `DATABASE_URL` | — | PostgreSQL (Supabase) connection string; unset = SQLite |
| `DB_CONNECT_TIMEOUT` | `10` | Seconds to wait for a database connection |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | — | Supabase Storage for images; unset = `UPLOAD_FOLDER` |
| `SUPABASE_BUCKET` | `part-images` | Private bucket for images |
| `SUPABASE_SIGNED_URL_TTL` / `STORAGE_TIMEOUT` | `3600` / `15` | Signed image link lifetime / Storage request timeout (seconds) |
| `ALLOW_EPHEMERAL_STORAGE` | `false` | Let a Vercel deployment start without Supabase (data is lost) |
| `TRUST_PROXY` | `false` | Honour `X-Forwarded-*` from a reverse proxy |

## 12. Known limitations

- **Single admin.** There are no roles or extra users, by design.
- **No offline editing.** Changes need a connection; there is no background sync.
- **Rate limits with several workers.** Without `DATABASE_URL` the in-memory limit store is per process. Use Redis when running more than one worker. With `DATABASE_URL` the counters are shared through PostgreSQL.
- **SQLite allows one writer at a time.** That is fine for a single admin, but not intended for many concurrent writers.
#   P a r t - m a n a g e m e n t -  
 