// No-op, self-unregistering service worker.
//
// The Selkies PWA shipped a caching service worker that stored the app shell.
// Behind the Hub proxy that's harmful: the browser kept serving a STALE client
// (with absolute /turn + /{appName}/signalling paths that don't route through the
// proxy), so the stream never connected. Caching a live WebRTC client is
// pointless anyway. This SW intercepts nothing and removes any previously
// installed SW + caches, then reloads open pages so they fetch fresh.
self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    try { await self.clients.claim(); } catch (e) { /* ignore */ }
    try {
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => caches.delete(k)));
    } catch (e) { /* ignore */ }
    try { await self.registration.unregister(); } catch (e) { /* ignore */ }
    const clients = await self.clients.matchAll({ type: "window" });
    clients.forEach((c) => { try { c.navigate(c.url); } catch (e) {} });
  })());
});
// Intentionally no "fetch" handler → every request goes straight to the network.
