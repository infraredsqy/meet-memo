"""`tests/test_summary_action.py` —— `summarize` 动作与 `GET /api/summaries`（`1987cb20` phase2）。

覆盖 `phase2/task.md` 的接口口径与完成判定第 1 / 2 / 4 条：

  - **动作成功体**：通用信封上多一个 `summary` 键，逐键是
    `window` / `window_label` / `at` / `elapsed_seconds` / `lines` / `what` / `where` / `lost`；
  - **逐档失败**（判定次序固定：判态 → 校验 `window` → 看转写健康 → 看模型端点配置 → 才动手）：
    `not_recording`(409)、`invalid_setting`(400)、`transcript_unavailable`(503)、
    `summary_failed`(502) 的两档 `detail`，逐字与那张表一致；**前面几档没过时一个请求都不发出去、
    一条记录都不写**（用记账的假客户端读数为 0 钉住）；
  - **只读**：打一次 `summarize` 前后取两次快照，`revision` 与顶层十键逐字相同（`summary` 键
    一个字都不许进快照）；`GET /api/summaries` 同样不涨 `revision`；
  - **不挡别人**（复审 minor 6）：假客户端在 `complete()` 里原地停住，同一刻另一条
    `GET /api/state` 的往返仍压在毫秒量级 —— 那一次模型调用跑在工作线程上，
    事件循环照旧转（判据见 `test_other_requests_are_not_blocked_while_summarizing()`）；
  - **记录**：只记成功产出的那几次，按时间先后；没有场次时 `session_id` 为 `null`；
  - `window` 省略时取配置的缺省档；四档取值各跑一次，`window_label` 与人话标签对得上。
  - **只有「键省略 / 给 `null`」才落缺省档**（`test_a_missing_or_null_window_falls_back_but_any_other_value_is_rejected`
    那条把这一格钉死）：`""` / `"   "` / `"15M"` / 数字 `15` 一律 `invalid_setting`(400)、
    `detail.legal` 是四档全集 —— 空串这一格给的是值，「省略」说的是键不在了，**不许**被静默当成缺省档；
  - **出口兜底**（`test_a_missing_injection_is_blocked_at_the_egress_guard`）：故意不注入
    `runtime.summary_client` 时，`tests/conftest.py` 的 `_no_real_model_call` 在会中总结那条
    HTTP 出口当场拦下那一笔（记账清单里就是配置里那个真地址，而这一趟回的是带着兜底名字的
    502）—— 判据按目标地址来（本机回环放行，其余一律拦）。

假件都从注入点进（`runtime.summary_client`），**一条真实端点请求都不发**。
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import threading
import time
import urllib.error
from pathlib import Path

import pytest

from meeting.backends.base import ReadOnlyConfig, TranscriptLine
from meeting.service.runtime import ACTION_CODES, ACTION_NAMES
from tests import _helpers as helpers

#: 慢档标记：本文件与 `tests/test_api_actions.py` 同档（起真应用 + 真 lifespan）。
pytestmark = pytest.mark.slow

#: 用例里的显式超时（不留裸秒数）
ACTION_POLL_TIMEOUT_SECONDS = 15.0
#: 轮询间隔
ACTION_POLL_INTERVAL_SECONDS = 0.05
#: 停在 `complete()` 里的那个假客户的闸门最多等多久（用例自己放它走的不走这一档）
GATE_TIMEOUT_SECONDS = 5.0
#: 「总结期间另一条请求」的往返上限：修好之后实测毫秒量级，被按住时就是这个闸门的时长
STATE_PROBE_LIMIT_SECONDS = 2.0
#: 超时那一档用的墙钟预算（秒；配置层下界是 1.0，用例不真等满默认的 25 秒）
SHORT_DEADLINE_SECONDS = 1.0
#: 等一条注定卡住的总结应答最多等多久（秒）：它就是本用例的看门，不许无限等
STUCK_RESPONSE_LIMIT_SECONDS = 15.0
#: 出口兜底那条用例里「放行档」探一次回环死端口的连接上限（秒）
#: （本机上要么当场被拒、要么等到这个上限 —— 两种都是真的走到了传输层，故取小值）
REQUEST_REFUSED_TIMEOUT_SECONDS = 0.3

#: `summary` 子对象的键全集（`phase2/task.md` 那张成功体逐字写死）
SUMMARY_KEYS = {
    "window", "window_label", "at", "elapsed_seconds", "lines",
    "what", "where", "lost",
}
#: `GET /api/summaries` 每一条的键全集（与应答体那个 `summary` 同形，**少 `lines` 与 `at` 之外没有别的差别**）
ENTRY_KEYS = {"window", "window_label", "at", "elapsed_seconds", "what", "where", "lost"}
#: 快照顶层九键（`summary` 一个都不许多）
TOP_LEVEL_KEYS = {
    "revision", "service", "top_state", "session", "sources",
    "alerts", "transcript", "ended", "settings",
}

#: 假客户端答的那份合法输出（三条固定标签齐备，跑一次就成功）
GOOD_BULLETS = json.dumps(
    {
        "bullets": [
            "【讲什么】在过下周的排期",
            "【讲到哪】说到第三项",
            "【丢掉什么】18:30 左右本地那一路有个识别失败的缺口",
        ]
    },
    ensure_ascii=False,
)


class LedgerClient:
    """**记账的假总结客户端**：`complete()` 只加计数、按脚本答话，一条请求都不发。

    `calls` 是用例读的那个数 —— 「判定次序真的拦在前面吗」这条判据就落在**它为 0** 上
    （不是「发出去失败了才报错」）。`messages` 留最近一次的入参，供断言窗口文本用。
    """

    def __init__(self, *, content: str = GOOD_BULLETS, error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.calls = 0
        self.timeouts: list[float] = []
        self.messages: list[dict[str, str]] = []

    def complete(self, messages, *, timeout_seconds: float) -> str:
        self.calls += 1
        self.timeouts.append(float(timeout_seconds))
        self.messages = [dict(message) for message in messages]
        if self.error is not None:
            raise self.error
        return self.content


class GatedClient:
    """**会停住的假总结客户端**：进 `complete()` 就亮灯，然后在闸门上等用例放行。

    复审 minor 6 那一幕的假件（真场景里等的是 25 秒的模型端点）：`entered` 让用例知道
    「这一次总结真的动手了」，`release` 由用例放（用例自己收尾，不留固定 sleep）。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def complete(self, messages, *, timeout_seconds: float) -> str:
        del messages, timeout_seconds
        self.calls += 1
        self.entered.set()
        self.release.wait(GATE_TIMEOUT_SECONDS)
        return GOOD_BULLETS


class FakeTranscriber:
    """假转写后端：只实现 runtime 用得着的那几个协议成员（`health` 可切）。"""

    def __init__(self, *, health: str = "normal") -> None:
        self._health = health
        self.line_sink = None
        self.started = 0
        self.stopped = 0

    def bind(self, line_sink, *, config=None) -> None:
        self.line_sink = line_sink

    def start(self, mode: str, session_dir: str) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def health(self) -> str:
        return self._health

    def backlog_depth(self) -> int:
        return 0

    def segment_timeout(self) -> float:
        return 20.0


def lines_for(count: int, *, span_seconds: float, step: float | None = None):
    """铺一批转写行：末行落在「此刻」，往前按 `step`（缺省均匀铺满 `span_seconds`）排开。"""
    now = time.time()
    gap = float(step) if step else (float(span_seconds) / max(1, count - 1))
    start = now - gap * (count - 1)
    return [
        TranscriptLine(
            ts=start + gap * index,
            source="mic" if index % 2 == 0 else "loopback",
            text=f"第 {index} 行内容",
            kind="line",
        )
        for index in range(count)
    ]


@contextlib.contextmanager
def live(tmp_path: Path, values: dict | None = None, *, health: str = "normal"):
    """起一个进程内已启动的运行时；`yield (client, runtime, transcriber, client_stub)`。

    转写后端换成假件（`health` 可切）：真转写后端（`auto`）没起过场次时报 `stopped`，
    而会中总结的正常路径要求转写健康 —— 假件让这一档可控，也让 503 那一档能单独造。
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    out_root = tmp_path / "out"
    out_root.mkdir(parents=True, exist_ok=True)
    config = {
        "capture.backend": "dev",
        "web.dist_dir": str(helpers.missing_dist_dir(home)),
        # 端点四件套配齐（密钥是**假值**：假客户端从注入点进，一个真实请求都不发出去）。
        # 不配齐的话每一档都会先撞「模型端点没配」那一关 —— 而那一关有它自己的用例。
        "summary.base_url": "http://127.0.0.1:1/v1",
        "summary.api_key": "test-key-not-used",
        "summary.model": "test-model",
    }
    config.update(values or {})
    with helpers.app_client(home, values=config) as client:
        client.place_root = out_root
        runtime = client.app.state.runtime
        transcriber = FakeTranscriber(health=health)
        runtime.transcriber_backend = transcriber
        transcriber.bind(runtime.bind_transcriber(transcriber), config=runtime._config_view())
        stub = LedgerClient()
        runtime.summary_client = stub
        yield client, runtime, transcriber, stub


def post(client, action: str, args: dict | None = None):
    body = {"action": action}
    if args is not None or action == "start":
        body["args"] = helpers.start_args(action, args, client.place_root)
    return client.post("/api/actions", json=body, headers={"X-Meeting-Service": "1"})


def snapshot_of(client) -> dict:
    return client.get("/api/state").json()


def summaries_of(client) -> dict:
    return client.get("/api/summaries").json()


def feed(runtime, transcriber, lines) -> None:
    """把这批行经**生产路径的那一个 sink** 喂进缓冲（`bind()` 交给后端的那个出口）。"""
    sink = transcriber.line_sink
    assert sink is not None, "假转写后端还没被 bind() 上，喂不进任何一行"
    for line in lines:
        sink(line)


def start_recording(client, *, mode: str | None = None) -> None:
    args = {} if mode is None else {"mode": mode}
    response = post(client, "start", args)
    assert response.status_code == 200, response.text


def wait_until(predicate, *, timeout: float = ACTION_POLL_TIMEOUT_SECONDS, what: str = "条件"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(ACTION_POLL_INTERVAL_SECONDS)
    raise AssertionError(f"等待超时（{timeout}s）：{what}（最后观察值 {last!r}）")


def wait_for_done(client):
    return wait_until(
        lambda: (lambda s: s if s["ended"] and s["ended"]["phase"] == "done" else None)(
            snapshot_of(client)
        ),
        what="收尾停在 done",
    )


# ---------------------------------------------------------------------------
# 动作名与 code 表的冻结清单（新增两项）
# ---------------------------------------------------------------------------


def test_summarize_and_its_two_codes_are_registered():
    """`summarize` 进动作名清单；`transcript_unavailable` / `summary_failed` 进 `code` 全集。
    `workspace_open` / `workspace_remove` 两只是工作台收编节点 `e2f6166b` 加的。"""
    assert ACTION_NAMES == (
        "start", "stop", "mute_session", "settings_update", "reprobe", "dev_inject",
        "summarize", "workspace_open", "workspace_remove",
    )
    assert set(ACTION_CODES) == {
        "already_recording", "draining", "capture_unavailable", "not_recording",
        "invalid_setting", "persist_failed", "action_unavailable",
        "transcript_unavailable", "summary_failed",
    }


def test_summarize_is_in_the_action_dispatch(tmp_path):
    """路由真的分派到这一支：`summarize` 的动作名不再撞「不认识的动作名」那一档。"""
    with live(tmp_path) as (client, _runtime, _transcriber, _stub):
        start_recording(client)
        response = post(client, "summarize", {"window": "15m"})
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True


# ---------------------------------------------------------------------------
# 成功体
# ---------------------------------------------------------------------------


def test_summarize_returns_the_three_sections_on_the_common_envelope(tmp_path):
    """成功体 = 通用信封 + 一个 `summary` 键；逐键形状与三段内容都对。

    三段内容来自**假客户端**的那份 `{"bullets": [...]}`：三条固定标签各归一段。
    """
    with live(tmp_path) as (client, runtime, transcriber, stub):
        start_recording(client)
        feed(runtime, transcriber, lines_for(40, span_seconds=600))

        before = snapshot_of(client)
        response = post(client, "summarize", {"window": "15m"})
        assert response.status_code == 200, response.text
        body = response.json()

        assert set(body) == {"ok", "revision", "summary"}
        assert body["ok"] is True
        assert body["revision"] == before["revision"], "总结是只读的：revision 是当前版本号"
        summary = body["summary"]
        assert set(summary) == SUMMARY_KEYS
        assert summary["window"] == "15m"
        assert summary["window_label"] == "15 分钟"
        assert summary["lines"] == 40
        assert isinstance(summary["elapsed_seconds"], float)
        assert summary["elapsed_seconds"] >= 0.0
        assert summary["at"].startswith("20")
        assert summary["what"] == ["在过下周的排期"]
        assert summary["where"] == ["说到第三项"]
        assert summary["lost"] == ["18:30 左右本地那一路有个识别失败的缺口"]
        assert stub.calls == 1
        # 窗口文本经真的装配路径进到消息里（末行那句一定在里面）
        assert "第 39 行内容" in stub.messages[-1]["content"]


@pytest.mark.parametrize(
    ("window", "label"),
    [("5m", "5 分钟"), ("15m", "15 分钟"), ("30m", "30 分钟"), ("full", "整场")],
)
def test_summarize_accepts_the_four_windows(tmp_path, window, label):
    """四档取值各跑一次，`window` 与 `window_label` 逐档对得上。"""
    with live(tmp_path) as (client, runtime, transcriber, _stub):
        start_recording(client)
        feed(runtime, transcriber, lines_for(10, span_seconds=300))
        response = post(client, "summarize", {"window": window})
        assert response.status_code == 200, response.text
        summary = response.json()["summary"]
        assert (summary["window"], summary["window_label"]) == (window, label)


def test_summarize_falls_back_to_the_configured_default_window(tmp_path):
    """`args.window` 省略时取配置的缺省档（这里显式配成 `5m`）。

    **`window` 给 `null` 与「键省略」同解**：只有这两种才是「没给这一格」。`""` /
    `"   "` / `"15M"` / 数字 `15` 都是**给了值**，一律进四档校验（见下面那条用例）。
    """
    with live(tmp_path, {"summary.default_window": "5m"}) as (
        client, runtime, transcriber, _stub
    ):
        start_recording(client)
        feed(runtime, transcriber, lines_for(6, span_seconds=120))
        response = post(client, "summarize")
        assert response.status_code == 200, response.text
        assert response.json()["summary"]["window"] == "5m"
        assert response.json()["summary"]["window_label"] == "5 分钟"

        # 显式 `null` 同一档：落配置的缺省档，不是非法取值
        explicit_null = post(client, "summarize", {"window": None})
        assert explicit_null.status_code == 200, explicit_null.text
        assert explicit_null.json()["summary"]["window"] == "5m"
        assert explicit_null.json()["summary"]["window_label"] == "5 分钟"


def test_an_empty_window_is_a_success_with_three_empty_arrays(tmp_path):
    """窗口里一行都没有时照旧 `ok: true`，三段都是空数组（B15 的「空数组合法」）。"""
    with live(tmp_path) as (client, _runtime, _transcriber, stub):
        start_recording(client)
        response = post(client, "summarize", {"window": "5m"})
        assert response.status_code == 200, response.text
        summary = response.json()["summary"]
        assert summary["lines"] == 0
        assert summary["what"] == [] and summary["where"] == [] and summary["lost"] == []
        assert stub.calls == 0, "一行都没有时一个请求都不发出去"


def test_the_window_keeps_only_the_rows_inside_it(tmp_path):
    """窗口真的按时间切：铺 40 分钟的行，`5m` 只收最近 5 分钟那几行。"""
    with live(tmp_path) as (client, runtime, transcriber, stub):
        start_recording(client)
        rows = lines_for(41, span_seconds=2400, step=60.0)
        feed(runtime, transcriber, rows)
        response = post(client, "summarize", {"window": "5m"})
        assert response.status_code == 200, response.text
        summary = response.json()["summary"]
        # 40 分钟里每步 60 秒，窗口 300 秒 → 末行 + 前 5 行 = 6 行
        assert summary["lines"] == 6
        text = stub.messages[-1]["content"]
        # 行首时间戳那一半**照生产代码同一处渲染算出来**（`window.render_window()`：取那一行
        # 自己的 `ts`，走 `datetime.fromtimestamp(...).strftime("%H:%M:%S")`）—— 期望值因此与
        # 本地墙钟走到哪个小时无关（早先断的是首字符 `"[0"`，那条只在本地 00–09 点成立）。
        import re

        from meeting.summary import window as window_module

        last = rows[-1]
        rendered_last = window_module.render_window(
            [
                window_module.BufferedLine(
                    seq=len(rows) - 1, ts=last.ts, source=last.source, text=last.text
                )
            ]
        )
        assert rendered_last in text, (
            f"窗口文本里该有末行那一条（带着它自己的 `[HH:MM:SS]` 时间戳）：{rendered_last!r}"
        )
        # 每一条窗口行都带一个**读得出来**的行首时间戳（形状由生产代码那一处定），
        # 行数也与摘要报的 `lines` 对得上
        stamped = [line for line in text.splitlines() if line.startswith("[")]
        assert len(stamped) == summary["lines"], (summary["lines"], stamped)
        for line in stamped:
            assert re.fullmatch(r"\[\d{2}:\d{2}:\d{2}\] .+ · (本地|远端)", line), line
        assert "第 34 行内容" not in text, "窗口外的那一行一个字都不该进消息"
        assert "第 40 行内容" in text


# ---------------------------------------------------------------------------
# 五档失败（判定次序固定）
# ---------------------------------------------------------------------------


def test_summarize_is_not_recording_outside_a_session(tmp_path):
    """`idle` 与 `ended` 两档 → `not_recording`(409)、`detail` 恒是空对象、请求数 0。"""
    with live(tmp_path) as (client, runtime, transcriber, stub):
        response = post(client, "summarize", {"window": "15m"})
        assert response.status_code == 409
        payload = response.json()
        assert set(payload) == {"ok", "code", "message", "detail"}
        assert payload["code"] == "not_recording"
        assert payload["detail"] == {}
        assert stub.calls == 0

        # ended（收尾干净走完那一档）同样拒绝
        start_recording(client)
        assert post(client, "stop").status_code == 200
        wait_for_done(client)
        ended = post(client, "summarize", {"window": "15m"})
        assert ended.status_code == 409
        assert ended.json()["code"] == "not_recording"
        assert ended.json()["detail"] == {}
        assert stub.calls == 0


def test_an_illegal_window_is_rejected_before_anything_is_sent(tmp_path):
    """`window` 取值非法 → `invalid_setting`(400)，`detail` 三键逐字；请求数 0、记录空。"""
    with live(tmp_path) as (client, runtime, transcriber, stub):
        start_recording(client)
        feed(runtime, transcriber, lines_for(5, span_seconds=120))
        response = post(client, "summarize", {"window": "7m"})
        assert response.status_code == 400
        payload = response.json()
        assert payload["code"] == "invalid_setting"
        assert payload["detail"] == {
            "key": "window", "value": "7m", "legal": ["5m", "15m", "30m", "full"],
        }
        assert stub.calls == 0
        assert summaries_of(client)["entries"] == []


#: **不是四档、也不等于「没给这一格」**的取值（C1 的那一格钉在这一批上）
NON_CHOICE_WINDOWS: tuple[Any, ...] = ("", "   ", "15M", 15)


def test_a_missing_or_null_window_falls_back_but_any_other_value_is_rejected(tmp_path):
    """**只有「键省略 / 给 `null`」落缺省档**，其余一律进四档校验（含空串）。

    这条钉的是 C1 那一格：`""` 与 `"   "` 是同一类「给了值但值不在四档里」，不许一个走
    200（悄悄替人挑一档）、另一个走 400。四个取值**在同一个场次里各打一次**（合并真应用
    启动，不是拆用例）：逐档断言 HTTP 400、`code`、`detail` 逐字（`value` 是**原样**那一格，
    不是 `strip()` 之后的），并确认一个请求都没发出去、一条记录都没写。
    """
    with live(tmp_path) as (client, runtime, transcriber, stub):
        start_recording(client)
        feed(runtime, transcriber, lines_for(5, span_seconds=120))

        for value in NON_CHOICE_WINDOWS:
            response = post(client, "summarize", {"window": value})
            assert response.status_code == 400, (
                f"window={value!r} 应回 400（取值非法），实测 {response.status_code}："
                f"{response.text}"
            )
            payload = response.json()
            assert payload["code"] == "invalid_setting", f"window={value!r} 的 code 不对"
            assert payload["detail"] == {
                "key": "window", "value": value, "legal": ["5m", "15m", "30m", "full"],
            }, f"window={value!r} 的 detail 不对"
            assert payload["message"], f"window={value!r} 少了给人看的那句话"

        assert stub.calls == 0, "前面几档没过时一个模型请求都不许发出去"
        assert summaries_of(client)["entries"] == [], "失败的那几次不记"


def test_a_missing_injection_is_blocked_at_the_egress_guard(tmp_path, _no_real_model_call):
    """**没注入假件时真出口被兜底拦住**（故意不注入的那一条自证用例）。

    `runtime.summary_client` 为空时生产路径会**静默现造一个真 `ModelClient`**，而
    `summary.base_url` 的配置默认值就是真地址 —— 于是「端点配齐了、忘了注入」这个组合
    会真的把窗口文本 POST 出去（花机主的 token）。`tests/conftest.py` 的 `_no_real_model_call`
    就是拦这一下的网：出口处按**目标地址**判，本机回环放行、其余当场抛
    `RealModelCallForbidden`。

    本条把三半都量出来：

      1. 兜底真的**在出口处拦下了那一笔**（夹具那个记账清单里恰好一条、就是被拦下的地址）；
      2. 这一趟总结**没有出门**，而是照既有口径把它报成 `summary_failed`(502)，`message` 与
         `detail.error` 里带着兜底的名字与被拦下的地址（判据是文字，人一眼看得出是**兜底**
         拦的，不是端点真的报错）；
      3. 放行那一档确实走到**真传输层**（回环上没人听的端口 → `URLError`，不是被桩吞掉），
         且判据按地址来（`localhost` / `127.0.0.1` 放行，其余一律拦）。
    """
    from meeting.summary import client as summary_client
    from tests.conftest import RealModelCallForbidden, is_local_endpoint

    with live(tmp_path, {"summary.base_url": "https://api.deepseek.com/v1"}) as (
        client, runtime, transcriber, _stub
    ):
        runtime.summary_client = None  # 「忘了注入」的现场：不设假件
        start_recording(client)
        feed(runtime, transcriber, lines_for(6, span_seconds=120))
        response = post(client, "summarize", {"window": "15m"})

        assert response.status_code == 502, response.text
        payload = response.json()
        assert payload["code"] == "summary_failed"
        assert "RealModelCallForbidden" in payload["message"]
        assert "api.deepseek.com" in payload["detail"]["error"]
        assert summaries_of(client)["entries"] == [], "被拦下的那一次不算成功产出"
        # 兜底的记账清单：恰好一笔、就是配置里那个真地址（它**没有**被发出去）
        assert _no_real_model_call == ["https://api.deepseek.com/v1/chat/completions"]

    # 判据按目标地址来：本机回环放行（将来接本地假端点不受影响），其余一律拦
    assert is_local_endpoint("http://127.0.0.1:1/v1/chat/completions")
    assert is_local_endpoint("http://localhost:8080/v1/chat/completions")
    assert not is_local_endpoint("https://api.deepseek.com/v1/chat/completions")
    # 放行那一档确实交给真传输层（回环上没人听的端口 → URLError，看门自己那一种异常不会出现：
    # 本机上这一格要么当场被拒、要么等到那 0.3 秒的连接上限，两种都只有真传输层给得出）
    with pytest.raises(urllib.error.URLError) as refused:
        summary_client.urllib.request.urlopen(
            "http://127.0.0.1:1/v1/chat/completions", timeout=REQUEST_REFUSED_TIMEOUT_SECONDS
        )
    assert not isinstance(refused.value, RealModelCallForbidden)


def test_a_stopped_transcript_is_reported_as_transcript_unavailable(tmp_path):
    """转写停了 / 没起过 → `transcript_unavailable`(503)、`detail = {"health": "stopped"}`。"""
    with live(tmp_path, health="stopped") as (client, runtime, transcriber, stub):
        start_recording(client)
        feed(runtime, transcriber, lines_for(5, span_seconds=120))
        response = post(client, "summarize", {"window": "15m"})
        assert response.status_code == 503
        payload = response.json()
        assert payload["code"] == "transcript_unavailable"
        assert payload["detail"] == {"health": "stopped"}
        assert payload["message"]
        assert stub.calls == 0
        assert summaries_of(client)["entries"] == []


def test_a_session_without_a_transcriber_backend_is_transcript_unavailable(tmp_path):
    """转写后端整个缺席（`null` 那一档）同样回 503，而不是把动作打崩。"""
    with live(tmp_path) as (client, runtime, transcriber, stub):
        start_recording(client)
        runtime.transcriber_backend = None
        response = post(client, "summarize", {"window": "15m"})
        assert response.status_code == 503
        assert response.json()["code"] == "transcript_unavailable"
        assert response.json()["detail"] == {"health": "stopped"}
        assert stub.calls == 0
        del transcriber


def test_a_behind_transcript_still_summarizes(tmp_path):
    """转写只是「落后」时照常出结果（503 那一档只管 `stopped`：落后不等于停）。"""
    with live(tmp_path, health="behind") as (client, runtime, transcriber, stub):
        start_recording(client)
        feed(runtime, transcriber, lines_for(9, span_seconds=180))
        response = post(client, "summarize", {"window": "15m"})
        assert response.status_code == 200, response.text
        assert response.json()["summary"]["lines"] == 9
        assert stub.calls == 1


def test_a_missing_model_endpoint_names_the_keys_and_sends_nothing(tmp_path):
    """模型端点没配 → `summary_failed`(502)、`detail.missing` 点名缺的键；请求数 0。"""
    with live(tmp_path) as (client, runtime, transcriber, stub):
        start_recording(client)
        feed(runtime, transcriber, lines_for(5, span_seconds=120))
        real_view = runtime._config_view
        runtime._config_view = lambda: ReadOnlyConfig(
            {
                "summary.base_url": "   ",
                "summary.api_key": "   ",
                "summary.model": "test-model",
            }
        )
        try:
            response = post(client, "summarize", {"window": "15m"})
        finally:
            runtime._config_view = real_view

        assert response.status_code == 502
        payload = response.json()
        assert payload["code"] == "summary_failed"
        assert payload["detail"] == {
            "missing": ["summary.base_url", "summary.api_key"],
        }
        assert payload["message"]
        assert stub.calls == 0
        assert summaries_of(client)["entries"] == []


def test_a_budget_exhaustion_is_reported_with_elapsed_and_attempts(tmp_path):
    """预算用尽 / 端点报错 / 解析连试带败 → `summary_failed`(502)、`{elapsed_seconds, attempts}`。"""
    from meeting.summary import SummaryError

    with live(tmp_path) as (client, runtime, transcriber, stub):
        start_recording(client)
        feed(runtime, transcriber, lines_for(5, span_seconds=120))
        runtime.summary_client = stub = LedgerClient(
            error=SummaryError("timeout", "假端点：按测试要求超时")
        )
        response = post(client, "summarize", {"window": "15m"})
        assert response.status_code == 502
        payload = response.json()
        assert payload["code"] == "summary_failed"
        assert set(payload["detail"]) == {"elapsed_seconds", "attempts"}
        assert payload["detail"]["attempts"] == 1, "端点的四档错误当场收工，不重放"
        assert isinstance(payload["detail"]["elapsed_seconds"], float)
        assert payload["message"]
        assert summaries_of(client)["entries"] == []


def test_every_failure_branch_leaves_the_revision_alone(tmp_path):
    """四种拒绝路径一个请求都不发、一条记录都不写、`revision` 一步不涨。"""
    with live(tmp_path) as (client, runtime, transcriber, stub):
        start_recording(client)
        feed(runtime, transcriber, lines_for(5, span_seconds=120))
        before = snapshot_of(client)

        assert post(client, "summarize", {"window": "7m"}).status_code == 400
        transcriber._health = "stopped"
        assert post(client, "summarize", {"window": "15m"}).status_code == 503
        transcriber._health = "normal"

        # 模型端点没配那一档：把只读配置视图换掉（真配置文件一个字都不动）。
        # 这里走的是**同一个读入口** `_config_view()` —— 缺的键由 `SummarySettings.missing`
        # 按 `SETTING_KEYS` 的次序点名。两格都交纯空白串：`_lookup()` 把「视图里没有这一格」
        # 与「值是 `None`」回落成代码默认值，而空白串照字典原样传下去，
        # `SummarySettings` 的 `strip()` 之后就是「没配」那一档（与配置文件里写空串同解）。
        real_view = runtime._config_view
        runtime._config_view = lambda: ReadOnlyConfig(
            {
                "summary.base_url": "   ",
                "summary.api_key": "   ",
                "summary.model": "test-model",
            }
        )
        try:
            assert post(client, "summarize", {"window": "15m"}).status_code == 502
        finally:
            runtime._config_view = real_view

        assert stub.calls == 0
        assert summaries_of(client)["entries"] == []
        assert snapshot_of(client)["revision"] == before["revision"]


def test_other_requests_are_not_blocked_while_summarizing(tmp_path):
    """总结进行期间，另一条 `GET /api/state` 仍在毫秒量级往返（复审 minor 6 的钉子）。

    那一趟模型调用是**同步**的（真客户端是 `urllib.request`）：跑在事件循环上时它把
    `/api/state`、三个 SSE 通道、心跳与健康节拍一起按住（复审实测：假客户端原地 sleep 3 秒，
    同一刻另一条 `GET /api/state` 等了 2.70 秒），而设计章 4.5 对 `summary-running` 那一档
    写的正是「其余照常」。

    本用例把那条路量出来：假客户端停在 `complete()` 里，此刻从**另一条线程**打一次
    `/api/state` 并计时。修好之后它是毫秒量级；把调用放回事件循环上时它会等于 `GATE_TIMEOUT_SECONDS`
    （5 秒），断在下面那条上限上。
    """
    with live(tmp_path) as (client, runtime, transcriber, _stub):
        start_recording(client)
        feed(runtime, transcriber, lines_for(20, span_seconds=300))
        gated = GatedClient()
        runtime.summary_client = gated
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            pending = pool.submit(post, client, "summarize", {"window": "15m"})
            assert gated.entered.wait(GATE_TIMEOUT_SECONDS), "假客户端没被调到，量不到这一幕"
            # 此刻那一次总结正停在工作线程里：另一条请求的往返就量这一瞬
            started = time.monotonic()
            state = client.get("/api/state")
            state_seconds = time.monotonic() - started
            assert state.status_code == 200, state.text
            assert state_seconds < STATE_PROBE_LIMIT_SECONDS, (
                f"总结期间 GET /api/state 往返 {state_seconds:.3f} 秒："
                "那一次模型调用又跑回事件循环上了"
            )
            gated.release.set()
            response = pending.result(timeout=GATE_TIMEOUT_SECONDS)
            assert response.status_code == 200, response.text
            assert response.json()["summary"]["lines"] == 20
        finally:
            gated.release.set()
            pool.shutdown(wait=False)


def test_a_wedged_client_is_cut_off_by_the_wall_clock_budget(tmp_path):
    """客户端的 `timeout_seconds` 不当真时，等待仍以 `summary.deadline_seconds` 为界。

    内核按那条预算现算每次请求的 `timeout_seconds`，而**等待上限**由
    `runtime._summary_wait()` 一并守住：到点放弃等待、照契约把 `{elapsed_seconds, attempts}`
    报成 `summary_failed`(502)。这里用一个真卡住的假客户端把这条线量出来。
    """
    with live(tmp_path, {"summary.deadline_seconds": SHORT_DEADLINE_SECONDS}) as (
        client, runtime, transcriber, _stub
    ):
        start_recording(client)
        feed(runtime, transcriber, lines_for(6, span_seconds=120))
        wedged = GatedClient()
        runtime.summary_client = wedged
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            started = time.monotonic()
            response = pool.submit(post, client, "summarize", {"window": "15m"}).result(
                timeout=STUCK_RESPONSE_LIMIT_SECONDS
            )
            elapsed = time.monotonic() - started
            # 等待层按那条预算收工（内核那边同样卡着，两层用的是同一条线）
            assert elapsed < STUCK_RESPONSE_LIMIT_SECONDS
            assert response.status_code == 502, response.text
            payload = response.json()
            assert payload["code"] == "summary_failed"
            assert set(payload["detail"]) == {"elapsed_seconds", "attempts"}
            assert payload["detail"]["attempts"] == 1
            assert isinstance(payload["detail"]["elapsed_seconds"], float)
            assert summaries_of(client)["entries"] == []
        finally:
            wedged.release.set()
            pool.shutdown(wait=False)


# ---------------------------------------------------------------------------
# 只读：快照一个字都不变
# ---------------------------------------------------------------------------


def test_summarize_does_not_touch_the_snapshot(tmp_path):
    """打一次 `summarize` 前后两次快照：顶层十键齐、逐键逐字相同（`summary` 不在里面）。"""
    with live(tmp_path) as (client, runtime, transcriber, _stub):
        start_recording(client)
        feed(runtime, transcriber, lines_for(30, span_seconds=600))

        before = snapshot_of(client)
        response = post(client, "summarize", {"window": "15m"})
        assert response.status_code == 200, response.text
        after = snapshot_of(client)

        assert set(after) == TOP_LEVEL_KEYS
        assert "summary" not in after
        assert after["revision"] == before["revision"]
        # 场次时长每秒都在走，比不了的那一格单独排除；其余键逐字相同
        before_session = dict(before["session"])
        after_session = dict(after["session"])
        before_session.pop("elapsed_seconds")
        after_session.pop("elapsed_seconds")
        assert after_session == before_session
        assert {key: value for key, value in after.items() if key != "session"} == {
            key: value for key, value in before.items() if key != "session"
        }


def test_the_two_new_codes_do_not_leak_into_the_snapshot(tmp_path):
    """失败档与成功档都不写快照、不广播：连打四次之后快照仍是十键、`revision` 不动。"""
    with live(tmp_path) as (client, runtime, transcriber, _stub):
        start_recording(client)
        feed(runtime, transcriber, lines_for(8, span_seconds=200))
        before = snapshot_of(client)["revision"]
        for _ in range(2):
            assert post(client, "summarize", {"window": "15m"}).status_code == 200
            assert post(client, "summarize", {"window": "7m"}).status_code == 400
        assert snapshot_of(client)["revision"] == before


# ---------------------------------------------------------------------------
# GET /api/summaries
# ---------------------------------------------------------------------------


def test_summaries_returns_the_successful_runs_in_order(tmp_path):
    """`GET /api/summaries` 取回本场按过的、成功产出的那几次，按时间先后。"""
    with live(tmp_path) as (client, runtime, transcriber, _stub):
        start_recording(client)
        feed(runtime, transcriber, lines_for(40, span_seconds=600))

        first = post(client, "summarize", {"window": "5m"})
        assert first.status_code == 200, first.text
        second = post(client, "summarize", {"window": "full"})
        assert second.status_code == 200, second.text

        listing = summaries_of(client)
        assert set(listing) == {"session_id", "entries"}
        assert listing["session_id"] == runtime.snapshot.session.id
        entries = listing["entries"]
        assert [entry["window"] for entry in entries] == ["5m", "full"]
        assert [entry["window_label"] for entry in entries] == ["5 分钟", "整场"]
        for entry in entries:
            # 记录那一条按 `GET /api/summaries` 的口径：应答体里那七格，**没有 `lines`**
            assert set(entry) == ENTRY_KEYS
            assert entry["what"] and entry["where"] and entry["lost"]
        # `at` 是「按下的那一刻」定下来的，此后取多少次都是同一个值
        assert entries[0]["at"] == first.json()["summary"]["at"]
        assert entries[1]["at"] == second.json()["summary"]["at"]
        assert summaries_of(client)["entries"] == entries


def test_summaries_is_empty_before_anyone_presses(tmp_path):
    """一次都没按过时 `entries` 是空数组；连场次都没有时 `session_id` 为 `null`。"""
    with live(tmp_path) as (client, runtime, transcriber, _stub):
        idle = summaries_of(client)
        assert idle == {"session_id": None, "entries": []}

        start_recording(client)
        inside = summaries_of(client)
        assert inside["session_id"] == runtime.snapshot.session.id
        assert inside["entries"] == []


def test_summaries_does_not_touch_the_revision(tmp_path):
    """读一次记录是纯读：`revision` 原地不动、快照十键照旧。"""
    with live(tmp_path) as (client, runtime, transcriber, _stub):
        start_recording(client)
        feed(runtime, transcriber, lines_for(10, span_seconds=300))
        assert post(client, "summarize", {"window": "15m"}).status_code == 200
        before = snapshot_of(client)
        assert summaries_of(client)["entries"]
        after = snapshot_of(client)
        assert after["revision"] == before["revision"]
        assert set(after) == TOP_LEVEL_KEYS


def test_only_successful_runs_are_recorded(tmp_path):
    """失败的那几次不记（没有「那几条」可记）：四次失败之后记录仍是空的。"""
    with live(tmp_path, health="stopped") as (client, runtime, transcriber, _stub):
        start_recording(client)
        feed(runtime, transcriber, lines_for(6, span_seconds=120))
        for _ in range(2):
            assert post(client, "summarize").status_code == 503
        assert post(client, "summarize", {"window": "7m"}).status_code == 400
        assert summaries_of(client)["entries"] == []


def test_records_are_forgotten_when_the_next_session_starts(tmp_path):
    """新的一场 `start` 把记录表清空（上一场那几次不跟着进这一场）。"""
    with live(tmp_path) as (client, runtime, transcriber, _stub):
        start_recording(client)
        feed(runtime, transcriber, lines_for(20, span_seconds=400))
        assert post(client, "summarize", {"window": "15m"}).status_code == 200
        assert len(summaries_of(client)["entries"]) == 1

        assert post(client, "stop").status_code == 200
        wait_for_done(client)
        # 结束档里记录还在（场次没换）
        assert len(summaries_of(client)["entries"]) == 1

        start_recording(client)
        assert summaries_of(client)["entries"] == []


def test_summarize_works_in_the_alert_state(tmp_path):
    """`alert` 态照样可用（逐态可用性表：`recording` / `alert` 两态 200）。"""
    with live(tmp_path, {"alert.silence_seconds": 1, "alert.recover_seconds": 1}) as (
        client, runtime, transcriber, _stub
    ):
        start_recording(client)
        feed(runtime, transcriber, lines_for(12, span_seconds=240))
        runtime.capture_backend.inject_fault(
            source="loopback", fault="silence", after_seconds=0.0, duration_seconds=60.0
        )
        wait_until(lambda: snapshot_of(client)["top_state"] == "alert", what="进 alert")
        response = post(client, "summarize", {"window": "15m"})
        assert response.status_code == 200, response.text
        assert response.json()["summary"]["lines"] == 12


def test_the_summaries_endpoint_keeps_the_middleware_guards(tmp_path):
    """新端点落在既有三道防线的覆盖范围内：`/api/*` 一律 `no-store`。"""
    with live(tmp_path) as (client, _runtime, _transcriber, _stub):
        response = client.get("/api/summaries")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
