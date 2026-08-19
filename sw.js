/* Service worker — Mappa Pluviometrica Toscana (PWA offline) */
const CACHE = 'piogge-toscana-v1';
const SHELL = [
  './',
  './index.html',
  './dati.js',
  './logo.svg',
  './manifest.webmanifest',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
  'https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js'
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      // addAll fallirebbe tutto se una risorsa non risponde: le aggiungo una a una.
      .then(c => Promise.all(SHELL.map(u => c.add(u).catch(() => null))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Dati: network-first, così restano freschi quando c'è connessione.
  if (url.pathname.endsWith('dati.js') || url.pathname.endsWith('stations.json')) {
    e.respondWith(
      fetch(req).then(r => {
        const cp = r.clone();
        caches.open(CACHE).then(c => c.put(req, cp));
        return r;
      }).catch(() => caches.match(req))
    );
    return;
  }

  // Tiles mappa/radar: solo rete (non le metto in cache), se offline falliscono in silenzio.
  if (/tile\.openstreetmap\.org|rainviewer\.com/.test(url.host)) {
    return; // lascia gestire al browser
  }

  // Guscio app + CDN: cache-first, con aggiornamento in background.
  e.respondWith(
    caches.match(req).then(hit => {
      const net = fetch(req).then(r => {
        if (r && r.status === 200) {
          const cp = r.clone();
          caches.open(CACHE).then(c => c.put(req, cp));
        }
        return r;
      }).catch(() => hit);
      return hit || net;
    })
  );
});
