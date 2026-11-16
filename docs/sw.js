/* Offline shell for the dashboard.
 *
 * The app shell is cached so the page opens instantly from the home screen,
 * but data.json is always network-first: showing a stale price as if it were
 * current would be worse than showing nothing.
 */

var CACHE = "radar-v2";  // bump on any shell change, or phones serve the old app.js from cache
var SHELL = ["./", "index.html", "styles.css", "app.js", "manifest.webmanifest", "icon.svg"];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE).then(function (cache) { return cache.addAll(SHELL); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== CACHE; })
        .map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") return;

  if (request.url.indexOf("data.json") !== -1) {
    // Network first, cache only as a last resort.
    event.respondWith(
      fetch(request).then(function (response) {
        var copy = response.clone();
        caches.open(CACHE).then(function (cache) { cache.put(request, copy); });
        return response;
      }).catch(function () { return caches.match(request); })
    );
    return;
  }

  event.respondWith(
    caches.match(request).then(function (cached) {
      return cached || fetch(request);
    })
  );
});
