/* Service Worker · 离线外壳与静态缓存
 *
 * 设计目标（与 server.py 的鉴权链严格对齐）：
 *   1. 应用外壳离线可开 —— 弱网/地铁/自习室无网时仍能进入 App 看到界面；
 *   2. 付费内容绝不缓存 —— /api/content 走「登录 + 审核 + 单次票据 + 限流」四层校验，
 *      一旦被 SW 缓存，票据校验形同虚设、且题库会长期滞留在设备上，故一律 Network-Only；
 *   3. 权益与进度实时 —— /api/** 全部不缓存，避免出现「已过期却显示有权限」的错乱。
 *
 * 缓存版本：改动缓存策略时递增 CACHE_VER，旧缓存会被 activate 阶段自动清理。
 */
const CACHE_VER = 'v2';
const SHELL_CACHE = 'zb-shell-' + CACHE_VER;
const ASSET_CACHE = 'zb-asset-' + CACHE_VER;

/* 应用外壳：安装时预缓存，保证断网首启也有完整界面 */
const SHELL_ASSETS = [
  './',
  './index.html',
  './manifest.webmanifest',
  './icon-192.png',
  './icon-512.png',
  './apple-touch-icon.png',
  './favicon-32.png',
  './logo-brand.png',
  './img/img-avatar.png',
  './img/img-admin.png',
  './img/img-pay.png',
  './img/img-qr.png',
  './img/img-wx.png',
];

/* 绝不能缓存的路径前缀（鉴权 / 票据 / 权益 / 下载） */
const NEVER_CACHE = [
  '/api/',
  '/download/',
  '/content.json',
  '/admin',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(SHELL_CACHE)
      // 单个资源失败不应让整次安装失败（例如某个可选图缺失）
      .then((c) => Promise.allSettled(SHELL_ASSETS.map((u) => c.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== SHELL_CACHE && k !== ASSET_CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('message', (e) => {
  if (e.data === 'SKIP_WAITING') self.skipWaiting();
});

/* SW 作用域前缀：云端部署为 '/'，GitHub Pages 子路径部署为 '/52ZX/'。
 * 判路径前先剥掉该前缀，使 NEVER_CACHE（鉴权/票据/权益/下载）规则在两种部署下完全一致。 */
const SCOPE_PATH = new URL(self.registration.scope).pathname;
function relPath(pathname){
  if(SCOPE_PATH && SCOPE_PATH !== '/' && pathname.indexOf(SCOPE_PATH) === 0){
    return '/' + pathname.slice(SCOPE_PATH.length);
  }
  return pathname;
}
function isNeverCache(url) {
  const p = relPath(url.pathname);
  return NEVER_CACHE.some((pre) => p === pre || p.startsWith(pre));
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  /* 只处理 GET；POST（登录/同步/下载授权）必须直达网络 */
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  /* 跨域资源（如云端直链）不拦截，交给浏览器默认处理 */
  if (url.origin !== self.location.origin) return;

  /* 导航请求：网络优先，失败回落到缓存的外壳（保证离线可进入 App） */
  if (req.mode === 'navigate') {
    e.respondWith(
      fetch(req)
        .then((r) => {
          const copy = r.clone();
          caches.open(SHELL_CACHE).then((c) => c.put('./index.html', copy)).catch(() => {});
          return r;
        })
        .catch(() => caches.match('./index.html', { ignoreSearch: true })
          .then((hit) => hit || caches.match('./'))
          .then((hit) => hit || new Response(
            '<meta charset="utf-8"><h3 style="font-family:sans-serif;padding:24px">离线中，请联网后重试</h3>',
            { headers: { 'Content-Type': 'text/html; charset=utf-8' } }
          )))
    );
    return;
  }

  /* 鉴权 / 票据 / 权益 / 下载 —— 绝不缓存，直连网络 */
  if (isNeverCache(url)) return;

  /* 静态资源：缓存优先（图片体积小、变更少），命中后后台静默更新 */
  e.respondWith(
    caches.match(req).then((hit) => {
      const net = fetch(req).then((r) => {
        if (r && r.status === 200 && r.type === 'basic') {
          const copy = r.clone();
          caches.open(ASSET_CACHE).then((c) => c.put(req, copy)).catch(() => {});
        }
        return r;
      }).catch(() => hit);
      return hit || net;
    })
  );
});
