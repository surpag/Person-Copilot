const CACHE_NAME = 'agent-v2';   // ← 版本号升级，旧缓存会被自动清掉

// 只预缓存不变的东西（图标、manifest），不缓存 HTML
const PRECACHE = [
  '/static/icon.svg',
  '/static/manifest.json',
];

// 安装：预缓存静态资源
self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((c) => c.addAll(PRECACHE))
  );
  self.skipWaiting();
});

// 激活：清理旧版本缓存
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// 拦截：网络优先
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // 只处理同源
  if (url.origin !== self.location.origin) return;

  // API 请求直连
  if (
    url.pathname.startsWith('/chat') ||
    url.pathname.startsWith('/sessions') ||
    url.pathname.startsWith('/uploads') ||
    url.pathname.startsWith('/tasks') ||
    url.pathname.startsWith('/memories') ||
    url.pathname.startsWith('/health')
  ) {
    return;
  }

  // 只缓存 GET
  if (e.request.method !== 'GET') return;

  // 网络优先策略
  e.respondWith(
    fetch(e.request)
      .then((response) => {
        // 成功 → 更新缓存
        if (response.ok) {
          const cloned = response.clone();
          caches.open(CACHE_NAME).then((c) => c.put(e.request, cloned));
        }
        return response;
      })
      .catch(() => {
        // 网络失败 → 用缓存
        return caches.match(e.request);
      })
  );
});