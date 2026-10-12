"""九条路由的**唯一注册点**（phase2 §3.7 / §3.8 / §3.9 / §3.11 / §3.12；`1987cb20` phase2 加 `summaries`；
工作台收编节点 e2f6166b 加 `sessions` / `dirs` 两只读端点与 `workspace_open` / `workspace_remove` 两动作；
会后进度读数节点 `a9affaa8` 加 `sessions/{dir}` 那只单场状态端点；设置抽屉节点 `311f84f5` 加
`devices` 那只设备清单端点；人名节点 `91b6752e` 加 `speaker_rename` 那只改名动作；打断会后的
b1c6a58e 加 `post_stop` 那只停掉动作；资料节点 285bf58c 加会议资料的四条端点
——清单 / 上传 / 取原文件 / 删除，都在 `/api/sessions/{dir:path}` 之后那两层路径上，
注册在单场状态那条**之前**（`{dir:path}` 收得下斜杠，注册在后会抢走资料那几条）。

全应用只在这里注册一次：

  - `GET /api/state`（完整版，body 就是 `to_dict()` 的产出）；
  - `GET /api/events`（SSE：`snapshot` 先行 + `state` / `levels` / `transcript` 三通道）；
  - `GET /api/heartbeat`（完整版，`service` 字面量必须有）；
  - `POST /api/actions`（信封 + 十六个动作名 + `code` 表；分发走 `_ACTION_HANDLERS`
    那张「动作名 → 处理器」登记表，并在导入时与 runtime 的 `ACTION_NAMES` 核对 ——
    两份名单对不上，服务起不来）；
  - `GET /api/summaries`（本场按过的、成功产出的那几次会中总结；读内存，不落文件）；
  - `GET /api/sessions`（工作区注册表：按路径去重的那一份"我这些会"，读盘上注册表文件）；
  - `GET /api/sessions/{id}`（单场状态：这一场此刻跑到哪一档、会后离线跑过几块 / 共几块、
    正在跑哪一路、已经跑了多久；`{id}` 是这一场的目录路径，形状见 `docs/contract.md` 4.9）；
  - 会议资料四条（资料节点 285bf58c，`{id}` 同样是 URL 编码的场次目录路径；
    语义见 `docs/contract.md` 4.12）：
    - `GET /api/sessions/{id}/materials`（清单：目录就是清单，没有 JSON 记录）；
    - `POST /api/sessions/{id}/materials?name=`（上传：原样字节流式写盘；二进制正文
      装不进 JSON 动作信封，是唯一一组不走 `/api/actions` 的写端点——头哨由中间件
      按端点扩到它们身上，`meeting/service/app.py` 的 `_LocalGuardsMiddleware`）；
    - `GET /api/sessions/{id}/materials/{name}`（取原文件，`Content-Type` 按扩展名）；
    - `DELETE /api/sessions/{id}/materials/{name}`（删那一个文件）；
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
import mimetypes
from pathlib import Path
from typing import Any, Callable

import anyio
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from meeting.service.runtime import ACTION_NAMES, ActionError, ServiceRuntime

_log = logging.getLogger("meeting.service.routes")

#: SSE 的响应类型
SSE_MEDIA_TYPE = "text/event-stream"

#: `from` 档（选择器文件模式）拷贝源文件时的块大小：一块一兆，原样字节、不整段进内存
_MATERIALS_COPY_CHUNK_BYTES = 1 << 20


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
                "capture_gaps": {"total_count": 0, "total_seconds": 0.0, "recent": []},
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


# 这三个处理器都是**同步 def**（不走协程）：它们各自整段只是调 runtime 的那只纯读
# 读取器（`sessions_dict()` / `session_status_dict()` / `dirs_listing()` 每行 stat +
# 读 session.json + 注册表读，都不是异步的）——写成 async 会让这些盘上读数在事件循环
# 线程上跑；Starlette 对同步处理器自动丢线程池执行，行为与原本一致。
def _api_sessions(request: Request) -> Response:
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


def _api_session_status(request: Request) -> Response:
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


def _api_dirs(request: Request) -> Response:
    """目录选择器的现场：`{home, cwd, up, dirs}`（**200**；`?dir=` 省略时从主目录起步）。

    纯读：只判每层子目录的名目（三档），不写任何东西。「软件自己会不建目录」这条
    在这里同样成立 —— 选择器只用来挑一个**已有**目录。

    `?files=1`（资料界面的选择器文件模式要的，160d1a78）：应答体**多一个 `files` 键**
    ——这一层里的普通文件每项 `{name, dir, size_bytes}`，与 `dirs` 分开两列。不带这个
    参数时没有这个键，旧应答体一个字节不动（挑录音落点的老路径照旧）。
    """
    runtime = _runtime_of(request)
    if runtime is None:  # pragma: no cover - 应用未装配
        return JSONResponse({"error": "runtime 未装配"}, status_code=500)
    runtime.bind_from_scope(request.scope)
    with_files = (request.query_params.get("files") or "").strip().lower() in ("1", "true")
    try:
        return JSONResponse(
            runtime.dirs_listing(request.query_params.get("dir"), with_files=with_files)
        )
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
# 会议资料四条（资料节点 285bf58c）：清单 / 上传 / 取原文件 / 删除
# ---------------------------------------------------------------------------


def _materials_missing_error(name: str) -> ActionError:
    """资料里没有这一份的失败体（`material_missing`，404；取原文件与删除共用）。"""
    return ActionError(
        "material_missing",
        f"资料里没有这一份：{name}",
        status=404,
        detail={"name": name},
    )


def _api_materials_listing(request: Request) -> Response:
    """清单：`{dir, materials_dir, items:[{name, size_bytes, added_at}]}`（**200**）。

    **目录就是清单**（没有 JSON 记录）：只读 `meeting_materials/` 的名目，目录还不在
    （一场还没传过资料）时 `items` 是空数组、目录也不建——「首次上传时由服务创建」
    是上传那一侧的事。与 `GET /api/sessions` 同一档纯读：不提交、不涨 `revision`、
    不广播。寻址失败（目录不在原地 / 两样都不是）按 4.12 的失败口径回 404。
    """
    runtime = _runtime_of(request)
    if runtime is None:  # pragma: no cover - 应用未装配
        return JSONResponse({"error": "runtime 未装配"}, status_code=500)
    runtime.bind_from_scope(request.scope)
    try:
        return JSONResponse(runtime.materials_listing(request.path_params.get("dir")))
    except ActionError as exc:
        return _error_response(exc)


async def _api_materials_upload(request: Request) -> Response:
    """上传：请求体就是文件的原样字节，**流式写盘**（收到多少写多少，不整段进内存）。

    - `name` 必给（查询串），**不可信**：服务侧清洗防路径穿越、重名自动加序号后缀
      （`runtime_materials.sanitize_material_name()` 与 `materials_reserve()`）；
    - **不转码、不压缩、不限类型、大小不设服务端硬顶**；请求的 `Content-Type` 只是
      发送方的说法，服务不读它也不校验它；
    - `from=<路径>`（选择器文件模式要的，160d1a78）：字节不从请求正文来，从盘上那个
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
    """
    runtime = _runtime_of(request)
    if runtime is None:  # pragma: no cover - 应用未装配
        return JSONResponse({"error": "runtime 未装配"}, status_code=500)
    runtime.bind_from_scope(request.scope)

    #: `from` 档（选择器文件模式）：拷贝源头必须是**已存在的普通文件**，先验再占名
    from_param = request.query_params.get("from")
    source_path: Path | None = None
    if from_param is not None and from_param.strip() != "":
        source_path = Path(from_param)
        if not source_path.is_file():
            return _error_response(
                _action_error(
                    "invalid_setting",
                    f"要拷入的文件不存在：{from_param}",
                    status=400,
                    detail={
                        "key": "from",
                        "value": from_param,
                        "legal": ["已存在的文件路径（目录选择器文件模式给的那一个）"],
                    },
                )
            )

    try:
        path = runtime.materials_reserve(
            request.path_params.get("dir"), request.query_params.get("name")
        )
    except ActionError as exc:
        return _error_response(exc)

    def _discard_partial() -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 残骸清理失败只留痕
            _log.warning("上传失败后的半截文件收不走：%s", path)

    try:
        while True:
            try:
                handle = await anyio.open_file(path, "xb")
                break
            except FileExistsError:
                # 查完重名到真正落盘之间被人抢了位：让到下一个序号名再试
                path = runtime.materials_next_free(path)
        try:
            if source_path is None:
                async for chunk in request.stream():
                    await handle.write(chunk)
            else:
                # from 档：流式拷贝盘上那份源文件（原样字节，一块一兆，不整段进内存）
                source = await anyio.open_file(source_path, "rb")
                try:
                    while True:
                        chunk = await source.read(_MATERIALS_COPY_CHUNK_BYTES)
                        if not chunk:
                            break
                        await handle.write(chunk)
                finally:
                    await source.aclose()
        finally:
            await handle.aclose()
    except ActionError:
        _discard_partial()
        raise
    except OSError as exc:
        _discard_partial()
        return _error_response(
            _action_error(
                "persist_failed",
                f"资料写盘失败（{type(exc).__name__}: {exc}）",
                status=500,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            )
        )
    except BaseException:
        _discard_partial()
        raise
    material = runtime.materials_record(path)
    _log.info("会议资料落盘：%s（%d 字节）", path, material["size_bytes"])
    return JSONResponse(
        {"ok": True, "revision": runtime.revision, "material": material}
    )


async def _api_materials_file(request: Request) -> Response:
    """取原文件：原始字节 + 按扩展名判的 `Content-Type`（认不出 `application/octet-stream`）。

    `Content-Type` 用标准库 `mimetypes.guess_type()` 按扩展名判断——它只是给界面的
    提示，服务不校验内容、也不转码；从盘上流式读出（`FileResponse`）。名字先过与上传
    同一套清洗（防路径穿越），存不在这个名字上回 404 `material_missing`。
    """
    runtime = _runtime_of(request)
    if runtime is None:  # pragma: no cover - 应用未装配
        return JSONResponse({"error": "runtime 未装配"}, status_code=500)
    runtime.bind_from_scope(request.scope)
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


async def _api_materials_delete(request: Request) -> Response:
    """删除：**只删那一个文件**（场次目录与资料目录里别的文件一个字节不动）。

    名字先过与上传同一套清洗；存不在这个名字上（含重复删除）如实回 404
    `material_missing`。成功体 = 通用信封 + `material: {name}`；不涨 `revision`。
    """
    runtime = _runtime_of(request)
    if runtime is None:  # pragma: no cover - 应用未装配
        return JSONResponse({"error": "runtime 未装配"}, status_code=500)
    runtime.bind_from_scope(request.scope)
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


# ---------------------------------------------------------------------------
# POST /api/actions —— 动作登记表（分发只认这张表，函数末尾没有第三条出路）
# ---------------------------------------------------------------------------

#: 动作处理器：`(runtime, args) → 额外成功键`（async；`None` = 通用信封
#: `{"ok": true, "revision": N}` 就够）。每个动作成功体的形状只写在**这一处** ——
#: 从前「分支 + 成功体」要在 if/elif 链和返回体两处一起写的日子到此为止。
_ActionHandler = Callable[[ServiceRuntime, dict[str, Any]], Any]


async def _act_start(runtime: ServiceRuntime, args: dict[str, Any]) -> None:
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
    # 成功体在通用信封上多一个 `summary` 键（先例是 `settings_update` 的
    # `applied` / `pending_restart`）；`revision` 是**当前**版本号 —— 总结是只读的，
    # 这里不提交、不涨版本、不广播。
    # `summarize()` 是一条协程：那一次模型调用在**工作线程**里跑
    # （`runtime._summary_wait()`，复审 minor 6），等它的只有本请求这一条协程 ——
    # 于是总结那几秒里 `/api/state` 与三个 SSE 通道照常应答。
    return {"summary": await runtime.summarize(args)}


async def _act_workspace_open(runtime: ServiceRuntime, args: dict[str, Any]) -> dict[str, Any]:
    # 成功体在通用信封上多一个 `workspace` 键（先例是 `summarize` 的 `summary`）：
    # 判别档位与去重回扣由服务给，界面不派生；失败档（目录不存在、非空未确认、
    # 注册表写盘失败）走 ActionError 的 `code` 表。
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


async def _act_pause(runtime: ServiceRuntime, args: dict[str, Any]) -> None:
    runtime.pause_session()


async def _act_resume(runtime: ServiceRuntime, args: dict[str, Any]) -> None:
    runtime.resume_session()


async def _act_set_source(runtime: ServiceRuntime, args: dict[str, Any]) -> None:
    runtime.set_source(args)


async def _act_speaker_rename(runtime: ServiceRuntime, args: dict[str, Any]) -> dict[str, Any]:
    # 说话人改名（R10 第四条 / R30）：只写这一场目录里的人名表（不拉任何离线/纪要、
    # 不动快照），成功体在通用信封上多一个 `speaker` 键——写了哪一场、哪一个键、
    # 现在叫什么；界面改完当场生效走的是重问单场状态那一跳。
    return {"speaker": runtime.speaker_rename(args)}


#: 会后的两个按钮（R26）与停掉会后精细稿那一趟的动作（b1c6a58e，R28）共用同一条
#: 成功体规矩：起一条后台线程即算启动成功 / 立起取消旗标即算受理成功，成功体在通用
#: 信封上多一个 `post` 键——「起的是哪一场、停的是哪一场」，停没停成由盘上的状态文件
#: 回答（`post_stop` 的受理回执多一格 `stopping: true`）。
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


async def _api_actions(request: Request) -> Response:
    """动作端点：信封 + 十六个动作名 + `code` 表（中间件已校验两个必需请求头）。

    分发走模块顶那张「动作名 → 处理器」登记表（`_ACTION_HANDLERS`）：表是分发的唯一
    依据，并在本模块导入时与 `ACTION_NAMES` 核对过（`_reconcile_action_registry()`）。
    查表查不到的名字在这里**显式失败** —— 从前那条「穿过分发链落到函数末尾、
    什么都不做却回 `ok: true`」的假成功通道，在结构上不存在了。
    """
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
    """十三条 `/api/*` 路由（`/debug` 在 `debug_routes()` 里，同一个模块）。"""
    return [
        Route("/api/state", _api_state, methods=["GET"]),
        Route("/api/events", _api_events, methods=["GET"]),
        Route("/api/heartbeat", _api_heartbeat, methods=["GET"]),
        Route("/api/actions", _api_actions, methods=["POST"]),
        Route("/api/summaries", _api_summaries, methods=["GET"]),
        Route("/api/sessions", _api_sessions, methods=["GET"]),
        # 会议资料四条（资料节点 285bf58c）：注册在单场状态那条**之前**——
        # `{dir:path}` 收得下斜杠，单场状态若排在前面会把 `/materials` 那两层
        # 路径整个吃进 `{dir}` 里。GET 与 DELETE 同路径异方法：Starlette 按注册
        # 顺序找 FULL 匹配，方法不匹配的算 PARTIAL，互不抢。
        Route("/api/sessions/{dir:path}/materials", _api_materials_listing, methods=["GET"]),
        Route("/api/sessions/{dir:path}/materials", _api_materials_upload, methods=["POST"]),
        Route("/api/sessions/{dir:path}/materials/{name}", _api_materials_file, methods=["GET"]),
        Route(
            "/api/sessions/{dir:path}/materials/{name}",
            _api_materials_delete,
            methods=["DELETE"],
        ),
        # 单场状态（会后视图的进度区）：`{dir:path}` 是为了收下 URL 编码的绝对路径
        # ——Windows 上的 `C:\会议\250906` 里那两段反斜杠过不了默认的路径段匹配。
        # 注册在资料四条之后：星形路由按注册顺序匹配，资料那几条先吃自己的路径。
        Route("/api/sessions/{dir:path}", _api_session_status, methods=["GET"]),
        Route("/api/dirs", _api_dirs, methods=["GET"]),
        Route("/api/devices", _api_devices, methods=["GET"]),
    ]


def debug_routes() -> list[Route]:
    """`GET /debug` 的唯一注册点（**从 `meeting/service/static.py` 摘除了旧注册**）。"""
    return [Route("/debug", _debug_page, methods=["GET"])]


def all_routes() -> list[Route]:
    """十四条路由一次给全（装配模块只负责把它们挂上应用）。"""
    return api_routes() + debug_routes()


__all__ = [
    "SSE_MEDIA_TYPE",
    "all_routes",
    "api_routes",
    "debug_routes",
]
