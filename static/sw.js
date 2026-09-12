const CACHE = 'stryketboys-v1';
const SHELL = ['/'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  if (e.request.url.includes('/api/')) return;
  e.respondWith(
    fetch(e.request)
      .then(resp => {
        const clone = resp.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return resp;
      })
      .catch(() => caches.match(e.request))
  );
});

// ── Push notifications (see "Push notifications" in CLAUDE.md) ──
// The payload is JSON built server-side in app.py's broadcast_push(): {title, body, tag}.
self.addEventListener('push', e => {
  let payload = { title: 'StryketBoys', body: '' };
  try { payload = e.data.json(); } catch (err) { /* keep default */ }
  e.waitUntil(
    self.registration.showNotification(payload.title || 'StryketBoys', {
      body: payload.body || '',
      tag: payload.tag,
      icon: '/static/icon-192.png',
      badge: '/static/icon-192.png',
    })
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      for (const client of clientList) {
        if ('focus' in client) return client.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow('/');
    })
  );
});
