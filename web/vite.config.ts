import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * /api 转发目标：服务只监听 127.0.0.1，端口由 MEETING_PORT 覆盖，默认 8765。
 *
 * `proxyReq` 里那四行都挂在**转发出去的请求头**上，各自的作用面并不一样：
 *   - `removeHeader('accept-encoding')` **是这里真正防缓冲的那一手**：不告诉后端
 *     「我这边收得下 gzip」，后端就不会按压缩流分块，中间层也就没有攒够一整块再吐的理由；
 *   - `removeHeader('connection')` 摘的是逐跳头：留着它会让代理按短连接处理，
 *     长流的 SSE 会被掐断；
 *   - `setHeader('x-accel-buffering', 'no')` 与 `setHeader('cache-control', 'no-store')`
 *     是**带在转发请求上的**，服务侧读不读它们都随它 —— 它们挡不住中间层对**响应**的缓冲。
 *
 * 契约里要求的那条防缓冲指令是**响应头** `X-Accel-Buffering: no`，由服务自己在
 * `GET /api/events` 的应答上发出（`docs/contract.md` 第 3 章，服务侧
 * `meeting/service/routes.py` 的 `_api_events`）；`Cache-Control: no-store` 同理，
 * 由 `/api/*` 的中间件无条件补上。**这两条不靠 Vite 代发**，配置里这两行只是顺带
 * 把意图写在请求上，真正让 SSE 不被缓冲的是摘掉 `accept-encoding`。
 *
 * `changeOrigin` 取 **false**：`true` 时转发过去的 `Host` 会变成 `127.0.0.1:<port>`，
 * 取 `false` 则保持浏览器原样（`localhost:<vite端口>`）—— 排障时抓包看到的就是浏览器
 * 实际发出的那个 Host，更好读。两种取值都能过服务那关：服务的 Host 校验只比 hostname
 * 部分（`meeting/service/app.py` 的 `_host_allowed()`，允许集合 `127.0.0.1` / `localhost`），
 * 上面的两个 hostname 都在集合里。
 */
const target = `http://127.0.0.1:${process.env.MEETING_PORT ?? '8765'}`;

export default defineConfig({
  plugins: [react()],
  // build.outDir 用默认值 dist —— 也就是 web/dist，与服务配置键 web.dist_dir 的默认值对上。
  server: {
    proxy: {
      '/api': {
        target,
        changeOrigin: false,
        ws: false,
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            proxyReq.removeHeader('connection');
            proxyReq.removeHeader('accept-encoding');
            proxyReq.setHeader('x-accel-buffering', 'no');
            proxyReq.setHeader('cache-control', 'no-store');
          });
        },
      },
    },
  },
});
