"""前端产物挂载、产物缺失说明页与 `/debug` 自检页的读取（phase1 §3.8；phase2 §3.12 摘除注册）。

- 构建产物目录由 `web.dist_dir` 指定，**默认 `web/dist`**（`c62dec56` 的 React 工程放 `web/`，
  产物落 `web/dist`）。
- 目录存在 → 挂在 `/`（`StaticFiles(html=True)`）；**目录不存在 → 服务照常起**，`/` 给一页
  看得懂的说明，并**在终端与 log 各写一行**。
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

from starlette.responses import HTMLResponse
from starlette.routing import Mount, Route
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


async def _debug_page(request) -> HTMLResponse:
    """（旧注册点的处理体，保留给 `routes.py` 之外的直接调用；路由注册已搬走。）"""
    return HTMLResponse(read_debug_page())


def debug_routes() -> list:
    """**已摘除**：`/debug` 的唯一注册点搬到了 `meeting/service/routes.py`（phase2 §3.12）。

    保留这个函数只为一个明确的目的：它**不返回任何路由**，于是任何还照着旧写法
    `routes.extend(static_module.debug_routes())` 挂载的地方都挂不上第二条 `GET /debug`。
    全应用五条路由只在 `routes.py` 注册一次（重复注册不报错但会让其中一份永远不生效）。
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


async def _root_missing_dist(request) -> HTMLResponse:
    """`/` 的说明页处理器（只在产物目录不存在时注册）。"""
    return HTMLResponse(_missing_dist_page(), status_code=200)


def static_routes(dist_dir: str) -> list:
    """按 `web.dist_dir` 决定 `/` 挂什么：产物在 → `StaticFiles(html=True)`；否则说明页。

    **必须在应用组装里最后调用。**
    """
    dist_path = Path(dist_dir)
    if dist_path.is_dir():
        _log.info("静态挂载：把前端产物 %s 挂在 /（StaticFiles(html=True)）", dist_path)
        return [Mount("/", app=StaticFiles(directory=str(dist_path), html=True), name="web-dist")]

    message = f"前端产物还没构建（{dist_dir} 不存在）。{DIST_MISSING_MESSAGE}"
    _log.warning("静态挂载回退：%s", message)
    print(f"静态挂载回退：{message}", flush=True)
    return [Route("/", _root_missing_dist, methods=["GET"])]


__all__ = [
    "DEBUG_PAGE_NAME",
    "debug_page_path",
    "debug_routes",
    "read_debug_page",
    "static_routes",
]
