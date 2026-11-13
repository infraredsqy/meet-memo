"""web 分支：把这一场会后纪要**交付**给 DSH web 服务端（的后续增量）。

## 为什么要有这一层

headless 那条路（`dsh --profile headless`）建的会话**不带工作区归属**：它直接经
`agents.create({meta: {cwd}})` 建会话，压根不碰 DSH 的 workspace registry，而 DSH 只在
workspace domain 首次初始化时按 cwd 收养过一次游离会话、之后不再收养。于是那一场在
DSH GUI 里落在「未分组」里，用户不知道纪要跑到哪儿了。

`session/create` 的契约是「`workspaceId` 与 `cwd` 只能给一个」，而**只给 `cwd` 也不进
工作区分组** —— 要让它出现在场次目录对应的那个工作区里，只有「先 `workspace/create`
（幂等）、再**只带 `workspaceId`** 建会话、最后 `session/prompt`」这一条路。它走 DSH 的
`/api`，因此这一层要抄三样协议细节（2026-09-28 按 DSH 0.1.5-rc.2 实测校准）：

  ① **端点写斜杠形式**（`workspace/create` 这类）：点号写法回 not found，而且
     `workspace.list` 这个一元方法**根本不存在**（工作区清单走 `workspace/follow` 那条流）
     —— 所以存活探针用**只读、无参数**的 `session/modelCatalog`；
  ② **信封是 `{type, rpcId, method, payload:{args:<wire>:{…}}}`**：`<wire>` 取自端点描述符的
     `wire` 字段，这三个端点都叫 `request`（`session/list` 那种要的是 `_request`）；
  ③ **`/api` 只认 HMAC 签名的 browser-session cookie**：没有它一律 401。签名密钥是持久的，
     住在 `~/.dsh/.credentials.yaml` 的 `client-connection/browser-session` 记录里、跨重启有效，
     因此这里**自签**一枚（有效期 30 天，与 dsh 默认一致），不依赖任何一次性进程态。

## 交付语义：投递 accepted 即算交付完成

这一层**不等** DSH 那边把纪要与附录写完，也不校验产物：**停不停 DSH 是用户的事**，
超出这一层的边界。它与 `launch.launch_headless()` 的分工因此是清楚的：

  - `launch_headless()`：起一个自己的子进程，**等它退出**并按四条判据校验产物
    （走到那里说明纪要真的在盘上）；
  - 本模块的 `handoff()`：把这一场**交给 DSH 服务端**，返回时只保证「三步都 accepted」，
    终态记 `delivered`。跑成什么样，在 DSH GUI 的那条会话里看得见。

## 权限不是这一层说了算的

headless 那条路靠子进程环境里那格 `DSH_PERMISSION_MODE=danger-full-access`（它没有界面应答
审批）；走 `/api` 建的会话**权限取 DSH 服务端自己的设置**，本层给不了。DSH 那边若落在
`approval=ask`，无人值守的纪要会卡在等审批 —— 这是走 web 分支要认下的一件事，如实写在这里。

## 接缝

`_post_json()` 是本模块**唯一**的 HTTP 出口（探针与三条 RPC 都走它）：用例把它换成假件，
或者对着一个本机假端点跑真 `urlopen`（两条路都不碰真实的 3080）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meeting.minutes.inputs import write_text_atomic
from meeting.minutes.status import (
    MINUTES_RUN_LOG_NAME,
    RUN_PROMPT_NAME,
    WORKING_DIR,
    DeliveryForbidden,
    FAILURE_TIMEOUT,
    ISOLATION_GRANT,
    ISOLATION_MESSAGE,
    write_run_log,
)

_log = logging.getLogger("meeting.minutes.dsh_web")

#: DSH web 服务端的缺省地址（配置键 `minutes.dsh_url` 的缺省值，两处同值）
DEFAULT_DSH_URL = "http://127.0.0.1:3080"

#: 探针的两段超时：先 TCP（端口没人听就到此为止），再发一次只读 RPC
PROBE_TCP_TIMEOUT_SECONDS = 2.0
PROBE_RPC_TIMEOUT_SECONDS = 3.0

#: 单次 `/api` 调用的超时
RPC_TIMEOUT_SECONDS = 10.0

#: 交付这一步的整段墙钟上限（`runner` 递下来的 `minutes.timeout_seconds` 是几小时那一档，
#: 那是给 DSH 生成用的预算，不该当成一次 HTTP 交付的预算）
DEFAULT_HANDOFF_SECONDS = 60.0
MIN_HANDOFF_SECONDS = 1.0
MAX_HANDOFF_SECONDS = 60.0

#: 连接层抖动时的重试次数与退避基数（`ok=false` 与 4xx **一律不重试**：重放同一个错误）
MAX_ATTEMPTS = 2
RETRY_BASE_DELAY_SECONDS = 1.0

#: 存活探针用的方法（只读、无参数、确实存在）
LIVENESS_METHOD = "session/modelCatalog"

#: 交付的三步（次序固定，理由见模块头）
WORKSPACE_CREATE = "workspace/create"
SESSION_CREATE = "session/create"
SESSION_PROMPT = "session/prompt"

#: 三个端点的参数名（端点描述符的 `wire` 字段）
REQUEST_WIRE = "request"

#: 交付失败的分档（读方按它分档，不必去解析错误消息）。`FAILURE_TIMEOUT` 的唯一定义点
#: 在 `status.py`（两条分支的失败词落进同一格 `dsh.failure`，同名词必须同值）。
#:
#: 第一档说的是**凭据**：这一层唯一在握手之前就能断掉的环节是自签 cookie（读不到
#: `~/.dsh/.credentials.yaml`、那条记录不在、密钥长度不对）。连不上服务端那一档不在这里
#: —— 走不到交付这一步（`probe()` 已经把「没在跑」判在前面了）。
FAILURE_CREDENTIALS = "credentials"
FAILURE_WORKSPACE = "workspace_failed"
FAILURE_SESSION = "session_failed"
FAILURE_PROMPT = "prompt_failed"
FAILURES: tuple[str, ...] = (
    FAILURE_CREDENTIALS,
    FAILURE_WORKSPACE,
    FAILURE_SESSION,
    FAILURE_PROMPT,
    FAILURE_TIMEOUT,
)

#: 签名密钥的落点与它在凭据文件里的那条记录
CREDENTIALS_PATH = Path.home() / ".dsh" / ".credentials.yaml"
SECRET_RECORD = "client-connection/browser-session"
SECRET_PATTERN = re.compile(
    rf"{re.escape(SECRET_RECORD)}:[\s\S]*?secret:\s*([A-Za-z0-9_\-]+)"
)

#: 放行参数缺席时的拒绝理由（`probe()` / `handoff()` 共用；话术与 headless 启动器
#: 同源——括号补语取 `status.ISOLATION_GRANT` 那一份，措辞不再各漂各的）
ISOLATION_REASON = f"{ISOLATION_MESSAGE}（{ISOLATION_GRANT}）"

#: 自签 cookie 的有效期（与 dsh 默认的 cookieMaxAgeDays 一致）
COOKIE_MAX_AGE_MS = 30 * 24 * 3600 * 1000

#: 这一层唯一的 HTTP 出口（用例的接缝）
PostJson = Callable[[str, str, bytes, str, float], tuple[int, str]]


class WebHandoffError(RuntimeError):
    """交付这一步自己的错误（签不出 cookie、三条 RPC 之一被拒）。

    与 `launch.LaunchError` 同一档：它是这一层的**调用方错误**，`handoff()` 自己
    不抛它（那一档走 `HandoffResult.failure`），抛它的是探针与内部步骤。
    """


@dataclass(frozen=True)
class HandoffResult:
    """一次交付的结果（成功与各档失败共用同一个形状）。

    `workspace_id` / `workspace_title` / `session_id` 是交付的落点（失败时可能只走到一半，
    前面几步的取值照样带上 —— 排查时最要紧的正是「走到哪儿断的」）。`seconds` 是这一趟的
    墙钟秒数，`failure` 是 `FAILURES` 里的一档，`error` 是给人看的原因。
    """

    ok: bool
    url: str
    seconds: float
    workspace_id: str = ""
    workspace_title: str = ""
    session_id: str = ""
    failure: str | None = None
    error: str | None = None
    steps: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        """状态文件里 `dsh` 那一格要的那几样（与 headless 那一版同形，多两格落点）。"""
        return {
            "branch": "web",
            "url": self.url,
            "seconds": round(float(self.seconds), 3),
            "workspace_id": self.workspace_id,
            "workspace_title": self.workspace_title,
            "session_id": self.session_id,
            "steps": list(self.steps),
            "failure": self.failure,
        }


# --- 自签 cookie ------------------------------------------------------------


def _b64url_encode(raw: bytes) -> str:
    """base64url（去掉 `=` 填充）：cookie 的名字与三段值都用这个口径。"""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    """base64url 解码（按 DSH 的写法补回填充）。"""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def load_signing_secret(path: str | Path | None = None) -> bytes:
    """读出持久签名密钥（32 字节）。

    凭据文件是一份 YAML，但这里只认一条记录、一个字段，因此**不引 YAML 依赖**：正则取出来
    的那段是 base64url 编码的密钥。取不到、长度不对都当场抛 `WebHandoffError` —— 调用方
    （`probe()`）把它翻成「web 分支不可用」，不给半枚 cookie 去撞 401。
    """
    credentials = CREDENTIALS_PATH if path is None else Path(path)
    if not credentials.exists():
        raise WebHandoffError(
            f"读不到 DSH 凭据文件 {credentials} —— 确认 dsh web 至少成功启动过一次"
)
    match = SECRET_PATTERN.search(credentials.read_text(encoding="utf-8"))
    if match is None:
        raise WebHandoffError(
            f"凭据文件里没有 {SECRET_RECORD} 记录（{credentials}）"
)
    secret = _b64url_decode(match.group(1))
    if len(secret) != 32:
        raise WebHandoffError(f"签名密钥长度异常（{len(secret)} 字节，应为 32）")
    return secret


def signing_cookie(
    url: str, *, secret: bytes | None = None, credentials: str | Path | None = None
) -> str:
    """自签一枚 browser-session cookie：`<name>=v1.<body>.<hmac>`。

    cookie 名 = `dsh-auth-` + base64url(sha256(authority))；值是
    `v1.<base64url(json payload)>.<base64url(hmac_sha256(secret, body))>`。payload 里的
    `authority` 就是 `host:port`（DSH 按它认这份 cookie 是给谁的）。
    """
    authority = urllib.parse.urlsplit(url).netloc
    key = load_signing_secret(credentials) if secret is None else secret
    name = "dsh-auth-" + _b64url_encode(
        hashlib.sha256(authority.encode("utf-8")).digest()
)
    now = int(time.time() * 1000)
    payload = {
        "version": 1,
        "authority": authority,
        "issuedAt": now,
        "expiresAt": now + COOKIE_MAX_AGE_MS,
    }
    body = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _b64url_encode(
        hmac.new(key, body.encode("ascii"), hashlib.sha256).digest()
)
    return f"{name}=v1.{body}.{signature}"


# --- HTTP 出口与 RPC --------------------------------------------------------


def _post_json(
    url: str, method: str, payload: bytes, cookie: str, timeout: float
) -> tuple[int, str]:
    """POST 一次 `/api/<method>`，交回 `(状态码, 文本)`；**HTTP 错误不抛异常**。

    这是本模块唯一的 HTTP 出口：`4xx` / `5xx` 也照常交回状态码，由 `rpc_call()` 决定
    「这一档重试有没有意义」（401 / 404 重试多少次都一样）。
    """
    request = urllib.request.Request(
        f"{url}/api/{method}",
        data=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Cookie": cookie,
        },
        method="POST",
)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        with_error_body = ""
        try:
            with_error_body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover - 错误体读不出来时只剩状态行
            with_error_body = str(exc.reason)
        return int(exc.code), with_error_body


def _envelope(method: str, request: Mapping[str, Any] | None) -> bytes:
    """一条 unary 请求的信封（`args` 那一层不能少，参数名的出处见模块头 ②）。"""
    body: dict[str, Any] = {
        "type": "client-request",
        "rpcId": str(uuid.uuid4()),
        "method": method,
        "payload": {"args": {}} if request is None else {"args": {REQUEST_WIRE: dict(request)}},
    }
    return json.dumps(body).encode("utf-8")


def rpc_call(
    url: str,
    method: str,
    request: Mapping[str, Any] | None,
    *,
    cookie: str,
    timeout: float = RPC_TIMEOUT_SECONDS,
    attempts: int = MAX_ATTEMPTS,
    post: PostJson | None = None,
) -> dict[str, Any]:
    """一次 `/api` unary 调用，交回 result 槽（`ok` / `value` / `error`）。

    `request=None` 表示这个端点没有参数（探针那个 `session/modelCatalog` 就是这一档）。
    连接层抖动最多重试 `attempts` 次（每次之间退避 1s、2s…）；**`ok=false` 与 4xx 一律
    当场交回原因**，不重试 —— 重放同一个错误只是把同一句话再说一遍。
    """
    send = _post_json if post is None else post
    payload = _envelope(method, request)
    last = ""
    for attempt in range(1, max(1, int(attempts)) + 1):
        try:
            status, text = send(url, method, payload, cookie, float(timeout))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = f"连不上 DSH web（{url}）：{type(exc).__name__}: {exc}"
        else:
            if status in (401, 403):
                raise WebHandoffError(
                    f"{method} 鉴权被拒（HTTP {status}）：{_short(text)}"
                    "——凭据文件里那枚签名密钥与 DSH 服务端对不上（或它重启时换了密钥）"
)
            if status == 404:
                raise WebHandoffError(
                    f"{method} 端点不存在（HTTP 404）：{_short(text)}"
                    "——DSH 的方法名或端点写法变了"
)
            if status >= 500:
                last = f"{method} 服务端出错（HTTP {status}）：{_short(text)}"
            else:
                try:
                    envelope = json.loads(text)
                except ValueError as exc:
                    raise WebHandoffError(
                        f"{method} 的应答不是 JSON（HTTP {status}）：{_short(text)}"
) from exc
                if envelope.get("type") != "server-response":
                    raise WebHandoffError(f"{method} 的应答信封异常：{_short(text)}")
                result = envelope.get("result") or {}
                if not result.get("ok"):
                    error = result.get("error") or {}
                    raise WebHandoffError(
                        f"{method} 失败：{error.get('code')}: {error.get('message')}"
)
                return dict(result)
        if attempt < attempts:
            _sleep(RETRY_BASE_DELAY_SECONDS * attempt)
    raise WebHandoffError(last or f"{method} 调用失败")


def _short(text: str, limit: int = 200) -> str:
    """把一段应答截短（状态文件与 log 里只留够用的那一截）。"""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


#: `time.sleep` 的接缝（用例不必真的等退避）
_sleep: Callable[[float], None] = time.sleep


# --- 存活探针 ---------------------------------------------------------------


def probe(
    url: str = DEFAULT_DSH_URL,
    *,
    allow_dsh: bool = False,
    timeout: float = PROBE_RPC_TIMEOUT_SECONDS,
    post: PostJson | None = None,
    connect: Callable[[tuple[str, int], float], Any] | None = None,
    credentials: str | Path | None = None,
) -> tuple[bool, str]:
    """DSH web 服务端在不在跑：先 TCP 探端口，再发一次只读 RPC 确认协议与鉴权。

    `allow_dsh` 是**默认拒绝**的人工放行参数：不放行（缺省）就在**任何动作之前**当场抛
    `DeliveryForbidden` —— TCP 不发、凭据不读、RPC 不发。放行标记由
    `MinutesHook.launch(deliver=True)` 经 `runner.run()` 下传，只有两条人工入口会给真；
    未来任何代码绕过纪要层直接 import 本函数，也到不了 DSH。

    交回 `(在跑吗, 说明)`，**说明是给用户看的实话**：探不通时说清是「端口没人听」还是
    「签不出 cookie」还是「只读探针没通过」—— 不许把「鉴权失败」写成「DSH 没在跑」。
    任何一档不成立都算「没在跑」，`runner` 据此走现成的 headless 分支。
    """
    if not allow_dsh:
        raise DeliveryForbidden(f"probe() 被拒：{ISOLATION_REASON}")
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    opener = socket.create_connection if connect is None else connect
    try:
        with opener((host, port), PROBE_TCP_TIMEOUT_SECONDS):
            pass
    except OSError as exc:
        return False, f"{url} 端口没人应答（{type(exc).__name__}: {exc}）"
    try:
        cookie = signing_cookie(url, credentials=credentials)
    except WebHandoffError as exc:
        return False, f"签不出 browser-session cookie：{exc}"
    try:
        rpc_call(
            url,
            LIVENESS_METHOD,
            None,
            cookie=cookie,
            timeout=timeout,
            attempts=1,
            post=post,
)
    except WebHandoffError as exc:
        return False, f"只读探针 {LIVENESS_METHOD} 没通过：{exc}"
    return True, ""


# --- 交付 -------------------------------------------------------------------


def handoff(
    session_dir: str | Path,
    *,
    prompt_text: str,
    url: str = DEFAULT_DSH_URL,
    timeout: float | None = None,
    allow_dsh: bool = False,
    post: PostJson | None = None,
    credentials: str | Path | None = None,
    write_log: bool = True,
) -> HandoffResult:
    """把这一场交付给 DSH web 服务端：工作区 → 会话 → 提示词。

    `session_dir` 是场次目录（它就是要挂进工作区的那个目录），`prompt_text` 是**已经渲染好的**
    提示词全文（`runner` 从 `render_session_prompt()` 拿到它）—— 它走 `session/prompt` 的正文，
    不经过任何命令行，因此没有 argv 的截断问题。

    `allow_dsh` 是**默认拒绝**的人工放行参数：不放行（缺省）就在**任何动作之前**当场抛
    `DeliveryForbidden` —— 目录不建、凭据不读、三次 RPC 一次都不发。放行标记由
    `MinutesHook.launch(deliver=True)` 经 `runner.run()` 下传（两条人工入口才给真）。

    三步**次序固定**：`session/create` 只带 `workspaceId`（不带 `cwd`），会话的 cwd 就是该工作区
    绑定的那个目录，于是它**落在场次目录对应的工作区分组里**而不是「未分组」。中间任何一步
    失败都如实交回 `failure`（不偷偷改走 headless —— 交付失败要看得见）。

    `write_log=True` 时落两份盘上的记录：`working/run_prompt.md`（发出去的提示词全文）与
    `working/minutes-run.log`（这一趟三步的记录）—— 它们是状态文件 `artifacts` 里声明的那两格，
    不落的话声明就成了空话。
    """
    if not allow_dsh:
        raise DeliveryForbidden(f"handoff() 被拒：{ISOLATION_REASON}")
    directory = Path(session_dir)
    directory.mkdir(parents=True, exist_ok=True)
    budget = _handoff_budget(timeout)
    begun = time.monotonic()
    deadline = begun + budget
    steps: list[str] = []
    record: dict[str, str] = {}
    workspace_id = ""
    workspace_title = ""
    session_id = ""

    def elapsed() -> float:
        return time.monotonic() - begun

    def remaining() -> float:
        return deadline - time.monotonic()

    def fail(failure: str, error: str) -> HandoffResult:
        if write_log:
            _write_log(
                directory,
                url=url,
                record=record,
                failure=failure,
                error=error,
                seconds=elapsed(),
                workspace=f"{workspace_title} {workspace_id}".strip(),
                session_id=session_id,
)
        return HandoffResult(
            ok=False,
            url=url,
            seconds=elapsed(),
            workspace_id=workspace_id,
            workspace_title=workspace_title,
            session_id=session_id,
            failure=failure,
            error=error,
            steps=tuple(steps),
)

    try:
        cookie = signing_cookie(url, credentials=credentials)
    except WebHandoffError as exc:
        return fail(FAILURE_CREDENTIALS, f"签不出 browser-session cookie：{exc}")

    if write_log:
        write_text_atomic(
            directory / WORKING_DIR / RUN_PROMPT_NAME, prompt_text
)

    # 一、工作区（幂等：同一目录返回已有工作区）
    if remaining() <= 0.0:
        return fail(FAILURE_TIMEOUT, f"交付预算 {budget:.0f} 秒用完了（还没建工作区）")
    try:
        result = rpc_call(
            url,
            WORKSPACE_CREATE,
            {"path": str(directory)},
            cookie=cookie,
            timeout=min(RPC_TIMEOUT_SECONDS, remaining()),
            post=post,
)
    except WebHandoffError as exc:
        return fail(FAILURE_WORKSPACE, f"{WORKSPACE_CREATE} 失败：{exc}")
    steps.append(WORKSPACE_CREATE)
    workspace = (result.get("value") or {}).get("workspace") or {}
    workspace_id = str(workspace.get("workspaceId") or "")
    workspace_title = str(workspace.get("title") or "")
    record["workspace"] = json.dumps(workspace, ensure_ascii=False)
    if not workspace_id:
        return fail(FAILURE_WORKSPACE, f"{WORKSPACE_CREATE} 没交回 workspaceId：{result}")

    # 二、会话（只带 workspaceId → 生在在场次目录对应的那个工作区里）
    if remaining() <= 0.0:
        return fail(FAILURE_TIMEOUT, f"交付预算 {budget:.0f} 秒用完了（还没建会话）")
    try:
        result = rpc_call(
            url,
            SESSION_CREATE,
            {"workspaceId": workspace_id},
            cookie=cookie,
            timeout=min(RPC_TIMEOUT_SECONDS, remaining()),
            post=post,
)
    except WebHandoffError as exc:
        return fail(FAILURE_SESSION, f"{SESSION_CREATE} 失败：{exc}")
    steps.append(SESSION_CREATE)
    session_id = str((result.get("value") or {}).get("sessionId") or "")
    record["session"] = json.dumps(result.get("value") or {}, ensure_ascii=False)
    if not session_id:
        return fail(FAILURE_SESSION, f"{SESSION_CREATE} 没交回 sessionId：{result}")

    # 三、提示词全文（queue 模式：会话在 DSH GUI 里开始跑）
    if remaining() <= 0.0:
        return fail(FAILURE_TIMEOUT, f"交付预算 {budget:.0f} 秒用完了（提示词还没发出去）")
    request = {
        "requestId": str(uuid.uuid4()),
        "sessionId": session_id,
        "mode": "queue",
        "content": [{"type": "text", "text": prompt_text}],
    }
    try:
        rpc_call(
            url,
            SESSION_PROMPT,
            request,
            cookie=cookie,
            timeout=min(RPC_TIMEOUT_SECONDS, remaining()),
            post=post,
)
    except WebHandoffError as exc:
        return fail(FAILURE_PROMPT, f"{SESSION_PROMPT} 失败：{exc}")
    steps.append(SESSION_PROMPT)
    record["prompt"] = f"content: {len(prompt_text)} 字；mode: queue"

    if write_log:
        _write_log(
            directory,
            url=url,
            record=record,
            failure=None,
            error=None,
            seconds=elapsed(),
            workspace=f"{workspace_title} {workspace_id}".strip(),
            session_id=session_id,
)
    _log.info(
        "会后纪要已交付 DSH web：工作区 %s（%s）、会话 %s、耗时 %.3fs",
        workspace_title or "?",
        workspace_id,
        session_id,
        elapsed(),
)
    return HandoffResult(
        ok=True,
        url=url,
        seconds=elapsed(),
        workspace_id=workspace_id,
        workspace_title=workspace_title,
        session_id=session_id,
        steps=tuple(steps),
)


def _handoff_budget(timeout: float | None) -> float:
    """交付这一步的墙钟预算：`runner` 递下来的那一格被夹在上下界之间。

    `minutes.timeout_seconds` 是给**生成**用的预算（默认四小时、上界一天），拿它当一次
    HTTP 交付的预算等于没上限；因此这里夹到 `[MIN_HANDOFF_SECONDS, MAX_HANDOFF_SECONDS]`。
    """
    try:
        value = float(DEFAULT_HANDOFF_SECONDS if timeout is None else timeout)
    except (TypeError, ValueError):
        return DEFAULT_HANDOFF_SECONDS
    return max(MIN_HANDOFF_SECONDS, min(value, MAX_HANDOFF_SECONDS))


def _write_log(
    directory: Path,
    *,
    url: str,
    record: Mapping[str, str],
    failure: str | None,
    error: str | None,
    seconds: float,
    workspace: str,
    session_id: str,
) -> None:
    """落 `working/minutes-run.log`（头几行说清这一趟是怎么交付的、成没成）。

    写盘骨架走 `status.write_run_log()`（两路共用的唯一实现），这里只出 web 分支的
    头行与分节。没有子进程的 stdout / stderr 可落，分节落的是三步 RPC 的请求与应答
    摘要，没走到的节写明占位。
    """
    header = [
        f"# 交付：{WORKSPACE_CREATE} → {SESSION_CREATE} → {SESSION_PROMPT}",
        f"# 场次目录：{directory}",
        f"# DSH web：{url}",
        f"# 结果：{'交付成功' if failure is None else f'交付失败（{failure}）'}",
    ]
    if workspace:
        header.append(f"# 工作区：{workspace}")
    if session_id:
        header.append(f"# 会话：{session_id}")
    header.append(f"# 耗时：{float(seconds):.3f}s")
    if error:
        header.append(f"# 失败：{error}")
    step_keys = (
        (WORKSPACE_CREATE, "workspace"),
        (SESSION_CREATE, "session"),
        (SESSION_PROMPT, "prompt"),
)
    write_run_log(
        directory / WORKING_DIR / MINUTES_RUN_LOG_NAME,
        header=header,
        sections=tuple(
            (step, record.get(key, "（没走到这一步）")) for step, key in step_keys
),
)


__all__ = [
    "CREDENTIALS_PATH",
    "DEFAULT_DSH_URL",
    "DEFAULT_HANDOFF_SECONDS",
    "DeliveryForbidden",
    "FAILURES",
    "FAILURE_PROMPT",
    "FAILURE_SESSION",
    "FAILURE_TIMEOUT",
    "FAILURE_CREDENTIALS",
    "FAILURE_WORKSPACE",
    "HandoffResult",
    "ISOLATION_REASON",
    "LIVENESS_METHOD",
    "MAX_HANDOFF_SECONDS",
    "MIN_HANDOFF_SECONDS",
    "REQUEST_WIRE",
    "SESSION_CREATE",
    "SESSION_PROMPT",
    "WORKSPACE_CREATE",
    "WebHandoffError",
    "handoff",
    "load_signing_secret",
    "probe",
    "rpc_call",
    "signing_cookie",
]
