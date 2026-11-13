"""`tests/test_dev_backend.py` —— dev 采集替身（设计口径第六行；§3.4）。

**非产品路径**：dev 替身默认关闭，只有把 `capture.backend` 配成 `dev`（或 `MEETING_CAPTURE=dev`）
才会被解析到。它提供确定性合成电平（种子 = 模块级常量 `DEV_LEVEL_SEED`）与三档可注入故障。

覆盖（整文件慢档； 收尾轮按机主判尺删去时钟观察用例后，只剩这三条纯逻辑测试）：

  - 确定性（构造两个 `DevCaptureBackend()`、取前若干帧逐项比对，电平序列**逐帧相同**）；
  - **不写真实音频、`finalize()` 只登记**（把真实存在的场次目录交给替身，
    跑完目录里空无一物）；
  - `inject_fault()` 的参数校验：非法 `source` / `fault` / `duration_seconds` /
    `after_seconds` 一律抛 `ValueError`；非 dev 后端 `dev_inject` 回
    `action_unavailable`(404) 的那一档在 `tests/test_api_actions.py` 的
    `test_dev_inject_is_unavailable_on_a_non_dev_backend`。

三档故障（`silence` / `stall` / `lost`）的窗口语义、`after_seconds` 到点生效 / 恢复、
对同一路替换注入的时序行为**没有直测**（收尾轮删除，机主裁决不恢复）。
"""

from __future__ import annotations

import time

import pytest

from tests import _helpers as helpers
from meeting.backends.base import LevelFrame
from meeting.backends.dev import (
    DEV_FAULT_DEFAULT_SECONDS,
    DEV_FAULT_KINDS,
    DEV_LEVEL_SEED,
    DevCaptureBackend,
)

#: 慢档标记：`pytest.mark.slow` 的归位判据见 `docs/service.md`。
pytestmark = pytest.mark.slow

#: 显式等待上限（秒）
DEV_WAIT_TIMEOUT_SECONDS = 10.0
#: 故障窗口的长度（够观测「到点生效」与「到点恢复」；
#: 唤醒用例里还要盖住「累计到阈值 → 起告警」这一段，所以取 3 秒）
DEV_FAULT_WINDOW_SECONDS = 3.0
#: 停帧窗口：比帧时钟的 2.0 秒窗口长，才观测得到「HTTP 通着但录音停了」
DEV_STALL_WINDOW_SECONDS = 3.0
#: `lost` 的窗口：取得比恢复阈值 + 帧时钟窗口都长，才观测得到「窗口内这一路整段是断的」。
#:
#: **取值由指挥者授权收口（第七轮审查 m4）**：用户把「单条用例 ≤5s」定为硬要求，这里从时序
#: 允许的上界 5.0 收到 3.0 —— 仍然盖得住那两条（恢复阈值 1.0 秒 + 帧时钟窗口 2.0 秒），
#: 窗口里的采样点也仍然有 3 次以上。授权来源：5 秒红线（契约 §3.8 末表里这一项写的是
#: 「比恢复阈值 + 帧时钟窗口都长」，没有钉死 5.0）。
DEV_LOST_WINDOW_SECONDS = 3.0
#: `lost` 窗口里两次采样之间的间隔（比帧间隔宽一点，够看出这一路在不在发帧）
DEV_LOST_SAMPLE_INTERVAL_SECONDS = 0.15

#: 唤醒用例（`test_silence_fault_reaches_alert_then_recovers_with_a_short_threshold`）**自己那一个**
#: 静默窗口（秒）：这一条要连着等两段，窗口每多 1 秒，用例墙钟就跟着多 1 秒（起告警那一段与
#: 窗口重叠，窗口到点之后还要再累计一次恢复阈值）。取值仍按契约那条口径（「窗口要覆盖
#: 累计到阈值 → 起告警 → 窗口到点 → 再累计到恢复阈值」）：静默阈值 1.0 秒、恢复阈值 1.0 秒，
#: 2.0 秒给「起告警」留了整 1 秒余量（本机 dev 替身 0.1 秒一帧，余量是帧间隔的十倍）。
#: 进程内那三条（`dev_service` 那几条）仍用 `DEV_FAULT_WINDOW_SECONDS`，采样点一个没少。
#:
#: **取值由指挥者授权收口（第七轮审查 m4）**：授权来源同样是 5 秒红线 —— 这一条要连着等两段
#: 阈值累计，窗口长度直接进用例墙钟；判据的形状一字未改（仍然等 `alert` 起、等它恢复回
#: `recording`）。契约 §3.8 末表里这一项写的是「窗口要覆盖那整段」，没有钉死具体秒数。
DEV_ALERT_FAULT_WINDOW_SECONDS = 2.0


# ---------------------------------------------------------------------------
# 纯进程内：确定性、帧率、登记
# ---------------------------------------------------------------------------


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


def test_finalize_only_keeps_books(tmp_path):
    """**不写真实音频**：`finalize()` 只登记，盘上一份文件都不落地。

    「只登记」的判据落在两处：调用计数加一；把**真实存在的场次目录**交给替身之后，
    这一口子跑完目录里依旧空无一物（`iterdir()` 一个条目都没有）。此前这里只断言
    `_session_dir` 这个字符串还被替身记着 —— 那句话证明不了「没落音频」，因为整条用例
    并没有碰过盘。现在目录是真的（`tmp_path`），盘上有没有东西由 `iterdir()` 直接说话。
    （替身不再有 `transcode()`：转码归 `meeting/capture/transcode.py` 的独立工序，
    替身没有真实 PCM 可转。）
    """
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    backend = DevCaptureBackend()
    backend.start("both", str(session_dir))
    backend.stop()
    backend.finalize()
    assert backend.finalize_calls == 1
    # 登记走完之后场次目录仍在（替身只拿它登记），盘上一份音频都没有
    assert backend._session_dir == str(session_dir)  # noqa: SLF001 - 替身只把它记着，不拿它落盘
    assert list(session_dir.iterdir()) == [], [p.name for p in session_dir.iterdir()]


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


