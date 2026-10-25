"""`tests/test_summary_buffer.py` —— 会中总结那条内存缓冲的接线（`1987cb20` phase2）。

这一份盯的是 `phase2/task.md` 的「缓冲与生命周期」那一节，逐条对着完成判定第 3 条：

  1. **转写行经 `bind_transcriber()` 那个 sink 进缓冲**：假转写后端喂 N 行，
     `summarize` 拿到的窗口行数对得上，喂进去的正文原样在消息里；
  2. **只进一次**：`bind()` 被调两次之后，生产路径上喂进来的同一行只收一趟（`== 1`），
     另有一条对照证明「两个 sink 都被调时确实会收两遍」（`== 2`）—— 两条合起来，
     断言的方向才站得住；
  3. **`start_session()` 之后缓冲与记录都空**：两笔走**同一个入口**清
     （`_clear_session_summary()`），只清一边会留下「记录里的那几次还挂着、取回的窗口早没了」；
  4. **`shutdown()` 一并清掉**（会话级内存不跟着进程尾巴走）；
  5. **转写行不参与状态迁移**这条既有口径没被这一层碰坏：喂行不涨 `revision`。

假转写后端从注入点进（`runtime.transcriber_backend` + `bind()`），**一个真实端点请求都不发**。
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

import pytest

from meeting.backends.base import TranscriptLine
from meeting.summary import TranscriptBuffer
from tests import _helpers as helpers

#: 慢档标记：本文件与 `tests/test_api_actions.py` 同档（起真应用 + 真 lifespan）。
pytestmark = pytest.mark.slow

#: 假客户端答的那份合法输出（三条固定标签齐备）
GOOD_BULLETS = (
    '{"bullets": ["【讲什么】在过排期", "【讲到哪】说到第三项", "【丢掉什么】有一处缺口"]}'
)


class LedgerClient:
    """**登记的假总结客户端**（与 `test_summary_action.py` 同一副形状）。"""

    def __init__(self, *, content: str = GOOD_BULLETS) -> None:
        self.content = content
        self.calls = 0
        self.messages: list[dict[str, str]] = []

    def complete(self, messages, *, timeout_seconds: float) -> str:
        self.calls += 1
        self.messages = [dict(message) for message in messages]
        return self.content


class FakeTranscriber:
    """假转写后端：协议成员齐备，`start()` 什么都不起（喂行由用例自己经 sink 做）。"""

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


@contextlib.contextmanager
def live(tmp_path: Path, values: dict | None = None):
    """起一个进程内已启动的运行时，并把转写后端换成假件；`yield (client, runtime, backend, stub)`。"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    out_root = tmp_path / "out"
    out_root.mkdir(parents=True, exist_ok=True)
    config = {
        "capture.backend": "dev",
        "web.dist_dir": str(helpers.missing_dist_dir(home)),
        "summary.base_url": "http://127.0.0.1:1/v1",
        "summary.api_key": "test-key-not-used",
        "summary.model": "test-model",
    }
    config.update(values or {})
    with helpers.app_client(home, values=config) as client:
        client.place_root = out_root
        runtime = client.app.state.runtime
        backend = FakeTranscriber()
        runtime.transcriber_backend = backend
        backend.bind(runtime.bind_transcriber(backend), config=runtime._config_view())
        stub = LedgerClient()
        runtime.summary_client = stub
        yield client, runtime, backend, stub


def post(client, action: str, args: dict | None = None):
    body = {"action": action}
    if args is not None or action == "start":
        body["args"] = helpers.start_args(action, args, client.place_root)
    return client.post("/api/actions", json=body, headers={"X-Meeting-Service": "1"})


def a_line(index: int, *, ts: float | None = None) -> TranscriptLine:
    """一行转写（`ts` 不给就取此刻）。"""
    return TranscriptLine(
        ts=time.time() if ts is None else float(ts),
        source="mic" if index % 2 == 0 else "loopback",
        text=f"第 {index} 行内容",
        kind="line",
    )


def wait_until(predicate, *, timeout: float = 15.0, what: str = "条件"):
    """轮询等到谓词命中（收尾那几步与告警判定都是真墙钟上的事）。"""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError(f"等待超时（{timeout}s）：{what}（最后观察值 {last!r}）")


def start_session(client, *, mode: str | None = None) -> None:
    """开一场（收尾还没落定时 `start` 会被 `draining` 挡下，这里等到落定再开）。

    **`waiting` 算落定**（与运行时 `drain_has_settled()` 同口径）：第 4 步 `post` 按
    「结束录音」之后不自动拉起会后（R26），做完该做的就停在 `waiting`（＝等你动手），
    那一格正是收尾跑完时的正常读数。
    """
    wait_until(
        lambda: (lambda snap: True if snap["ended"] is None or all(
            step["status"] in ("done", "skipped", "failed", "waiting")
            for step in snap["ended"]["steps"]
        ) else None)(client.get("/api/state").json()),
        what="上一场收尾落定",
    )
    args = {} if mode is None else {"mode": mode}
    response = post(client, "start", args)
    assert response.status_code == 200, response.text


def feed(backend, lines) -> None:
    """经**生产路径的那一个 sink** 喂行（`bind()` 交给后端的就是它）。"""
    sink = backend.line_sink
    assert sink is not None, "假转写后端还没被 bind() 上，喂不进任何一行"
    for line in lines:
        sink(line)


# ---------------------------------------------------------------------------
# 收：转写行进缓冲
# ---------------------------------------------------------------------------


def test_transcript_lines_reach_the_buffer_through_the_sink(tmp_path):
    """喂 N 行进 sink → 缓冲里就是那 N 行（`seq` / `ts` / `source` / `text` 都照实）。"""
    with live(tmp_path) as (client, runtime, backend, stub):
        start_session(client)
        lines = [a_line(index) for index in range(12)]
        feed(backend, lines)

        buffered = runtime.summary_buffer.lines()
        assert len(buffered) == 12
        assert [item.seq for item in buffered] == list(range(12))
        assert [item.text for item in buffered] == [line.text for line in lines]
        assert [item.source for item in buffered] == [line.source for line in lines]
        assert [item.ts for item in buffered] == [line.ts for line in lines]
        assert all(item.kind == "line" for item in buffered)

        response = post(client, "summarize", {"window": "15m"})
        assert response.status_code == 200, response.text
        assert response.json()["summary"]["lines"] == 12
        assert stub.calls == 1
        # 喂进去的正文原样进了消息（渲染层只加时间戳与来源标签）
        assert "第 11 行内容" in stub.messages[-1]["content"]


def test_the_buffer_hands_out_a_copy(tmp_path):
    """`lines()` 交回的是副本：调用方排序切片都动不到缓冲里那一份。"""
    with live(tmp_path) as (client, runtime, backend, _stub):
        start_session(client)
        feed(backend, [a_line(0), a_line(1)])
        handed = runtime.summary_buffer.lines()
        handed.clear()
        assert len(runtime.summary_buffer) == 2
        assert len(runtime.summary_buffer.lines()) == 2


def test_the_window_count_matches_what_was_fed(tmp_path):
    """窗口行数对得上：19 行里 `5m` 只收最近 5 分钟那几行（行自己带 `ts`）。"""
    with live(tmp_path) as (client, runtime, backend, stub):
        start_session(client)
        now = time.time()
        lines = [a_line(index, ts=now - 60.0 * (18 - index)) for index in range(19)]
        feed(backend, lines)

        response = post(client, "summarize", {"window": "5m"})
        assert response.status_code == 200, response.text
        summary = response.json()["summary"]
        # 每步 60 秒、窗口 300 秒 → 末行 + 前 5 行 = 6 行
        assert summary["lines"] == 6
        assert "第 18 行内容" in stub.messages[-1]["content"]
        # 缓冲本身仍是全部 19 行（切窗口发生在读的时候）
        assert len(runtime.summary_buffer) == 19


# ---------------------------------------------------------------------------
# 只进一次：`bind()` 调两次
# ---------------------------------------------------------------------------


def test_two_binds_do_not_collect_the_same_line_twice(tmp_path):
    """`bind()` 被调两次之后，生产路径上喂进来的那一行只进缓冲一次。

    这里走的是**生产实际会发生的那条路**：换后端时旧后端手上的那个 sink 不再被调用，
    同一行只得一趟。断言的方向因此是「只收一遍」（`== 1`）—— 收两遍才是出事。
    """
    with live(tmp_path) as (client, runtime, backend, stub):
        start_session(client)
        first_sink = backend.line_sink

        again = FakeTranscriber()
        second_sink = runtime.bind_transcriber(again)
        again.bind(second_sink, config=runtime._config_view())
        assert second_sink is not first_sink

        # 一行经**当下这一个** sink 进来（后端每次 bind() 都换掉手上的出口，
        # 于是生产路径上同一行只会走一个 sink）
        line = a_line(0)
        again.line_sink(line)

        assert len(runtime.summary_buffer) == 1, "生产路径上同一行只该进缓冲一趟"
        assert [item.text for item in runtime.summary_buffer.lines()] == [line.text]
        response = post(client, "summarize", {"window": "15m"})
        assert response.status_code == 200, response.text
        assert response.json()["summary"]["lines"] == 1
        assert stub.calls == 1
        assert first_sink is not None


def test_the_two_sinks_would_double_count_if_both_were_invoked(tmp_path):
    """对照：两个 sink **都被调**时确实会收两遍 —— 这就是上一条要挡住的那一幕。

    上一条断言的是「只收一遍」，这一条把它的方向立住：同一行走两个出口时缓冲里真的会有两条。
    两个 sink 都调用是**假件的刻意走法**（真后端每次 `bind()` 都换掉手上的出口，走不到这里）；
    什么时候会有后端把新旧两个出口都留着调，缓冲里就会各收一条 —— `TranscriptBuffer` 是
    唯一的收口，它拦不住「同一行被喂两遍」这件事，因为那一行确实来了两趟。

    两条合起来才说明白：`== 1` 是生产路径的实情，`== 2` 是叠两层出口的后果。
    """
    with live(tmp_path) as (client, runtime, backend, _stub):
        start_session(client)
        first_sink = backend.line_sink
        again = FakeTranscriber()
        second_sink = runtime.bind_transcriber(again)
        again.bind(second_sink, config=runtime._config_view())

        line = a_line(0)
        first_sink(line)
        second_sink(line)
        assert len(runtime.summary_buffer) == 2, "叠两层出口时同一行会收两遍"
        # 两趟都是同一行：正文一字不差，只有入队序与墙钟不同 —— 这正是「多了一条」的样子
        buffered = runtime.summary_buffer.lines()
        assert [item.text for item in buffered] == [line.text, line.text]
        assert [item.seq for item in buffered] == [0, 1]
        assert client.get("/api/state").status_code == 200


def test_rebinding_the_backend_keeps_the_earlier_lines(tmp_path):
    """`reprobe` 那类重新 `bind()` 不把已经收下的行清掉，也不让后面几行进两遍。"""
    with live(tmp_path) as (client, runtime, backend, _stub):
        start_session(client)
        feed(backend, [a_line(0), a_line(1)])

        again = FakeTranscriber()
        sink = runtime.bind_transcriber(again)
        again.bind(sink, config=runtime._config_view())
        feed(again, [a_line(2), a_line(3)])

        assert [item.seq for item in runtime.summary_buffer.lines()] == [0, 1, 2, 3]
        assert [item.text for item in runtime.summary_buffer.lines()] == [
            "第 0 行内容", "第 1 行内容", "第 2 行内容", "第 3 行内容",
        ]


def test_feeding_lines_does_not_touch_the_revision(tmp_path):
    """转写行不参与状态迁移这条既有口径没被碰坏：喂行不涨 `revision`。"""
    with live(tmp_path) as (client, runtime, backend, _stub):
        start_session(client)
        before = runtime.revision
        feed(backend, [a_line(index) for index in range(5)])
        assert len(runtime.summary_buffer) == 5
        assert runtime.revision == before


# ---------------------------------------------------------------------------
# 生命周期：新的一场 / 关停
# ---------------------------------------------------------------------------


def test_start_session_clears_both_the_buffer_and_the_records(tmp_path):
    """新的一场 `start` 把缓冲与记录表**一起**清空（两笔走同一个入口）。"""
    with live(tmp_path) as (client, runtime, backend, _stub):
        start_session(client)
        feed(backend, [a_line(index) for index in range(8)])
        assert post(client, "summarize", {"window": "15m"}).status_code == 200
        assert len(runtime.summary_buffer) == 8
        assert len(runtime.summary_records) == 1

        assert post(client, "stop").status_code == 200
        # 结束档里两笔都还在（场次没换）
        assert len(runtime.summary_buffer) == 8
        assert len(runtime.summary_records) == 1

        start_session(client)
        assert len(runtime.summary_buffer) == 0
        assert runtime.summary_records == []
        # 可观察面同样归零：窗口里一行都没有 + 记录空
        listing = client.get("/api/summaries").json()
        assert listing["entries"] == []
        response = post(client, "summarize", {"window": "15m"})
        assert response.status_code == 200, response.text
        assert response.json()["summary"]["lines"] == 0


def test_the_clear_helper_empties_both_slots(tmp_path):
    """`_clear_session_summary()` 一处动两笔：缓冲与记录表同时归零。"""
    with live(tmp_path) as (client, runtime, backend, _stub):
        start_session(client)
        feed(backend, [a_line(0)])
        assert post(client, "summarize", {"window": "15m"}).status_code == 200
        assert len(runtime.summary_buffer) == 1 and len(runtime.summary_records) == 1

        runtime._clear_session_summary(reason="用例直接调一次")
        assert len(runtime.summary_buffer) == 0
        assert runtime.summary_records == []


def test_shutdown_clears_both_slots(tmp_path):
    """`shutdown()` 一并清掉两笔（会话级内存不跟着进程尾巴走）。"""
    with live(tmp_path) as (client, runtime, backend, _stub):
        start_session(client)
        feed(backend, [a_line(index) for index in range(4)])
        assert post(client, "summarize", {"window": "15m"}).status_code == 200
        assert len(runtime.summary_buffer) == 4
        assert len(runtime.summary_records) == 1

        import asyncio

        asyncio.run(runtime.shutdown())
        assert len(runtime.summary_buffer) == 0
        assert runtime.summary_records == []


# ---------------------------------------------------------------------------
# 缓冲这一层自己的性质（不进运行时的那些）
# ---------------------------------------------------------------------------


def test_the_runtime_uses_the_package_buffer_type(tmp_path):
    """运行时手里那个缓冲就是 `meeting.summary.TranscriptBuffer`（不是另写的一份）。"""
    with live(tmp_path) as (_client, runtime, _backend, _stub):
        assert isinstance(runtime.summary_buffer, TranscriptBuffer)
        assert len(runtime.summary_buffer) == 0


def test_records_stay_in_memory_only(tmp_path):
    """记录表只活在内存里：场次目录里不长出任何跟会中总结有关的文件。"""
    with live(tmp_path) as (client, runtime, backend, _stub):
        start_session(client)
        feed(backend, [a_line(index) for index in range(6)])
        assert post(client, "summarize", {"window": "15m"}).status_code == 200
        session_dir = Path(runtime.snapshot.session.dir)
        names = sorted(item.name for item in session_dir.iterdir())
        assert "session.json" in names
        assert not any("summar" in name.lower() for name in names), names

