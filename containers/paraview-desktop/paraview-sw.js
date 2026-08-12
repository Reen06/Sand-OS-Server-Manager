// FreeCAD (Selkies) service worker — passthrough, NO caching.
//
// Its only purpose is to make the streamed FreeCAD app installable as a PWA
// (Chrome needs a registered service worker with a fetch handler). It caches
// NOTHING: caching the WebRTC client shell previously served a stale client
// whose absolute paths didn't route through the Hub proxy, so the stream never
// connected. Network-only = always fresh.
//
// It must also NOT unregister other scopes (an earlier self-unregistering no-op
// wiped the dashboard's service worker on the same origin).
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', (event) => {
  // Only take navigations; subresources use default handling. Network-only.
  if (event.request.mode === 'navigate') {
    event.respondWith(fetch(event.request));
  }
});
