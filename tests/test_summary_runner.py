"""`meeting/summary/runner.py` 的契约用例：一次会中总结的编排、deadline 与失败分档。

盯的是六件事（对应任务书的完成判定与给复审的自检项）：

  - **成功那一档**：窗口切好、提示词装配好、端点答回那几条、三小段归位；
  - **空数组合法**：`{"bullets": []}` 照成功那一档收场（`ok=True` 且三段都是空的）；
  - **窗口里一行都没有**：同样 `ok=True`，并且**一个请求都不发**；
  - **配置缺键**：`ok=False`、`detail["missing"]` 点名缺的键，请求**在出门前就停住**；
  - **deadline 落在墙钟预算上**：用假钟量 —— 总耗时贴近那条 deadline，
    而不是「尝试次数 × 单次超时」；报出来的 `elapsed_seconds` 与实测值对得上；
  - **失败分档的形状**：`{"elapsed_seconds", "attempts"}` 两格恒在，端点的错误原文进 `error`。

假客户端把每次调用的超时与消息都记下来，因此「每次请求的超时是剩余预算」这件事也量得到。
全部用例都不碰网络，单条压在看门线以内。
"""

from __future__ import annotations

import time

import pytest

from meeting.summary.client import SummaryError
from meeting.summary.runner import (
    WINDOW_CHOICES,
    WINDOW_LABELS_BY_CHOICE,
    run_summary,
)
from meeting.summary.settings import SummarySettings
from meeting.summary.window import BufferedLine

#: 用例的墙钟上限（与 `tests/conftest.py` 那道看门同一个数）
CASE_WALL_CLOCK_LIMIT_SECONDS = 5.0

#: 样本的锚点时刻：**取此刻**。窗口按契约锚在**缓冲末行的 `ts`** 上（`run_summary()` 恒传的
#: 当前墙钟只在缓冲为空时兜底），样本因此都贴着此刻造，免得撞上「末行早于此刻」那一条口径。
ANCHOR_TS = time.time()

#: 一段可用的端点配置（用例只覆盖关心的那一格）
READY_SETTINGS = SummarySettings(
    base_url="https://api.example.invalid/v1",
    api_key="test-key-not-a-real-one",
    model="example-model",
    deadline_seconds=25.0,
    max_attempts=3,
    default_window="15m",
)

#: 一份三小段齐备的模型输出
GOOD_CONTENT = (
    '{"bullets": ['
    '"【讲什么】在过下个季度的排期", '
    '"【讲到哪】第三项的负责人还没定", '
    '"【丢掉什么】21:33 远端有一句识别超时"'
    "]}"
)


def _line(seq: int, ts: float, text: str, source: str = "mic") -> BufferedLine:
    """造一条缓冲里的行。"""
    return BufferedLine(seq=seq, ts=ts, source=source, text=text, kind="line")


def _window_lines(count: int = 3, *, step: float = 1.0) -> list[BufferedLine]:
    """造一小段窗口样本。"""
    return [_line(index, ANCHOR_TS + index * step, f"第 {index} 句") for index in range(count)]


class FakeClock:
    """假单调钟：`advance()` 往前走，`__call__` 读当前值（注入 `monotonic` 用）。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = float(start)

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)

    def __call__(self) -> float:
        return self.value


class FakeClient:
    """假端点客户端：按剧本逐次交回内容（`SummaryError` 也算一条剧本）。

    `sleep` 是**扮演端点自己花掉的时间**（真端点十几秒才回来，这里用一个极小的数
    替它把那段墙钟走一遍）。它等的是时间本身，因此这一处 `time.sleep` 有意留着：
    量的是 deadline 在真钟下的收敛。
    """

    def __init__(self, *results: object, sleep: float = 0.0) -> None:
        self.results = list(results)
        self.sleep = float(sleep)
        self.calls: list[tuple[list[dict[str, str]], float]] = []

    def complete(self, messages: list[dict[str, str]], *, timeout_seconds: float) -> str:
        self.calls.append(([dict(message) for message in messages], float(timeout_seconds)))
        if self.sleep:
            time.sleep(self.sleep)
        result = self.results[min(len(self.calls) - 1, len(self.results) - 1)]
        if isinstance(result, BaseException):
            raise result
        return str(result)


def test_success_fills_the_three_sections() -> None:
    """一次成功：三段归位、`bullets` 是原始的几条、`ok=True`、`attempts=1`。"""
    client = FakeClient(GOOD_CONTENT)

    outcome = run_summary(
        _window_lines(3),
        window="15m",
        settings=READY_SETTINGS,
        client=client,
        started_at="2025-09-19T21:30:00",
    )

    assert outcome.ok is True
    assert outcome.error is None
    assert outcome.detail == {}
    assert outcome.window == "15m"
    assert outcome.window_seconds == 900.0
    assert outcome.window_label == "15 分钟"
    assert outcome.lines == 3
    assert outcome.attempts == 1
    assert outcome.bullets == (
        "【讲什么】在过下个季度的排期",
        "【讲到哪】第三项的负责人还没定",
        "【丢掉什么】21:33 远端有一句识别超时",
    )
    assert outcome.what == ("在过下个季度的排期",)
    assert outcome.where == ("第三项的负责人还没定",)
    assert outcome.lost == ("21:33 远端有一句识别超时",)
    assert outcome.unlabeled == 0
    assert outcome.sections == {
        "what": ("在过下个季度的排期",),
        "where": ("第三项的负责人还没定",),
        "lost": ("21:33 远端有一句识别超时",),
    }
    assert outcome.elapsed_seconds < CASE_WALL_CLOCK_LIMIT_SECONDS
    assert len(client.calls) == 1


def test_success_sends_the_window_text_and_the_window_label() -> None:
    """发出去的消息里带着窗口正文与那一档的人话标签（装配这一步真的接上了）。"""
    client = FakeClient(GOOD_CONTENT)

    run_summary(
        _window_lines(2),
        window="5m",
        settings=READY_SETTINGS,
        client=client,
        started_at="2025-09-19T21:30:00",
    )

    messages, _timeout = client.calls[0]
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "【最近 5 分钟的转写】" in messages[1]["content"]
    assert "第 0 句 · 本地" in messages[1]["content"]
    assert "会议开始时刻：2025-09-19T21:30:00" in messages[1]["content"]


def test_window_slicing_only_feeds_the_tail() -> None:
    """窗口是按时间戳切的：十分钟前那两句**不进**提示词。

    样本按「十分钟前的两句 → 两分钟前那三句」排开；末行在 `ANCHOR_TS − 118`，
    5 分钟窗口的边界落在它往前 300 秒处，那两句更早的因此整条落在窗口外。
    """
    lines = [
        _line(0, ANCHOR_TS - 600, "十分钟前的一句"),
        _line(1, ANCHOR_TS - 599, "另一句更早的"),
    ] + [_line(index + 2, ANCHOR_TS - 120 + index, f"最近第 {index} 句") for index in range(3)]
    client = FakeClient(GOOD_CONTENT)

    outcome = run_summary(lines, window="5m", settings=READY_SETTINGS, client=client)

    assert outcome.lines == 3
    assert "十分钟前的一句" not in client.calls[0][0][1]["content"]
    assert "最近第 2 句" in client.calls[0][0][1]["content"]


def test_full_window_takes_every_line() -> None:
    """`整场` 把缓冲里的全部行收进窗口（含那两句更早的），标签是「整场」。"""
    lines = [
        _line(0, ANCHOR_TS - 600, "十分钟前的一句"),
        _line(1, ANCHOR_TS - 599, "另一句更早的"),
    ] + [_line(index + 2, ANCHOR_TS - 120 + index, f"最近第 {index} 句") for index in range(3)]
    client = FakeClient(GOOD_CONTENT)

    outcome = run_summary(lines, window="full", settings=READY_SETTINGS, client=client)

    assert outcome.window == "full"
    assert outcome.window_seconds is None
    assert outcome.window_label == "整场"
    assert outcome.lines == 5
    assert "十分钟前的一句" in client.calls[0][0][1]["content"]


def test_a_stalled_transcript_still_gets_its_window() -> None:
    """转写停转时窗口照旧有内容：末行 `ts` 早于此刻 20 分钟，`run_summary()` 仍能拿到那几行。

    场景（C1 的回归钉）：实时转写停了一会儿，缓冲里最后一行已经是 20 分钟前的事。
    窗口的锚点是**缓冲末行的 `ts`**（task.md 的接口契约），`run_summary()` 恒传的当前墙钟
    只在缓冲为空时兜底。旧写法拿此刻当锚点，这里会切出 0 行：`ok=True`、`attempts=0`、
    一个请求都不发 —— 用户按了按钮却收到一个静默的空结果。
    """
    lines = [
        _line(index, ANCHOR_TS - 20 * 60.0 - (4 - index), f"停转前第 {index} 句")
        for index in range(5)
    ]
    client = FakeClient(GOOD_CONTENT)

    outcome = run_summary(
        lines, window="15m", settings=READY_SETTINGS, client=client, wall_clock=lambda: ANCHOR_TS
    )

    assert outcome.ok is True
    assert outcome.lines == 5
    assert outcome.attempts == 1
    assert len(client.calls) == 1
    assert "停转前第 4 句" in client.calls[0][0][1]["content"]


def test_empty_array_is_success() -> None:
    """`{"bullets": []}` 是**合法结果**：`ok=True`、三段都空、`attempts=1`（不重试）。"""
    client = FakeClient('{"bullets": []}')

    outcome = run_summary(_window_lines(3), window="15m", settings=READY_SETTINGS, client=client)

    assert outcome.ok is True
    assert outcome.error is None
    assert outcome.bullets == ()
    assert (outcome.what, outcome.where, outcome.lost) == ((), (), ())
    assert outcome.unlabeled == 0
    assert len(client.calls) == 1


def test_empty_window_sends_nothing_and_succeeds() -> None:
    """窗口里一行都没有：`ok=True`、三段空、**一个请求都不发**（界面给空态文案）。"""
    client = FakeClient(GOOD_CONTENT)

    outcome = run_summary([], window="15m", settings=READY_SETTINGS, client=client)

    assert outcome.ok is True
    assert outcome.error is None
    assert outcome.detail == {}
    assert outcome.lines == 0
    assert outcome.attempts == 0
    assert outcome.bullets == ()
    assert client.calls == []


def test_missing_configuration_stops_before_any_request() -> None:
    """配置缺键那一档：`ok=False`、`detail["missing"]` 点名缺的键、请求在出门前就停住。"""
    client = FakeClient(GOOD_CONTENT)
    settings = SummarySettings(base_url="", api_key="", model="example-model")

    outcome = run_summary(_window_lines(3), window="15m", settings=settings, client=client)

    assert outcome.ok is False
    assert outcome.detail == {"missing": ["summary.base_url", "summary.api_key"]}
    assert outcome.attempts == 0
    assert "summary.base_url" in (outcome.error or "")
    assert client.calls == []


def test_a_missing_window_falls_back_to_the_configured_default() -> None:
    """`window` 取值不在四档里时落配置的缺省档（动作层那一层已经拦过一次）。"""
    client = FakeClient(GOOD_CONTENT)
    settings = SummarySettings(
        base_url="https://api.example.invalid/v1",
        api_key="k",
        model="m",
        default_window="30m",
    )

    outcome = run_summary(_window_lines(2), window="7m", settings=settings, client=client)

    assert outcome.window == "30m"
    assert outcome.window_label == "30 分钟"
    assert outcome.window_seconds == 1800.0


def test_the_four_window_labels_match_the_design_wording() -> None:
    """四档的标签逐字对齐设计章 2.3 的四个按钮。"""
    assert WINDOW_CHOICES == ("5m", "15m", "30m", "full")
    assert WINDOW_LABELS_BY_CHOICE == {
        "5m": "5 分钟",
        "15m": "15 分钟",
        "30m": "30 分钟",
        "full": "整场",
    }


def test_parse_failure_retries_with_the_hint() -> None:
    """第一次输出不是 JSON：再试一次，第 2 次的消息末尾带上那句提示。"""
    client = FakeClient("我先说两句：这里没有 JSON", GOOD_CONTENT)

    outcome = run_summary(_window_lines(3), window="15m", settings=READY_SETTINGS, client=client)

    assert outcome.ok is True
    assert outcome.attempts == 2
    assert len(client.calls) == 2
    first_messages, _ = client.calls[0]
    second_messages, _ = client.calls[1]
    assert "上一次的输出不是合法 JSON" not in first_messages[-1]["content"]
    assert "上一次的输出不是合法 JSON" in second_messages[-1]["content"]


def test_all_unlabelled_bullets_retry() -> None:
    """一条固定标签都认不出来：算解析失败，再试一次（个别认不出不算）。"""
    client = FakeClient('{"bullets": ["漏了标签的一條", "又漏了一条"]}', GOOD_CONTENT)

    outcome = run_summary(_window_lines(3), window="15m", settings=READY_SETTINGS, client=client)

    assert outcome.ok is True
    assert outcome.attempts == 2


def test_partially_labelled_bullets_pass_through() -> None:
    """个别认不出的条目不算失败：它落进第一段，`unlabeled` 报出条数。"""
    client = FakeClient('{"bullets": ["【讲什么】有标签", "这一条漏了标签"]}')

    outcome = run_summary(_window_lines(3), window="15m", settings=READY_SETTINGS, client=client)

    assert outcome.ok is True
    assert outcome.attempts == 1
    assert outcome.what == ("有标签", "这一条漏了标签")
    assert outcome.unlabeled == 1


def test_parse_failures_exhaust_the_attempt_ceiling() -> None:
    """连试带败（三次都不是合法 JSON）：`ok=False`、`attempts` 是上限、`error` 说清是哪一档。"""
    client = FakeClient("不是 JSON", "还不是 JSON", "仍然不是 JSON")
    settings = SummarySettings(
        base_url="https://api.example.invalid/v1",
        api_key="k",
        model="m",
        deadline_seconds=60.0,
        max_attempts=3,
    )

    outcome = run_summary(_window_lines(3), window="15m", settings=settings, client=client)

    assert outcome.ok is False
    assert len(client.calls) == 3
    assert outcome.detail["attempts"] == 3
    assert "合法" in (outcome.error or "")
    assert set(outcome.detail) == {"elapsed_seconds", "attempts"}


def test_endpoint_error_stops_right_away() -> None:
    """端点那一档报错**当场收工**（照 V1：API 报错不再把同一个错误重放一遍）。"""
    client = FakeClient(SummaryError("bad_status", "HTTP 401：Authentication Fails"))

    outcome = run_summary(_window_lines(3), window="15m", settings=READY_SETTINGS, client=client)

    assert outcome.ok is False
    assert len(client.calls) == 1
    assert outcome.attempts == 1
    assert "HTTP 401" in (outcome.error or "")
    assert outcome.detail == {"elapsed_seconds": outcome.elapsed_seconds, "attempts": 1}


def test_timeout_of_the_endpoint_also_stops_right_away() -> None:
    """端点超时同样当场收工，`detail` 那两格照旧填上。"""
    client = FakeClient(SummaryError("timeout", "请求超时（25 秒）"))

    outcome = run_summary(_window_lines(3), window="15m", settings=READY_SETTINGS, client=client)

    assert outcome.ok is False
    assert len(client.calls) == 1
    assert outcome.detail["attempts"] == 1
    assert "timeout" in (outcome.error or "")


def test_each_attempt_gets_the_remaining_budget() -> None:
    """每次请求的超时是**剩余预算**：第一次是接近整条 deadline，第二次是减去上一次的花销。"""
    clock = FakeClock()

    class SpendingClient(FakeClient):
        def complete(self, messages, *, timeout_seconds):  # noqa: D102 - 假件
            self.calls.append(([dict(m) for m in messages], float(timeout_seconds)))
            clock.advance(10.0)
            return "不是 JSON"

    client = SpendingClient()
    settings = SummarySettings(
        base_url="https://api.example.invalid/v1",
        api_key="k",
        model="m",
        deadline_seconds=25.0,
        max_attempts=3,
    )

    outcome = run_summary(
        _window_lines(3), window="15m", settings=settings, client=client, monotonic=clock
    )

    # 25 秒预算：第一次拿满 25，花了 10 之后第二次拿 15，再花 10 之后只剩 5 秒 → 第三次拿 5
    assert [round(item[1], 3) for item in client.calls] == [25.0, 15.0, 5.0]
    assert outcome.ok is False
    assert outcome.attempts == 3
    assert outcome.elapsed_seconds == pytest.approx(30.0, abs=0.01)


def test_the_attempt_ceiling_is_reported_as_the_deadline_not_as_a_wall() -> None:
    """次数上限只是上限，真正的刹车是那条 deadline。

    每次 10 秒的假端点、25 秒的 deadline、上限 3 次：撞上限时**总共 30 秒**，
    而超预算那一档的判据是「回来的时候已经过了 deadline」（第一次拿到 25 秒、
    第二次 15 秒，第三次只剩 5 秒 —— 那一次本来就发不太出去）。
    """
    clock = FakeClock()

    class SpendingClient(FakeClient):
        def complete(self, messages, *, timeout_seconds):  # noqa: D102 - 假件
            self.calls.append(([dict(m) for m in messages], float(timeout_seconds)))
            clock.advance(10.0)
            return "不是 JSON"

    client = SpendingClient()
    settings = SummarySettings(
        base_url="https://api.example.invalid/v1",
        api_key="k",
        model="m",
        deadline_seconds=25.0,
        max_attempts=3,
    )

    started = clock()
    outcome = run_summary(
        _window_lines(3), window="15m", settings=settings, client=client, monotonic=clock
    )
    measured = clock() - started

    # 上限用完了，但每一次拿到的超时都是**当时剩下的预算**：25 → 15 → 5
    assert len(client.calls) == settings.max_attempts
    assert [round(item[1], 3) for item in client.calls] == [25.0, 15.0, 5.0]
    assert measured == pytest.approx(30.0, abs=0.01)
    assert outcome.elapsed_seconds == pytest.approx(measured, abs=0.001)
    assert outcome.detail == {"elapsed_seconds": outcome.elapsed_seconds, "attempts": 3}
    assert "等了 30" in (outcome.error or "")


def test_budget_runs_out_and_the_report_says_how_long_it_waited() -> None:
    """预算用尽那一档：报出「等了多久、试了几次」，两格与假钟对得上。"""
    clock = FakeClock()

    class SpendingClient(FakeClient):
        def complete(self, messages, *, timeout_seconds):  # noqa: D102 - 假件
            self.calls.append(([dict(m) for m in messages], float(timeout_seconds)))
            clock.advance(9.0)
            return "不是 JSON"

    client = SpendingClient()
    settings = SummarySettings(
        base_url="https://api.example.invalid/v1",
        api_key="k",
        model="m",
        deadline_seconds=25.0,
        max_attempts=5,
    )

    outcome = run_summary(
        _window_lines(3), window="15m", settings=settings, client=client, monotonic=clock
    )

    # 9 + 9 + 9 = 27 秒：第三次回来时已经过了 deadline，因此停在第三次
    assert outcome.ok is False
    assert outcome.attempts == 3
    assert outcome.elapsed_seconds == pytest.approx(27.0, abs=0.01)
    assert outcome.detail == {"elapsed_seconds": pytest.approx(27.0, abs=0.01), "attempts": 3}
    assert "等了 27" in (outcome.error or "")
    assert "试了 3 次" in (outcome.error or "")


def test_a_deadline_already_spent_sends_nothing() -> None:
    """预算在第一次尝试之前就用光了：一个请求都不发，`attempts=0`、`ok=False`。"""
    clock = FakeClock()

    class SlowClock(FakeClock):
        def __call__(self) -> float:  # noqa: D102 - 假钟
            return self.value

    ticking = SlowClock()
    client = FakeClient(GOOD_CONTENT)

    def monotonic() -> float:
        # 第一次读（记开始时刻）之后立刻跳到预算之外
        ticking.advance(100.0)
        return ticking.value

    outcome = run_summary(
        _window_lines(3),
        window="15m",
        settings=READY_SETTINGS,
        client=client,
        monotonic=monotonic,
    )

    assert outcome.ok is False
    assert outcome.attempts == 0
    assert client.calls == []


def test_real_clock_converges_within_the_budget() -> None:
    """真钟下也收得住：假端点每次睡 0.05 秒，1 秒预算里最多试 3 次，总耗时压在看门线内。"""
    client = FakeClient("不是 JSON", sleep=0.05)
    settings = SummarySettings(
        base_url="https://api.example.invalid/v1",
        api_key="k",
        model="m",
        deadline_seconds=1.0,
        max_attempts=3,
    )

    started = time.monotonic()
    outcome = run_summary(_window_lines(3), window="15m", settings=settings, client=client)
    measured = time.monotonic() - started

    assert measured < CASE_WALL_CLOCK_LIMIT_SECONDS
    assert outcome.ok is False
    assert len(client.calls) == 3
    assert outcome.elapsed_seconds == pytest.approx(measured, abs=0.05)


def test_the_timeout_passed_to_the_endpoint_is_never_zero() -> None:
    """交给端点的超时恒是一个正数（哪怕剩余预算只剩一丝）。"""
    clock = FakeClock()

    class TightClient(FakeClient):
        def complete(self, messages, *, timeout_seconds):  # noqa: D102 - 假件
            self.calls.append(([dict(m) for m in messages], float(timeout_seconds)))
            clock.advance(24.99)
            return "不是 JSON"

    client = TightClient()
    settings = SummarySettings(
        base_url="https://api.example.invalid/v1",
        api_key="k",
        model="m",
        deadline_seconds=25.0,
        max_attempts=3,
    )

    run_summary(_window_lines(3), window="15m", settings=settings, client=client, monotonic=clock)

    assert client.calls
    assert all(item[1] > 0 for item in client.calls)


def test_the_outcome_carries_the_window_shape_on_failure_too() -> None:
    """失败那一档照常带着窗口的形状（界面不必再算一遍「这是哪一档、多少行」）。"""
    client = FakeClient(SummaryError("no_content", "content 是空的"))

    outcome = run_summary(_window_lines(4), window="30m", settings=READY_SETTINGS, client=client)

    assert outcome.ok is False
    assert outcome.window == "30m"
    assert outcome.window_label == "30 分钟"
    assert outcome.window_seconds == 1800.0
    assert outcome.lines == 4
    assert outcome.bullets == ()
