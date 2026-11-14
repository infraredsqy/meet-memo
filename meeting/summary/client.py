"""OpenAI 兼容端点的调用（标准库实现，传输层可注入）。

线上语义：

  - `POST {base_url}/chat/completions`，`Authorization: Bearer {api_key}`、
    `Content-Type: application/json`；
  - payload `{"model", "messages", "temperature": 0.3, "stream": False}`；
    `extra_body` 非空则**原样并进去**（厂商特有字段，字段名以厂商文档为准）；
  - 读 `choices[0].message.content`；`reasoning_content`（thinking）**丢掉**；
  - 非 2xx **优先取**响应体里 `error.message` 的原文。

两处裁定：

  - **传输层走标准库 `urllib.request`**：运行时的 import 链不碰 `httpx` / `requests`。
    `httpx` 只服务测试层的真服务子进程。
  - **单次超时不写死**：`complete()` 收一个 `timeout_seconds`，由 `run_summary()` 按
    「剩余预算」现算。

`post` 是**注入点**（默认走 `urllib.request`）：测试与探针都从这个口子进，
因此这一层的用例一条网络请求都不发。注入的可调用对象两种写法都收（见 `_post_request()`）：

  - `post(url, json=payload, headers=headers, timeout=timeout)` —— 与 `requests.post` 同形，
    返回值只要有 `.status_code` / `.json()` / `.text` 三个属性;
  - `post(request)` —— 收一个 `HttpRequest`（`url` / `payload` / `headers` / `timeout`），
    最省事的假件用这一种。
"""

from __future__ import annotations

import inspect
import json
import socket
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from meeting.summary.settings import DEADLINE_KEY, _default

CHAT_COMPLETIONS_PATH = "/chat/completions"

TEMPERATURE = 0.3

#: 一次请求的兜底超时（秒）：只作 `HttpRequest.timeout` 这个 dataclass 字段的默认值
#: ——生产路径上 `run_summary()` 按「剩余预算」现算并显式传超时。数值取登记正本
#: `summary.deadline_seconds` 那一格（`config.default_for`，经 `settings._default()`），
#: 不再另存一个同值字面量。
DEFAULT_TIMEOUT_SECONDS = float(_default(DEADLINE_KEY))

#: `SummaryError.reason` 的取值域
ERROR_REASONS: tuple[str, ...] = ("timeout", "bad_status", "bad_json", "no_content")


class SummaryError(RuntimeError):
    """一次端点调用没成功。

    `reason` 是机器可读的四档之一（`timeout` / `bad_status` / `bad_json` / `no_content`），
    `detail` 是**给人看的原文**（异常消息、HTTP 状态与响应体里的 `error.message`）
    ——用户要看原文，不做中文包装。
    """

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = str(reason)
        self.detail = str(detail)
        super().__init__(f"{self.reason}: {self.detail}")

    def as_dict(self) -> dict[str, str]:
        return {"reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class HttpRequest:
    """准备发出去的一次请求（**注入点收到的就是这一个**）。"""

    url: str
    payload: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class HttpResponse:
    """默认传输层交回的一份响应（`post` 注入点的假件照这个形状造）。"""

    status_code: int
    body: str

    @property
    def ok(self) -> bool:
        return 200 <= int(self.status_code) < 300

    @property
    def text(self) -> str:
        """响应体原文（非 2xx 那几档取 `error.message` 失败时的退路）。"""
        return self.body

    def json(self) -> Any:
        """解析响应体（不是 JSON 就抛 `ValueError`，调用方按 `bad_json` 收）。"""
        return json.loads(self.body)


def endpoint_url(base_url: str) -> str:
    """`base_url` → chat completions 的完整地址（末尾多余的斜杠收掉）。"""
    return f"{str(base_url).rstrip('/')}{CHAT_COMPLETIONS_PATH}"

def build_payload(
    *,
    model: str,
    messages: Sequence[Mapping[str, str]],
    extra_body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """组请求体：标准四格 + 并进来的 `extra_body`。"""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [dict(message) for message in messages],
        "temperature": TEMPERATURE,
        "stream": False,
    }
    if extra_body:
        payload.update(dict(extra_body))
    return payload


def _http_post(request: HttpRequest) -> HttpResponse:
    """默认传输层：标准库 `urllib.request`。

    `HTTPError`（非 2xx）照常把状态码与响应体读回来交出去（错误码那几档由 `complete()`
    按同一套口径处理，与「连接失败」分开）；连接层的问题原样抛，由调用方按 `timeout`
    或 `bad_status` 收。
    """
    body = json.dumps(request.payload, ensure_ascii=False).encode("utf-8")
    prepared = urllib.request.Request(
        request.url, data=body, headers=dict(request.headers), method="POST"
    )
    try:
        with urllib.request.urlopen(prepared, timeout=float(request.timeout)) as response:
            return HttpResponse(
                status_code=int(getattr(response, "status", 0) or 0),
                body=response.read().decode("utf-8", errors="replace"),
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
        return HttpResponse(status_code=int(exc.code), body=raw)


def _accepts_keywords(callable_object: Callable[..., Any]) -> bool:
    try:
        signature = inspect.signature(callable_object)
    except (TypeError, ValueError):  # pragma: no cover - 内建 / C 实现的可调用对象
        return False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return {"json", "headers", "timeout"} <= set(signature.parameters)


def _post_request(post: Callable[..., Any], request: HttpRequest) -> Any:
    """按注入对象认得的写法把它调起来（两种写法见模块头）。"""
    if _accepts_keywords(post):
        return post(
            request.url,
            json=dict(request.payload),
            headers=dict(request.headers),
            timeout=float(request.timeout),
        )
    return post(request)


def _error_message_of(response: Any) -> str:
    """非 2xx 时那句话：优先响应体里 `error.message` 的原文。

    读不出 JSON、或者里面没有 `error` 那一格时，退成**响应体原文** ——
    一段 HTML 错误页也比一句「端点报错」有用。
    """
    try:
        error = response.json().get("error")
    except Exception:
        return str(getattr(response, "text", "") or "")
    if isinstance(error, dict):
        return str(error.get("message", error))
    return str(error)


def _status_code_of(response: Any) -> int:
    return int(getattr(response, "status_code", 0) or 0)


class ModelClient:
    """一次会中总结要用的端点客户端（构造一次，可以在多轮总结里重复用）。

    四个字段是**构造那一刻的快照**：中途改配置不影响已经在飞的那一次调用。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        extra_body: dict[str, Any] | None = None,
        post: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = str(base_url or "")
        self.api_key = str(api_key or "")
        self.model = str(model or "")
        self.extra_body = dict(extra_body) if extra_body else None
        self._post = _http_post if post is None else post

    @property
    def url(self) -> str:
        """这次调用会打到的地址（供 log 与 `/debug`；密钥不在里面）。"""
        return endpoint_url(self.base_url)

    @classmethod
    def from_settings(cls, settings: Any, *, post: Callable[..., Any] | None = None) -> "ModelClient":
        """从一份 `SummarySettings` 造一个客户端（动作层最常用的一条路）。"""
        return cls(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
            extra_body=settings.extra_body,
            post=post,
        )

    def complete(
        self, messages: Sequence[dict[str, str]], *, timeout_seconds: float
    ) -> str:
        """发一次 chat 调用，交回 assistant 的 `content`；没成功就抛 `SummaryError`。

        `timeout_seconds` 由调用方给（`run_summary()` 给的是「剩余预算」）。
        """
        request = HttpRequest(
            url=self.url,
            payload=build_payload(
                model=self.model, messages=messages, extra_body=self.extra_body
            ),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=float(timeout_seconds),
        )
        try:
            response = _post_request(self._post, request)
        except (TimeoutError, socket.timeout) as exc:
            raise SummaryError("timeout", f"请求超时（{timeout_seconds:g} 秒）：{exc}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise SummaryError(
                    "timeout", f"请求超时（{timeout_seconds:g} 秒）：{exc.reason}"
                ) from exc
            raise SummaryError("bad_status", f"连接端点失败：{exc}") from exc
        except OSError as exc:
            raise SummaryError("bad_status", f"连接端点失败：{type(exc).__name__}: {exc}") from exc

        status_code = _status_code_of(response)
        if not (200 <= status_code < 300):
            raise SummaryError(
                "bad_status", f"HTTP {status_code}：{_error_message_of(response)}"
            )

        try:
            document = response.json()
        except Exception as exc:
            raise SummaryError("bad_json", f"响应体不是 JSON：{exc}") from exc

        try:
            message = document["choices"][0]["message"]
            # thinking（`reasoning_content`）在这里丢掉：要的是最终答案那一段
            content = message.get("content") or ""
        except Exception as exc:
            raise SummaryError("bad_json", f"响应体里没有 choices[0].message：{exc}") from exc

        if not str(content).strip():
            raise SummaryError("no_content", "端点的 choices[0].message.content 是空的")
        return str(content)


__all__ = [
    "CHAT_COMPLETIONS_PATH",
    "DEFAULT_TIMEOUT_SECONDS",
    "ERROR_REASONS",
    "TEMPERATURE",
    "HttpRequest",
    "HttpResponse",
    "ModelClient",
    "SummaryError",
    "build_payload",
    "endpoint_url",
]
