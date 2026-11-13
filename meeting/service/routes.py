"""十六条路由的唯一注册点（十五条 `/api/*` + `/debug`）。

- `GET /api/state`（完整版，body 就是 `to_dict()` 的产出）；
- `GET /api/events`（SSE：`snapshot` 先行 + `state` / `levels` / `transcript` 三通道）；
- `GET /api/heartbeat`（`service` 字面量必须有）；
- `POST /api/actions`（信封 + 十九个动作名 + `code` 表，分发走 `_ACTION_HANDLERS`，导入时
  与 runtime 的 `ACTION_NAMES` 核对，对不上服务起不来）；
- `GET /api/summaries`（本场成功的会中总结，读内存不落文件）；
- `GET /api/sessions`（工作区注册表）、`GET /api/sessions/{id}`（单场状态，
  形状见 `docs/contract.md` 4.9）、`GET /api/sessions/{id}/transcripts`（稿子正文）；
- 会议资料四条（资料节点，语义见 `docs/contract.md` 4.12）：清单 / 上传 /
  取原文件 / 删除——上传是唯一不走 `/api/actions` 的写端点（二进制正文装不进 JSON 信封），
  头哨由中间件按端点扩过去；
- `GET /api/dirs` / `POST /api/dirs`（目录选择器：现场判读 + 新建目录，REST 写入口）、
  `GET /api/devices`（设备清单，只枚举不开设备）、`GET /debug`（骨架自检页）。

资料四条与稿子正文注册在单场状态那条**之前**：`{dir:path}` 收得下斜杠，注册在后会抢走
那几条。装配模块与 `static.py` 里的旧注册已摘除——同一路径重复注册不报错，留着旧注册
会让完整版永远不生效。新路由全部落在三道本地防线中间件的覆盖范围内。
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import mimetypes
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
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


def _runtime_missing_response() -> Response:
    """runtime 未装配时的兜底应答（应用未装配那一档，500）。"""
    return JSONResponse({"error": "runtime 未装配"}, status_code=500)


def _heartbeat_missing_response() -> Response:
    """`GET /api/heartbeat` 未装配时的兜底应答（形状照 `heartbeat_dict()` 的键面）。

    端口探针靠 `service` 字面量认亲，未装配也要按心跳自己的形状如实答「什么都还没跑」
    ——不是通用错误体。
    """
    from meeting.service.app import SERVICE_ID

    return JSONResponse(
        {
            "service": SERVICE_ID,
            "server_time": "",
            "revision": 0,
            "top_state": "idle",
            "recording_alive": None,
            "seconds_since_last_tick": None,
            "capture_gaps": {"total_count": 0, "total_seconds": 0.0, "recent": []},
        }
    )


def _needs_runtime(
    handler: Callable[[Request], Any] | None = None,
    *,
    fallback: Callable[[], Response] | None = None,
    bind: bool = True,
) -> Any:
    """给路由处理器装上公共序言（15 只处理器逐字同一段的收敛）：
    取 runtime → 未装配回兜底应答 → `bind_from_scope` → 放行。

    HTTP 行为逐字不变：`fallback` 换掉未装配那一档的应答（`_api_heartbeat` 的兜底是
    心跳自己的形状）；`bind=False` 关掉 `bind_from_scope`（`_api_actions` 原本就不
    bind）。同步处理器仍包成同步——Starlette 按处理器是不是协程决定丢不丢线程池，
    盘上读数照旧不在事件循环上跑。
    """

    def install(fn: Callable[[Request], Any]) -> Callable[[Request], Any]:
        def missing() -> Response:
            return fallback() if fallback is not None else _runtime_missing_response()

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(request: Request) -> Response:
                runtime = _runtime_of(request)
                if runtime is None:  # pragma: no cover - 应用未装配
                    return missing()
                if bind:
                    runtime.bind_from_scope(request.scope)
                return await fn(request)
        else:
            @functools.wraps(fn)
            def wrapper(request: Request) -> Response:
                runtime = _runtime_of(request)
                if runtime is None:  # pragma: no cover - 应用未装配
                    return missing()
                if bind:
                    runtime.bind_from_scope(request.scope)
                return fn(request)

        return wrapper

    if handler is None:
        return install
    return install(handler)


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


# GET /api/state · GET /api/heartbeat


@_needs_runtime
async def _api_state(request: Request) -> Response:
    runtime = _runtime_of(request)
    return JSONResponse(runtime.to_dict())


@_needs_runtime(fallback=_heartbeat_missing_response)
async def _api_heartbeat(request: Request) -> Response:
    """`service` 字面量恒为 `"meeting"`（端口策略的探针靠它认亲）。"""
    runtime = _runtime_of(request)
    return JSONResponse(runtime.heartbeat_dict())


# GET /api/summaries（设计：本场按过的那些次会中总结）


@_needs_runtime
async def _api_summaries(request: Request) -> Response:
    """本场总结记录：`{session_id, entries}`（**200**，任何状态下都给）。

    记录活在运行时的内存里、**不落文件**（设计章 2.3 / 5.1）；只记**成功产出**的那几次，
    按时间先后排。没有场次或一次都没按过时 `session_id` 为 `null`、`entries` 是空数组。
    读它是**纯读**：不提交、不涨 `revision`、不广播。
    """
    runtime = _runtime_of(request)
    return JSONResponse(runtime.summaries_dict())


# GET /api/events（SSE）


@_needs_runtime
async def _api_events(request: Request) -> Response:
    runtime = _runtime_of(request)
    return StreamingResponse(
        runtime.hub.stream(runtime.to_dict),
        media_type=SSE_MEDIA_TYPE,
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",  # 避免中间层缓冲
            "Connection": "keep-alive",
        },
    )


# GET /api/sessions · GET /api/dirs（工作台收编节点：两只读端点）


# 这几个处理器是同步 def（不走协程）：它们只是调 runtime 的纯读读取器（盘上 stat 与
# 读 session.json，都不是异步的）；写成 async 会让盘上读数在事件循环线程上跑。
# Starlette 对同步处理器自动丢线程池执行。
@_needs_runtime
def _api_sessions(request: Request) -> Response:
    """工作区注册表：`{sessions, recent}`（**200**，任何状态下都给）。

    与 `GET /api/summaries` 同一档纯读：不提交、不涨 `revision`、不广播；
    "最近一场"只从注册表读（启动与请求侧都**不扫任何场次目录**，
    `docs/design-intent.md` 第 4 条）。
    """
    runtime = _runtime_of(request)
    return JSONResponse(runtime.sessions_dict())


@_needs_runtime
def _api_session_status(request: Request) -> Response:
    """单场状态：`GET /api/sessions/{id}`，`{id}` 是**这一场的目录路径**（URL 编码的绝对路径）。

    会后视图那一片进度区就读它：这一场此刻跑到哪一档、会后离线跑过几块、共几块、
    正在跑哪一路、已经跑了多久（应答体逐格见 `docs/contract.md` 第 4.9 节）。

    与 `GET /api/sessions` / `GET /api/summaries` 同一档**纯读**：不提交、不涨 `revision`、
    不广播。路径打不到已登记的工作区时按第 4.9 节的失败口径回 404（回的是通用失败信封：
    `{ok, code, message, detail}`）。
    """
    runtime = _runtime_of(request)
    try:
        return JSONResponse(runtime.session_status_dict(request.path_params.get("dir")))
    except ActionError as exc:
        return _error_response(exc)


@_needs_runtime
def _api_session_transcripts(request: Request) -> Response:
    """这一场的稿子正文：`GET /api/sessions/{id}/transcripts`（会后区的两份稿）。

    两份稿（会中稿 / 会后稿，`docs/design-intent.md` 第 11 条：平级）× 本地 / 远端
    两路各一份；文件在的那一路给 `text`（正文原样），不在的那一路给 `reason`
    （缺稿的那一档，缺稿话归界面翻）。`{id}` 与失败口径跟单场状态那一只同一套
    （404 两档）。与 `GET /api/sessions` 同一档**纯读**：不提交、不涨 `revision`、
    不广播。
    """
    runtime = _runtime_of(request)
    try:
        return JSONResponse(runtime.session_transcripts_dict(request.path_params.get("dir")))
    except ActionError as exc:
        return _error_response(exc)


@_needs_runtime
def _api_dirs(request: Request) -> Response:
    """目录选择器的现场：`{home, cwd, up, here, dirs}`（**200**；`?dir=` 省略时从主目录起步）。

    纯读：只判每层子目录的名目（三档），不写任何东西。`up` **到盘根才落 `null`**
    （用户主目录不再是天花板，盘根那一档界面按它把「向上一层」置灰）；`here` 是
    正在浏览这一层自己的名目判读——选择器里这一层本身可选。

    `?files=1`（资料界面的选择器文件模式要的）：应答体**多一个 `files` 键**
    ——这一层里的普通文件每项 `{name, dir, size_bytes}`，与 `dirs` 分开两列。不带这个
    参数时没有这个键。
    """
    runtime = _runtime_of(request)
    with_files = (request.query_params.get("files") or "").strip().lower() in ("1", "true")
    try:
        return JSONResponse(
            runtime.dirs_listing(request.query_params.get("dir"), with_files=with_files)
        )
    except ActionError as exc:
        return _error_response(exc)


@_needs_runtime
def _api_dirs_create(request: Request) -> Response:
    """新建目录：`POST /api/dirs?dir=<父层>&name=<名字>`（写端点，头哨在中间件）。

    与 `GET /api/dirs` 同族的一只 REST 写入口（先例是资料上传那条不走动作信封的
    `POST …/materials`）：落点是正在浏览的这一层（`dir` 省略时主目录，与 GET 同一
    起点），名字必给、必须用户亲手起——软件不自动命名、不自动挑落点（设计意图
    第 3、4 条的新口径），只建一层、不做删除、不做改名。

    成功体 = 通用信封 + `directory` 键：新建那一行的名目判读（刚建出来是 `empty`），
    界面拿它把新行插进清单并直接置为选定。拒绝档三条（一层不存在 / 名字非法 /
    同名已存在）与盘上出岔档由 `runtime.dirs_create()` 按契约给；**不涨 `revision`、
    不广播**（选择器的现场独立于四态快照，先例同上）。
    """
    runtime = _runtime_of(request)
    try:
        row = runtime.dirs_create(
            request.query_params.get("dir"), request.query_params.get("name")
        )
    except ActionError as exc:
        return _error_response(exc)
    return JSONResponse({"ok": True, "revision": runtime.revision, "directory": row})


@_needs_runtime
async def _api_devices(request: Request) -> Response:
    """两路各自的候选设备端点表：`{mic: [...], loopback: [...]}`（**200**，任何状态下都给）。

    设置抽屉的两个设备下拉读它。与 `GET /api/dirs` 同一档**纯读**：
    只枚举端点表、**不开任何设备**（`probe()` 会短暂试开，清单不走它）；
    不提交、不涨 `revision`、不广播。
    """
    runtime = _runtime_of(request)
    try:
        return JSONResponse(runtime.devices_listing())
    except ActionError as exc:
        return _error_response(exc)


# 会议资料四条（资料节点）：清单 / 上传 / 取原文件 / 删除


def _materials_missing_error(name: str) -> ActionError:
    """资料里没有这一份的失败体（`material_missing`，404；取原文件与删除共用）。"""
    return ActionError(
        "material_missing",
        f"资料里没有这一份：{name}",
        status=404,
        detail={"name": name},
    )


@_needs_runtime
def _api_materials_listing(request: Request) -> Response:
    """清单：`{dir, materials_dir, items:[{name, size_bytes, added_at}]}`（**200**）。

    **目录就是清单**（没有 JSON 记录）：只读 `meeting_materials/` 的名目，目录还不在
    （一场还没传过资料）时 `items` 是空数组、目录也不建——「首次上传时由服务创建」
    是上传那一侧的事。与 `GET /api/sessions` 同一档纯读：不提交、不涨 `revision`、
    不广播。寻址失败（目录不在原地 / 两样都不是）按 4.12 的失败口径回 404。
    """
    runtime = _runtime_of(request)
    try:
        return JSONResponse(runtime.materials_listing(request.path_params.get("dir")))
    except ActionError as exc:
        return _error_response(exc)


@_needs_runtime
async def _api_materials_upload(request: Request) -> Response:
    """上传：请求体就是文件的原样字节，**流式写盘**（收到多少写多少，不整段进内存）。

    - `name` 必给（查询串），**不可信**：服务侧清洗防路径穿越、重名自动加序号后缀
      （`runtime_materials.sanitize_material_name()` 与 `materials_reserve()`）；
    - **不转码、不压缩、不限类型、大小不设服务端硬顶**；请求的 `Content-Type` 只是
      发送方的说法，服务不读它也不校验它；
    - `from=<路径>`（选择器文件模式要的）：字节不从请求正文来，从盘上那个
      **已有文件流式拷入**——同样原样字节、不整段进内存；`from` 给到不存在的文件回
      **400 `invalid_setting`**（`detail = {key: "from", value, legal}`），此时盘上
      什么都不落；给了 `from` 时请求正文被忽略。清洗 / 重名让位 / 头哨与正文上传
      同一套；
    - 写盘走**独占创建**（`xb`）+ 异步文件（`anyio`，写在工作线程上、事件循环照转，
      心跳与三个 SSE 通道不被一次大上传按住）；「查完重名到真正落盘」之间被人抢了位
      的竞态由 `materials_next_free()` 让位重试，不截断别人正在写的文件；
    - 中途失败（盘上出岔 / 客户端断开）把**半截文件收走**，不留残骸；
    - 成功体 = 通用信封 + `material: {name, size_bytes, added_at}`（贴上时刻 = 落盘
      那一刻的文件修改时间，不保留原文件的时间戳）。**不涨 `revision`**——资料层
      独立于四态快照，信封里的 `revision` 只是当前版本号（先例是 `summarize`）。

    域编排（`from` 档源校验、占名、流式写盘、半截清理）收在
    `runtime_materials.RuntimeMaterialsMixin.materials_receive()` 一处；路由层只留
    HTTP 面：参数与字节流的来路、失败信封与成功信封。
    """
    runtime = _runtime_of(request)
    try:
        material = await runtime.materials_receive(
            request.path_params.get("dir"),
            request.query_params.get("name"),
            body=request.stream(),
            from_param=request.query_params.get("from"),
        )
    except ActionError as exc:
        return _error_response(exc)
    return JSONResponse(
        {"ok": True, "revision": runtime.revision, "material": material}
    )


@_needs_runtime
async def _api_materials_file(request: Request) -> Response:
    """取原文件：原始字节 + 按扩展名判的 `Content-Type`（认不出 `application/octet-stream`）。

    `Content-Type` 用标准库 `mimetypes.guess_type()` 按扩展名判断——它只是给界面的
    提示，服务不校验内容、也不转码；从盘上流式读出（`FileResponse`）。名字先过与上传
    同一套清洗（防路径穿越），存不在这个名字上回 404 `material_missing`。
    """
    runtime = _runtime_of(request)
    try:
        _, path = runtime.materials_item_path(
            request.path_params.get("dir"), request.path_params.get("name")
        )
        if not path.is_file():
            raise _materials_missing_error(path.name)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media_type)
    except ActionError as exc:
        return _error_response(exc)


@_needs_runtime
async def _api_materials_delete(request: Request) -> Response:
    """删除：**只删那一个文件**（场次目录与资料目录里别的文件一个字节不动）。

    名字先过与上传同一套清洗；存不在这个名字上（含重复删除）如实回 404
    `material_missing`。成功体 = 通用信封 + `material: {name}`；不涨 `revision`。
    """
    runtime = _runtime_of(request)
    try:
        _, path = runtime.materials_item_path(
            request.path_params.get("dir"), request.path_params.get("name")
        )
        if not path.is_file():
            raise _materials_missing_error(path.name)
        try:
            path.unlink()
        except OSError as exc:
            raise ActionError(
                "persist_failed",
                f"资料删不掉（{type(exc).__name__}: {exc}）",
                status=500,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            ) from exc
        _log.info("会议资料删除：%s", path)
        return JSONResponse(
            {"ok": True, "revision": runtime.revision, "material": {"name": path.name}}
        )
    except ActionError as exc:
        return _error_response(exc)


# POST /api/actions —— 动作登记表（分发只认这张表，函数末尾没有第三条出路）

#: 动作处理器：`(runtime, args) → 额外成功键`（async；`None` = 通用信封
#: `{"ok": true, "revision": N}` 就够）。每个动作成功体的形状只写在这一处。
_ActionHandler = Callable[[ServiceRuntime, dict[str, Any]], Any]


async def _act_start(runtime: ServiceRuntime, args: dict[str, Any]) -> None:
    # flush 已落定而收尾尾巴（log/post）还在飞的那一档，start 的闸放行 —— 这里先等
    # 尾巴落定再动手（只等不拒；等不到才如实 409），快照不会被半路的收尾踩到。
    await runtime.settle_drain_tail()
    runtime.start_session(args.get("mode"), args.get("dir"))


async def _act_stop(runtime: ServiceRuntime, args: dict[str, Any]) -> None:
    runtime.stop_session("user")


async def _act_mute_session(runtime: ServiceRuntime, args: dict[str, Any]) -> None:
    runtime.mute_session()


async def _act_settings_update(runtime: ServiceRuntime, args: dict[str, Any]) -> dict[str, Any]:
    extra = runtime.settings_update(args)
    return {"applied": extra["applied"], "pending_restart": extra["pending_restart"]}


async def _act_reprobe(runtime: ServiceRuntime, args: dict[str, Any]) -> None:
    runtime.reprobe()


async def _act_dev_inject(runtime: ServiceRuntime, args: dict[str, Any]) -> None:
    runtime.dev_inject(args)


async def _act_summarize(runtime: ServiceRuntime, args: dict[str, Any]) -> dict[str, Any]:
    # 成功体多一个 `summary` 键；`revision` 是当前版本号（总结是只读的，不提交不广播）。
    # `summarize()` 是协程：模型调用在工作线程里跑（`runtime._summary_wait()`），
    # 等它的只有本请求这一条协程——总结那几秒里 `/api/state` 与三个 SSE 通道照常应答。
    return {"summary": await runtime.summarize(args)}


async def _act_workspace_open(runtime: ServiceRuntime, args: dict[str, Any]) -> dict[str, Any]:
    # 成功体多一个 `workspace` 键：判别档位与去重回扣由服务给，界面不派生。
    return {
        "workspace": runtime.workspace_open(args.get("dir"),
                                            confirm=bool(args.get("confirm"))),
    }


async def _act_workspace_remove(runtime: ServiceRuntime, args: dict[str, Any]) -> dict[str, Any]:
    return {"workspace": runtime.workspace_remove(args.get("dir"))}


async def _act_post_offline(runtime: ServiceRuntime, args: dict[str, Any]) -> dict[str, Any]:
    return {"post": runtime.post_offline(args)}


async def _act_post_minutes(runtime: ServiceRuntime, args: dict[str, Any]) -> dict[str, Any]:
    return {"post": runtime.post_minutes(args)}


async def _act_post_stop(runtime: ServiceRuntime, args: dict[str, Any]) -> dict[str, Any]:
    return {"post": runtime.post_stop(args)}


async def _act_transcode_run(runtime: ServiceRuntime, args: dict[str, Any]) -> dict[str, Any]:
    # 成功体多一个 `transcode` 键：回执只说「起没起、跑的哪一场」，跑成什么样由盘上的
    # `meeting_transcode_status.json` 回答。
    return {"transcode": runtime.transcode_run(args)}


async def _act_transcode_pause(runtime: ServiceRuntime, args: dict[str, Any]) -> dict[str, Any]:
    return {"transcode": runtime.transcode_pause(args)}


async def _act_transcode_resume(runtime: ServiceRuntime, args: dict[str, Any]) -> dict[str, Any]:
    return {"transcode": runtime.transcode_resume(args)}


async def _act_pause(runtime: ServiceRuntime, args: dict[str, Any]) -> None:
    runtime.pause_session()


async def _act_resume(runtime: ServiceRuntime, args: dict[str, Any]) -> None:
    runtime.resume_session()


async def _act_set_source(runtime: ServiceRuntime, args: dict[str, Any]) -> None:
    runtime.set_source(args)


async def _act_speaker_rename(runtime: ServiceRuntime, args: dict[str, Any]) -> dict[str, Any]:
    # 说话人改名（R10 第四条 / R30）：只写这一场的人名表（不拉离线/纪要、不动快照），
    # 成功体多一个 `speaker` 键；界面改完当场生效走的是重问单场状态那一跳。
    return {"speaker": runtime.speaker_rename(args)}


#: 会后的两个按钮（R26）与 `post_stop`（R28）共用同一条成功体规矩：
#: 起一条后台线程即算启动成功 / 立起取消旗标即算受理成功，成功体多一个 `post` 键，
#: 停没停成由盘上的状态文件回答（`post_stop` 的回执多一格 `stopping: true`）。
_ACTION_HANDLERS: dict[str, _ActionHandler] = {
    "start": _act_start,
    "stop": _act_stop,
    "mute_session": _act_mute_session,
    "settings_update": _act_settings_update,
    "reprobe": _act_reprobe,
    "dev_inject": _act_dev_inject,
    "summarize": _act_summarize,
    "workspace_open": _act_workspace_open,
    "workspace_remove": _act_workspace_remove,
    "post_offline": _act_post_offline,
    "post_minutes": _act_post_minutes,
    "post_stop": _act_post_stop,
    "transcode_run": _act_transcode_run,
    "transcode_pause": _act_transcode_pause,
    "transcode_resume": _act_transcode_resume,
    "pause": _act_pause,
    "resume": _act_resume,
    "set_source": _act_set_source,
    "speaker_rename": _act_speaker_rename,
}


def _reconcile_action_registry() -> None:
    """启动期核对：登记表与 `ACTION_NAMES` 两份名单必须恰好对上，对不上当场炸。

    `ACTION_NAMES`（runtime 冻结的那一份，`detail.known` 与契约按它答）与分发表的
    名单从前靠纪律同步：给 `ACTION_NAMES` 加名漏加分支时，请求穿过分发链落到函数末尾、
    什么都不做却回 `ok: true`。现在登记表是分发的唯一依据，本模块被导入（＝应用装配、
    服务起跑之前）时把两边数一遍：任何一边多一个名、少一个名，启动就此失败 ——
    漂移活不过启动。
    """
    registered = set(_ACTION_HANDLERS)
    frozen = set(ACTION_NAMES)
    if registered == frozen:
        return
    raise RuntimeError(
        "动作清单漂移，拒绝启动："
        f"ACTION_NAMES 有而登记表没有：{sorted(frozen - registered)}；"
        f"登记表有而 ACTION_NAMES 没有：{sorted(registered - frozen)}。"
        "请核对 meeting/service/routes.py 的 `_ACTION_HANDLERS` 与 "
        "meeting/service/runtime.py 的 `ACTION_NAMES`"
    )


_reconcile_action_registry()


@_needs_runtime(bind=False)
async def _api_actions(request: Request) -> Response:
    """动作端点：信封 + 十九个动作名 + `code` 表（中间件已校验两个必需请求头）。

    分发走模块顶那张「动作名 → 处理器」登记表（`_ACTION_HANDLERS`）：表是分发的唯一
    依据，并在本模块导入时与 `ACTION_NAMES` 核对过（`_reconcile_action_registry()`）。
    查表查不到的名字在这里**显式失败** —— 从前那条「穿过分发链落到函数末尾、
    什么都不做却回 `ok: true`」的假成功通道，在结构上不存在了。
    """
    runtime = _runtime_of(request)

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
        handler = _ACTION_HANDLERS.get(action)
        if handler is None:  # pragma: no cover - 导入期核对之后到不了（漂移挡在启动前）
            raise ActionError(
                "invalid_setting",
                f"动作 {action!r} 在 ACTION_NAMES 里却没有登记处理器"
                "（runtime 接受而 routes 未实现 = 两份清单漂移）："
                "请核对 meeting/service/routes.py 的 `_ACTION_HANDLERS` 与 "
                "meeting/service/runtime.py 的 `ACTION_NAMES`",
                status=500,
                detail={"action": action, "known": list(ACTION_NAMES)},
            )
        extra = await handler(runtime, args)
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

    body: dict[str, Any] = {"ok": True, "revision": runtime.revision}
    if extra:
        body.update(extra)
    return JSONResponse(body)


# GET /debug


async def _debug_page(request: Request) -> Response:
    """骨架自检页：从**包内**读取（`package-data` 覆盖 `meeting/service/*.html`）。"""
    from starlette.responses import HTMLResponse

    from meeting.service import static as static_module

    return HTMLResponse(static_module.read_debug_page())


# 注册


def api_routes() -> list[Route]:
    """十五条 `/api/*` 路由（`/debug` 在 `debug_routes()` 里，同一个模块）。"""
    return [
        Route("/api/state", _api_state, methods=["GET"]),
        Route("/api/events", _api_events, methods=["GET"]),
        Route("/api/heartbeat", _api_heartbeat, methods=["GET"]),
        Route("/api/actions", _api_actions, methods=["POST"]),
        Route("/api/summaries", _api_summaries, methods=["GET"]),
        Route("/api/sessions", _api_sessions, methods=["GET"]),
        # 会议资料四条：注册在单场状态那条之前（`{dir:path}` 收得下斜杠，排在后面会被
        # 吃进 `{dir}`）。GET 与 DELETE 同路径异方法：Starlette 找 FULL 匹配，方法不匹配
        # 的算 PARTIAL，互不抢。
        Route("/api/sessions/{dir:path}/materials", _api_materials_listing, methods=["GET"]),
        Route("/api/sessions/{dir:path}/materials", _api_materials_upload, methods=["POST"]),
        Route("/api/sessions/{dir:path}/materials/{name}", _api_materials_file, methods=["GET"]),
        Route(
            "/api/sessions/{dir:path}/materials/{name}",
            _api_materials_delete,
            methods=["DELETE"],
        ),
        # 稿子正文：与资料四条同一批，注册在单场状态那条之前（`{dir:path}` 会吃进
        # `/transcripts` 那层路径）。
        Route(
            "/api/sessions/{dir:path}/transcripts",
            _api_session_transcripts,
            methods=["GET"],
        ),
        # 单场状态（会后视图的进度区）：`{dir:path}` 是为了收下 URL 编码的绝对路径
        # ——Windows 路径里的反斜杠过不了默认的路径段匹配。注册在资料四条之后：
        # 星形路由按注册顺序匹配，资料那几条先吃自己的路径。
        Route("/api/sessions/{dir:path}", _api_session_status, methods=["GET"]),
        Route("/api/dirs", _api_dirs, methods=["GET"]),
        # 新建目录（目录选择器节点）：与 GET 同路径异方法，REST 写入口、不走动作信封，
        # 头哨由中间件的核对表盖住。
        Route("/api/dirs", _api_dirs_create, methods=["POST"]),
        Route("/api/devices", _api_devices, methods=["GET"]),
    ]


def debug_routes() -> list[Route]:
    """`GET /debug` 的唯一注册点（**从 `meeting/service/static.py` 摘除了旧注册**）。"""
    return [Route("/debug", _debug_page, methods=["GET"])]


def all_routes() -> list[Route]:
    """十六条路由一次给全（十五条 `/api/*` + `/debug`；装配模块只负责把它们挂上应用）。"""
    return api_routes() + debug_routes()


__all__ = [
    "SSE_MEDIA_TYPE",
    "all_routes",
    "api_routes",
    "debug_routes",
]
