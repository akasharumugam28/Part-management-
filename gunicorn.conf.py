# gunicorn -c gunicorn.conf.py wsgi:app
# SQLite allows one writer at a time; a few workers with threads is plenty for
# a single-admin inventory app. With >1 worker, point RATELIMIT_STORAGE_URI at
# Redis so login rate limits are shared between workers.
bind = "127.0.0.1:8000"
workers = 2
threads = 4
timeout = 120  # slow image uploads on mobile connections
accesslog = "-"
errorlog = "-"
forwarded_allow_ips = "127.0.0.1"
