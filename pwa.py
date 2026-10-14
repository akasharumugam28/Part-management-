"""PWA endpoints: service worker (served from the root scope), manifest, offline page."""
from flask import Blueprint, current_app, render_template, send_from_directory

bp = Blueprint("pwa", __name__)


@bp.route("/service-worker.js")
def service_worker():
    response = send_from_directory(
        current_app.static_folder, "service-worker.js", mimetype="application/javascript"
    )
    # Browsers must always revalidate the worker so updates are picked up.
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@bp.route("/manifest.webmanifest")
def manifest():
    response = send_from_directory(
        current_app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json"
    )
    response.headers["Cache-Control"] = "no-cache"
    return response


@bp.route("/offline")
def offline():
    return render_template("offline.html")
