"""`tests/test_capture_gaps.py` —— 帧空洞的「留痕」链（gil-stall-hole 节点）。

问题（`.work/issues/specs/opening-window-false-capture-alarm.md:52-53` 实测）：录中发生
GIL 饿死 / 整进程冻结时，采帧与判停的线程**一起冻住**，解冻后服务侧的帧时刻立刻刷新，
`recording_alive` 全程 `true` —— 内容丢了 2.4s，记录上不留痕。

修法（设计已拍板）：**检测点在生产侧** —— 发帧线程每次产帧先与上一拍做差（墙钟不冻，
解冻后第一拍就得到真实空洞时长），超过既有窗口（`meeting/state.py` 的
`max(2.0s, 3 × frame_interval)`，公式不新造）记为一次空洞事件，**再**刷新时刻；记录走
心跳应答的新增字段 `capture_gaps`（`recording_alive` 的语义一个字不动）。

覆盖：

  ① 检测器纯逻辑（`meeting/capture/gaps.py`，时钟注入）：首帧前不记；窗内不记；
     超窗报一次；同一回合的后续拍不连记；`reset()` 清基准；窗口就是那把尺。
  ② 引擎刻度线接线（`_SourceWorker._emit_level`）：先报 `FrameGap` 再发电平帧；
     下一拍不连记。
  ③ 运行时记录（`ServiceRuntime._apply_frame_gap`）：两路同回合合并为一条、秒数正确；
     两个回合各算一条；最近列表有界（10 条）而累计数不设界；恢复后
     `recording_alive == true` 且空洞仍在；新一场清空记录；录音态之外不登记。
  ④ dev 假采集档的区分：`silence`（发零电平**帧**）不构成空洞；单路 `lost`（另一路
     帧照发）不构成空洞；`stall` / 两路真断流才构成 —— 端到端一条（慢档：真实停发
     3.5s 超过 2.0s 窗口，恢复后记录里一条、`recording_alive` 回 `true`）。

快档红线：除慢档那条端到端外，全部用注入时钟 / 直调事实出口，无 sleep、无真子进程。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import time
from pathlib import Path

import numpy as np
import pytest

from meeting.backends.base import FrameGap, LevelFrame, SourceFacts, SourceLost
from meeting.backends.dev import DevCaptureBackend
from meeting.capture.engine import _SourceWorker
from meeting.capture.gaps import FrameGapDetector
from meeting.state import frame_clock_window
from tests import _helpers as helpers

#: 空洞记录在心跳应答里的键（与 `ServiceRuntime.capture_gaps_view()` 的产出对应）
GAP_FIELDS = {"total_count", "total_seconds", "recent"}


@contextlib.contextmanager
def live(tmp_path: Path, values: dict | None = None):
    """进程内已启动的运行时 + ASGI 客户端（与 `test_heartbeat.py` 同一套挂法）。"""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    out_root = tmp_path / "out"
    out_root.mkdir(parents=True, exist_ok=True)
    config = {"capture.backend": "dev",
              "web.dist_dir": str(helpers.missing_dist_dir(home))}
    config.update(values or {})
    with helpers.app_client(home, values=config) as client:
        client.place_root = out_root
        yield client, client.app.state.runtime


def post(client, action: str, args: dict | None = None):
    body: dict = {"action": action}
    if args is not None or action == "start":
        body["args"] = helpers.start_args(action, args, client.place_root)
    return client.post("/api/actions", json=body, headers={"X-Meeting-Service": "1"})


def wait_until(predicate, *, timeout: float = 5.0, what: str = "条件"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError(f"等待超时（{timeout}s）：{what}（最后观察值 {last!r}）")


# ---------------------------------------------------------------------------
# ① 检测器纯逻辑（时钟注入）
# ---------------------------------------------------------------------------


class _GapLog:
    """记录 `on_gap` 调用的sink（`(seconds, detected)` 二元组列表）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[float, float]] = []

    def __call__(self, seconds: float, detected: float) -> None:
        self.calls.append((seconds, detected))


def make_detector(frame_interval: float = 0.5) -> tuple[FrameGapDetector, _GapLog]:
    log = _GapLog()
    return FrameGapDetector(frame_interval=frame_interval, on_gap=log), log


def test_detector_first_observe_only_sets_the_baseline():
    """① 首帧前无上一帧：只立基准、不报 —— 与「还没有第一帧」未知档互不干扰。"""
    detector, log = make_detector()
    assert detector.observe(now=1000.0) is None
    assert log.calls == []
    # 紧接着的一拍在窗内，同样不报
    assert detector.observe(now=1000.5) is None
    assert log.calls == []


def test_detector_reports_one_hole_per_freeze_round():
    """① 超窗报一次、下一拍不连记（同一空洞回合在源头就合成一拍一事）。"""
    detector, log = make_detector(frame_interval=0.5)
    detector.observe(now=1000.0)
    # 冻结 2.4s（窗口 2.0s）后的第一拍：报一次，秒数就是真实空洞时长
    reported = detector.observe(now=1002.4)
    assert reported == pytest.approx(2.4)
    assert log.calls == [(pytest.approx(2.4), 1002.4)]
    # 解冻后的常规节拍：上一拍已刷新，不再连记
    assert detector.observe(now=1002.9) is None
    assert detector.observe(now=1003.4) is None
    assert len(log.calls) == 1
    # 很久之后的第二次冻结：新的一回合
    assert detector.observe(now=1010.0) == pytest.approx(6.6)
    assert len(log.calls) == 2


def test_detector_ignores_gaps_inside_the_window_and_resets():
    """① 窗口就是 `frame_clock_window()` 那把尺（含 2.0s 下限）；`reset()` 清基准。"""
    detector, log = make_detector(frame_interval=0.5)
    assert detector.window == frame_clock_window(0.5) == 2.0
    detector.observe(now=1000.0)
    # 恰好等于窗口不算空洞（判停口径：`<= window` 算活着；空洞要**超**窗）
    assert detector.observe(now=1002.0) is None
    assert detector.observe(now=1002.05) is None
    assert log.calls == []
    detector.reset()
    # 基准清掉之后的第一拍重新立基准，跨过 reset 的间隔不追认
    assert detector.observe(now=2000.0) is None
    assert log.calls == []


# ---------------------------------------------------------------------------
# ② 引擎刻度线接线
# ---------------------------------------------------------------------------


class _LatestOnly:
    """最小源替身：只供 `latest_samples()`（`_emit_level` 不碰读侧与写侧）。"""

    def latest_samples(self) -> np.ndarray:
        return np.empty(0, dtype=np.int16)


def test_engine_tick_reports_the_gap_before_the_level_frame(monkeypatch):
    """② 发帧线程产帧：先报一次 `FrameGap`，**再**发电平帧；下一拍不连记。

    时钟注入：`engine` 与 `gaps` 两个模块的 `time.monotonic` 是同一个 `time` 模块对象，
    一处打桩两处生效（用例不 sleep、不真等窗口）。
    """
    facts: list[object] = []
    worker = _SourceWorker(
        "mic", _LatestOnly(), None,  # writer 只被写侧用到，本路径不碰
        emit=facts.append, frame_interval=0.5, active=True,
    )
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    worker._emit_level()
    clock[0] += 0.5
    worker._emit_level()
    assert not [f for f in facts if isinstance(f, FrameGap)], "窗内的常规节拍不报空洞"

    clock[0] += 3.7  # 冻结 3.7s（窗口 2.0s）后的第一拍
    worker._emit_level()
    gaps = [f for f in facts if isinstance(f, FrameGap)]
    assert len(gaps) == 1
    assert gaps[0].source == "mic"
    assert gaps[0].seconds == pytest.approx(3.7)
    assert gaps[0].detected == pytest.approx(1004.2)
    # 先报空洞、再发电平帧：同拍的三条事实里 `FrameGap` 排头
    tail = facts[-3:]
    assert [type(f).__name__ for f in tail] == ["FrameGap", "LevelFrame", "SourceFacts"]

    clock[0] += 0.5
    worker._emit_level()
    assert len([f for f in facts if isinstance(f, FrameGap)]) == 1, "解冻后的节拍不连记"


# ---------------------------------------------------------------------------
# ③ 运行时记录
# ---------------------------------------------------------------------------


def test_runtime_ledger_records_merges_and_keeps_totals(tmp_path):
    """③ 合并为一条、秒数正确；恢复后 `recording_alive == true` 且空洞仍在。"""
    with live(tmp_path) as (client, runtime):
        assert post(client, "start").status_code == 200
        wait_until(lambda: client.get("/api/heartbeat").json()["recording_alive"] is True,
                   what="recording_alive == True")

        empty = client.get("/api/heartbeat").json()["capture_gaps"]
        assert empty == {"total_count": 0, "total_seconds": 0.0, "recent": []}, "没报过就是空记录"

        # 同一次冻结、两路各报一条（区间重叠）：合并为一条，秒数取区间并集
        #（两条的发现时刻有毫秒级错位，并集比单条长出一点，但不翻倍）
        now = time.monotonic()
        runtime.apply_fact(FrameGap(source="mic", seconds=2.4, detected=now))
        runtime.apply_fact(FrameGap(source="loopback", seconds=2.4, detected=now + 0.01))
        merged = client.get("/api/heartbeat").json()["capture_gaps"]
        assert set(merged) == GAP_FIELDS
        assert merged["total_count"] == 1
        assert merged["total_seconds"] == pytest.approx(2.4, abs=0.05)
        assert len(merged["recent"]) == 1
        entry = merged["recent"][0]
        assert set(entry) == {"at", "seconds"}
        assert entry["seconds"] == pytest.approx(2.4, abs=0.05)
        assert dt.datetime.fromisoformat(entry["at"]).tzinfo is not None, "发生时刻是墙钟 ISO"

        # 很久之后的第二个回合：各算一条
        runtime.apply_fact(FrameGap(source="mic", seconds=3.0, detected=now + 60.0))
        twice = client.get("/api/heartbeat").json()["capture_gaps"]
        assert twice["total_count"] == 2
        assert twice["total_seconds"] == pytest.approx(5.4, abs=0.05)
        assert len(twice["recent"]) == 2

        # `recording_alive` 的语义一个字不动：现在采得到就是 true，空洞照旧在记录里
        beat = client.get("/api/heartbeat").json()
        assert beat["recording_alive"] is True
        assert beat["capture_gaps"]["total_count"] == 2
        # 记录只在心跳域：不进快照、不涨 revision
        assert "capture_gaps" not in client.get("/api/state").json()


def test_runtime_recent_list_is_bounded_but_totals_keep_count(tmp_path):
    """③ 最近列表有界（至多 10 条，按发生先后排）、累计数不设界。"""
    with live(tmp_path) as (client, runtime):
        assert post(client, "start").status_code == 200
        wait_until(lambda: client.get("/api/heartbeat").json()["recording_alive"] is True,
                   what="recording_alive == True")
        base = time.monotonic()
        for index in range(12):
            runtime.apply_fact(
                FrameGap(source="mic", seconds=2.5, detected=base + index * 60.0)
            )
        view = client.get("/api/heartbeat").json()["capture_gaps"]
        assert view["total_count"] == 12
        assert view["total_seconds"] == pytest.approx(30.0)
        assert len(view["recent"]) == 10, "最近列表最多留 10 条"
        # 留下的是**最近**那 10 条（最早的两条被挤出列表，但累计数还在）
        assert view["recent"][0]["at"] <= view["recent"][-1]["at"]


def test_runtime_ignores_gaps_outside_a_recording_session(tmp_path):
    """③ 录音态之外不登记（空闲态来的空洞事实被事实分派拒绝在记录外）。"""
    with live(tmp_path) as (client, runtime):
        assert client.get("/api/heartbeat").json()["top_state"] == "idle"
        runtime.apply_fact(FrameGap(source="mic", seconds=9.0, detected=time.monotonic()))
        assert client.get("/api/heartbeat").json()["capture_gaps"]["total_count"] == 0


def test_runtime_gap_ledger_resets_with_the_session(tmp_path):
    """③ 新一场清空记录：空洞是「本场」的记录，上一场的不进这一场。"""
    with live(tmp_path) as (client, runtime):
        assert post(client, "start").status_code == 200
        wait_until(lambda: client.get("/api/heartbeat").json()["recording_alive"] is True,
                   what="recording_alive == True")
        runtime.apply_fact(FrameGap(source="mic", seconds=2.4, detected=time.monotonic()))
        assert client.get("/api/heartbeat").json()["capture_gaps"]["total_count"] == 1

        assert post(client, "stop").status_code == 200
        wait_until(
            lambda: (lambda s: s if s["ended"] and s["ended"]["phase"] == "done" else None)(
                client.get("/api/state").json()
            ),
            what="收尾停在 done",
        )
        assert post(client, "start").status_code == 200
        wait_until(lambda: client.get("/api/heartbeat").json()["recording_alive"] is True,
                   what="第二场 recording_alive == True")
        assert client.get("/api/heartbeat").json()["capture_gaps"] == {
            "total_count": 0, "total_seconds": 0.0, "recent": [],
        }


# ---------------------------------------------------------------------------
# ④ dev 假采集档的区分
# ---------------------------------------------------------------------------


def _kinds(received: list[object], kind: type) -> list[object]:
    return [fact for fact in received if isinstance(fact, kind)]


def test_dev_silence_keeps_the_beat_and_records_no_gap():
    """④ `silence` 发的是零电平**帧**、节拍没断：不构成空洞。"""
    backend = DevCaptureBackend()
    backend.inject_fault(source="mic", fault="silence", after_seconds=0.0, duration_seconds=10.0)
    received = backend.collect_frames(6, timeout=5.0)
    assert _kinds(received, LevelFrame), "silence 档照常发帧（零电平）"
    assert _kinds(received, FrameGap) == [], "帧没断就没有空洞"


def test_dev_single_route_lost_is_not_a_session_gap():
    """④ 单路 `lost`：另一路的帧照发，「任意存活路」的整档口径下不构成空洞。

    那一路的断流由 `SourceLost` 的致命级在报，空洞记录不重复记这句话。
    """
    backend = DevCaptureBackend()
    backend.inject_fault(source="loopback", fault="lost", after_seconds=0.0, duration_seconds=1.0)
    received = backend.collect_frames(6, timeout=5.0)
    assert _kinds(received, SourceLost), "断流那一路照旧报 SourceLost"
    mic_frames = [f for f in _kinds(received, LevelFrame) if f.source == "mic"]
    assert mic_frames, "另一路的电平帧照发"
    assert _kinds(received, FrameGap) == [], "整档没断就不算空洞"


@pytest.mark.slow
def test_dev_stall_records_one_merged_gap_end_to_end(tmp_path):
    """④ 端到端（慢档）：`stall` 停发 3.5s（超 2.0s 窗口）→ 恢复后记录里一条。

    dev 的发帧线程恢复后第一拍把真实空洞时长算出来，两路各报一条、服务侧合并成一条；
    `recording_alive` 回 `true` 且空洞仍在 —— 「留痕」与「现在采不采得到」各管一头。
    真实停发要超过 2.0s 的窗口才能被观测，这一条因此进慢档（收尾跑）。
    """
    with live(tmp_path) as (client, _runtime):
        assert post(client, "start").status_code == 200
        wait_until(lambda: client.get("/api/heartbeat").json()["recording_alive"] is True,
                   what="recording_alive == True")
        assert post(
            client, "dev_inject",
            {"source": "mic", "fault": "stall", "after_seconds": 0.0,
             "duration_seconds": 3.5},
        ).status_code == 200
        wait_until(
            lambda: client.get("/api/heartbeat").json()["recording_alive"] is False,
            timeout=6.0, what="停发进入窗口：recording_alive == False",
        )
        recovered = wait_until(
            lambda: (lambda hb: hb if hb["recording_alive"] is True else None)(
                client.get("/api/heartbeat").json()
            ),
            timeout=8.0, what="停发窗口结束：recording_alive 回 True",
        )
        view = recovered["capture_gaps"]
        assert view["total_count"] == 1, "同一回合（两路各报一条）合并为一条"
        assert view["total_seconds"] >= 3.5, "秒数是真实空洞时长"
        assert len(view["recent"]) == 1
        dt.datetime.fromisoformat(view["recent"][0]["at"])
