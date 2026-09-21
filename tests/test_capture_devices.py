"""`tests/test_capture_devices.py` —— 设备模型与挑选策略（**纯逻辑，无硬件、无 ffmpeg**）。

被验的是 `meeting/capture/devices.py` 里那两段**从 V1 拷过来的内核**
（`mnotes_core/recorder.py:371-413` 与 `:444-465`）在换成「端点表 + 假试开函数」之后
行为是否还认得出原样：

  - V1 的候选筛选：同名 + `max_input_channels > 0`；
  - V1 的 `host_rank`：`WASAPI → DirectSound → MME → 其它(9)`；
  - V1 的两条短路口：「**同名候选只有一条 → 直接返回**」与「**全试完都不成 → 回落首个候选**」；
  - V1 的 loopback 三级：精确名 → `CABLE Input` 且不含 `16ch` → `default_speaker()`；
  - V2 补的三件事：没配名字时也挑（好让快照报得出真设备名）、抓取型虚拟端点不进候选、
    试开时读回样本判「有没有真实波形」。

端点表与试开函数都是本文件造的假件；真机那一侧不在自动化测试里验（真开设备需要机主点头）。
"""

from __future__ import annotations

import pytest

from meeting.backends.base import SOURCE_STATUS
from meeting.capture import devices as devices_module
from meeting.capture.devices import (
    HOST_RANK_FALLBACK,
    PROBE_FAILED,
    PROBE_READY,
    DeviceEndpoint,
    OpenAttempt,
    measure_samples,
    select_loopback,
    select_microphone,
)

# ---------------------------------------------------------------------------
# 假件
# ---------------------------------------------------------------------------

#: **同名多候选**的一张端点表：一块 Focusrite 在 DirectSound / WASAPI / MME 下同名，
#: 另有一条 MME 的 Realtek 与一条 `CABLE Output`（抓取型）。名字照本机实测原文，
#: MME 的截断名也照抄（V1 的注释里点过 Windows 会截断）。
MACHINE_INPUTS: tuple[DeviceEndpoint, ...] = (
    DeviceEndpoint(
        "Analogue 1 + 2 (2- Focusrite USB Audio)", "Windows WASAPI", "input", index=18,
        channels=2,
    ),
    DeviceEndpoint(
        "Analogue 1 + 2 (2- Focusrite USB Audio)", "Windows DirectSound", "input", index=8,
        channels=4,
    ),
    DeviceEndpoint("Analogue 1 + 2 (2- Focusrite US", "MME", "input", index=1, channels=4),
    DeviceEndpoint("Microsoft 声音映射器 - Input", "MME", "input", index=0, channels=2),
    DeviceEndpoint("CABLE Output (VB-Audio Virtual Cable)", "MME", "input", index=2,
                   channels=16),
    DeviceEndpoint(
        "CABLE Output (VB-Audio Virtual Cable)", "Windows WASAPI", "input", index=16,
        channels=2,
    ),
)

#: 本机 render 端点表（`soundcard.all_speakers()` 的实测原文；系统默认那条排第一）
MACHINE_RENDERS: tuple[DeviceEndpoint, ...] = (
    DeviceEndpoint("扬声器 (2- Focusrite USB Audio)", "", "render", index=0),
    DeviceEndpoint("CABLE Input (VB-Audio Virtual Cable)", "", "render", index=1),
)

#: 另一台机器上的形状：loopback 表里同时有 16ch 端点，它必须被挡掉
RENDERS_WITH_16CH: tuple[DeviceEndpoint, ...] = (
    DeviceEndpoint("扬声器 (Focusrite USB Audio)", "", "render", index=0),
    DeviceEndpoint("CABLE Input (16ch) (VB-Audio Virtual Cable)", "", "render", index=1),
    DeviceEndpoint("CABLE Input (VB-Audio Virtual Cable)", "", "render", index=2),
)

#: 一段有真实波形的样本
WAVE = [0, 120, -300, 44, 900, -12, 7, -400, 220, 0]
#: 一条没在走音频的虚拟线交回的样本（常数抖动：有值、没有变化）
DC_JITTER = [1, -1, 0, 1, 0, -1] * 20
#: 精确全零（本机 DirectSound 的 Focusrite 端点交回的就是这个）
SILENCE = [0] * 120


def open_with(frames: list[int]) -> OpenAttempt:
    """造一次「开成了」的试开结果（样本由调用方给）。"""
    return OpenAttempt(opened=True, sample=measure_samples(frames))


def refuse(reason: str = "Invalid sample rate [PaErrorCode -9997]") -> OpenAttempt:
    """造一次「开不了」的试开结果。"""
    return OpenAttempt(opened=False, error=reason)


def opener(mapping: dict[str, OpenAttempt], *, default: OpenAttempt | None = None):
    """按端点名查结果的假试开函数（没登记的名字走 `default`，缺省「开不了」）。"""
    calls: list[str] = []

    def test_open(endpoint: DeviceEndpoint) -> OpenAttempt:
        calls.append(endpoint.name)
        if endpoint.name in mapping:
            return mapping[endpoint.name]
        return default if default is not None else refuse("假件：这个名字没登记")

    test_open.calls = calls  # type: ignore[attr-defined]
    return test_open


# ---------------------------------------------------------------------------
# 样本判定（V2 补的那一步）
# ---------------------------------------------------------------------------


def test_measure_samples_reports_peak_and_step():
    """`measure_samples()` 量出峰值与相邻最大变化（试开判定的两个输入）。"""
    sample = measure_samples(WAVE)
    assert sample.peak == 900
    assert sample.max_step == 912  # |900 - (-12)|
    assert sample.frames == len(WAVE)


def test_measure_samples_of_an_empty_block():
    """空块：两个数都是 0、块数 0（不许在这里抛）。"""
    sample = measure_samples([])
    assert (sample.peak, sample.max_step, sample.frames) == (0, 0, 0)


def test_has_sound_needs_a_signal_that_moves():
    """判定要看样本：有波形算真，全零与常数抖动都不算。"""
    assert devices_module._has_sound(open_with(WAVE)) is True
    assert devices_module._has_sound(open_with(SILENCE)) is False
    assert devices_module._has_sound(open_with(DC_JITTER)) is False
    assert devices_module._has_sound(refuse()) is False
    # 开起来了但样本没读回来（`sample is None`）同样不算
    assert devices_module._has_sound(OpenAttempt(opened=True, sample=None)) is False


def test_attempt_reason_says_what_happened():
    """没选上的原因是人话，且分得清「开不了」与「能打开但没波形」。"""
    assert "开不了" in devices_module.attempt_reason(refuse())
    assert "Invalid sample rate" in devices_module.attempt_reason(refuse())
    flat = devices_module.attempt_reason(open_with(SILENCE))
    assert "能打开" in flat and "没有波形" in flat and "峰值 0" in flat


# ---------------------------------------------------------------------------
# host_rank（V1 `_resolve_mic_device` 里的那段）
# ---------------------------------------------------------------------------


def test_host_rank_follows_the_v1_table():
    """排序键：`WASAPI` → `DirectSound` → `MME` → 其余 `HOST_RANK_FALLBACK`(9)。"""
    assert HOST_RANK_FALLBACK == 9
    assert devices_module.host_rank("WASAPI") == 0
    assert devices_module.host_rank("Windows WASAPI") == 0
    assert devices_module.host_rank("DirectSound") == 1
    assert devices_module.host_rank("Windows DirectSound") == 1
    assert devices_module.host_rank("MME") == 2
    assert devices_module.host_rank("Windows WDM-KS") == 9
    assert devices_module.host_rank("") == 9
    # `host_api_rank` 是同一个函数的别名（老名字留着）
    assert devices_module.host_api_rank is devices_module.host_rank


def test_sort_candidates_uses_host_rank():
    """候选排序就是 `cands.sort(key=host_rank)`（只按 rank，稳定排序）。"""
    ordered = devices_module.sort_candidates(MACHINE_INPUTS)
    assert [devices_module.host_rank(item.host_api) for item in ordered] == sorted(
        devices_module.host_rank(item.host_api) for item in MACHINE_INPUTS
    )
    # WASAPI(0) 两条在最前、DirectSound(1) 一条随后、MME(2) 三条收尾
    assert [item.host_api for item in ordered] == [
        "Windows WASAPI", "Windows WASAPI", "Windows DirectSound",
        "MME", "MME", "MME",
    ]
    # 稳定排序：同一个 rank 内部保持端点表原来的相对次序
    shared = [item for item in ordered if item.host_api == "MME"]
    assert shared == [item for item in MACHINE_INPUTS if item.host_api == "MME"]


# ---------------------------------------------------------------------------
# 抓取型虚拟端点（V2 补的那一条）
# ---------------------------------------------------------------------------


def test_pseudo_capture_needs_both_tokens():
    """抓取型判据 = 两个模块级常量同时命中（`CABLE Output` + `VB-Audio Virtual`）。"""
    assert devices_module.is_pseudo_capture("CABLE Output (VB-Audio Virtual Cable)") is True
    assert devices_module.is_pseudo_capture("CABLE Output (VB-Audio Virtual ") is True
    # `CABLE Input` 是 render 端点（播出去的那一侧），不在抓取型判据里
    assert devices_module.is_pseudo_capture("CABLE Input (VB-Audio Virtual Cable)") is False
    # 真实声卡的端点一条都不该被判成虚拟线
    assert devices_module.is_pseudo_capture("Analogue 1 + 2 (2- Focusrite USB Audio)") is False
    assert devices_module.is_pseudo_capture("立体声混音 (Realtek High Definition Audio)") is False


def test_marker_tokens_survive_hyphenated_spellings():
    """标记词在连字符写法下照样认得出（牌子在不同库里写法不一）。"""
    assert devices_module.token_form("CABLE-In-16ch (VB-Audio Virtual Cable)") == (
        "cable in 16ch (vb audio virtual cable)"
    )
    assert devices_module.is_pseudo_capture("CABLE-Output (VB-Audio-Virtual-Cable)") is True
    renders = (
        DeviceEndpoint("扬声器 (Focusrite USB Audio)", "", "render", index=0),
        DeviceEndpoint("CABLE-In-16ch (VB-Audio Virtual Cable)", "", "render", index=1),
        DeviceEndpoint("CABLE-Input (VB-Audio Virtual Cable)", "", "render", index=2),
    )
    cable, tier = devices_module.loopback_candidates(renders, None)
    assert tier == "cable"
    assert [item.name for item in cable] == ["CABLE-Input (VB-Audio Virtual Cable)"]


def test_pseudo_capture_endpoints_stay_out_of_the_mic_candidates():
    """抓取型虚拟端点不进候选（本机系统默认输入就是它）。"""
    # 显式点名那条虚拟线：候选收窄到它，并标记成「显式配的抓取型端点」
    candidates, explicit_pseudo = devices_module.mic_candidates(
        MACHINE_INPUTS, "CABLE Output (VB-Audio Virtual Cable)"
    )
    assert explicit_pseudo is True
    assert [item.name for item in candidates] == ["CABLE Output (VB-Audio Virtual Cable)"] * 2

    # 没配名字时：种子取序号最小的**非抓取型**输入端点（`Microsoft 声音映射器 - Input` #0），
    # 于是那条虚拟线一条都不进候选
    candidates, explicit_pseudo = devices_module.mic_candidates(MACHINE_INPUTS, None)
    assert explicit_pseudo is False
    assert [item.name for item in candidates] == ["Microsoft 声音映射器 - Input"]


# ---------------------------------------------------------------------------
# 麦克风：V1 的骨架
# ---------------------------------------------------------------------------


def test_a_lone_candidate_is_returned_without_trying_it():
    """V1 的短路：**同名候选只有一条**就直接返回它（一次试开都不做）。"""
    test_open = opener({}, default=refuse("假件：不该被调到"))
    outcome = select_microphone(
        MACHINE_INPUTS, "Microsoft 声音映射器 - Input", test_open
    )
    assert outcome.status == PROBE_READY
    assert outcome.device_name() == "Microsoft 声音映射器 - Input"
    assert test_open.calls == []  # type: ignore[attr-defined]
    assert "候选只有一条" in outcome.detail


def test_same_named_candidates_are_tried_in_host_rank_order():
    """同名多候选：按 host rank 逐个试开，取第一个**真的有波形**的（V2 的读回样本那一步）。

    本机实测的摆法：WASAPI 那条抛 `Invalid sample rate`、DirectSound 那条能开但全是零、
    MME 那条有真实波形 —— 选中 MME，且试开次序是 WASAPI → DirectSound → MME。
    """
    name = "Analogue 1 + 2 (2- Focusrite USB Audio"
    endpoints = (
        DeviceEndpoint(name, "Windows WASAPI", "input", index=18, channels=2),
        DeviceEndpoint(name, "Windows DirectSound", "input", index=8, channels=4),
        DeviceEndpoint(name, "MME", "input", index=1, channels=4),
    )
    attempts: list[str] = []

    def per_host(endpoint: DeviceEndpoint) -> OpenAttempt:
        attempts.append(endpoint.host_api)
        if endpoint.host_api == "Windows WASAPI":
            return refuse()
        if endpoint.host_api == "Windows DirectSound":
            return open_with(SILENCE)
        return open_with(WAVE)

    outcome = select_microphone(endpoints, name, per_host)
    assert outcome.status == PROBE_READY
    assert outcome.endpoint is not None
    assert outcome.endpoint.host_api == "MME"
    assert attempts == ["Windows WASAPI", "Windows DirectSound", "MME"]
    assert "有真实波形" in outcome.detail
    assert "没有波形" in outcome.detail  # DirectSound 那条的留痕
    assert "Invalid sample rate" in outcome.detail  # WASAPI 那条的留痕


def test_all_candidates_silent_falls_back_to_the_first_one():
    """V1 的回落：全都开不成（或开了没波形）→ **使用首个候选**。"""
    name = "Analogue 1 + 2 (2- Focusrite USB Audio"
    endpoints = (
        DeviceEndpoint(name, "Windows WASAPI", "input", index=18, channels=2),
        DeviceEndpoint(name, "Windows DirectSound", "input", index=8, channels=4),
        DeviceEndpoint(name, "MME", "input", index=1, channels=4),
    )
    seen: list[str] = []

    def always_silent(endpoint: DeviceEndpoint) -> OpenAttempt:
        seen.append(endpoint.host_api)
        return open_with(SILENCE)

    outcome = select_microphone(endpoints, name, always_silent)
    assert outcome.status == PROBE_READY
    assert outcome.device_name() == name
    assert outcome.endpoint is not None
    # 回落的是**排序后**的首个候选 = WASAPI 那条（V1 的 `cands.sort` 之后 `cands[0]`）
    assert outcome.endpoint.host_api == "Windows WASAPI"
    assert seen == ["Windows WASAPI", "Windows DirectSound", "MME"]
    assert "使用首个候选" in outcome.detail


def test_no_candidate_reports_probe_failed():
    """候选为空（端点表里没有匹配的输入端点）→ `probe_failed`，`detail` 是人话。"""
    outcome = select_microphone(MACHINE_INPUTS, "一块并不存在的声卡", lambda endpoint: refuse())
    assert outcome.status == PROBE_FAILED
    assert outcome.device_name() is None
    assert outcome.status in SOURCE_STATUS
    assert "没命中" in outcome.detail
    assert "候选为空" in outcome.detail


def test_default_input_is_used_when_nothing_is_configured():
    """没配名字：种子取默认输入那条端点，显式配置那一栏的提醒一句都不出现。"""
    test_open = opener({}, default=open_with(WAVE))
    outcome = select_microphone(MACHINE_INPUTS, None, test_open)
    assert outcome.status == PROBE_READY
    assert outcome.device_name() == "Microsoft 声音映射器 - Input"
    assert "显式配置" not in outcome.detail


# ---------------------------------------------------------------------------
# 麦克风：显式配置
# ---------------------------------------------------------------------------


def test_explicit_name_is_matched_exactly_first():
    """精确名命中（V1 的 `d.get("name") == name`）：候选就是那一条，别的一条都不碰。"""
    name = "Analogue 1 + 2 (2- Focusrite USB Audio"
    endpoints = (
        DeviceEndpoint(name, "Windows WASAPI", "input", index=18, channels=2),
        DeviceEndpoint(name, "Windows DirectSound", "input", index=8, channels=4),
        DeviceEndpoint("Microsoft 声音映射器 - Input", "MME", "input", index=0, channels=2),
    )
    seen: list[str] = []

    def only_wasapi(endpoint: DeviceEndpoint) -> OpenAttempt:
        seen.append(endpoint.host_api)
        return open_with(WAVE)

    outcome = select_microphone(endpoints, name, only_wasapi)
    assert outcome.device_name() == name
    assert outcome.matched_prefix is False
    assert seen == ["Windows WASAPI"]  # 第一条就有波形，后面的不再试


def test_explicit_name_falls_back_to_a_prefix_match():
    """MME 端点名会被 Windows 截断：精确名不中时用**双向前缀**兜，报实际命中的那个名字。"""
    full = "Analogue 1 + 2 (2- Focusrite USB Audio)"
    endpoints = (DeviceEndpoint("Analogue 1 + 2 (2- Focusrite US", "MME", "input", index=1),)
    outcome = select_microphone(
        endpoints, full, lambda endpoint: open_with(SILENCE)
    )
    assert outcome.device_name() == "Analogue 1 + 2 (2- Focusrite US"
    assert outcome.matched_prefix is True
    assert "按前缀兜到" in outcome.detail

    # 精确名命中时不带前缀标记
    exact = (DeviceEndpoint(full, "Windows DirectSound", "input", index=8),)
    outcome_exact = select_microphone(exact, full, lambda endpoint: open_with(SILENCE))
    assert outcome_exact.matched_prefix is False


def test_explicit_capture_endpoint_is_honoured_with_a_note():
    """显式配了 `CABLE Output` 这类抓取型端点：照办，但 `detail` 里留一句提醒。"""
    target = "CABLE Output (VB-Audio Virtual Cable)"
    outcome = select_microphone(MACHINE_INPUTS, target, lambda endpoint: open_with(WAVE))
    assert outcome.status == PROBE_READY
    assert outcome.device_name() == target
    assert outcome.explicit_pseudo is True
    assert "抓取型虚拟端点" in outcome.detail
    assert "同一路内容" in outcome.detail


# ---------------------------------------------------------------------------
# loopback：V1 的三级
# ---------------------------------------------------------------------------


def test_loopback_prefers_cable_input_without_16ch():
    """没配名字：名字含 `CABLE Input` 且不含 `16ch` 的 render 端点（V1 那一级）。"""
    outcome = select_loopback(MACHINE_RENDERS, None, lambda endpoint: open_with(SILENCE))
    assert outcome.status == PROBE_READY
    assert outcome.device_name() == "CABLE Input (VB-Audio Virtual Cable)"
    assert "16ch" in outcome.detail  # 那句「选 16ch 会静音」的说明

    # 表里同时有 16ch 端点（另一台机器的形状）时，16ch 那条既不中选也不被试开
    calls: list[str] = []

    def record(endpoint: DeviceEndpoint) -> OpenAttempt:
        calls.append(endpoint.name)
        return open_with(SILENCE)

    outcome_b = select_loopback(RENDERS_WITH_16CH, None, record)
    assert outcome_b.device_name() == "CABLE Input (VB-Audio Virtual Cable)"
    assert calls == ["CABLE Input (VB-Audio Virtual Cable)"]


def test_loopback_explicit_name_wins():
    """配了名字：精确命中那个 render 端点（V1 的 `if s.name == name: return s`）。"""
    target = "扬声器 (2- Focusrite USB Audio)"
    calls: list[str] = []

    def record(endpoint: DeviceEndpoint) -> OpenAttempt:
        calls.append(endpoint.name)
        return open_with(SILENCE)

    outcome = select_loopback(MACHINE_RENDERS, target, record)
    assert outcome.status == PROBE_READY
    assert outcome.device_name() == target
    assert calls == [target]


def test_loopback_falls_back_to_the_default_speaker():
    """没有 `CABLE Input` 类端点时取系统默认输出（端点表第一条）。"""
    renders = (
        DeviceEndpoint("扬声器 (Realtek High Definition Audio)", "", "render", index=0),
        DeviceEndpoint("耳机 (Realtek High Definition Audio)", "", "render", index=1),
    )
    outcome = select_loopback(renders, None, lambda endpoint: open_with(SILENCE))
    assert outcome.status == PROBE_READY
    assert outcome.device_name() == "扬声器 (Realtek High Definition Audio)"
    assert "系统默认输出" in outcome.detail


def test_loopback_without_any_render_endpoint_reports_probe_failed():
    """一条 render 端点都没有 → `probe_failed`，`detail` 是人话。"""
    outcome = select_loopback([], None, lambda endpoint: open_with(SILENCE))
    assert outcome.status == PROBE_FAILED
    assert outcome.device_name() is None
    assert "一个 render" in outcome.detail
    assert outcome.status in SOURCE_STATUS


def test_loopback_target_that_cannot_be_opened_reports_probe_failed():
    """挑中的 render 端点打不开 → `probe_failed`，`detail` 说清是哪一条、为什么。"""
    outcome = select_loopback(
        MACHINE_RENDERS, None, lambda endpoint: refuse("soundcard 里没有这条端点的 loopback")
    )
    assert outcome.status == PROBE_FAILED
    assert outcome.device_name() is None
    assert "soundcard 里没有这条端点的 loopback" in outcome.detail


def test_loopback_candidates_tiers():
    """`loopback_candidates()` 的四档：explicit / cable / default_speaker / none。"""
    explicit, tier = devices_module.loopback_candidates(
        MACHINE_RENDERS, "扬声器 (2- Focusrite USB Audio)"
    )
    assert tier == "explicit"
    assert [item.name for item in explicit] == ["扬声器 (2- Focusrite USB Audio)"]

    cable, tier = devices_module.loopback_candidates(MACHINE_RENDERS, None)
    assert tier == "cable"
    assert [item.name for item in cable] == ["CABLE Input (VB-Audio Virtual Cable)"]

    # 显式名没命中 → 落到 Cable 那一档（V1 的 `log.warning(...)` 之后继续往下走）
    fallback, tier = devices_module.loopback_candidates(MACHINE_RENDERS, "不存在的设备")
    assert tier == "cable"

    default, tier = devices_module.loopback_candidates(RENDERS_WITH_16CH[:1], None)
    assert tier == "default_speaker"
    assert [item.name for item in default] == ["扬声器 (Focusrite USB Audio)"]

    empty, tier = devices_module.loopback_candidates([], None)
    assert tier == "none" and empty == []


# ---------------------------------------------------------------------------
# 形状与名字匹配
# ---------------------------------------------------------------------------


def test_status_values_stay_inside_the_frozen_domain():
    """挑选结论只报 `ready` / `probe_failed`（`unprobed` 是「还没接后端」那一档）。"""
    ready = select_loopback(MACHINE_RENDERS, None, lambda endpoint: open_with(SILENCE))
    failed = select_microphone([], None, lambda endpoint: refuse())
    assert {ready.status, failed.status} <= set(SOURCE_STATUS)
    assert {ready.status, failed.status} == {PROBE_READY, PROBE_FAILED}


def test_name_matches_reports_prefix_fallbacks():
    """`name_matches()`：精确命中不带前缀标记，前缀兜带；不相干的名字一条都不中。"""
    assert devices_module.name_matches("MME 端点", "MME 端点") == (True, False)
    assert devices_module.name_matches("MME 端点（截断", "MME 端点（截断名）") == (True, True)
    assert devices_module.name_matches("MME 端点（截断名）", "MME 端点（截断") == (True, True)
    assert devices_module.name_matches("另一个设备", "MME 端点") == (False, False)
    assert devices_module.name_matches("", "MME 端点") == (False, False)


def test_endpoint_model_rejects_an_unknown_role():
    """端点方向只有 `input` / `render` 两档，写错当场抛。"""
    with pytest.raises(ValueError):
        DeviceEndpoint("某端点", "MME", "loopback")


def test_devices_module_does_not_import_audio_libraries():
    """纯逻辑那一侧的硬要求：这个模块不 import 声卡库（没有声卡库也 import 得动）。"""
    import ast
    import pathlib
    import sys

    source = pathlib.Path(devices_module.__file__).read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for banned in ("sounddevice", "soundcard", "numpy", "pydub"):
        assert not any(name.split(".")[0] == banned for name in imported), (
            f"meeting/capture/devices.py 不许 import {banned}（挑选策略是纯逻辑）"
        )
    assert "meeting.capture.devices" in sys.modules
