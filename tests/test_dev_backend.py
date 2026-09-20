"""`tests/test_dev_backend.py` —— dev 采集替身（phase2 §5.1 第六行；§3.4）。

**非产品路径**：dev 替身默认关闭，只有把 `capture.backend` 配成 `dev`（或 `MEETING_CAPTURE=dev`）
才会被解析到。它提供确定性合成电平（种子 = 模块级常量 `DEV_LEVEL_SEED`）与三档可注入故障。

覆盖：

  - 确定性（构造两个 `DevCaptureBackend()`、取前若干帧逐项比对，电平序列**逐帧相同**）；
  - 100ms 一帧、`frame_interval == 0.1`（**类属性形态**）；
  - 三种故障语义：`silence` → 电平落到 −80 dBFS 以下并保持；`stall` → 两路事实帧**整体停发**
    （HTTP 照常应答）；`lost` → 直接来一条 `SourceLost`；
  - `after_seconds` 到点生效；`duration_seconds` 到点自动恢复（三档各断言一次）；
  - 对同一路再次注入**替换前一次注入**并按新的 `after_seconds` / `duration_seconds` 重新计时；
  - **不写真实音频、`finalize()` 只记账**；非 dev 后端 `dev_inject` 回 `action_unavailable`(404)。
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

import _helpers as helpers
from meeting.backends.base import LevelFrame
from meeting.backends.dev import (
    DEV_FAULT_DEFAULT_SECONDS,
    DEV_FAULT_KINDS,
    DEV_FRAME_INTERVAL_SECONDS,
    DEV_LEVEL_SEED,
    DEV_SILENCE_DBFS,
    DevCaptureBackend,
)

#: 显式等待上限（秒）
DEV_WAIT_TIMEOUT_SECONDS = 10.0
#: 故障窗口的长度（够观测「到点生效」与「到点恢复」；
#: 唤醒用例里还要盖住「累计到阈值 → 起告警」这一段，所以取 3 秒）
DEV_FAULT_WINDOW_SECONDS = 3.0
#: 唤醒用例里静默要持续到起告警，窗口得比「静默阈值 + 恢复阈值」长
DEV_ALERT_WINDOW_SECONDS = 30.0
#: 停帧窗口：比帧时钟的 2.0 秒窗口长，才观测得到「HTTP 通着但录音停了」
DEV_STALL_WINDOW_SECONDS = 4.0
#: `lost` 的窗口：取得比恢复阈值 + 帧时钟窗口都长，才观测得到「窗口内这一路整段是断的」
DEV_LOST_WINDOW_SECONDS = 5.0


# ---------------------------------------------------------------------------
# 纯进程内：确定性、帧率、记账
# ---------------------------------------------------------------------------


def test_seed_is_the_module_level_constant():
    """种子的唯一来源是模块级常量（不来自调用方、配置或环境变量）。"""
    assert isinstance(DEV_LEVEL_SEED, int)
    assert DevCaptureBackend().seed == DEV_LEVEL_SEED


def test_two_instances_produce_the_same_level_sequence():
    """同种子同帧序：两个实例前若干帧的电平序列**逐帧相同**。"""
    first = DevCaptureBackend()
    second = DevCaptureBackend()
    for source in ("mic", "loopback"):
        sequence_a = first.level_sequence(source, 12)
        sequence_b = second.level_sequence(source, 12)
        assert sequence_a == sequence_b
        assert len(sequence_a) == 12
    # 两路各自的序列不同（同一路才要求相同）
    assert first.level_sequence("mic", 5) != first.level_sequence("loopback", 5)


def test_frame_interval_is_a_class_attribute_of_one_tenth():
    """帧间隔的声明位是**类属性** `frame_interval`，dev 替身报 `0.1` 秒。"""
    assert DevCaptureBackend.frame_interval == 0.1
    assert DEV_FRAME_INTERVAL_SECONDS == 0.1
    assert DevCaptureBackend().frame_interval == 0.1


def test_dev_emits_frames_at_about_a_hundred_milliseconds():
    """起一路采集：两路各拿到 `LevelFrame`，约 100ms 一帧。"""
    backend = DevCaptureBackend()
    collected: list = []

    def sink(fact) -> None:
        collected.append(fact)

    backend.bind(sink)
    backend.start("both", "<dev-100ms>")
    try:
        time.sleep(0.55)
    finally:
        backend.stop()
    levels = [fact for fact in collected if isinstance(fact, LevelFrame)]
    assert len(levels) >= 4, f"0.55 秒里该有 4 帧以上：{len(levels)}"
    assert {fact.source for fact in levels} == {"mic", "loopback"}
    # 约 100ms 一帧：帧时间戳的间隔在 [0.05, 0.25] 之间
    stamps = [fact.ts for fact in levels if fact.source == "mic"]
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert gaps and all(0.05 <= gap <= 0.25 for gap in gaps), gaps


def test_finalize_and_transcode_only_keep_books():
    """**不写真实音频**：`finalize()` / `transcode()` 只记账。"""
    backend = DevCaptureBackend()
    session_dir = backend._session_dir  # noqa: SLF001 - 只为断言「没落任何音频文件」
    backend.start("both", "<dev-bookkeeping>")
    backend.stop()
    backend.finalize()
    backend.transcode()
    assert backend.finalize_calls == 1
    assert backend.transcode_calls == 1
    assert session_dir is None


def test_probe_reports_both_sources_ready():
    """两路探测都报 `ready`（合成电平随时可用），`device_name` 标明是替身。"""
    probes = {probe.source: probe for probe in DevCaptureBackend().probe()}
    assert set(probes) == {"mic", "loopback"}
    assert {probe.status for probe in probes.values()} == {"ready"}
    assert all(probe.device_name and probe.device_name.startswith("dev-") for probe in probes.values())


def test_inject_validates_source_and_fault():
    """非法 `source` / `fault` / `duration_seconds` 一律抛 `ValueError`（端点转 400）。"""
    backend = DevCaptureBackend()
    with pytest.raises(ValueError):
        backend.inject_fault(source="keyboard", fault="silence")
    with pytest.raises(ValueError):
        backend.inject_fault(source="mic", fault="explode")
    with pytest.raises(ValueError):
        backend.inject_fault(source="mic", fault="silence", duration_seconds=0)
    with pytest.raises(ValueError):
        backend.inject_fault(source="mic", fault="silence", after_seconds=-1)
    assert DEV_FAULT_KINDS == ("silence", "stall", "lost")
    assert DEV_FAULT_DEFAULT_SECONDS == 30.0


def test_fault_window_replaces_a_previous_injection_on_the_same_source():
    """同一路再次注入：**替换前一次注入**并按新的 `after_seconds` / `duration_seconds` 重新计时。"""
    backend = DevCaptureBackend()
    now = 1000.0
    backend.inject_fault(source="mic", fault="silence", after_seconds=0.0,
                         duration_seconds=100.0, now=now)
    assert backend.fault_state("mic", now + 0.1) == "silence"

    backend.inject_fault(source="mic", fault="lost", after_seconds=5.0,
                         duration_seconds=1.0, now=now)
    assert backend.fault_state("mic", now + 0.1) is None, "新的窗口在 5 秒之后才开始"
    assert backend.fault_state("mic", now + 5.1) == "lost"
    assert backend.fault_state("mic", now + 6.2) is None, "到点自动恢复"
    # 另一路没被这次注入牵连
    assert backend.fault_state("loopback", now + 5.1) is None


def test_stall_marks_both_sources():
    """`stall` 是「两路的事实帧整体停发」：两路都记上这档故障。"""
    backend = DevCaptureBackend()
    now = 500.0
    backend.inject_fault(source="mic", fault="stall", after_seconds=0.0,
                         duration_seconds=2.0, now=now)
    assert backend.active_faults(now + 0.1) == {"mic": "stall", "loopback": "stall"}
    assert backend.active_faults(now + 2.5) == {}


# ---------------------------------------------------------------------------
# 真服务：三档故障的语义、到点生效、到点恢复
# ---------------------------------------------------------------------------


@pytest.fixture()
def dev_service(tmp_path, service_process):
    """一个 dev 后端的真服务子进程（家目录与输出根都在临时目录里）。"""
    home = tmp_path / "home"
    home.mkdir()
    if not helpers.config_path_of(home).exists():
        helpers.write_config_json(home, {})
    proc = service_process(home=home, capture="dev", out_root=tmp_path / "out")
    proc.wait_ready()
    return proc


def post(proc, action: str, args: dict | None = None):
    body = {"action": action}
    if args is not None:
        body["args"] = args
    response = httpx.post(
        f"{proc.base_url}/api/actions",
        json=body,
        headers={"Content-Type": "application/json", "X-Meeting-Service": "1"},
        timeout=10.0,
        trust_env=False,
    )
    return response


def snapshot(proc) -> dict:
    return helpers.local_get(f"{proc.base_url}/api/state", timeout=5.0).json()


def wait_until(predicate, *, timeout: float = DEV_WAIT_TIMEOUT_SECONDS, what: str = "条件"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError(f"等待超时（{timeout}s）：{what}（最后观察值 {last!r}）")


def test_silence_fault_drops_the_level_below_minus_eighty_then_recovers(dev_service):
    """`silence`：电平落到 −80 dBFS 以下并保持；到点后回到正常合成电平。"""
    proc = dev_service
    assert post(proc, "start").status_code == 200
    wait_until(lambda: snapshot(proc)["sources"][0]["level_dbfs"] is not None, what="收到电平")

    assert post(
        proc, "dev_inject",
        {"source": "loopback", "fault": "silence", "after_seconds": 0.0,
         "duration_seconds": DEV_FAULT_WINDOW_SECONDS},
    ).status_code == 200

    silent = wait_until(
        lambda: (lambda s: s if (
            [item for item in s["sources"] if item["id"] == "loopback"][0]["level_dbfs"] or 0
        ) <= -80.0 else None)(snapshot(proc)),
        what="loopback 落到 −80 dBFS 以下",
    )
    loopback = [item for item in silent["sources"] if item["id"] == "loopback"][0]
    assert loopback["level_dbfs"] <= -80.0
    # 另一路照常
    assert [item for item in silent["sources"] if item["id"] == "mic"][0]["health"] == "capturing"

    # 到点自动恢复：电平回到 −80 以上
    wait_until(
        lambda: (lambda s: s if (
            [item for item in s["sources"] if item["id"] == "loopback"][0]["level_dbfs"] or -100.0
        ) > -80.0 else None)(snapshot(proc)),
        what="loopback 回到正常合成电平",
    )


def test_stall_fault_stops_frames_while_http_keeps_answering(dev_service):
    """`stall`：两路事实帧整体停发（HTTP 照常应答 → `recording_alive` 变假）；到点恢复。

    停发窗口取得比帧时钟窗口（2.0 秒）长，于是「HTTP 通着但录音停了」这一档确实可观测。
    """
    proc = dev_service
    assert post(proc, "start").status_code == 200
    wait_until(
        lambda: helpers.local_get(f"{proc.base_url}/api/heartbeat", timeout=2.0).json()[
            "recording_alive"
        ] is True,
        what="recording_alive == True",
    )

    assert post(
        proc, "dev_inject",
        {"source": "mic", "fault": "stall", "after_seconds": 0.0,
         "duration_seconds": DEV_STALL_WINDOW_SECONDS},
    ).status_code == 200

    stalled = wait_until(
        lambda: (lambda h: h if h["recording_alive"] is False else None)(
            helpers.local_get(f"{proc.base_url}/api/heartbeat", timeout=2.0).json()
        ),
        timeout=DEV_STALL_WINDOW_SECONDS,
        what="HTTP 通着但录音停了",
    )
    assert stalled["seconds_since_last_tick"] > 2.0
    # HTTP 照常应答：状态接口也照常 200
    assert helpers.local_get(f"{proc.base_url}/api/state", timeout=5.0).status_code == 200

    # 到点自动恢复：帧时钟刷新、`recording_alive` 回真
    wait_until(
        lambda: helpers.local_get(f"{proc.base_url}/api/heartbeat", timeout=2.0).json()[
            "recording_alive"
        ] is True,
        what="stall 解除后 recording_alive 回 True",
    )


def test_lost_fault_raises_a_source_lost_fact(dev_service):
    """`lost`：直接来一条 `SourceLost` —— 该路 `health` 变 `lost`（另一路照常）。"""
    proc = dev_service
    assert post(proc, "start").status_code == 200
    wait_until(lambda: snapshot(proc)["sources"][0]["level_dbfs"] is not None, what="收到电平")

    assert post(
        proc, "dev_inject",
        {"source": "loopback", "fault": "lost", "after_seconds": 0.0,
         "duration_seconds": DEV_LOST_WINDOW_SECONDS},
    ).status_code == 200

    lost = wait_until(
        lambda: (lambda s: s if (
            [item for item in s["sources"] if item["id"] == "loopback"][0]["health"] == "lost"
        ) else None)(snapshot(proc)),
        what="loopback health == lost",
    )
    assert lost["alerts"]["critical"]["source"] == "loopback"
    assert [item for item in lost["sources"] if item["id"] == "mic"][0]["health"] == "capturing"

    # 到点后事实帧恢复发送（帧时钟重新走起来）
    wait_until(
        lambda: helpers.local_get(f"{proc.base_url}/api/heartbeat", timeout=2.0).json()[
            "recording_alive"
        ] is True,
        what="lost 解除后帧时钟恢复",
    )


@pytest.fixture()
def dev_service_short_recovery(tmp_path, service_process):
    """一个「恢复阈值调小」的 dev 真服务（只有断流那一条用例用得上）。

    默认恢复阈值是 10 秒；窗口到点之后还要连着有信号那么久才算恢复，用例等不起，
    所以这里显式把它调到 1 秒（**只写在用例里，不改代码默认值**）。
    """
    home = tmp_path / "home-short-recovery"
    home.mkdir()
    helpers.write_config_json(home, {"alert.silence_seconds": 10, "alert.recover_seconds": 1})
    proc = service_process(home=home, capture="dev", out_root=tmp_path / "out-short-recovery")
    proc.wait_ready()
    return proc


def test_lost_fault_keeps_the_source_dark_for_the_whole_window(dev_service_short_recovery):
    """`lost` 的窗口里这一路**整段断流**：窗口内多个采样点都还是 `lost`；另一路全程照常。

    窗口一开始那条 `SourceLost` 之后一拍电平帧都不发（否则电平帧会把这一路喂活，
    对端据此判回 `restored`，而故障窗口还没到点）。窗口到点后重新发帧、经恢复阈值回到
    `capturing`（恢复阈值在本用例里显式调小，才等得到这一步）。
    """
    proc = dev_service_short_recovery
    assert post(proc, "start").status_code == 200
    wait_until(lambda: snapshot(proc)["sources"][0]["level_dbfs"] is not None, what="收到电平")

    injected_at = time.monotonic()
    assert post(
        proc, "dev_inject",
        {"source": "loopback", "fault": "lost", "after_seconds": 0.0,
         "duration_seconds": DEV_LOST_WINDOW_SECONDS},
    ).status_code == 200

    # 窗口内的多个采样点：这一路一直是断的，另一路一直是采着的，会话停在 alert
    samples = 0
    while time.monotonic() - injected_at < DEV_LOST_WINDOW_SECONDS - 0.3:
        sampled = snapshot(proc)
        by_id = {item["id"]: item for item in sampled["sources"]}
        assert by_id["loopback"]["health"] == "lost", (
            f"注入后第 {samples} 次采样：窗口还没到点，这一路该一直保持 lost（实际 "
            f"{by_id['loopback']['health']!r}）"
        )
        assert by_id["mic"]["health"] == "capturing", f"第 {samples} 次采样：另一路该照常采"
        assert by_id["mic"]["level_dbfs"] is not None
        assert sampled["top_state"] == "alert"
        samples += 1
        time.sleep(DEV_FRAME_INTERVAL_SECONDS * 2)
    assert samples >= 3, f"窗口里该采到 3 次以上（实际 {samples} 次）"

    # 窗口到点：这一路重新发帧，经恢复阈值走既有的恢复路径（该路 health 从 lost 换成
    # `restored`，`alerts.critical` 清空、`top_state` 回到 `recording`）；
    # 另一路（mic）全程 `capturing`，收尾时仍是它扛着帧时钟。
    recovered = wait_until(
        lambda: (lambda s: s if (
            s["top_state"] == "recording"
            and s["alerts"]["critical"] is None
            and [item for item in s["sources"] if item["id"] == "loopback"][0]["health"]
            == "restored"
        ) else None)(snapshot(proc)),
        what="lost 窗口到点后这一路走出 lost、会话回到 recording",
    )
    by_id = {item["id"]: item for item in recovered["sources"]}
    assert by_id["mic"]["health"] == "capturing"
    assert by_id["loopback"]["level_dbfs"] is not None, "这一路又开始报电平了"


def test_silence_fault_reaches_alert_then_recovers_with_a_short_threshold(tmp_path, service_process):
    """`silence` 到点后该路回到正常合成电平、连续有信号达恢复阈值 → 回 `recording`。

    注入的窗口要覆盖「累计到阈值 → 起告警 → 窗口到点 → 再累计到恢复阈值」这一整段。
    """
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {"alert.silence_seconds": 1, "alert.recover_seconds": 1})
    proc = service_process(home=home, capture="dev", out_root=tmp_path / "out")
    proc.wait_ready()
    assert post(proc, "start").status_code == 200

    assert post(
        proc, "dev_inject",
        {"source": "loopback", "fault": "silence", "after_seconds": 0.0,
         "duration_seconds": DEV_FAULT_WINDOW_SECONDS},
    ).status_code == 200
    alerted = wait_until(lambda: (lambda s: s if s["top_state"] == "alert" else None)(snapshot(proc)),
                         what="top_state == alert")
    assert alerted["alerts"]["critical"]["source"] == "loopback"
    assert alerted["session"]["frozen"] is False, "进 alert 不等于停录"

    recovered = wait_until(
        lambda: (lambda s: s if s["top_state"] == "recording" else None)(snapshot(proc)),
        what="注入结束后回到 recording",
    )
    assert recovered["alerts"]["critical"] is None


def test_dev_inject_is_unavailable_on_the_default_null_backend(tmp_path, service_process):
    """非 dev 后端 `dev_inject` 回 `action_unavailable`(404)。"""
    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {})
    proc = service_process(home=home, capture="null", out_root=tmp_path / "out")
    proc.wait_ready()
    response = post(proc, "dev_inject", {"source": "mic", "fault": "silence"})
    assert response.status_code == 404
    assert response.json()["code"] == "action_unavailable"
