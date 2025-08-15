/* Progressive Web App Service Worker for Somewheria */
const CACHE_VERSION = 'v1.0.0';
const STATIC_CACHE = `static-${CACHE_VERSION}`;
const OFFLINE_URL = '/offline';

const CORE_ASSETS = [
  '/',
  '/for-rent',
  '/about',
  '/contact',
  OFFLINE_URL,
  '/manifest.webmanifest',
  '/static/web_light_rd_SI@1x.png'
];

// Helper: add assets safely (ignore failures so install doesn't abort if a URL 404s)
async function safeAddAll(cache, urls) {
  for (const url of urls) {
    try {
      await cache.add(url);
    } catch (e) {
      // ignore individual failures to avoid breaking install
      // console.debug('SW: failed to precache', url, e);
    }
  }
}

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(STATIC_CACHE);
    await safeAddAll(cache, CORE_ASSETS);
    self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.map(key => {
        if (key !== STATIC_CACHE) {
          return caches.delete(key);
        }
      })
    );
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const req = event.request;

  // Only handle GET
  if (req.method !== 'GET') {
    return;
  }

  // Navigation requests (HTML pages)
  if (req.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        // Network-first for navigations
        const fresh = await fetch(req);
        return fresh;
      } catch (e) {
        // Fallback to any cached copy of the same page, then offline page
        const cache = await caches.open(STATIC_CACHE);
        const cachedPage = await cache.match(req, { ignoreSearch: true });
        if (cachedPage) return cachedPage;
        const offline = await cache.match(OFFLINE_URL);
        return offline || new Response('Offline', { status: 503, headers: { 'Content-Type': 'text/plain' } });
      }
    })());
    return;
  }

  const url = new URL(req.url);

  // Same-origin static assets: cache-first
  if (url.origin === self.location.origin) {
    const isStatic = ['style', 'script', 'image', 'font'].includes(req.destination);
    if (isStatic) {
      event.respondWith((async () => {
        const cached = await caches.match(req);
        if (cached) return cached;
        try {
          const resp = await fetch(req);
          const cache = await caches.open(STATIC_CACHE);
          cache.put(req, resp.clone());
          return resp;
        } catch (e) {
          // If image fails, try to return something graceful (optional)
          return caches.match('/static/web_light_rd_SI@1x.png') || new Response('', { status: 404 });
        }
      })());
      return;
    }

    // Other same-origin GET requests: network-first with cache fallback
    event.respondWith((async () => {
      try {
        const resp = await fetch(req);
        return resp;
      } catch (e) {
        return (await caches.match(req)) || new Response('Offline', { status: 503 });
      }
    })());
    return;
  }

  // Cross-origin (e.g., CDN): network-first with runtime caching fallback
  event.respondWith((async () => {
    try {
      const resp = await fetch(req);
      // Only cache successful opaque/ok GET responses
      const clone = resp.clone();
      const cache = await caches.open(STATIC_CACHE);
      cache.put(req, clone);
      return resp;
    } catch (e) {
      const cached = await caches.match(req);
      if (cached) return cached;
      return new Response('', { status: 504 });
    }
  })());
});
