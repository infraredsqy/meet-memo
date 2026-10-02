"""九条路由的**唯一注册点**（phase2 §3.7 / §3.8 / §3.9 / §3.11 / §3.12；`1987cb20` phase2 加 `summaries`；
工作台收编节点 e2f6166b 加 `sessions` / `dirs` 两只读端点与 `workspace_open` / `workspace_remove` 两动作；
会后进度读数节点 `a9affaa8` 加 `sessions/{dir}` 那只单场状态端点；设置抽屉节点 `311f84f5` 加
`devices` 那只设备清单端点）。

全应用只在这里注册一次：

  - `GET /api/state`（完整版，body 就是 `to_dict()` 的产出）；
  - `GET /api/events`（SSE：`snapshot` 先行 + `state` / `levels` / `transcript` 三通道）；
  - `GET /api/heartbeat`（完整版，`service` 字面量必须有）；
  - `POST /api/actions`（信封 + 十一个动作名 + `code` 表）；
  - `GET /api/summaries`（本场按过的、成功产出的那几次会中总结；读内存，不落文件）；
  - `GET /api/sessions`（工作区注册表：按路径去重的那一份"我这些会"，读盘上注册表文件）；
  - `GET /api/sessions/{id}`（单场状态：这一场此刻跑到哪一档、会后离线跑过几块 / 共几块、
    正在跑哪一路、已经跑了多久；`{id}` 是这一场的目录路径，形状见 `docs/contract.md` 4.9）；
  - `GET /api/dirs`（目录选择器的现场：一层子目录的名目判读，读现场不写任何东西）；
  - `GET /api/devices`（两路各自的候选设备端点表：只读、纯枚举、不开设备）；
  - `GET /debug`（骨架自检页，从包内读）。

**装配模块 `app.py` 里那两条最小版注册与 `static.py` 的 `/debug` 注册都已在本 Phase 摘除**：
路由按注册顺序匹配、同一路径重复注册不报错，留着旧注册会让完整版永远不生效。

新路由全部落在 phase1 那三道本地防线中间件（Host 校验 / `X-Meeting-Service: 1` /
`/api/*` 的 `no-store`、无 CORS）的覆盖范围内 —— 中间件在应用装配处挂上，与路由注册点无关。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from meeting.service.runtime import ACTION_NAMES, ActionError, ServiceRuntime

_log = logging.getLogger("meeting.service.routes")

#: SSE 的响应类型
SSE_MEDIA_TYPE = "text/event-stream"


def _runtime_of(request: Request) -> ServiceRuntime | None:
    """从应用状态里取运行时（路由处理器只读、不写状态）。

    `request` 为 `None`（直接调处理函数的用例）或应用未装配时回 `None`，由调用方给兜底应答。
    """
    app = getattr(request, "app", None)
    if app is None:
        return None
    return getattr(app.state, "runtime", None)


def _error_response(exc: ActionError) -> JSONResponse:
    """失败体：`{"ok": false, "code", "message", "detail"}`（`code` 机器可读）。"""
    return JSONResponse(
        {
            "ok": False,
            "code": exc.code,
            "message": exc.message,
            "detail": exc.detail,
        },
        status_code=exc.status,
    )


def _action_error(code: str, message: str, *, status: int = 400,
                  detail: dict[str, Any] | None = None) -> ActionError:
    return ActionError(code, message, status=status, detail=detail)


# ---------------------------------------------------------------------------
# GET /api/state · GET /api/heartbeat
# ---------------------------------------------------------------------------


async def _api_state(request: Request) -> Response:
    runtime = _runtime_of(request)
    if runtime is None:  # pragma: no cover - 应用未装配
        return JSONResponse({"error": "runtime 未装配"}, status_code=500)
    runtime.bind_from_scope(request.scope)
    return JSONResponse(runtime.to_dict())


async def _api_heartbeat(request: Request) -> Response:
    """`service` 字面量恒为 `"meeting"`（端口策略的探针靠它认亲）。"""
    from meeting.service.app import SERVICE_ID

    runtime = _runtime_of(request)
    if runtime is None:  # pragma: no cover - 应用未装配
        return JSONResponse(
            {
                "service": SERVICE_ID,
                "server_time": "",
                "revision": 0,
                "top_state": "idle",
                "recording_alive": None,
                "seconds_since_last_tick": None,
            }
        )
    runtime.bind_from_scope(request.scope)
    return JSONResponse(runtime.heartbeat_dict())


# ---------------------------------------------------------------------------
# GET /api/summaries（`1987cb20` phase2：本场按过的那些次会中总结）
# ---------------------------------------------------------------------------


async def _api_summaries(request: Request) -> Response:
    """本场总结记录：`{session_id, entries}`（**200**，任何状态下都给）。

    记录活在运行时的内存里、**不落文件**（设计章 2.3 / 5.1）；只记**成功产出**的那几次，
    按时间先后排。没有场次或一次都没按过时 `session_id` 为 `null`、`entries` 是空数组。
    读它是**纯读**：不提交、不涨 `revision`、不广播。
    """
    runtime = _runtime_of(request)
    if runtime is None:  # pragma: no cover - 应用未装配
        return JSONResponse({"error": "runtime 未装配"}, status_code=500)
    runtime.bind_from_scope(request.scope)
    return JSONResponse(runtime.summaries_dict())


# ---------------------------------------------------------------------------
# GET /api/events（SSE）
# ---------------------------------------------------------------------------


async def _api_events(request: Request) -> Response:
    runtime = _runtime_of(request)
    if runtime is None:  # pragma: no cover - 应用未装配
        return JSONResponse({"error": "runtime 未装配"}, status_code=500)
    runtime.bind_from_scope(request.scope)
    return StreamingResponse(
        runtime.hub.stream(runtime.to_dict),
        media_type=SSE_MEDIA_TYPE,
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",  # 避免中间层缓冲
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# GET /api/sessions · GET /api/dirs（工作台收编节点 e2f6166b：两只读端点）
# ---------------------------------------------------------------------------


async def _api_sessions(request: Request) -> Response:
    """工作区注册表：`{sessions, recent}`（**200**，任何状态下都给）。

    与 `GET /api/summaries` 同一档纯读：不提交、不涨 `revision`、不广播；
    "最近一场"只从注册表读（启动与请求侧都**不扫任何场次目录**，
    `docs/design-intent.md` 第 4 条）。
    """
    runtime = _runtime_of(request)
    if runtime is None:  # pragma: no cover - 应用未装配
        return JSONResponse({"error": "runtime 未装配"}, status_code=500)
    runtime.bind_from_scope(request.scope)
    return JSONResponse(runtime.sessions_dict())


async def _api_session_status(request: Request) -> Response:
    """单场状态：`GET /api/sessions/{id}`，`{id}` 是**这一场的目录路径**（URL 编码的绝对路径）。

    会后视图那一片进度区就读它：这一场此刻跑到哪一档、会后离线跑过几块、共几块、
    正在跑哪一路、已经跑了多久（应答体逐格见 `docs/contract.md` 第 4.9 节）。

    与 `GET /api/sessions` / `GET /api/summaries` 同一档**纯读**：不提交、不涨 `revision`、
    不广播。路径打不到已登记的工作区时按第 4.9 节的失败口径回 404（回的是通用失败信封：
    `{ok, code, message, detail}`）。
    """
    runtime = _runtime_of(request)
    if runtime is None:  # pragma: no cover - 应用未装配
        return JSONResponse({"error": "runtime 未装配"}, status_code=500)
    runtime.bind_from_scope(request.scope)
    try:
        return JSONResponse(runtime.session_status_dict(request.path_params.get("dir")))
    except ActionError as exc:
        return _error_response(exc)


async def _api_dirs(request: Request) -> Response:
    """目录选择器的现场：`{home, cwd, up, dirs}`（**200**；`?dir=` 省略时从主目录起步）。

    纯读：只判每层子目录的名目（三档），不写任何东西。「软件自己会不建目录」这条
    在这里同样成立 —— 选择器只用来挑一个**已有**目录。
    """
    runtime = _runtime_of(request)
    if runtime is None:  # pragma: no cover - 应用未装配
        return JSONResponse({"error": "runtime 未装配"}, status_code=500)
    runtime.bind_from_scope(request.scope)
    try:
        return JSONResponse(runtime.dirs_listing(request.query_params.get("dir")))
    except ActionError as exc:
        return _error_response(exc)


async def _api_devices(request: Request) -> Response:
    """两路各自的候选设备端点表：`{mic: [...], loopback: [...]}`（**200**，任何状态下都给）。

    设置抽屉的两个设备下拉读它（`311f84f5`）。与 `GET /api/dirs` 同一档**纯读**：
    只枚举端点表、**不开任何设备**（`probe()` 会短暂试开，清单不走它）；
    不提交、不涨 `revision`、不广播。
    """
    runtime = _runtime_of(request)
    if runtime is None:  # pragma: no cover - 应用未装配
        return JSONResponse({"error": "runtime 未装配"}, status_code=500)
    runtime.bind_from_scope(request.scope)
    try:
        return JSONResponse(runtime.devices_listing())
    except ActionError as exc:
        return _error_response(exc)


# ---------------------------------------------------------------------------
# POST /api/actions
# ---------------------------------------------------------------------------


async def _api_actions(request: Request) -> Response:
    """动作端点：信封 + 七个动作名 + `code` 表（中间件已校验两个必需请求头）。"""
    runtime = _runtime_of(request)
    if runtime is None:  # pragma: no cover - 应用未装配
        return JSONResponse({"error": "runtime 未装配"}, status_code=500)

    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        return _error_response(
            _action_error("invalid_setting", "请求体不是合法 JSON", status=400)
        )
    if not isinstance(payload, dict):
        return _error_response(
            _action_error("invalid_setting", "请求体不是 JSON 对象", status=400)
        )

    action = payload.get("action")
    args = payload.get("args") or {}
    if not isinstance(args, dict):
        return _error_response(
            _action_error("invalid_setting", "args 必须是对象", status=400)
        )
    if action not in ACTION_NAMES:
        return _error_response(
            _action_error(
                "invalid_setting",
                f"不认识的动作名：{action!r}（可用：{' / '.join(ACTION_NAMES)}）",
                status=400,
                detail={"action": action, "known": list(ACTION_NAMES)},
            )
        )

    try:
        if action == "start":
            runtime.start_session(args.get("mode"), args.get("dir"))
        elif action == "stop":
            runtime.stop_session("user")
        elif action == "mute_session":
            runtime.mute_session()
        elif action == "reprobe":
            runtime.reprobe()
        elif action == "settings_update":
            extra = runtime.settings_update(args)
            return JSONResponse(
                {
                    "ok": True,
                    "revision": runtime.revision,
                    "applied": extra["applied"],
                    "pending_restart": extra["pending_restart"],
                }
            )
        elif action == "dev_inject":
            runtime.dev_inject(args)
        elif action == "summarize":
            # 成功体在通用信封上多一个 `summary` 键（先例是 `settings_update` 的
            # `applied` / `pending_restart`）；`revision` 是**当前**版本号 —— 总结是只读的，
            # 这里不提交、不涨版本、不广播。
            # `summarize()` 是一条协程：那一次模型调用在**工作线程**里跑
            # （`runtime._summary_wait()`，复审 minor 6），等它的只有本请求这一条协程 ——
            # 于是总结那几秒里 `/api/state` 与三个 SSE 通道照常应答。
            return JSONResponse(
                {
                    "ok": True,
                    "revision": runtime.revision,
                    "summary": await runtime.summarize(args),
                }
            )
        elif action == "workspace_open":
            # 成功体在通用信封上多一个 `workspace` 键（先例是 `summarize` 的 `summary`）：
            # 判别档位与去重回扣由服务给，界面不派生；失败档（目录不存在、非空未确认、
            # 注册表写盘失败）走 ActionError 的 `code` 表。
            return JSONResponse(
                {
                    "ok": True,
                    "revision": runtime.revision,
                    "workspace": runtime.workspace_open(args.get("dir"),
                                                        confirm=bool(args.get("confirm"))),
                }
            )
        elif action == "workspace_remove":
            return JSONResponse(
                {
                    "ok": True,
                    "revision": runtime.revision,
                    "workspace": runtime.workspace_remove(args.get("dir")),
                }
            )
        elif action in ("post_offline", "post_minutes"):
            # 会后的两个按钮（R26）：起一条后台线程即算启动成功，成功体在通用信封上多一个
            # `post` 键（先例是 `summarize` 的 `summary` / `workspace_open` 的 `workspace`）——
            # 它说的是「这一趟跑的是哪一场、跑的哪一趟」，跑成什么样由盘上的状态文件回答。
            started = (
                runtime.post_offline(args)
                if action == "post_offline"
                else runtime.post_minutes(args)
            )
            return JSONResponse(
                {"ok": True, "revision": runtime.revision, "post": started}
            )
    except ActionError as exc:
        return _error_response(exc)
    except Exception as exc:  # 未捕获异常带堆栈进 log（R16 的最低要求）
        _log.exception("动作 %s 处理时抛了未捕获异常", action)
        return _error_response(
            _action_error(
                "invalid_setting",
                f"动作 {action} 处理失败：{type(exc).__name__}: {exc}",
                status=500,
            )
        )

    return JSONResponse({"ok": True, "revision": runtime.revision})


# ---------------------------------------------------------------------------
# GET /debug
# ---------------------------------------------------------------------------


async def _debug_page(request: Request) -> Response:
    """骨架自检页：从**包内**读取（`package-data` 覆盖 `meeting/service/*.html`）。"""
    from starlette.responses import HTMLResponse

    from meeting.service import static as static_module

    return HTMLResponse(static_module.read_debug_page())


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------


def api_routes() -> list[Route]:
    """九条 `/api/*` 路由（`/debug` 在 `debug_routes()` 里，同一个模块）。"""
    return [
        Route("/api/state", _api_state, methods=["GET"]),
        Route("/api/events", _api_events, methods=["GET"]),
        Route("/api/heartbeat", _api_heartbeat, methods=["GET"]),
        Route("/api/actions", _api_actions, methods=["POST"]),
        Route("/api/summaries", _api_summaries, methods=["GET"]),
        Route("/api/sessions", _api_sessions, methods=["GET"]),
        # 单场状态（会后视图的进度区）：`{dir:path}` 是为了收下 URL 编码的绝对路径
        # ——Windows 上的 `C:\会议\250906` 里那两段反斜杠过不了默认的路径段匹配。
        # 注册在 `/api/sessions` 之后：星形路由按注册顺序匹配，两条路径不冲突。
        Route("/api/sessions/{dir:path}", _api_session_status, methods=["GET"]),
        Route("/api/dirs", _api_dirs, methods=["GET"]),
        Route("/api/devices", _api_devices, methods=["GET"]),
    ]


def debug_routes() -> list[Route]:
    """`GET /debug` 的唯一注册点（**从 `meeting/service/static.py` 摘除了旧注册**）。"""
    return [Route("/debug", _debug_page, methods=["GET"])]


def all_routes() -> list[Route]:
    """十条路由一次给全（装配模块只负责把它们挂上应用）。"""
    return api_routes() + debug_routes()


__all__ = [
    "SSE_MEDIA_TYPE",
    "all_routes",
    "api_routes",
    "debug_routes",
]
