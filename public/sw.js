const CACHE='collectra-v10b-ai-web-optional-photos';
const CORE=['/','/styles.css','/app.js','/assets/collectra-logo.png','/assets/icon-192.png'];
self.addEventListener('install',e=>{self.skipWaiting();e.waitUntil(caches.open(CACHE).then(c=>c.addAll(CORE)))});
self.addEventListener('activate',e=>e.waitUntil(Promise.all([caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))),self.clients.claim()])));
self.addEventListener('fetch',e=>{
  if(e.request.method!=='GET'||new URL(e.request.url).pathname.startsWith('/api/')) return;
  e.respondWith(fetch(e.request,{cache:'no-store'}).then(r=>{const copy=r.clone();caches.open(CACHE).then(c=>c.put(e.request,copy));return r}).catch(()=>caches.match(e.request)));
});
