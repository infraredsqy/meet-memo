"""`meeting/minutes/dsh_web.py`（web 分支的交付层）的契约用例。

一条用例都不连真实的 3080：假端点起在回环临时端口上（`tests/_dsh_web_fake.py`），
真往 3080 打那条路由 `tests/conftest.py` 的第七道兜底拦下。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest

from meeting.minutes import dsh_web
from tests._dsh_web_fake import (
    FakeDshWeb,
    request_of,
)

#: 本文件专门测交付协议与探针 / 交付这两层（假端点起在回环临时端口上，不碰真实 3080）：
#: 显式请第八道守卫（`tests/conftest.py::_no_real_dsh_delivery`）的豁免夹具 —— 探针 / 交付
#: 这两个原语在本文件里不打桩、收场也不判红。产品侧那道 `DeliveryForbidden` 默认拒绝闸
#: 照旧管用：下面的调用一律显式传 `allow_dsh=True`。真端口那一侧由第七道守卫兜着。
pytestmark = [pytest.mark.usefixtures("allow_dsh_protocol_stubs")]

SECRET = bytes(range(32))
SECRET_B64 = base64.urlsafe_b64encode(SECRET).decode("ascii").rstrip("=")

CREDENTIALS_TEXT = (
    "other-record:\n"
    "  secret: nope\n"
    "client-connection/browser-session:\n"
    f"  secret: {SECRET_B64}\n"
)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _credentials(tmp_path: Path, text: str = CREDENTIALS_TEXT) -> Path:
    """写一份临时凭据文件并把它接到模块上（用例不碰机主真实家目录）。"""
    path = tmp_path / "credentials" / ".credentials.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 `dsh_web.CREDENTIALS_PATH` 指到本用例的临时凭据文件上，交回那个落点。"""
    path = _credentials(tmp_path)
    monkeypatch.setattr(dsh_web, "CREDENTIALS_PATH", path)
    return path


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """退避那一格换成「让出一次就接着走」：用例不必真的等 1 秒。"""
    monkeypatch.setattr(dsh_web, "_sleep", lambda seconds: None)


def test_cookie_name_follows_the_authority_and_the_signature_verifies(
    credentials: Path,
) -> None:
    """cookie 名按 authority 算，值是 `v1.<body>.<hmac>`，签名能验回来、有效期是 30 天。"""
    cookie = dsh_web.signing_cookie("http://127.0.0.1:3080")

    name, value = cookie.split("=", 1)
    expected_name = "dsh-auth-" + _b64url(hashlib.sha256(b"127.0.0.1:3080").digest())
    assert name == expected_name

    version, body, signature = value.split(".")
    assert version == "v1"
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert payload["authority"] == "127.0.0.1:3080"
    assert payload["expiresAt"] - payload["issuedAt"] == 30 * 24 * 3600 * 1000

    expected = _b64url(hmac.new(SECRET, body.encode("ascii"), hashlib.sha256).digest())
    assert signature == expected


def test_another_port_gets_another_cookie_name(credentials: Path) -> None:
    """换一个 authority 就是**另一枚** cookie，名字按各自的 authority 现算。

    这一条钉的是「签给谁的」那件事：cookie 名里嵌着 `sha256(authority)`，因此同一个密钥
    在 3080 上签出来的那一枚，到了 3099 上前缀就对不上（DSH 按名字找它那一份）。
    """
    first = dsh_web.signing_cookie("http://127.0.0.1:3080")
    second = dsh_web.signing_cookie("http://127.0.0.1:3099")

    for url, cookie in (("127.0.0.1:3080", first), ("127.0.0.1:3099", second)):
        name = cookie.split("=", 1)[0]
        assert name == "dsh-auth-" + _b64url(hashlib.sha256(url.encode()).digest())
    assert first.split("=")[0] != second.split("=")[0]


def test_a_credentials_file_without_that_record_is_reported(tmp_path: Path) -> None:
    """凭据文件里没有那条记录时当场报错（名字里点名找的是哪一条）。"""
    path = _credentials(tmp_path, "something-else:\n  secret: nope\n")

    with pytest.raises(dsh_web.WebHandoffError, match="browser-session"):
        dsh_web.load_signing_secret(path)


def test_a_missing_credentials_file_is_reported(tmp_path: Path) -> None:
    """凭据文件压根不在时也一样如实报错。"""
    with pytest.raises(dsh_web.WebHandoffError, match="凭据文件"):
        dsh_web.load_signing_secret(tmp_path / "nowhere" / ".credentials.yaml")


def test_a_short_secret_is_reported(tmp_path: Path) -> None:
    """密钥不是 32 字节时当场报错（DSH 那边是定长密钥）。"""
    short = _b64url(b"0123456789abcdef")
    path = _credentials(
        tmp_path, f"client-connection/browser-session:\n  secret: {short}\n"
    )

    with pytest.raises(dsh_web.WebHandoffError, match="32"):
        dsh_web.load_signing_secret(path)


def test_probe_passes_on_a_live_endpoint(credentials: Path) -> None:
    """假端点在跑时探针通过，而且它探的是**只读**的 `session/modelCatalog`。"""
    with FakeDshWeb() as fake:
        alive, detail = dsh_web.probe(fake.url, allow_dsh=True)

    assert alive is True
    assert detail == ""
    assert fake.methods() == ["session/modelCatalog"]
    args = fake.request("session/modelCatalog")["body"]["payload"]["args"]
    assert args == {}, "探针那个端点没有参数，args 是空对象"


def test_probe_calls_a_rejected_auth_by_its_name(credentials: Path) -> None:
    """401 时说明里点名 401（不许写成「DSH 没在跑」）。"""
    with FakeDshWeb({"session/modelCatalog": (401, "unauthorized")}) as fake:
        alive, detail = dsh_web.probe(fake.url, allow_dsh=True)

    assert alive is False
    assert "401" in detail, detail


@pytest.mark.slow
def test_probe_reports_a_dead_port_with_the_address(credentials: Path) -> None:
    """端口没人听时说明里带上那个地址。"""
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    alive, detail = dsh_web.probe(f"http://127.0.0.1:{dead_port}", allow_dsh=True)

    assert alive is False
    assert str(dead_port) in detail, detail


def test_probe_gives_up_after_one_attempt(credentials: Path) -> None:
    """探针只发一次：探不动就落回 headless，不许让一个 500 把拉起这一步拖住十几秒。

    `rpc_call()` 对 5xx 会重试（那是给真正的交付调用准备的），探针因此显式只要一次尝试 ——
    这一条把那个 `attempts=1` 钉住：假端点收第二次请求就说明探针变慢了。
    """
    with FakeDshWeb({"session/modelCatalog": (500, "boom")}) as fake:
        alive, detail = dsh_web.probe(fake.url, allow_dsh=True)

    assert alive is False
    assert "只读探针" in detail, detail
    assert len(fake.requests) == 1, f"探针重试了：{fake.methods()}"


def test_probe_reports_unreadable_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """签不出 cookie 时交回「没在跑」，说明里点名 cookie 那一档。"""
    monkeypatch.setattr(dsh_web, "CREDENTIALS_PATH", tmp_path / "nowhere" / ".credentials.yaml")

    with FakeDshWeb() as fake:
        alive, detail = dsh_web.probe(fake.url, allow_dsh=True)

    assert alive is False
    assert "cookie" in detail, detail
    assert fake.methods() == [], "签不出 cookie 就不该往端点上发请求"


def test_handoff_walks_the_three_steps_in_order(tmp_path: Path, credentials: Path) -> None:
    """工作区 → 会话 → 提示词，次序固定；会话只带 workspaceId，提示词带 requestId 与 queue。"""
    session = tmp_path / "260927"
    session.mkdir()

    with FakeDshWeb() as fake:
        result = dsh_web.handoff(session, prompt_text="正文在这里。", url=fake.url, allow_dsh=True)

    assert result.ok is True, result.error
    assert result.failure is None
    assert result.workspace_id == "ws-260927"
    assert result.session_id == "session-abc"
    assert result.steps == ("workspace/create", "session/create", "session/prompt")
    assert fake.methods() == ["workspace/create", "session/create", "session/prompt"]

    assert request_of(fake.request("workspace/create")) == {"path": str(session)}
    created = request_of(fake.request("session/create"))
    assert created == {"workspaceId": "ws-260927"}, "带上 cwd 会被网关拒、而且会话不进工作区"
    prompted = request_of(fake.request("session/prompt"))
    assert prompted["sessionId"] == "session-abc"
    assert prompted["mode"] == "queue"
    assert prompted["requestId"], "session/prompt 的 requestId 是必填"
    assert prompted["content"] == [{"type": "text", "text": "正文在这里。"}]
    for item in fake.requests:
        assert item["cookie"], "每一条都要带 self-signed cookie"
        assert item["path"].startswith("/api/"), "端点走斜杠形式"
        assert "args" in item["body"]["payload"], "信封里 args 那一层不能少"


def test_handoff_leaves_the_two_on_disk_records(tmp_path: Path, credentials: Path) -> None:
    """盘上落两份记录：发出去的提示词全文与三步的记录（它们是状态文件里声明的那两格）。"""
    session = tmp_path / "260927"
    session.mkdir()

    with FakeDshWeb() as fake:
        dsh_web.handoff(session, prompt_text="正文在这里。", url=fake.url, allow_dsh=True)

    prompt = (session / "working" / "run_prompt.md").read_text(encoding="utf-8")
    log = (session / "working" / "minutes-run.log").read_text(encoding="utf-8")
    assert prompt == "正文在这里。"
    assert "交付成功" in log
    assert "workspace/create" in log and "session/prompt" in log
    assert "session-abc" in log
    assert "正文在这里。" not in log, "提示词全文在 run_prompt.md 里，log 只记长度"


def test_handoff_reports_each_step_that_can_break(tmp_path: Path, credentials: Path) -> None:
    """三步各自失败时分到各自的档，并且**不再往下走**。"""
    session = tmp_path / "260927"
    session.mkdir()

    routes: dict[str, Any] = {"workspace/create": ("error", "workspace/invalid-path", "坏了")}
    with FakeDshWeb(routes) as fake:
        result = dsh_web.handoff(session, prompt_text="正文。", url=fake.url, allow_dsh=True)
    assert result.ok is False
    assert result.failure == dsh_web.FAILURE_WORKSPACE
    assert "workspace/invalid-path" in str(result.error)
    assert fake.methods() == ["workspace/create"]

    routes = {
        "workspace/create": {"workspace": {"workspaceId": "ws-1", "title": "260927"}},
        "session/create": ("error", "workspace/not-found", "没了"),
    }
    with FakeDshWeb(routes) as fake:
        result = dsh_web.handoff(session, prompt_text="正文。", url=fake.url, allow_dsh=True)
    assert result.failure == dsh_web.FAILURE_SESSION
    assert result.workspace_id == "ws-1", "走到哪儿断的照样带上"
    assert fake.methods() == ["workspace/create", "session/create"]

    routes = {
        "workspace/create": {"workspace": {"workspaceId": "ws-1", "title": "260927"}},
        "session/create": {"sessionId": "session-abc"},
        "session/prompt": ("error", "session/not-found", "没了"),
    }
    with FakeDshWeb(routes) as fake:
        result = dsh_web.handoff(session, prompt_text="正文。", url=fake.url, allow_dsh=True)
    assert result.failure == dsh_web.FAILURE_PROMPT
    assert result.session_id == "session-abc"
    assert (session / "working" / "minutes-run.log").read_text(encoding="utf-8").count(
        "交付失败"
    ) == 1


def test_handoff_reports_a_missing_workspace_id(
    tmp_path: Path, credentials: Path
) -> None:
    """`workspace/create` 回了个空壳时也交回失败（不拿着空 id 往下建会话）。"""
    session = tmp_path / "260927"
    session.mkdir()

    with FakeDshWeb({"workspace/create": {"workspace": {}}}) as fake:
        result = dsh_web.handoff(session, prompt_text="正文。", url=fake.url, allow_dsh=True)

    assert result.failure == dsh_web.FAILURE_WORKSPACE
    assert "workspaceId" in str(result.error)
    assert fake.methods() == ["workspace/create"]


def test_a_spent_budget_is_reported_as_timeout(
    tmp_path: Path, credentials: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """预算用光时第一档就交回 `timeout`（一次请求都不发）。"""
    session = tmp_path / "260927"
    session.mkdir()
    monkeypatch.setattr(dsh_web, "_handoff_budget", lambda timeout: 0.0)

    with FakeDshWeb() as fake:
        result = dsh_web.handoff(session, prompt_text="正文。", url=fake.url, allow_dsh=True)

    assert result.failure == dsh_web.FAILURE_TIMEOUT
    assert fake.methods() == []


def test_the_four_hour_budget_never_becomes_an_http_timeout(
    tmp_path: Path, credentials: Path
) -> None:
    """`runner` 递下来的 `minutes.timeout_seconds`（默认四小时）不该变成一次 HTTP 调用的超时。

    这一条钉的是「一次交付不许挂住几小时」：把三条 RPC 各自收到的 `timeout` 记下来，
    它们必须落在 `RPC_TIMEOUT_SECONDS` 那一档 —— 四小时那个数是给**生成**用的预算，
    交付这一步只拿它夹出整段的上界（`_handoff_budget()`）。
    """
    session = tmp_path / "260927"
    session.mkdir()
    seen: list[float] = []
    real = dsh_web._post_json

    def watched(url: str, method: str, payload: bytes, cookie: str, timeout: float):
        seen.append(float(timeout))
        return real(url, method, payload, cookie, timeout)

    with FakeDshWeb() as fake:
        result = dsh_web.handoff(
            session,
            prompt_text="正文。",
            url=fake.url,
            timeout=14400,
            post=watched,
            allow_dsh=True,
        )

    assert result.ok is True, result.error
    assert len(seen) == 3, seen
    assert all(0.0 < value <= dsh_web.RPC_TIMEOUT_SECONDS for value in seen), seen


def test_a_transport_blip_is_retried_then_succeeds(credentials: Path) -> None:
    """连接层抖动重试一次；第二次通了就算了了这一趟。"""
    attempts: list[str] = []
    real = dsh_web._post_json

    def flaky(url: str, method: str, payload: bytes, cookie: str, timeout: float):
        attempts.append(method)
        if len(attempts) == 1:
            raise OSError("连接被对端掐了")
        return real(url, method, payload, cookie, timeout)

    with FakeDshWeb() as fake:
        result = dsh_web.rpc_call(
            fake.url,
            "workspace/create",
            {"path": "C:/meetings/260927"},
            cookie="cookie=1",
            post=flaky,
        )

    assert result["ok"] is True
    assert attempts == ["workspace/create", "workspace/create"]


def test_a_rejected_call_is_not_retried(credentials: Path) -> None:
    """401 / 404 与 `ok=false` 重放同一个错误，因此一次就交回原因。"""
    with FakeDshWeb({"workspace/create": (404, "not found")}) as fake:
        with pytest.raises(dsh_web.WebHandoffError, match="404"):
            dsh_web.rpc_call(
                fake.url, "workspace/create", {"path": "x"}, cookie="cookie=1"
            )
        assert len(fake.requests) == 1

    with FakeDshWeb({"workspace/create": ("error", "workspace/name-conflict", "重名")}) as fake:
        with pytest.raises(dsh_web.WebHandoffError, match="name-conflict"):
            dsh_web.rpc_call(
                fake.url, "workspace/create", {"path": "x"}, cookie="cookie=1"
            )
        assert len(fake.requests) == 1


def test_the_http_error_body_lands_in_the_message(credentials: Path) -> None:
    """失败消息里带着 DSH 那句人话（排查时不必再去抓包）。"""
    with FakeDshWeb({"workspace/create": (401, "dsh web authentication required")}) as fake:
        with pytest.raises(dsh_web.WebHandoffError, match="authentication required"):
            dsh_web.rpc_call(
                fake.url, "workspace/create", {"path": "x"}, cookie="cookie=1"
            )
