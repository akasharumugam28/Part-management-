"""Admin authentication: login and logout."""
import functools
import logging
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import models
from extensions import limiter

bp = Blueprint("auth", __name__)
log = logging.getLogger(__name__)

# Endpoints reachable without a session.
PUBLIC_ENDPOINTS = {"auth.login", "static", "pwa.service_worker", "pwa.offline", "pwa.manifest"}
# Public endpoints that never render anything depending on the logged-in admin.
ASSET_ENDPOINTS = {"static", "pwa.service_worker", "pwa.manifest"}


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("admin") is None:
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapped


@bp.before_app_request
def load_admin():
    g.admin = None
    if request.endpoint in ASSET_ENDPOINTS:
        return  # public files: no database round trip per CSS/JS/icon request
    admin_id = session.get("admin_id")
    if admin_id is not None:
        admin = models.get_admin(admin_id)
        if admin is None:
            session.clear()
        else:
            g.admin = admin


def _safe_next(target):
    if not target:
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//"):
        return None
    return target


def _login_rate_limit():
    return current_app.config["LOGIN_RATE_LIMIT"]


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit(_login_rate_limit, methods=["POST"])
def login():
    if g.admin is not None:
        return redirect(url_for("parts.dashboard"))

    if request.method == "GET":
        return render_template("login.html", no_admin=not models.admin_exists())

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    if not username or not password:
        flash("Please fill in all required fields.", "error")
        return render_template("login.html", username=username), 400

    admin = models.verify_admin(username, password)
    if admin is None:
        log.warning("Failed login attempt for username=%r from %s", username, request.remote_addr)
        flash("Invalid username or password.", "error")
        return render_template("login.html", username=username), 401

    next_url = _safe_next(request.args.get("next"))
    session.clear()  # prevent session fixation
    session.permanent = True
    session["admin_id"] = admin["id"]
    log.info("Admin %r logged in from %s", admin["username"], request.remote_addr)
    flash("Login successful.", "success")
    return redirect(next_url or url_for("parts.dashboard"))


@bp.route("/logout", methods=["POST"])
def logout():
    if g.admin is not None:
        log.info("Admin %r logged out", g.admin["username"])
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))

