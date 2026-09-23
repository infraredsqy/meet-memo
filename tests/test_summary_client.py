"""`meeting/summary/client.py` 的契约用例：OpenAI 兼容端点的请求形状与四档错误。

`post` 是这一层的注入点，因此**一条网络请求都不发**：下面那个 `RecordingPost` 三种写法都收
（`requests` 形状的关键字、单个 `HttpRequest`、以及会把状态码收进记账里），
记账内容就是断言对象。

盯的是五件事：

  - **请求形状照 V1**：地址 `{base_url}/chat/completions`、两个头、payload 四格、
    `extra_body` 原样并进去（并进去的键覆盖同名标准格）；
  - **只读 `content`**：`reasoning_content`（thinking）丢掉；
  - **四档错误**：`timeout` / `bad_status` / `bad_json` / `no_content` 各一条，
    非 2xx 优先取响应体里 `error.message` 的原文；
  - **超时是每次现算的**：`complete(timeout_seconds=...)` 把那个数交给传输层；
  - **标准库实现**：默认传输层走 `urllib.request`（模块里不 import `httpx` / `requests`）。
"""

from __future__ import annotations

import json
import socket
import sys
import urllib.error

from meeting.summary.client import (
    CHAT_COMPLETIONS_PATH,
    ERROR_REASONS,
    HttpRequest,
    HttpResponse,
    ModelClient,
    SummaryError,
    build_payload,
    endpoint_url,
)


#: 一个固定端点（用例里的假地址，不指向任何真实服务）
BASE_URL = "https://example.invalid/v1"


def _reply(content: str, *, model: str = "fake-model") -> HttpResponse:
    """造一份成功的响应体（`content` 就是模型那段话）。"""
    return HttpResponse(
        status_code=200,
        body=json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}),
    )


class RecordingPost:
    """假传输层：收下每一次请求，按剧本交回一份响应（或抛一个异常）。

    两种调用写法都收：关键字那一套（`url, json=, headers=, timeout=`）与单个请求对象那一套。
    """

    def __init__(self, *, reply: HttpResponse | None = None, raises: BaseException | None = None) -> None:
        self.calls: list[HttpRequest] = []
        self.mode = "keywords"
        self._reply = reply if reply is not None else _reply("空的")
        self._raises = raises

    def __call__(self, *args: object, **kwargs: object) -> HttpResponse:
        if args and isinstance(args[0], HttpRequest):
            self.mode = "request-object"
            request = args[0]
        else:
            request = HttpRequest(
                url=str(args[0]),
                payload=dict(kwargs.get("json") or {}),
                headers=dict(kwargs.get("headers") or {}),
                timeout=float(kwargs.get("timeout") or 0.0),
            )
        self.calls.append(request)
        if self._raises is not None:
            raise self._raises
        return self._reply


def _client(post: object, **overrides: object) -> ModelClient:
    """造一个客户端（四个字段给齐，用例只覆盖关心的那一个）。"""
    fields: dict[str, object] = {
        "base_url": BASE_URL,
        "api_key": "test-key-not-a-real-one",
        "model": "fake-model",
    }
    fields.update(overrides)
    return ModelClient(post=post, **fields)  # type: ignore[arg-type]


def test_endpoint_url_appends_the_path_and_trims_trailing_slashes() -> None:
    """地址 = `base_url` 去掉末尾斜杠 + `/chat/completions`（照 V1）。"""
    assert endpoint_url("https://example.invalid/v1") == f"https://example.invalid/v1{CHAT_COMPLETIONS_PATH}"
    assert endpoint_url("https://example.invalid/v1/") == f"https://example.invalid/v1{CHAT_COMPLETIONS_PATH}"
    assert endpoint_url("https://example.invalid/v1///") == f"https://example.invalid/v1{CHAT_COMPLETIONS_PATH}"


def test_build_payload_carries_the_four_standard_fields() -> None:
    """payload 四格照 V1：`model` / `messages` / `temperature` 0.3 / `stream` False。"""
    payload = build_payload(model="m", messages=[{"role": "user", "content": "你好"}])

    assert payload == {
        "model": "m",
        "messages": [{"role": "user", "content": "你好"}],
        "temperature": 0.3,
        "stream": False,
    }


def test_build_payload_merges_extra_body_over_the_standard_fields() -> None:
    """`extra_body` 原样并进请求体（照 V1 的 `payload.update(extra_body)`）。"""
    payload = build_payload(
        model="m",
        messages=[],
        extra_body={"thinking": {"type": "disabled"}, "temperature": 0.9},
    )

    assert payload["thinking"] == {"type": "disabled"}
    # 同名键被覆盖：并入的那一份说话
    assert payload["temperature"] == 0.9


def test_complete_sends_the_bearer_header_and_the_payload() -> None:
    """一次成功调用：地址、两个头、payload、超时四样都对得上。"""
    post = RecordingPost(reply=_reply('{"bullets": []}'))

    content = _client(post, extra_body={"thinking": {"type": "disabled"}}).complete(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        timeout_seconds=7.5,
    )

    assert content == '{"bullets": []}'
    (request,) = post.calls
    assert request.url == f"{BASE_URL}{CHAT_COMPLETIONS_PATH}"
    assert request.headers["Authorization"] == "Bearer test-key-not-a-real-one"
    assert request.headers["Content-Type"] == "application/json"
    assert request.timeout == 7.5
    assert request.payload["model"] == "fake-model"
    assert request.payload["stream"] is False
    assert request.payload["temperature"] == 0.3
    assert request.payload["thinking"] == {"type": "disabled"}
    assert request.payload["messages"][-1]["content"] == "u"


def test_complete_accepts_the_request_object_style_injection() -> None:
    """注入点收「一个请求对象」那种写法：那种假件把四样摊在一个 `HttpRequest` 上。

    认写法这一步由 `inspect.signature` 做：只收单个位置参数的假件走这一条路。
    """
    seen: list[HttpRequest] = []

    def post(request: HttpRequest) -> HttpResponse:
        seen.append(request)
        return _reply("好的")

    content = _client(post).complete([{"role": "user", "content": "u"}], timeout_seconds=3.0)

    assert content == "好的"
    (request,) = seen
    assert request.url == f"{BASE_URL}{CHAT_COMPLETIONS_PATH}"
    assert request.timeout == 3.0
    assert request.payload["temperature"] == 0.3
    assert request.headers["Authorization"].startswith("Bearer ")


def test_complete_accepts_the_requests_style_injection() -> None:
    """注入点也收 `requests.post` 那种关键字写法（探针的假件走这一条）。"""
    post = RecordingPost(reply=_reply("好的"))

    content = _client(post).complete([{"role": "user", "content": "u"}], timeout_seconds=3.0)

    assert content == "好的"
    assert post.mode == "keywords"
    assert post.calls[0].payload["temperature"] == 0.3


def test_complete_drops_the_reasoning_content() -> None:
    """`reasoning_content`（thinking）丢掉，交回的是 `content` 那一段。"""
    post = RecordingPost(
        reply=HttpResponse(
            status_code=200,
            body=json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": '{"bullets": []}',
                                "reasoning_content": "我先想一想……",
                            }
                        }
                    ]
                }
            ),
        )
    )

    content = _client(post).complete([{"role": "user", "content": "u"}], timeout_seconds=3.0)

    assert content == '{"bullets": []}'
    assert "我先想一想" not in content


def test_complete_reports_a_timeout() -> None:
    """超时那一档：`reason == "timeout"`，detail 里带上这次给了多少秒。"""
    post = RecordingPost(raises=TimeoutError("timed out"))

    try:
        _client(post).complete([{"role": "user", "content": "u"}], timeout_seconds=2.0)
    except SummaryError as exc:
        assert exc.reason == "timeout"
        assert "2" in exc.detail
    else:  # pragma: no cover - 走不到这里
        raise AssertionError("超时该抛 SummaryError")


def test_complete_reports_a_socket_timeout_too() -> None:
    """`socket.timeout` 与 `TimeoutError` 是同一个类，归同一档。"""
    assert socket.timeout is TimeoutError
    post = RecordingPost(raises=socket.timeout("timed out"))

    try:
        _client(post).complete([{"role": "user", "content": "u"}], timeout_seconds=1.0)
    except SummaryError as exc:
        assert exc.reason == "timeout"
    else:  # pragma: no cover - 走不到这里
        raise AssertionError("socket 超时该抛 SummaryError")


def test_complete_reports_a_wrapped_url_timeout_as_a_timeout() -> None:
    """`URLError` 里裹着超时的，也归 `timeout` 那一档。"""
    post = RecordingPost(raises=urllib.error.URLError(TimeoutError("timed out")))

    try:
        _client(post).complete([{"role": "user", "content": "u"}], timeout_seconds=1.0)
    except SummaryError as exc:
        assert exc.reason == "timeout"
    else:  # pragma: no cover - 走不到这里
        raise AssertionError("裹着超时的 URLError 该抛 SummaryError")


def test_complete_reports_a_connection_failure_as_bad_status() -> None:
    """连不上（DNS / 连接被拒）那一档归 `bad_status`，detail 是异常原文。"""
    post = RecordingPost(raises=urllib.error.URLError("getaddrinfo failed"))

    try:
        _client(post).complete([{"role": "user", "content": "u"}], timeout_seconds=1.0)
    except SummaryError as exc:
        assert exc.reason == "bad_status"
        assert "getaddrinfo failed" in exc.detail
    else:  # pragma: no cover - 走不到这里
        raise AssertionError("连不上该抛 SummaryError")


def test_complete_prefers_the_error_message_from_a_non_2xx_body() -> None:
    """非 2xx：优先取响应体里 `error.message` 的原文（照 V1，不做中文包装）。"""
    post = RecordingPost(
        reply=HttpResponse(
            status_code=401,
            body=json.dumps({"error": {"message": "Authentication Fails", "type": "auth"}}),
        )
    )

    try:
        _client(post).complete([{"role": "user", "content": "u"}], timeout_seconds=1.0)
    except SummaryError as exc:
        assert exc.reason == "bad_status"
        assert exc.detail == "HTTP 401：Authentication Fails"
    else:  # pragma: no cover - 走不到这里
        raise AssertionError("非 2xx 该抛 SummaryError")


def test_complete_falls_back_to_the_raw_body_for_a_non_2xx_without_error_object() -> None:
    """响应体里没有 `error` 对象时，detail 退成响应体原文。"""
    post = RecordingPost(reply=HttpResponse(status_code=500, body="gateway blew up"))

    try:
        _client(post).complete([{"role": "user", "content": "u"}], timeout_seconds=1.0)
    except SummaryError as exc:
        assert exc.reason == "bad_status"
        assert "gateway blew up" in exc.detail
    else:  # pragma: no cover - 走不到这里
        raise AssertionError("非 2xx 该抛 SummaryError")


def test_complete_reports_a_non_json_body() -> None:
    """2xx 但响应体读不出 JSON：`bad_json`。"""
    post = RecordingPost(reply=HttpResponse(status_code=200, body="<html>502</html>"))

    try:
        _client(post).complete([{"role": "user", "content": "u"}], timeout_seconds=1.0)
    except SummaryError as exc:
        assert exc.reason == "bad_json"
    else:  # pragma: no cover - 走不到这里
        raise AssertionError("读不出 JSON 该抛 SummaryError")


def test_complete_reports_a_missing_choices_path() -> None:
    """2xx、是 JSON、但没有 `choices[0].message`：同样归 `bad_json`。"""
    post = RecordingPost(reply=HttpResponse(status_code=200, body=json.dumps({"choices": []})))

    try:
        _client(post).complete([{"role": "user", "content": "u"}], timeout_seconds=1.0)
    except SummaryError as exc:
        assert exc.reason == "bad_json"
        assert "choices[0].message" in exc.detail
    else:  # pragma: no cover - 走不到这里
        raise AssertionError("缺 choices 该抛 SummaryError")


def test_complete_reports_an_empty_content() -> None:
    """`content` 是空串或只有空白：`no_content`（那一段没有可解析的东西）。"""
    post = RecordingPost(reply=_reply("   "))

    try:
        _client(post).complete([{"role": "user", "content": "u"}], timeout_seconds=1.0)
    except SummaryError as exc:
        assert exc.reason == "no_content"
    else:  # pragma: no cover - 走不到这里
        raise AssertionError("空 content 该抛 SummaryError")


def test_summary_error_exposes_a_machine_readable_reason_and_a_human_detail() -> None:
    """`SummaryError` 的两格：`reason` 在四档取值域里，`detail` 是原文，`as_dict()` 可 JSON 化。"""
    error = SummaryError("bad_status", "HTTP 500：boom")

    assert error.reason in ERROR_REASONS
    assert error.detail == "HTTP 500：boom"
    assert error.as_dict() == {"reason": "bad_status", "detail": "HTTP 500：boom"}
    assert "bad_status" in str(error)


def test_from_settings_carries_the_four_fields() -> None:
    """`from_settings()` 把端点四件套照搬过来（接口契约里的那一条捷径）。"""
    from meeting.summary.settings import SummarySettings

    post = RecordingPost(reply=_reply("ok"))
    client = ModelClient.from_settings(
        SummarySettings(
            base_url="https://example.invalid/v1",
            api_key="k",
            model="m",
            extra_body={"thinking": None},
        ),
        post=post,
    )

    assert client.url == f"{BASE_URL}{CHAT_COMPLETIONS_PATH}"
    assert client.extra_body == {"thinking": None}


def test_default_transport_uses_the_standard_library() -> None:
    """默认传输层是标准库 `urllib.request`：本模块**不 import** `httpx` / `requests`。"""
    import meeting.summary.client as client_module

    assert client_module.urllib.request.__name__ == "urllib.request"
    assert "httpx" not in sys.modules or "httpx" not in vars(client_module)
    assert not hasattr(client_module, "requests")
