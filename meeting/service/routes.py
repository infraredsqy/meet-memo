"""六条路由的**唯一注册点**（phase2 §3.7 / §3.8 / §3.9 / §3.11 / §3.12；`1987cb20` phase2 加 `summaries`）。

全应用只在这里注册一次：

  - `GET /api/state`（完整版，body 就是 `to_dict()` 的产出）；
  - `GET /api/events`（SSE：`snapshot` 先行 + `state` / `levels` / `transcript` 三通道）；
  - `GET /api/heartbeat`（完整版，`service` 字面量必须有）；
  - `POST /api/actions`（信封 + 七个动作名 + `code` 表）；
  - `GET /api/summaries`（本场按过的、成功产出的那几次会中总结；读内存，不落文件）；
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
            runtime.start_session(args.get("mode"))
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
    """五条 `/api/*` 路由（`/debug` 在 `debug_routes()` 里，同一个模块）。"""
    return [
        Route("/api/state", _api_state, methods=["GET"]),
        Route("/api/events", _api_events, methods=["GET"]),
        Route("/api/heartbeat", _api_heartbeat, methods=["GET"]),
        Route("/api/actions", _api_actions, methods=["POST"]),
        Route("/api/summaries", _api_summaries, methods=["GET"]),
    ]


def debug_routes() -> list[Route]:
    """`GET /debug` 的唯一注册点（**从 `meeting/service/static.py` 摘除了旧注册**）。"""
    return [Route("/debug", _debug_page, methods=["GET"])]


def all_routes() -> list[Route]:
    """六条路由一次给全（装配模块只负责把它们挂上应用）。"""
    return api_routes() + debug_routes()


__all__ = [
    "SSE_MEDIA_TYPE",
    "all_routes",
    "api_routes",
    "debug_routes",
]
