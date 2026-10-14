"""PWA assets, service worker policy, offline page and security headers."""
import json
import re

from PIL import Image

from config import Config
from conftest import ROOT


def test_manifest(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("application/manifest+json")
    m = json.loads(r.data)
    assert m["name"] == "Part Manager"
    assert m["short_name"] == "Parts"
    assert m["display"] == "standalone"
    assert m["start_url"] == "/" and m["scope"] == "/"
    assert m["theme_color"] == "#0f766e"
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(i.get("purpose") == "maskable" for i in m["icons"])


def test_icons_exist_with_correct_sizes(client):
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("icon-maskable-512.png", 512)):
        path = ROOT / "static" / "icons" / name
        with Image.open(path) as img:
            assert img.size == (size, size)
        assert client.get(f"/static/icons/{name}").status_code == 200


def test_service_worker_served_from_root(client):
    r = client.get("/service-worker.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["Content-Type"]
    assert r.headers["Cache-Control"] == "no-cache"
    assert r.headers["Service-Worker-Allowed"] == "/"


def test_service_worker_version_matches_assets():
    sw = (ROOT / "static" / "service-worker.js").read_text(encoding="utf-8")
    assert re.search(r'const VERSION = "([^"]+)"', sw).group(1) == Config.ASSET_VERSION


def test_service_worker_does_not_cache_private_data():
    sw = (ROOT / "static" / "service-worker.js").read_text(encoding="utf-8")
    # Non-GET requests are never intercepted
    assert 'request.method !== "GET"' in sw
    # Navigations are network-only with offline fallback; only /static/ is cached
    assert "/offline" in sw
    assert 'url.pathname.startsWith("/static/")' in sw
    assert "/uploads/" not in sw.split("Everything else")[0].split("const PRECACHE")[1]
    # Update flow
    assert "SKIP_WAITING" in sw and "skipWaiting" in sw
    assert "caches.delete" in sw


def test_offline_page_is_public_and_generic(client, auth_client):
    anon = client.application.test_client().get("/offline")
    assert anon.status_code == 200
    html = anon.get_data(as_text=True)
    assert "You are offline. Reconnect to save changes." in html
    assert "Logout" not in html
    # Even when fetched with a session it must not include account UI
    html = auth_client.get("/offline").get_data(as_text=True)
    assert "Logout" not in html


def test_html_metadata_and_registration(client):
    html = client.get("/login").get_data(as_text=True)
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    assert '<meta name="theme-color" content="#0f766e">' in html
    assert 'name="viewport"' in html
    assert 'data-sw-url="/service-worker.js"' in html
    app_js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "navigator.serviceWorker" in app_js and "beforeinstallprompt" in app_js
    assert "You are offline. Reconnect to save changes." in app_js


def test_static_assets_listed_in_precache_exist(client):
    sw = (ROOT / "static" / "service-worker.js").read_text(encoding="utf-8")
    block = sw.split("const PRECACHE = [")[1].split("];")[0]
    urls = re.findall(r'"(/[^"]*)"|`(/[^`]*)`', block)
    for plain, templ in urls:
        url = (plain or templ).replace("${VERSION}", Config.ASSET_VERSION)
        assert client.get(url).status_code == 200, url


def test_security_headers(auth_client):
    r = auth_client.get("/")
    assert r.headers["Cache-Control"] == "no-store"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_templates_have_no_inline_scripts_or_styles():
    """The CSP forbids inline script/style, so templates must not use them."""
    for tpl in (ROOT / "templates").glob("*.html"):
        text = tpl.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), tpl.name
        assert "style=" not in text and "<style" not in text, tpl.name
        assert not re.search(r"\son[a-z]+=", text), tpl.name
