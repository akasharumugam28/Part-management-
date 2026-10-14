"""Part Manager - Flask application factory.

Development:  flask --app app run --debug
Production:   gunicorn -c gunicorn.conf.py wsgi:app   (or waitress-serve on Windows)
"""
import logging
import mimetypes
import os
import secrets
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_wtf.csrf import CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

import auth  # noqa: E402
import database  # noqa: E402
import part_routes  # noqa: E402
import pwa  # noqa: E402
from config import Config  # noqa: E402
from extensions import csrf, limiter  # noqa: E402

mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("image/webp", ".webp")

CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
    "font-src 'self'; connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


def create_app(overrides=None):
    app = Flask(__name__, instance_path=str(Config.INSTANCE_DIR))
    app.config.from_object(Config)
    if overrides:
        app.config.update(overrides)

    _ensure_secret_key(app)
    for key in ("UPLOAD_FOLDER", "BACKUP_FOLDER", "LOG_FOLDER"):
        Path(app.config[key]).mkdir(parents=True, exist_ok=True)
    _configure_logging(app)

    if app.config["TRUST_PROXY"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    csrf.init_app(app)
    limiter.init_app(app)
    database.register(app)
    database.init_db(app.config["DATABASE"])  # idempotent CREATE IF NOT EXISTS

    app.register_blueprint(auth.bp)
    app.register_blueprint(part_routes.bp)
    app.register_blueprint(pwa.bp)

    @app.route("/uploads/<path:filename>")
    @auth.login_required
    def uploaded_file(filename):
        response = send_from_directory(app.config["UPLOAD_FOLDER"], filename)
        # Filenames are random and never reused, so the browser may keep them;
        # "private" keeps them out of shared caches.
        response.headers["Cache-Control"] = "private, max-age=86400"
        return response

    @app.context_processor
    def template_helpers():
        def image_url(filename, kind="part"):
            if filename:
                return url_for("uploaded_file", filename=filename)
            return url_for("static", filename=f"img/placeholder-{kind}.svg")

        def asset(path):
            return url_for("static", filename=path, v=app.config["ASSET_VERSION"])

        return {"image_url": image_url, "asset": asset}

    @app.after_request
    def security_headers(response):
        h = response.headers
        h.setdefault("Content-Security-Policy", CSP)
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Permissions-Policy", "geolocation=(), microphone=()")
        if request.is_secure:
            h.setdefault("Strict-Transport-Security", "max-age=31536000")
        if not request.path.startswith("/static/") and "Cache-Control" not in h:
            # Authenticated inventory pages must never be stored by any cache.
            h["Cache-Control"] = "no-store"
        return response

    _register_error_handlers(app)
    return app


def _ensure_secret_key(app):
    if app.config.get("SECRET_KEY"):
        return
    if app.debug or app.testing or os.environ.get("FLASK_DEBUG") in ("1", "true"):
        app.config["SECRET_KEY"] = secrets.token_hex(32)
        app.logger.warning("SECRET_KEY not set; using a temporary key (sessions reset on restart).")
        return
    raise RuntimeError("SECRET_KEY environment variable must be set in production.")


def _configure_logging(app):
    root = logging.getLogger()
    if app.testing or any(getattr(h, "_trolley", False) for h in root.handlers):
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        Path(app.config["LOG_FOLDER"]) / "app.log", maxBytes=1_000_000, backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler._trolley = True
    root.addHandler(file_handler)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers if h is not file_handler):
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        console._trolley = True
        root.addHandler(console)


def _wants_json():
    return request.headers.get("X-Requested-With") == "fetch"


def _register_error_handlers(app):
    def error_page(status, title, message):
        if _wants_json():
            return jsonify(ok=False, message=message), status
        return render_template("error.html", status=status, title=title, message=message), status

    @app.errorhandler(400)
    def bad_request(_e):
        return error_page(400, "Bad request", "The request could not be processed.")

    @app.errorhandler(404)
    def not_found(_e):
        return error_page(404, "Not found", "The part you are looking for does not exist.")

    @app.errorhandler(405)
    def method_not_allowed(_e):
        return error_page(405, "Not allowed", "That action is not allowed here.")

    @app.errorhandler(413)
    def too_large(_e):
        mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return error_page(413, "Upload too large", f"The upload is larger than {mb} MB in total.")

    @app.errorhandler(429)
    def rate_limited(_e):
        message = "Too many login attempts. Please wait a minute and try again."
        if request.endpoint == "auth.login":
            flash(message, "error")
            return render_template("login.html", username=request.form.get("username", "")), 429
        return error_page(429, "Too many requests", message)

    @app.errorhandler(CSRFError)
    def csrf_error(_e):
        message = "Your session expired. Please refresh the page and try again."
        if _wants_json():
            return jsonify(ok=False, message=message), 400
        flash(message, "error")
        return render_template("error.html", status=400, title="Session expired", message=message), 400

    @app.errorhandler(500)
    def server_error(_e):
        return error_page(500, "Error", "Something went wrong. Please try again.")


if __name__ == "__main__":
    # Development only. Use gunicorn/waitress in production (see README).
    create_app().run(debug=True)
