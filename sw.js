/* China Eyes service worker.
 *
 * Why this file exists: Android Chrome will only offer "安装应用" when the page
 * has a manifest AND a registered service worker with a fetch handler. Without
 * one it degrades to "添加到主屏幕", which is a bookmark that opens in the browser
 * chrome — not what the reader asked for.
 *
 * What it must NOT do: cache data.js or index.html permanently. The whole point
 * of this site is that the data changes every three hours; a cache-first HTML or
 * JSON response would show yesterday's news from a fresh-looking app. So:
 *
 *   · icons, manifest  → cache-first (they never change, and they are what the
 *                        OS needs at install time)
 *   · navigation       → network-first, cache only as an offline fallback
 *   · everything else  → not intercepted at all (plain fetch)
 *
 * Bump CACHE when the icon set changes; the old cache is dropped on activate.
 */
var CACHE = 'ce-shell-v4';
var SHELL = [
  './',
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
  './icon-192-maskable.png',
  './icon-512-maskable.png',
  './apple-touch-icon.png',
  './favicon-32.png'
];

self.addEventListener('install', function (e) {
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE).then(function (c) {
      /* addAll rejects wholesale if one entry 404s, which would leave the app
         uninstallable over a single typo — so add them one by one. */
      return Promise.all(SHELL.map(function (u) {
        return c.add(u).catch(function () {});
      }));
    })
  );
});

self.addEventListener('activate', function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (k) {
        return k === CACHE ? null : caches.delete(k);
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

function isStatic(url) {
  return /\.(?:png|ico|svg|webp|jpg|jpeg|gif)$/i.test(url.pathname) ||
         /\/manifest\.json$/i.test(url.pathname);
}

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;

  var url;
  try { url = new URL(req.url); } catch (err) { return; }
  if (url.origin !== self.location.origin) return;

  /* Never serve these from cache: they are the data. */
  if (/data\.js$|\/data\//.test(url.pathname)) return;

  if (isStatic(url)) {
    e.respondWith(
      caches.open(CACHE).then(function (c) {
        return c.match(req).then(function (hit) {
          if (hit) return hit;
          return fetch(req).then(function (res) {
            if (res && res.ok) c.put(req, res.clone());
            return res;
          });
        });
      }).catch(function () { return fetch(req); })
    );
    return;
  }

  if (req.mode === 'navigate') {
    /* Network first. The cached copy is only an offline safety net, so a
       rebuild reaches the reader immediately. */
    e.respondWith(
      fetch(req).then(function (res) {
        var copy = res.clone();
        caches.open(CACHE).then(function (c) { c.put('./index.html', copy); });
        return res;
      }).catch(function () {
        return caches.match('./index.html').then(function (hit) {
          return hit || Response.error();
        });
      })
    );
  }
});
