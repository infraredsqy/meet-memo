"""会中关掉的那一路不报警、关断即收场、重开干净开场（节点 `fd087e08`）的进程级用例。

判尺（`docs/design-intent.md` 第 1、9、17 条）：关掉某一路是机主的主动选择（那一路写静音），
不是「采不到」——致命级只留给真采不到的那一路；点关掉，横幅要当场收场；重开是干净开场，
不落「曾中断 / 曾关掉」的残留横幅。真故障（不开关直接 `lost` / `silence`）的既有判据一个
字节不动（`tests/test_e2e_silence_alert.py` 那一支照旧是真故障的回归线）。

五条用例各锁一条不变量：

  1. **已关路的静音帧不进静默判定**：关掉 mic 后注入 `silence`，静默阈值过去之后横幅
     不出现、胶囊停在「录音中」，另一路照常采着——切断的只是「已关路」这一格。
  2. **关断即收场**：一路真故障挂着致命横幅，关掉**另一路**横幅不动；关掉横幅上那一路，
     横幅当场清空、胶囊回「录音中」、该路落「已停止」且 `lost_since` 清空。
  3. **重开 = 干净开场**：重开已清掉横幅的那一路，直接回 `capturing`（不回 `lost`）、
     判定记录从零起算——随后注入 `silence`，阈值过半时还没有横幅（旧计数若还在，
     重开第一拍就会被拉回致命级），攒满阈值才照旧出警。
  4. **真故障照旧 + 换挂**：没被关过的那一路直接 `lost` / `silence` 照旧升致命级；
     两路都真在 `lost` 时关掉横幅上那一路，致命级**换挂**另一路（原文照搬）、顶层停在
     `alert`——真采不到的那一路不许跟着一起从界面上消失。
  5. **已关路的迟到流中断是余波不是故障**：先关掉某一路，再让一条 `SourceLost` 迟到
     （关掉前后在途的那条线程余波）——它不进告警、不把 `health` 写回 `lost`、判定记录里
     没有这一路（徽标不许被拉回「信号丢失」）。

服务端全走 dev 替身（`capture.backend = dev`，进程内 `live()`），一条真声卡都不碰；
静默阈值经配置调小（不改代码默认值，手法与 `tests/test_e2e_silence_alert.py` 同源）。
"""

from __future__ import annotations

import pytest

from tests._app_drivers import live, post, snapshot_of
from tests._helpers import wait_until

#: 慢档标记：进程内真运行时 + 真墙钟等待的收尾验收，与 `tests/test_api_actions.py` 同档。
pytestmark = pytest.mark.slow

#: 本文件把静默阈值**显式调小**到这个秒数（写进 `live()` 的配置；代码默认值仍是 180）。
#: 收口理由与 `tests/test_e2e_silence_alert.py` 的 `TEST_SILENCE_SECONDS` 同一条：单条用例
#: ≤ 5 秒的硬要求优先，判据的形状不变（阈值经配置显式写小）。
TEST_SILENCE_SECONDS = 1

#: 「静默阈值已过去」的观察窗（秒）：阈值 1s + 余量，dev 替身 0.1s 一帧，
#: 旧行为（已关路的静音帧照进判定）在这一窗内必已升致命级。
DANGER_WINDOW_SECONDS = 2.0

#: 「阈值过半还没有横幅」的观察点（秒）：旧计数若没清，重开 + 注入后不到阈值一半就会出警。
HALF_THRESHOLD_SECONDS = 0.5

#: 故障注入的存活时长（秒）：盖过整条用例的墙钟即可。
FAULT_DURATION_SECONDS = 60.0


def _source(snapshot: dict, source_id: str) -> dict:
    for item in snapshot["sources"]:
        if item["id"] == source_id:
            return item
    raise AssertionError(f"快照里没有 {source_id} 这一路：{snapshot['sources']}")


def _elapsed(snapshot: dict) -> float:
    return float(snapshot["session"]["elapsed_seconds"])


def test_a_closed_channel_never_raises_the_silence_alert(tmp_path):
    """已关路的静音帧不进静默判定：阈值过去之后横幅不出现、胶囊停在「录音中」。"""
    with live(tmp_path, {"alert.silence_seconds": TEST_SILENCE_SECONDS}) as (
        client, runtime, _home, _out
    ):
        assert post(client, "start").status_code == 200
        assert post(client, "set_source", {"source": "mic", "active": False}).status_code == 200
        snap = snapshot_of(client)
        assert _source(snap, "mic")["health"] == "stopped"
        assert snap["top_state"] == "recording"

        runtime.capture_backend.inject_fault(
            source="mic", fault="silence", after_seconds=0.0, duration_seconds=FAULT_DURATION_SECONDS
        )
        base = _elapsed(snapshot_of(client))
        # 等观察窗走完（以场次自己的秒数为钟：它跟墙钟一起走）
        waited = wait_until(
            lambda: (
                lambda s: s if _elapsed(s) >= base + DANGER_WINDOW_SECONDS else None
            )(snapshot_of(client)),
            what="观察窗（阈值 + 余量）走完",
        )
        # 已关路没把横幅拉起来，胶囊停在「录音中」
        assert waited["top_state"] == "recording", waited["alerts"]
        assert waited["alerts"]["critical"] is None, waited["alerts"]
        # 已关路如实停在「已停止」；另一路照常采着（有电平读数）
        assert _source(waited, "mic")["health"] == "stopped"
        other = _source(waited, "loopback")
        assert other["health"] == "capturing", other
        assert other["level_dbfs"] is not None, other
        # 判定记录里没有这一路：帧没进判定器（切断在帧入口，不是横幅出现后拦一遍）
        assert "mic" not in runtime.judge._tracks  # noqa: SLF001 - 就为钉「帧没进判定」这一格


def test_closing_the_alerting_source_collapses_the_banner(tmp_path):
    """关断即收场：关掉横幅上那一路，横幅当场清空、胶囊回「录音中」；关别路横幅不动。"""
    with live(tmp_path, {"alert.silence_seconds": TEST_SILENCE_SECONDS}) as (
        client, runtime, _home, _out
    ):
        assert post(client, "start").status_code == 200
        runtime.capture_backend.inject_fault(
            source="loopback", fault="silence", after_seconds=0.0,
            duration_seconds=FAULT_DURATION_SECONDS,
        )
        alerting = wait_until(
            lambda: (
                lambda s: s if s["top_state"] == "alert" else None
            )(snapshot_of(client)),
            what="loopback 静默进 alert",
        )
        assert alerting["alerts"]["critical"]["source"] == "loopback"
        assert _source(alerting, "loopback")["health"] == "lost"

        # 先关「另一路」（横幅不是它的）：横幅原样挂着、顶层停在 alert
        assert post(client, "set_source", {"source": "mic", "active": False}).status_code == 200
        snap = snapshot_of(client)
        assert snap["top_state"] == "alert", snap["alerts"]
        assert snap["alerts"]["critical"]["source"] == "loopback", snap["alerts"]
        assert _source(snap, "mic")["health"] == "stopped"

        # 再关横幅上那一路：横幅当场收场、胶囊回「录音中」、该路落「已停止」
        assert post(client, "set_source", {"source": "loopback", "active": False}).status_code == 200
        snap = snapshot_of(client)
        assert snap["top_state"] == "recording", snap["alerts"]
        assert snap["alerts"]["critical"] is None, snap["alerts"]
        closed = _source(snap, "loopback")
        assert closed["health"] == "stopped", closed
        assert closed["lost_since"] is None, closed
        # 这一路的判定记录（在告警记号、静默计数、抑制窗口）一并收场
        assert "loopback" not in runtime.judge._tracks  # noqa: SLF001


def test_reopening_a_cleared_source_starts_clean(tmp_path):
    """重开 = 干净开场：直接回 `capturing`、不落残留横幅，静默从零起算、攒满阈值才出警。"""
    with live(tmp_path, {"alert.silence_seconds": TEST_SILENCE_SECONDS}) as (
        client, runtime, _home, _out
    ):
        assert post(client, "start").status_code == 200
        runtime.capture_backend.inject_fault(
            source="loopback", fault="silence", after_seconds=0.0,
            duration_seconds=FAULT_DURATION_SECONDS,
        )
        wait_until(
            lambda: (
                lambda s: s if s["top_state"] == "alert" else None
            )(snapshot_of(client)),
            what="loopback 静默进 alert",
        )
        # 关掉（横幅当场收场），再重新打开
        assert post(client, "set_source", {"source": "loopback", "active": False}).status_code == 200
        assert post(client, "set_source", {"source": "loopback", "active": True}).status_code == 200
        snap = snapshot_of(client)
        # 干净开场：不回 `lost`、横幅不回来；判定记录从零起算（重开后帧照常进判定，
        # 但那是一条全新的记录——旧计数若没清，静默秒数还挂在旧记录上）
        assert snap["top_state"] == "recording", snap["alerts"]
        assert snap["alerts"]["critical"] is None, snap["alerts"]
        reopened = _source(snap, "loopback")
        assert reopened["health"] == "capturing", reopened
        # 重开后的第一帧落进判定器时，起的必须是一条全新的记录（旧计数若没清，
        # 静默秒数还挂在旧记录上）——帧 0.1s 一条，等它进来再读
        reopened_track = wait_until(
            lambda: runtime.judge._tracks.get("loopback"),  # noqa: SLF001 - 就为等这一格
            what="重开后的第一帧落进判定器",
        )
        assert reopened_track.in_alert is False, "重开不落「在告警」的旧记号"
        assert reopened_track.silent_seconds < TEST_SILENCE_SECONDS, "静默从零起算（旧计数已收走）"

        # 重开后再注入静默：判定照旧在跑（没有因重开被永久关掉），但计数从零起算——
        # 阈值过半时还没有横幅（旧计数若没清，重开第一拍就会被拉回致命级）
        runtime.capture_backend.inject_fault(
            source="loopback", fault="silence", after_seconds=0.0,
            duration_seconds=FAULT_DURATION_SECONDS,
        )
        base = _elapsed(snapshot_of(client))
        halfway = wait_until(
            lambda: (
                lambda s: s if _elapsed(s) >= base + HALF_THRESHOLD_SECONDS else None
            )(snapshot_of(client)),
            what="静默攒到阈值过半",
        )
        assert halfway["top_state"] == "recording", halfway["alerts"]
        assert halfway["alerts"]["critical"] is None, halfway["alerts"]
        # 攒满阈值：照旧升致命级（真故障的那套判据原样）
        alerting = wait_until(
            lambda: (
                lambda s: s if s["top_state"] == "alert" else None
            )(snapshot_of(client)),
            what="重开后的静默攒满阈值、照旧出警",
        )
        assert alerting["alerts"]["critical"]["source"] == "loopback", alerting["alerts"]


def test_a_late_source_lost_on_a_closed_route_stays_quiet(tmp_path):
    """已关路的迟到流中断是「关流余波」：不进告警、不把 `health` 写回 `lost`。"""
    with live(tmp_path, {"alert.silence_seconds": TEST_SILENCE_SECONDS}) as (
        client, runtime, _home, _out
    ):
        assert post(client, "start").status_code == 200
        assert post(client, "set_source", {"source": "mic", "active": False}).status_code == 200
        snap = snapshot_of(client)
        assert _source(snap, "mic")["health"] == "stopped"
        assert snap["top_state"] == "recording"

        # 关掉**之后**才来的一条流中断事实（关掉前后在途的那条读线程余波；dev 替身对
        # 已关路注入 `lost` 走的是同一条事实出口，真引擎上是 worker 的 `mark_lost()`）
        runtime.capture_backend.inject_fault(
            source="mic", fault="lost", after_seconds=0.0, duration_seconds=FAULT_DURATION_SECONDS
        )
        base = _elapsed(snapshot_of(client))
        waited = wait_until(
            lambda: (
                lambda s: s if _elapsed(s) >= base + DANGER_WINDOW_SECONDS else None
            )(snapshot_of(client)),
            what="观察窗（阈值 + 余量）走完",
        )
        # 余波没有把横幅拉起来，胶囊停在「录音中」
        assert waited["top_state"] == "recording", waited["alerts"]
        assert waited["alerts"]["critical"] is None, waited["alerts"]
        # 旧行为会把已关路的 `health` 写回 `lost`（徽标回「信号丢失」）——钉死不许
        assert _source(waited, "mic")["health"] == "stopped"
        # 判定记录里没有这一路：这条流中断事实没进判定器
        assert "mic" not in runtime.judge._tracks  # noqa: SLF001 - 就为钉「事实没进判定」这一格


def test_real_faults_still_alert_and_the_banner_resurfaces_the_other_lost_route(tmp_path):
    """真故障照旧；两路都真在 lost 时关掉横幅那一路，致命级换挂另一路、顶层停在 alert。"""
    with live(tmp_path, {"alert.silence_seconds": TEST_SILENCE_SECONDS}) as (
        client, runtime, _home, _out
    ):
        assert post(client, "start").status_code == 200
        # 真故障（不开关、直接 lost）：照旧立刻升致命级
        runtime.capture_backend.inject_fault(
            source="mic", fault="lost", after_seconds=0.0, duration_seconds=FAULT_DURATION_SECONDS
        )
        lost = wait_until(
            lambda: (
                lambda s: s if s["top_state"] == "alert" else None
            )(snapshot_of(client)),
            what="mic 流中断进 alert",
        )
        assert lost["alerts"]["critical"]["source"] == "mic", lost["alerts"]
        mic_message = lost["alerts"]["critical"]["message"]
        assert "流中断" in mic_message, mic_message

        # 另一路也真出事（静默）：致命级换成新起的那条（「同时只留一条」的既有口径）
        runtime.capture_backend.inject_fault(
            source="loopback", fault="silence", after_seconds=0.0,
            duration_seconds=FAULT_DURATION_SECONDS,
        )
        both = wait_until(
            lambda: (
                lambda s: s
                if (s["alerts"]["critical"] or {}).get("source") == "loopback"
                else None
            )(snapshot_of(client)),
            what="loopback 静默接管致命级",
        )
        assert _source(both, "mic")["health"] == "lost"

        # 关掉横幅上那一路（loopback）：mic 还真在 lost——致命级换挂 mic、顶层停在 alert
        assert post(client, "set_source", {"source": "loopback", "active": False}).status_code == 200
        snap = snapshot_of(client)
        assert snap["top_state"] == "alert", snap["alerts"]
        resurfaced = snap["alerts"]["critical"]
        assert resurfaced is not None, snap["alerts"]
        assert resurfaced["source"] == "mic", resurfaced
        assert resurfaced["message"] == mic_message, resurfaced
        assert _source(snap, "loopback")["health"] == "stopped", _source(snap, "loopback")
        assert _source(snap, "mic")["health"] == "lost", _source(snap, "mic")
