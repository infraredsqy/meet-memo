"""前端产物挂载、产物缺失说明页与 `/debug` 自检页的读取（phase1 §3.8；phase2 §3.12 摘除注册）。

- 构建产物目录由 `web.dist_dir` 指定，**默认 `web/dist`**（`c62dec56` 的 React 工程放 `web/`，
  产物落 `web/dist`）。
- 目录存在 → `/` 走 `StaticFiles(html=True)`；**目录不存在 → 服务照常起**，`/` 给一页
  看得懂的说明。启动时目录在不在，都会**在终端与 log 各写一行**当时看到的情形。
- **`/` 的目录存在性按每请求判定**（不再钉死在启动时刻）：产物目录一建好，刷新即可见；
  又被删掉，刷新回说明页 —— 说明页文案承诺的「产物一建好，刷新即可见」由此为真。
  `StaticFiles` 实例**预建**（`check_dir=False`：建实例时不判目录，目录晚建也能服务）。
- **`/` 的挂载是二选一的条件分支**；**注册次序的硬约束**：静态 / 说明页的挂载函数必须在
  应用组装里**最后调用**，否则会把 API 与自检页一起吃走。
- 说明页**不许**写成 catch-all 把 `/api/*` 与 `/debug` 也接走。
- **`/debug` 这条路由的唯一注册点在 `meeting/service/routes.py`**（phase2 §3.12）：
  本模块只负责**从包内读出那一页**（`read_debug_page()`，走 `package-data`，不依赖 `web/dist`），
  phase1 挂在这里的旧注册已摘除。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from starlette.responses import HTMLResponse, Response
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from meeting.service.app import DIST_MISSING_MESSAGE

_log = logging.getLogger("meeting.service.static")

#: `/debug` 自检页在包内的文件名（`package-data` 带着它）
DEBUG_PAGE_NAME = "debug.html"

_DEBUG_MISSING_PAGE = (
    "<!doctype html><meta charset=\"utf-8\"><title>meeting /debug</title>"
    "<h1>/debug 自检页缺失</h1>"
    "<p>包内没找到 <code>meeting/service/debug.html</code>（骨架自检页）。</p>"
)


def debug_page_path() -> Path:
    """`/debug` 那页在包内的路径。"""
    return Path(__file__).resolve().parent / DEBUG_PAGE_NAME


def read_debug_page() -> str:
    """从**包内**读取自检页；读不到时给一页说明（不让 `/debug` 变成 500）。"""
    path = debug_page_path()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        _log.error("读不到自检页 %s（%s）", path, exc)
        return _DEBUG_MISSING_PAGE


def debug_routes() -> list:
    """**已摘除**：`/debug` 的唯一注册点搬到了 `meeting/service/routes.py`（phase2 §3.12）。

    保留这个函数只为一个明确的目的：它**不返回任何路由**，于是任何还照着旧写法
    `routes.extend(static_module.debug_routes())` 挂载的地方都挂不上第二条 `GET /debug`。
    全应用六条路由只在 `routes.py` 注册一次（重复注册不报错但会让其中一份永远不生效）。
    """
    return []


def _missing_dist_page() -> str:
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>meeting</title></head><body>"
        "<h1>meeting 服务在跑</h1>"
        f"<p>{DIST_MISSING_MESSAGE}</p>"
        "<p>这一页替服务报个信：产物目录一建好，刷新就能看到真正的界面。</p>"
        "</body></html>"
    )


class _RootByRequest:
    """`/` 的**每请求判定器**：产物目录在 → `StaticFiles`；不在 → 说明页。

    说明页文案承诺「产物一建好，刷新即可见」，于是目录存在性不能钉死在启动时刻：
    这里每个请求现场判一次。两侧的行为与旧的两分支逐一对应 —— 产物在：整个 `/` 子树
    交给 `StaticFiles(html=True)`（实例**预建**、`check_dir=False`，建实例不判目录，
    目录晚建也能服务）；产物不在：只有 `GET/HEAD /` 本身给说明页，其余路径与末端 404
    同形回 `Not Found`（不把说明页写成 catch-all）。
    """

    def __init__(self, dist_path: Path) -> None:
        self._dist_path = Path(dist_path)
        self._files = StaticFiles(directory=str(dist_path), html=True, check_dir=False)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            # 非 http 的一律原样转交。这一格此刻到不了（Starlette 的应用级 `lifespan`
            # 由 Router 自己处理、不往 Mount 转发，本服务也不开 websocket），留着是为了
            # 日后不变成哑弹：StaticFiles 第一句就是 assert scope["type"] == "http"，
            # 这里的 500 报错读得出来是静态挂载接不住这个 scope；将来接 websocket 路由，
            # 答案也在这里——静态挂载不给长连接。
            await self._files(scope, receive, send)
            return
        if not self._dist_path.is_dir():
            await self._missing_branch(scope, receive, send)
            return
        await self._files(scope, receive, send)

    async def _missing_branch(self, scope: dict, receive: Any, send: Any) -> None:
        """产物缺失分支：`GET/HEAD /` 给说明页，其余方法与路径回末端同形的 404。"""
        path = scope.get("path", "/")
        method = str(scope.get("method", "GET")).upper()
        if path == "/" and method in ("GET", "HEAD"):
            response: Response = HTMLResponse(_missing_dist_page(), status_code=200)
        else:
            response = Response("Not Found", status_code=404, media_type="text/plain")
        await response(scope, receive, send)


def static_routes(dist_dir: str) -> list:
    """`/` 挂**每请求判定**的分派器：产物在 → `StaticFiles(html=True)`；不在 → 说明页。

    启动时仍按当时看到的情形落一行 log（产物在：info；不在：warning + 终端 print，
    文案与行为都不变），但**目录存在性不再钉死在启动时刻** —— 之后建出 / 删掉产物目录，
    每个 `GET /` 都按当下情形作答。

    **必须在应用组装里最后调用**（这个挂载吃 `/` 下所有路径，API 与自检页要先注册）。
    """
    dist_path = Path(dist_dir)
    if dist_path.is_dir():
        _log.info("静态挂载：把前端产物 %s 挂在 /（StaticFiles(html=True)）", dist_path)
    else:
        message = f"前端产物还没构建（{dist_dir} 不存在）。{DIST_MISSING_MESSAGE}"
        _log.warning("静态挂载回退：%s", message)
        print(f"静态挂载回退：{message}", flush=True)
    return [Mount("/", app=_RootByRequest(dist_path), name="web-dist")]


__all__ = [
    "DEBUG_PAGE_NAME",
    "debug_page_path",
    "debug_routes",
    "read_debug_page",
    "static_routes",
]
