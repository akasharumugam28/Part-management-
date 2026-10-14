"""Real-browser end-to-end check (optional; not collected by pytest).

Starts the app under waitress on localhost, then drives installed Chrome/Edge
with Playwright through the main flows, service worker, offline mode,
installability and mobile/desktop layouts.

    pip install playwright
    python tests/e2e/browser_e2e.py            # uses installed Chrome
    python tests/e2e/browser_e2e.py msedge     # or Edge

Screenshots are written to tests/e2e/screenshots/.
"""
import re
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SHOTS = Path(__file__).resolve().parent / "screenshots"
PY = sys.executable
results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))
    print(("PASS " if condition else "FAIL ") + name + (f"  ({detail})" if detail and not condition else ""))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def png(path, color):
    Image.new("RGB", (300, 200), color).save(path, "PNG")
    return str(path)


def no_horizontal_overflow(page):
    return page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def main(channel="chrome"):
    SHOTS.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="tm-e2e-"))
    env = dict(os.environ, SECRET_KEY="e2e-" + "x" * 40, INSTANCE_DIR=str(tmp),
               DATABASE_PATH=str(tmp / "trolley_manager.db"), UPLOAD_FOLDER=str(tmp / "uploads"),
               BACKUP_FOLDER=str(tmp / "backups"), LOG_FOLDER=str(tmp / "logs"),
               SESSION_COOKIE_SECURE="false", RATELIMIT_ENABLED="false")
    subprocess.run([PY, "-m", "flask", "--app", "app", "create-admin"], cwd=ROOT, env=env, check=True)
    port = free_port()
    server = subprocess.Popen([PY, "-m", "waitress", "--host=127.0.0.1", f"--port={port}", "wsgi:app"],
                              cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://localhost:{port}"
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.2)

    img_a, img_b, img_c = (png(tmp / f"{n}.png", c) for n, c in
                           (("a", (13, 148, 136)), ("b", (200, 80, 40)), ("c", (40, 80, 200))))
    bad = tmp / "notes.png"
    bad.write_text("this is not an image")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel=channel)
            ctx = browser.new_context(base_url=base, viewport={"width": 390, "height": 844},
                                      is_mobile=True, has_touch=True, device_scale_factor=2)
            page = ctx.new_page()
            errors = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))

            # ---------------------------------------------------------- login
            page.goto("/")
            check("anonymous / redirects to login", "/login" in page.url)
            page.screenshot(path=SHOTS / "mobile-login.png", full_page=True)
            page.fill("#username", "admin")
            page.fill("#password", "wrong-password")
            page.click("main button[type=submit]")
            expect(page.locator(".toast-error")).to_contain_text("Invalid username or password.")
            check("invalid login message shown", True)
            page.fill("#username", "admin")
            page.fill("#password", "admin@123")
            page.click("main button[type=submit]")
            page.wait_for_url(base + "/")
            check("login goes straight to dashboard", page.url == base + "/")
            check("login success toast", page.locator(".toast-success", has_text="Login successful.").count() == 1)
            check("empty state shown", page.locator("text=No trolleys yet").count() == 1)
            page.screenshot(path=SHOTS / "mobile-dashboard-empty.png", full_page=True)

            # --------------------------------------------- service worker
            page.evaluate("navigator.serviceWorker.ready")
            page.reload()
            controlled = page.evaluate("!!navigator.serviceWorker.controller")
            check("service worker registered and controlling the page", controlled)
            cached = page.evaluate("""async () => {
                const keys = await caches.keys();
                const out = [];
                for (const k of keys) for (const r of await (await caches.open(k)).keys()) out.push(new URL(r.url).pathname);
                return out; }""")
            check("offline page + static assets precached", "/offline" in cached and "/static/css/style.css" in cached)
            check("no private pages cached", not any(u in cached for u in ("/", "/trolleys/new")) and
                  not any(u.startswith("/uploads/") for u in cached), str(cached))

            # ------------------------------------------ add trolley + parts
            page.click("main a:has-text('Add Trolley')")
            page.wait_for_url("**/trolleys/new")
            page.click("#create-btn")
            check("client-side required validation", page.locator("#form-error").is_visible()
                  and page.locator("#trolley_number-error").is_visible())
            page.fill("#trolley_number", "TR-E2E")
            page.fill("#trolley_name", "End to End Trolley")
            page.fill("#line_storage", "Line 1 Bay C")
            page.fill("#ols_storage", "OLS 7")
            page.set_input_files("#image", img_a)
            page.wait_for_function("document.querySelector('#image-preview').src.startsWith('blob:')", timeout=5000)
            check("trolley image preview shown", page.locator("#image-preview").get_attribute("src").startswith("blob:"))

            page.click("#add-part-btn")
            check("part required-field validation", page.locator("#pe-part_name").get_attribute("aria-invalid") == "true")

            page.set_input_files("#pe-image", str(bad))
            expect(page.locator(".toast-error", has_text="Invalid image format.")).to_be_visible()
            check("invalid image rejected client-side", page.locator("#pe-image").input_value() == "")

            total = 52
            for i in range(1, total + 1):
                page.fill("#pe-part_name", f"Part {i}")
                page.fill("#pe-part_number", f"PN-{i:03d}")
                page.fill("#pe-line_storage", f"L{i}")
                page.fill("#pe-ols_storage", f"O{i}")
                if i in (1, 49, 52):
                    page.set_input_files("#pe-image", img_b)
                if i % 2:
                    page.click("#add-part-btn")
                else:
                    page.press("#pe-ols_storage", "Enter")  # Enter adds the part too
            check(f"{total} pending parts added (no 48 cap)", page.locator("#part-count").inner_text() == str(total))
            check("entry fields cleared after add", page.locator("#pe-part_name").input_value() == "")

            # edit pending part #2
            page.locator("#pending-parts li").nth(1).locator('[data-action="edit"]').click()
            check("edit loads pending part", page.locator("#pe-part_name").input_value() == "Part 2")
            page.fill("#pe-part_name", "Part 2 edited")
            page.click("#add-part-btn")
            check("pending part edited", page.locator("#pending-parts li").nth(1).locator(".pending-name").inner_text() == "Part 2 edited")

            # delete pending part #3 (confirm dialog)
            page.locator("#pending-parts li").nth(2).locator('[data-action="delete"]').click()
            expect(page.locator("#confirm-dialog")).to_be_visible()
            page.click("#confirm-ok")
            check("pending part deleted", page.locator("#part-count").inner_text() == str(total - 1))
            page.screenshot(path=SHOTS / "mobile-add-trolley.png", full_page=False)
            check("mobile: no horizontal scroll on form", no_horizontal_overflow(page))

            # unsaved entry blocks create
            page.fill("#pe-part_name", "half typed")
            page.click("#create-btn")
            check("unsaved part entry warns before create", "not been added" in page.locator("#form-error").inner_text())
            page.fill("#pe-part_name", "")

            # double-submit prevention: slow the request down and click twice
            hits = []

            def slow(route):
                hits.append(1)
                time.sleep(1.0)
                route.continue_()

            page.route("**/trolleys/create", slow)
            page.click("#create-btn")
            # Second submission attempt while the first is in flight.
            still_disabled = page.evaluate("""() => {
                const form = document.getElementById('trolley-form');
                form.requestSubmit();
                return document.getElementById('create-btn').disabled; }""")
            check("create button disabled while saving", still_disabled)
            page.wait_for_url(re.compile(r".*/trolleys/\d+$"), timeout=30000)
            page.unroute("**/trolleys/create")
            check("only one create request sent", len(hits) == 1, f"hits={len(hits)}")
            expect(page.locator(".toast-success", has_text="Trolley created successfully")).to_be_visible()
            check("'Trolley created successfully' shown", True)
            check("details shows 51 parts", page.locator("#part-total").inner_text() == str(total - 1))
            check("part 49+ saved with image", page.locator("tr", has_text="PN-052").locator("img").get_attribute("src").startswith("/uploads/"))
            page.screenshot(path=SHOTS / "mobile-trolley-details.png", full_page=False)
            check("mobile: no horizontal scroll on details", no_horizontal_overflow(page))
            detail_url = page.url

            # ------------------------------------------------------ add part
            page.click("summary:has-text('Add Part')")
            page.fill("#part_name", "Late Part")
            page.fill("#part_number", "PN-LATE")
            page.fill("#line_storage", "LL")
            page.fill("#ols_storage", "OO")
            page.set_input_files("#image", img_c)
            page.click("main button:has-text('Save Part')")
            expect(page.locator(".toast-success", has_text="Part added successfully.")).to_be_visible()
            check("part added after creation; count updated", page.locator("#part-total").inner_text() == str(total))

            row = page.locator("tr", has_text="PN-LATE")
            check("parts have no edit/delete buttons",
                  row.locator("[aria-label^='Edit part'], [aria-label^='Delete part']").count() == 0)

            # ---------------------------------------------- offline mode
            ctx.set_offline(True)
            page.evaluate("window.dispatchEvent(new Event('offline'))")
            expect(page.locator("#offline-banner")).to_be_visible()
            check("offline banner text", "You are offline. Reconnect to save changes." in page.locator("#offline-banner").inner_text())
            check("write buttons disabled offline", page.locator("button[aria-label^='Delete part']").first.is_disabled())
            page.screenshot(path=SHOTS / "mobile-offline-banner.png")
            page.goto(base + "/")
            check("navigation offline shows offline page", page.locator("h1", has_text="You are offline").count() == 1)
            check("offline page has no private data", "TR-E2E" not in page.content())
            page.screenshot(path=SHOTS / "mobile-offline-page.png")
            ctx.set_offline(False)
            page.goto(detail_url)
            check("back online works", page.locator("#part-total").count() == 1)

            # ---------------------------------------------- installability
            cdp = ctx.new_cdp_session(page)
            manifest = cdp.send("Page.getAppManifest")
            check("manifest parsed by browser without errors", not manifest.get("errors"), str(manifest.get("errors")))

            # ------------------------------------ edit / delete trolley
            page.click("main a:has-text('Edit Trolley')")
            page.check("input[name=remove_image]")
            page.fill("#trolley_name", "Renamed Trolley")
            page.click("main button:has-text('Save Changes')")
            expect(page.locator(".toast-success", has_text="Trolley updated successfully.")).to_be_visible()
            check("trolley updated and image removed", "placeholder-trolley.svg" in page.locator(".detail-image").get_attribute("src"))

            # dashboard at desktop size
            desk = browser.new_context(base_url=base, viewport={"width": 1366, "height": 900},
                                       storage_state=ctx.storage_state())
            dpage = desk.new_page()
            dpage.goto("/")
            dpage.screenshot(path=SHOTS / "desktop-dashboard.png", full_page=True)
            check("desktop: no horizontal scroll", no_horizontal_overflow(dpage))
            dpage.goto(detail_url)
            check("desktop: parts rendered as table header", dpage.locator(".parts-table thead").is_visible())
            dpage.screenshot(path=SHOTS / "desktop-trolley-details.png")
            dpage.fill("#part-filter", "pn-05")
            visible = dpage.locator("#parts-table-body tr:visible").count()
            check("live part filter", visible == 3, f"visible={visible}")  # PN-050..052
            desk.close()

            page.goto("/")
            page.fill("#q", "nomatch")
            check("live search empty state", page.locator("#no-matches").is_visible())
            page.fill("#q", "")
            page.screenshot(path=SHOTS / "mobile-dashboard.png", full_page=True)
            page.locator("button[aria-label^='Delete trolley']").click()
            page.click("#confirm-ok")
            expect(page.locator(".toast-success", has_text="Trolley deleted successfully.")).to_be_visible()
            check("trolley deleted -> empty state", page.locator("text=No trolleys yet").count() == 1)

            # ---------------------------------------------------- logout
            page.click("button:has-text('Logout')")
            page.wait_for_url("**/login")
            page.goto("/")
            check("logout protects dashboard", "/login" in page.url)

            csp = [e for e in errors if "Content Security Policy" in e or "Refused" in e]
            check("no CSP violations", not csp, "; ".join(csp[:3]))
            other = [e for e in errors if e not in csp and "Failed to load resource" not in e]
            check("no JavaScript errors", not other, "; ".join(other[:3]))
            browser.close()

            # Chrome never offers installation in incognito, which is what a
            # normal Playwright context is, so check it in a real profile.
            profile = p.chromium.launch_persistent_context(str(tmp / "profile"), channel=channel, base_url=base)
            ppage = profile.new_page()
            ppage.goto("/login")
            ppage.fill("#username", "admin")
            ppage.fill("#password", "admin@123")
            ppage.click("main button[type=submit]")
            ppage.wait_for_url(base + "/")
            ppage.evaluate("navigator.serviceWorker.ready")
            ppage.reload()
            inst = profile.new_cdp_session(ppage).send("Page.getInstallabilityErrors")
            check("PWA installable (no installability errors)", inst.get("installabilityErrors") == [], str(inst))
            profile.close()
    finally:
        server.terminate()
        server.wait(10)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} browser checks passed")
    for name, _, detail in failed:
        print("  FAILED:", name, detail)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "chrome"))
