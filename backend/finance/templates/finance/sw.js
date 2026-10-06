{% autoescape off %}// Service worker (docs/qa/PLAN-011-dashboard-reports-mobile.md D9), rendered by finance.pwa.
// It keeps only the static files listed in PRECACHE and answers only requests for them.
// Every other request (pages, exports, /healthz, /grid/, /reports/, any API) is never
// answered here: the browser sends it to the network as if no worker existed, and nothing
// it returns is ever stored. No offline mode, no push, no background sync.
"use strict";

const VERSION = {{ version_json }};
const PRECACHE = {{ precache_json }};
const CACHE_NAME = "finance-static-" + VERSION;
const PRECACHED = new Set(PRECACHE);

self.addEventListener("install", function (event) {
  // cache: "reload" skips the HTTP cache, so a new version stores fresh copies.
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then(function (cache) {
        return cache.addAll(
          PRECACHE.map(function (url) {
            return new Request(url, { cache: "reload", credentials: "same-origin" });
          })
        );
      })
      .then(function () {
        return self.skipWaiting();
      })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches
      .keys()
      .then(function (names) {
        return Promise.all(
          names
            .filter(function (name) {
              return name !== CACHE_NAME;
            })
            .map(function (name) {
              return caches.delete(name);
            })
        );
      })
      .then(function () {
        return self.clients.claim();
      })
  );
});

function isPrecached(request) {
  if (request.method !== "GET" || request.mode === "navigate") {
    return false;
  }
  const url = new URL(request.url);
  return url.origin === self.location.origin && url.search === "" && PRECACHED.has(url.pathname);
}

self.addEventListener("fetch", function (event) {
  if (!isPrecached(event.request)) {
    // Passthrough: no respondWith, so the network answers and nothing is cached.
    return;
  }
  // Static branch: cache first. A precached file missing from the cache (evicted) is
  // fetched and stored again, unless it is marked private or no-store; this is the only
  // place the worker stores a response at run time.
  event.respondWith(
    caches.open(CACHE_NAME).then(function (cache) {
      return cache.match(event.request).then(function (cached) {
        if (cached) {
          return cached;
        }
        return fetch(event.request).then(function (response) {
          const control = response.headers.get("Cache-Control") || "";
          if (response.ok && response.type === "basic" && !/private|no-store/i.test(control)) {
            cache.put(event.request, response.clone());
          }
          return response;
        });
      });
    })
  );
});
{% endautoescape %}
