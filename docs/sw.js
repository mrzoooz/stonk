/* Service worker: keep the app usable on a phone with no signal.

   Everything is network-first with a cache fallback. An earlier version cached
   the app shell cache-first under a fixed version name, which meant a phone
   that had opened the app once kept serving that build forever - new features
   never arrived. These files are small and unhashed, so being correct about
   freshness matters more than saving a few milliseconds. */
var VERSION = 'vcp-v4';
var SHELL = [
  './', 'index.html', 'styles.css', 'app.js', 'chart.js',
  'manifest.webmanifest', 'icons/icon-192.png', 'icons/icon-512.png'
];

self.addEventListener('install', function (e) {
  e.waitUntil(
    caches.open(VERSION)
      .then(function (c) { return c.addAll(SHELL); })
      .catch(function () { /* a missing file must not block activation */ })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== VERSION; })
        .map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;
  if (new URL(req.url).origin !== self.location.origin) return;

  e.respondWith(
    fetch(req).then(function (res) {
      if (res && res.status === 200) {
        var copy = res.clone();
        caches.open(VERSION).then(function (c) { c.put(req, copy); });
      }
      return res;
    }).catch(function () {
      // Offline: serve the last good copy, falling back to the app shell so a
      // cold navigation still opens.
      return caches.match(req).then(function (hit) {
        return hit || caches.match('index.html');
      });
    })
  );
});
