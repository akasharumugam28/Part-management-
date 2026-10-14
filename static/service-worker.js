/* Part Manager service worker.
 *
 * Caching policy:
 *  - Static assets (/static/...) and the offline page are cached.
 *  - Inventory pages, uploaded images and every non-GET request always go to
 *    the network and are NEVER stored, so private data is not kept in a cache
 *    and nothing is reported as saved unless the server confirmed it.
 *  - If a page navigation fails because the device is offline, the cached
 *    offline page is shown instead.
 *
 * Bump VERSION whenever static assets change (keep it in sync with
 * Config.ASSET_VERSION) so clients receive an update.
 */
const VERSION = "1.4.0";
const CACHE = `trolley-static-${VERSION}`;
const OFFLINE_URL = "/offline";

const PRECACHE = [
  OFFLINE_URL,
  `/static/css/style.css?v=${VERSION}`,
  `/static/js/app.js?v=${VERSION}`,
  `/static/icons/icon-192.png?v=${VERSION}`,
  `/static/icons/apple-touch-icon.png?v=${VERSION}`,
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-maskable-512.png",
  "/static/img/placeholder-part.svg",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      // credentials: "omit" -> the offline page is fetched anonymously, so it
      // can never contain anything from the admin's session.
      cache.addAll(PRECACHE.map((url) => new Request(url, { credentials: "omit", cache: "reload" })))
    )
  );
  // Do not skipWaiting automatically: the page shows an "update available"
  // banner and asks the worker to activate when the admin chooses to reload.
});

self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k.startsWith("trolley-") && k !== CACHE).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Writes and cross-origin requests: straight to the network, untouched.
  if (request.method !== "GET" || url.origin !== self.location.origin) return;

  // Page navigations: network only; offline fallback on failure.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() =>
        caches.match(OFFLINE_URL).then((r) => r || new Response("You are offline. Reconnect to save changes.", {
          status: 503,
          headers: { "Content-Type": "text/plain; charset=utf-8" },
        }))
      )
    );
    return;
  }

  // Static assets: cache first, then network (and store the result).
  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    event.respondWith(
      caches.match(request).then(
        (cached) =>
          cached ||
          fetch(request).then((response) => {
            if (response.ok && response.type === "basic") {
              const copy = response.clone();
              caches.open(CACHE).then((cache) => cache.put(request, copy));
            }
            return response;
          })
      )
    );
  }
  // Everything else (e.g. /uploads/ images, JSON) is not intercepted.
});
