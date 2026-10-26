/* China Eyes service worker.
 *
 * Why this file exists: Android Chrome will only offer "安装应用" when the page
 * has a manifest AND a registered service worker with a fetch handler. Without
 * one it degrades to "添加到主屏幕", which is a bookmark that opens in the browser
 * chrome — not what the reader asked for.
 *
 * What it must NOT do: cache data.js permanently. The whole point of this site
 * is that the data changes every three hours; a cached JSON response would show
 * yesterday's news from a fresh-looking app.
 *
 *   · icons, manifest  → cache-first (they never change, and they are what the
 *                        OS needs at install time)
 *   · navigation       → STALE-WHILE-REVALIDATE (see below)
 *   · everything else  → not intercepted at all (plain fetch)
 *
 * Bump CACHE when the icon set changes; the old cache is dropped on activate.
 */
var CACHE = 'ce-shell-v5';
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
  return /\.(?:png|ico|svg|webp|jpg|jpeg|gif)$/i.test(url.pathname);
}

/* The manifest is NOT cache-first any more. It was, and that is half of why the
   reader's installed app kept showing a white OS splash screen long after the
   manifest background had been changed to #020C1A: the cached copy kept winning.
   The manifest is tiny, so a network round-trip costs nothing. */
function isManifest(url) {
  return /\/manifest\.json$/i.test(url.pathname);
}

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;

  var url;
  try { url = new URL(req.url); } catch (err) { return; }
  if (url.origin !== self.location.origin) return;

  /* Never serve these from cache: they are the data. */
  if (/data\.js$|\/data\//.test(url.pathname)) return;

  if (isManifest(url)) {
    e.respondWith(
      fetch(req).then(function (res) {
        if (res && res.ok) {
          var copy = res.clone();
          caches.open(CACHE).then(function (c) { c.put(req, copy); });
        }
        return res;
      }).catch(function () { return caches.match(req); })
    );
    return;
  }

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
    /* Stale-while-revalidate, and the reason is the reader's complaint on
       2026-10-26: "手机上点击客户端后去掉中间这个页面，直接进入到这个页面".
       That middle page is Android's own splash, drawn while the WebAPK waits for
       the first frame. Under network-first the first frame could not start until
       the round-trip finished, so the splash stayed up long enough to read as a
       separate screen. Serving the shell from cache paints immediately and cuts
       the splash to a blink; the fresh copy is fetched in the background and is
       what the next launch gets.
       This cannot show stale NEWS: data.js is never cached (see the guard above)
       and it is what actually carries the articles. The shell is markup, CSS and
       JS only. */
    e.respondWith(
      caches.open(CACHE).then(function (c) {
        return c.match('./index.html').then(function (hit) {
          var net = fetch(req).then(function (res) {
            if (res && res.ok) {
              var copy = res.clone();
              c.put('./index.html', copy);
            }
            return res;
          }).catch(function () {
            return hit || caches.match('./index.html').then(function (h) {
              return h || Response.error();
            });
          });
          return hit || net;
        });
      }).catch(function () { return fetch(req); })
    );
  }
});
