"""`tests/test_static.py` —— 静态产物、`/debug`、三道防线与完整版快照。

ASGI 层断言一律用 `with TestClient(app, base_url="http://127.0.0.1")` 的**上下文管理器形式**
（只有 `with` 才触发应用的生命周期钩子，`ServiceRuntime.startup()` 与快照的服务信息字段
都在那一步落定；`base_url` 把回环 `Host` 头带上，因此不再逐个请求显式带 Host）。

覆盖：
  - `web.dist_dir` 指到不存在的目录 → `GET /` 是说明页、`/debug` 正常、`/api/*` 正常；
  - 指到带 `index.html` 的真目录 → `GET /` 挂的是那份产物；
  - `/api/*` 的应答头带 `Cache-Control: no-store`（并且不发 CORS 头）；
  - `Host: evil.example` 得 **403**；裸 `POST /api/actions`（缺头 / 缺 `Content-Type`）得 **403**、
    **头齐的 `POST /api/actions` 由真实应答替换旧的 `404` 断言**（设计口径第 8 条）；
  - `GET /api/state` 完整版的结构（顶层键全集、`sources` 两项且 `envelope` 逐字为空数组 `[]`、
    `service` 六字段且 `version` 非空、四态真值、`settings` 五键与 `asr` 五子键齐、
    `devices` 两个子键（`mic` / `loopback`，没配时都是 `null`））；
  - 空闲态的可观察物：没有新建场次目录、没有 `session.json`、主 log 里没有场次开始行；
  - 启动序列第 5 步「打开浏览器」：`MEETING_NO_BROWSER=1` 时不开、不设时真的开（并落一行含
    被打开 URL 的 INFO）、失败只警告且服务照常起来。**这几条全在进程内**，`webbrowser.open`
    一律用 `monkeypatch` 换成只把 URL 记进 `calls` 的假实现 —— `meeting.service.app.webbrowser`
    在本进程里是标准库那个模块对象，换掉它的 `open` 之后，本进程内任何一条路径都碰不到真浏览器
    （子进程里那一支的隔离手段见 `tests/test_port_policy.py` 与 `tests/_helpers.py`；
    `tests/conftest.py` 另有一层 autouse 兜底）；
  - 未装配即取快照**抛错**（不留退化的空值）、`/api/heartbeat` 的兜底应答自洽。

**不断言 `revision` 的具体初始值**（设计契约冻结项 19 / 设计口径第 2 条）：
只断言「存在、是 int、单调递增」。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tests import _helpers as helpers
from meeting.service import app as service_app
from meeting.service.app import DIST_MISSING_MESSAGE

TOP_LEVEL_KEYS = {
    "revision",
    "service",
    "top_state",
    "session",
    "sources",
    "alerts",
    "transcript",
    "ended",
    "settings",
}
SERVICE_KEYS = {"started_at", "version", "port", "url", "log_path"}
SOURCE_KEYS = {
    "id",
    "label",
    "device_name",
    "health",
    "lost_since",
    "restored",
    "level_dbfs",
    "peak_dbfs",
    "envelope",
}
TRANSCRIPT_KEYS = {"health", "last_line_at", "buffered_lines", "failed_segments"}
SETTINGS_TOP_KEYS = {
    "alarm_sound", "heartbeat_interval_seconds", "summary_default_window",
    "summary_preset_names", "summary_active_preset", "asr", "devices",
}
#: 设计口径第 3 条：`asr` 的五个子键（取当前生效值）、`devices` 的两个子键（`mic` / `loopback`，没配时 `null`）
ASR_KEYS = {
    "max_segment_seconds",
    "timeout_seconds",
    "max_new_tokens",
    "repeat_max_unit",
    "repeat_min_coverage",
}
#: 顶层四态（设计口径）
TOP_STATES = {"idle", "recording", "alert", "ended"}
#: `sources[*].health` 的七取值（设计口径）
SOURCE_HEALTH = {
    "unprobed", "ready", "probe_failed", "capturing", "lost", "restored", "stopped",
}


@pytest.fixture()
def client_factory(tmp_path):
    """给出 `make(values)` / `make(values, home)`：把家目录与产品目录都指到临时目录下的 ASGI 客户端。

    `home` 不给时按自维护的计数换一个家目录；给了就用**用例自己那个**家目录 —— 需要按
    `<家目录>/.config/meeting/logs/meeting.log` 读主 log 的用例必须把家目录传进来
    （服务级 log 落在它下面，夹具与用例读的得是同一个目录）。

    **采集后端由 `helpers.app_client()` 补共用缺省**（没写 `capture.backend` 时收成 `dev`
    替身，见 `tests/_helpers.py` 的 `DEFAULT_CAPTURE_BACKEND`）：不补的话产品缺省 `auto`
    会在 startup 的 `probe()` 里真的去枚举并试开这台机器的声卡设备。要 `null` / 别的档的
    用例自己显式传，那时以 `values` 为准。本夹具因此**与这台机器上有没有声卡无关**。
    """
    import contextlib

    stack = contextlib.ExitStack()
    made = 0

    def make(values: dict | None = None, home=None, *, browser: str = "safe"):
        nonlocal made
        made += 1
        if home is None:
            home = tmp_path / f"home{made}"  # 自维护计数：每次换一个家目录，不读 ExitStack 的内部属性
        home = Path(home)
        home.mkdir(parents=True, exist_ok=True)
        return stack.enter_context(helpers.app_client(home, values=values, browser=browser))

    try:
        yield make
    finally:
        stack.close()


# `/` 的二选一分支


def test_root_serves_explanation_page_when_dist_missing(tmp_path, client_factory):
    """产物目录不存在 → `GET /` 给一页说明；服务照常起；`/debug` 与 `/api/*` 正常。"""
    home = tmp_path / "home_for_missing"
    home.mkdir()
    dist = helpers.missing_dist_dir(home)
    client = client_factory({"web.dist_dir": str(dist)})

    root = client.get("/")
    assert root.status_code == 200
    assert DIST_MISSING_MESSAGE in root.text
    assert "npm run build" in root.text
    assert "/debug" in root.text

    assert client.get("/debug").status_code == 200
    assert client.get("/api/heartbeat").status_code == 200
    assert client.get("/api/state").status_code == 200
    assert not dist.exists(), "服务不该为了这一页去建产物目录"


def test_root_serves_dist_when_present(tmp_path, client_factory):
    """产物目录存在 → `GET /` 挂的是那份产物（`StaticFiles(html=True)`）。"""
    home = tmp_path / "home_for_dist"
    home.mkdir()
    dist = helpers.write_dist_dir(home, marker="built-by-npm")
    client = client_factory({"web.dist_dir": str(dist)})

    root = client.get("/")
    assert root.status_code == 200
    assert "built-by-npm" in root.text
    assert DIST_MISSING_MESSAGE not in root.text
    # API 与自检页没有被静态挂载吃掉
    assert client.get("/api/heartbeat").status_code == 200
    assert client.get("/debug").status_code == 200


def test_root_switches_between_page_and_dist_per_request(tmp_path, client_factory):
    """`/` 的目录存在性按**每请求**判定：产物建好刷新即可见、删掉刷新回说明页。

    同一个进程、同一个应用实例里走完三步（启动缺失 → 建出 → 再删），中间不重启——
    这正是说明页文案「产物目录一建好，刷新就能看到真正的界面」的可观察判据。
    """
    import shutil

    home = tmp_path / "home_for_switch"
    home.mkdir()
    dist = helpers.missing_dist_dir(home)
    client = client_factory({"web.dist_dir": str(dist)})

    # ① 启动时无产物目录：说明页
    missing = client.get("/")
    assert missing.status_code == 200
    assert DIST_MISSING_MESSAGE in missing.text

    # ② 同一进程内建出目录：再 GET / 返回产物（不重启）
    dist.mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><meta charset=\"utf-8\"><title>built-later</title>"
        "<h1>built-later</h1>",
        encoding="utf-8",
    )
    built = client.get("/")
    assert built.status_code == 200
    assert "built-later" in built.text
    assert DIST_MISSING_MESSAGE not in built.text
    # API 与自检页没有被静态挂载吃走
    assert client.get("/api/heartbeat").status_code == 200
    assert client.get("/debug").status_code == 200

    # ③ 目录又被删：回到说明页
    shutil.rmtree(dist)
    removed = client.get("/")
    assert removed.status_code == 200
    assert DIST_MISSING_MESSAGE in removed.text


def test_debug_page_is_served_from_the_package(tmp_path, client_factory):
    """`/debug` 常驻可访问，且是从**包内**读取的骨架自检页（action 与事件流已接通）。"""
    home = tmp_path / "home_for_debug"
    home.mkdir()
    client = client_factory({"web.dist_dir": str(helpers.missing_dist_dir(home))})

    response = client.get("/debug")
    assert response.status_code == 200
    text = response.text
    # 非产品路径标注保留（设计口径）
    assert "非产品界面" in text
    assert "不承担任何告警职责" in text
    assert "/api/actions" in text
    assert "X-Meeting-Service" in text
    assert "EventSource" in text
    assert "/api/events" in text
    assert "/api/heartbeat" in text
    # 自检页从包内读：同一份内容应当就在包里
    assert "EventSource" in helpers.read_text(
        __import__("meeting.service.static", fromlist=["debug_page_path"]).debug_page_path()
    )


def test_api_paths_carry_no_store(tmp_path, client_factory):
    """`/api/*` 一律 `Cache-Control: no-store`，并且不发 CORS 头。"""
    home = tmp_path / "home_for_headers"
    home.mkdir()
    client = client_factory({"web.dist_dir": str(helpers.missing_dist_dir(home))})

    for path in ("/api/state", "/api/heartbeat"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers.get("access-control-allow-origin") is None


# 启动序列第 5 步「打开浏览器」


def _started_client_and_log(client, home):
    """确认「应用真的起来了」（两个最小版端点都答得上），并取回主 log 文本。

    只有 `with TestClient(...)` 触发过生命周期钩子，这两个端点才拿得到 200；
    因此这两条 200 同时证明 startup 钩子（含它尾部的打开浏览器那一步）跑完了。
    """
    assert client.get("/api/heartbeat").status_code == 200
    assert client.get("/api/state").status_code == 200
    return helpers.read_text(helpers.service_log_path_of(home))


def test_browser_is_not_opened_when_disabled(tmp_path, monkeypatch, client_factory):
    """`MEETING_NO_BROWSER=1` → 这一步不开浏览器（假替身一次都没被调到），落一行日志说明跳过。"""
    monkeypatch.setenv("MEETING_NO_BROWSER", "1")

    home = tmp_path / "home_no_browser"
    with client_factory({"web.dist_dir": str(helpers.missing_dist_dir(home))}, home) as client:
        log_text = _started_client_and_log(client, home)
        assert client.browser_calls.calls == [], "关掉这一步时不该调 webbrowser.open"

    assert "MEETING_NO_BROWSER=1，跳过打开浏览器" in log_text


def test_browser_is_opened_when_enabled(tmp_path, monkeypatch, client_factory):
    """不设该变量 → 这一步真的拿 `service.url` 去开浏览器（启动照旧完成），且**成功留一行 INFO**。

    三支各自留痕：跳过（`MEETING_NO_BROWSER=1`）、失败（另一条用例）、**成功**（本用例）。
    """
    monkeypatch.delenv("MEETING_NO_BROWSER", raising=False)

    home = tmp_path / "home_with_browser"
    with client_factory(
        {"web.dist_dir": str(helpers.missing_dist_dir(home))}, home, browser="open"
    ) as client:
        log_text = _started_client_and_log(client, home)
        calls = client.browser_calls.calls
        # 打开的就是这条服务自己的 URL（由监听 socket 的 getsockname() 读回值产生）
        service_url = client.get("/api/state").json()["service"]["url"]

    assert calls == [service_url]

    # 成功这一支也必须留痕：一行 INFO，含被打开的那个 URL（不许静默）
    success_lines = helpers.lines_with(log_text, "打开浏览器成功")
    assert len(success_lines) == 1, f"成功打开时应当恰有一行 INFO：{success_lines}"
    assert "INFO" in success_lines[0], f"这一行是 INFO 级：{success_lines[0]}"
    assert service_url in success_lines[0], f"这一行要含被打开的 URL：{success_lines[0]}"
    # 跳过与失败那两条文案在这一支里不该出现
    assert "跳过打开浏览器" not in log_text
    assert "打开浏览器失败" not in log_text
    assert "打开浏览器没成功" not in log_text


def test_browser_failure_only_warns_and_startup_continues(tmp_path, monkeypatch, client_factory):
    """`webbrowser.open` 抛异常 → 只警告、不阻塞：服务照常起来并可问 `/api/state`。"""
    monkeypatch.delenv("MEETING_NO_BROWSER", raising=False)

    home = tmp_path / "home_browser_boom"
    with client_factory(
        {"web.dist_dir": str(helpers.missing_dist_dir(home))}, home, browser="raise"
    ) as client:
        log_text = _started_client_and_log(client, home)
        calls = client.browser_calls.calls

    assert calls, "这一步应当真的被调到（假替身会把它记下来）"
    assert "打开浏览器失败（只警告、不阻塞）" in log_text
    assert "模拟没有可用的浏览器" in log_text


def test_launch_browser_helper_reports_failure(tmp_path, monkeypatch):
    """`launch_browser()` 的直接断言：关掉时不调、开不上时回 `False` 且只落警告。"""
    calls: list[str] = []
    monkeypatch.setattr(service_app.webbrowser, "open", lambda url: calls.append(url) or False)

    monkeypatch.setenv("MEETING_NO_BROWSER", "1")
    assert service_app.launch_browser("http://127.0.0.1:8765") is False
    assert calls == []

    monkeypatch.delenv("MEETING_NO_BROWSER", raising=False)
    assert service_app.launch_browser("http://127.0.0.1:8765") is False
    assert calls == ["http://127.0.0.1:8765"]


# 三道最低防线


def test_host_check_rejects_evil_host(tmp_path, client_factory):
    """非 `127.0.0.1` / `localhost` 的 Host 一律 403。"""
    home = tmp_path / "home_for_host"
    home.mkdir()
    client = client_factory({"web.dist_dir": str(helpers.missing_dist_dir(home))})

    assert client.get("/api/heartbeat", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    # 回环的两个写法都放行
    assert client.get("/api/heartbeat", headers={"Host": "localhost"}).status_code == 200
    assert client.get("/api/heartbeat", headers={"Host": "127.0.0.1:8765"}).status_code == 200


def test_action_request_without_custom_header_is_forbidden(tmp_path, client_factory):
    """裸 `POST /api/actions`（缺 `X-Meeting-Service`）→ 403（校验先于路由匹配）。"""
    home = tmp_path / "home_for_actions"
    home.mkdir()
    client = client_factory({"web.dist_dir": str(helpers.missing_dist_dir(home))})

    response = client.post(
        "/api/actions",
        json={"action": "start"},
        headers={"X-Meeting-Service": "0"},
    )
    assert response.status_code == 403

    response = client.post("/api/actions", json={"action": "start"})
    assert response.status_code == 403


def test_action_request_without_json_content_type_is_forbidden(tmp_path, client_factory):
    """带 `X-Meeting-Service: 1` 但 `Content-Type` 不是 `application/json` → 403。"""
    home = tmp_path / "home_for_actions_form"
    home.mkdir()
    client = client_factory({"web.dist_dir": str(helpers.missing_dist_dir(home))})

    response = client.post(
        "/api/actions",
        content=b"action=start",
        headers={"X-Meeting-Service": "1", "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 403


def test_action_request_with_both_headers_gets_a_real_answer(tmp_path, client_factory):
    """头齐的完整动作请求 → **真实应答**（设计口径第 8 条：旧的「路由还没有」404 一条不留）。

    默认（`helpers.app_client()` 补的 `dev` 采集替身）下 `start` 的真实结果是一条成功应答、
    并且盘上当场建出那一场的场次目录；同时这条应答证明请求确实被 `POST /api/actions` 的
    端点接走了（不是末端 404）。
    """
    home = tmp_path / "home_for_actions_ok"
    home.mkdir()
    out_root = tmp_path / "out_for_actions_ok"
    out_root.mkdir()
    client = client_factory(
        {"web.dist_dir": str(helpers.missing_dist_dir(home))}
    )

    response = client.post(
        "/api/actions",
        json={"action": "start", "args": {"dir": str(helpers.allocate_start_dir(out_root))}},
        headers={"X-Meeting-Service": "1"},
    )
    # 真实应答：dev 替身在场，`start` 起得来 —— 成功体就是 `{ok, revision}`
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["ok"] is True
    assert isinstance(payload["revision"], int)
    # 场次目录当场建在输出根下，快照里的 `session.id` 就是那个目录名
    session = client.get("/api/state").json()["session"]
    assert session is not None
    assert sorted(item.name for item in out_root.iterdir()) == [session["id"]]

    # 未知动作名同样由端点给出真实应答（不是末端 404）
    unknown = client.post(
        "/api/actions",
        json={"action": "definitely_not_an_action"},
        headers={"X-Meeting-Service": "1"},
    )
    assert unknown.status_code == 400
    assert unknown.json()["code"] == "invalid_setting"


# `GET /api/state` 的最小版结构


def test_api_state_shape_is_the_full_one(tmp_path, client_factory):
    """顶层键全集齐、嵌套字段按冻结形状、取值取到**真值**。"""
    home = tmp_path / "home_for_state"
    home.mkdir()
    # 采集后端显式取 `null` 替身：**测试不许碰真声卡**，这一条验的是空闲态快照的形状
    client = client_factory(
        {
            "web.dist_dir": str(helpers.missing_dist_dir(home)),
            "capture.backend": "null",
        }
    )

    response = client.get("/api/state")
    assert response.status_code == 200
    snapshot = json.loads(response.text)

    assert set(snapshot) == TOP_LEVEL_KEYS
    # ① `revision`：存在、是 int、**初始值不钉成常数**
    assert isinstance(snapshot["revision"], int)
    # ② 四态取真值（空闲态就是 idle）
    assert snapshot["top_state"] in TOP_STATES
    assert snapshot["top_state"] == "idle"

    service = snapshot["service"]
    assert set(service) == SERVICE_KEYS
    # ③ `service.version` 是**非空字符串**（null 与空串都不合格）
    assert isinstance(service["version"], str) and service["version"]
    assert service["port"] == 8765  # ASGI 层夹具把绑定设成回环 8765
    assert service["url"] == "http://127.0.0.1:8765"
    assert service["log_path"]
    assert service["started_at"]

    sources = snapshot["sources"]
    assert [item["id"] for item in sources] == ["mic", "loopback"]
    assert [item["label"] for item in sources] == ["本地", "远端"]
    for item in sources:
        assert set(item) == SOURCE_KEYS
        # `envelope` 的空形态**逐字断言为空数组 `[]`**；health 落在七取值域里
        assert item["envelope"] == []
        assert item["health"] in SOURCE_HEALTH
        assert item["health"] == "unprobed"  # 空闲态 + null 替身如实报「未探测」
        assert item["device_name"] is None

    # 空闲态不该挂任何提示级：提示级只在录音与告警态成立。`hints` 是空数组。
    # 下面就绪的 `transcript.health == "stopped"` 是另一件事 —— 那一格报的是「真转写后端
    # 还没起过场次」，空闲态本来就没有「转写停了」这条提示。
    assert snapshot["alerts"] == {"critical": None, "hints": []}
    assert set(snapshot["transcript"]) == TRANSCRIPT_KEYS
    # 空闲态的 `health` 是**从转写后端读来的**真值：这一场还没起过任何场次，真实现如实报
    # `stopped`（「空闲 = stopped」；`normal` 是 dataclass 默认值，
    # 服务起来那一刻就不再是它了）。另三格空闲态就是零。
    assert snapshot["transcript"] == {
        "health": "stopped",
        "last_line_at": None,
        "buffered_lines": 0,
        "failed_segments": 0,
    }
    # 空闲态的真值：没有会话、没有收尾摘要
    assert snapshot["session"] is None
    assert snapshot["ended"] is None

    settings = snapshot["settings"]
    assert set(settings) == SETTINGS_TOP_KEYS
    assert isinstance(settings["alarm_sound"], bool)
    assert isinstance(settings["heartbeat_interval_seconds"], int)
    # 缺省档是四档之一
    assert settings["summary_default_window"] == "15m"
    # `asr` 五子键齐且取当前生效值；`devices` 两个子键在没配时都是 `null`
    assert set(settings["asr"]) == ASR_KEYS
    assert settings["asr"]["timeout_seconds"] == 20
    assert settings["devices"] == {"mic": None, "loopback": None}


def test_api_state_revision_grows_by_one_per_state_change(tmp_path, client_factory):
    """`revision` 每次状态变更单调 `+1`；被拒绝的请求**不涨 `revision`**（设计口径第 2 条）。"""
    home = tmp_path / "home_for_revision"
    home.mkdir()
    out_root = tmp_path / "out_for_revision"
    out_root.mkdir()
    client = client_factory(
        {
            "web.dist_dir": str(helpers.missing_dist_dir(home)),
            "capture.backend": "dev",
        }
    )

    before = client.get("/api/state").json()["revision"]
    # 一次成功的 start 是一次状态变更 → 恰好 +1
    started = client.post(
        "/api/actions",
        json={"action": "start", "args": {"dir": str(helpers.allocate_start_dir(out_root))}},
        headers={"X-Meeting-Service": "1"},
    )
    assert started.status_code == 200, started.text
    after_start = started.json()["revision"]
    assert after_start == before + 1

    # 一次成功的 mute_session 也是一次状态变更 → 恰好 +1
    muted = client.post(
        "/api/actions", json={"action": "mute_session"}, headers={"X-Meeting-Service": "1"}
    )
    assert muted.status_code == 200, muted.text
    assert muted.json()["revision"] == after_start + 1
    assert client.get("/api/state").json()["revision"] == after_start + 1

    # 被拒绝的请求（录音中再 start）**不产生第二次迁移、不涨 revision**
    duplicate = client.post(
        "/api/actions", json={"action": "start"}, headers={"X-Meeting-Service": "1"}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "already_recording"
    assert client.get("/api/state").json()["revision"] == after_start + 1

    # 完成收尾（一次 stop，收尾三步各自提交）之后 revision 仍在单调往上走
    assert client.post(
        "/api/actions", json={"action": "stop"}, headers={"X-Meeting-Service": "1"}
    ).status_code == 200
    deadline = time.monotonic() + 15.0
    later = after_start + 1
    while time.monotonic() < deadline:
        snapshot = client.get("/api/state").json()
        if snapshot["revision"] > after_start + 1 and snapshot["ended"] is not None:
            if snapshot["ended"]["phase"] == "done":
                later = snapshot["revision"]
                break
        time.sleep(0.05)
    assert later > after_start + 1


def test_api_heartbeat_shape_is_the_full_one(tmp_path, client_factory):
    """`GET /api/heartbeat` 完整版：七字段齐、`service` 字面量必须有、空闲态两字段为 `null`。"""
    home = tmp_path / "home_for_heartbeat"
    home.mkdir()
    client = client_factory({"web.dist_dir": str(helpers.missing_dist_dir(home))})

    payload = client.get("/api/heartbeat").json()
    assert set(payload) == {
        "service",
        "server_time",
        "revision",
        "top_state",
        "recording_alive",
        "seconds_since_last_tick",
        "capture_gaps",
    }
    assert payload["service"] == "meeting"
    assert isinstance(payload["revision"], int)
    assert payload["top_state"] == "idle"
    # 空闲态：两字段都是 null（没有采集事实帧在跑）；空洞记录是空记录、不给 null
    assert payload["recording_alive"] is None
    assert payload["seconds_since_last_tick"] is None
    assert payload["capture_gaps"] == {"total_count": 0, "total_seconds": 0.0, "recent": []}


def test_heartbeat_handler_keeps_the_service_literal_without_runtime():
    """未装配时的兜底自洽：这条请求照样拿到 200 + 完整心跳字段（含 `service` 字面量）。

    路由的注册点在 `build_app()`，它必然先拿到 runtime，所以这一支在正常路径上走不到 ——
    这里直接调处理函数（同步测试，用 `asyncio.run` 跑那一个协程），把兜底应答的形状钉住。
    探针的判据是「200 + body 含 `"service": "meeting"`」，回 500 会让探针把这台自己的服务
    判成「别的程序占着」，跟判据打架。
    """
    import asyncio

    from meeting.service.routes import _api_heartbeat

    response = asyncio.run(_api_heartbeat(None))  # noqa: SLF001 - 直接钉住兜底分支

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert set(payload) == {
        "service",
        "server_time",
        "revision",
        "top_state",
        "recording_alive",
        "seconds_since_last_tick",
        "capture_gaps",
    }
    assert payload["service"] == "meeting"


def test_snapshot_is_not_available_before_the_runtime_is_assembled(tmp_path, monkeypatch):
    """未装配即取快照 → 抛错，不留退化的空值。"""
    from meeting import config as config_module
    from meeting.service import app as service_app

    home = tmp_path / "home_for_unassembled"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    loaded = config_module.load()

    runtime = service_app.build_runtime(loaded)
    with pytest.raises(Exception) as info:
        runtime.to_dict()
    assert "service.port" in str(info.value)

    # `service.url` 同样取不到（不许拿配置值或字面量顶替）
    with pytest.raises(Exception):
        runtime.url

    # 绑上监听地址读回值之后，快照照常产出
    runtime.set_binding("127.0.0.1", 8765)
    snapshot = runtime.to_dict()
    assert snapshot["service"]["port"] == 8765
    assert snapshot["service"]["url"] == "http://127.0.0.1:8765"


def test_idle_state_has_no_session_artifacts(tmp_path, client_factory):
    """空闲态的等价可观察物：没有新建场次目录、没有 `session.json`、log 里没有场次开始行。"""
    home = tmp_path / "home_for_idle"
    home.mkdir()
    out_root = tmp_path / "out"
    out_root.mkdir()
    before = sorted(item.name for item in out_root.iterdir())

    client = client_factory(
        {"web.dist_dir": str(helpers.missing_dist_dir(home))}
    )
    client.get("/api/state")

    assert sorted(item.name for item in out_root.iterdir()) == before
    assert not list(out_root.rglob("session.json"))
    assert not helpers.lines_with(helpers.read_text(helpers.service_log_path_of(home)), "场次开始")
