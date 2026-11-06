"""`tests/test_capture_devices.py` —— 设备模型与挑选策略（**纯逻辑，无硬件、无 ffmpeg**）。

被验的是 `meeting/capture/devices.py` 里那两段**从 V1 拷过来的内核**
（`mnotes_core/recorder.py:371-413` 与 `:444-465`）在换成「端点表 + 假试开函数」之后
行为是否还认得出原样（两处收尾按 `phase1/task.md` §3 的裁定改过，见下面最后两条）：

  - V1 的候选筛选：同名 + `max_input_channels > 0`；
  - V1 的 `host_rank`：`WASAPI → DirectSound → MME → 其它(9)`；
  - V1 的排序逐个试开骨架（`cands.sort(key=host_rank)` 后逐条试、取第一条成的）；
  - V1 的 loopback 三级：精确名 → `CABLE Input` 且不含 `16ch` → `default_speaker()`；
  - V2 补的四件事：没配名字时也挑（好让快照报得出真设备名）、抓取型虚拟端点不进候选、
    试开时读回样本判「有没有真实波形」、名字的**精确优先落到候选集合上**；
  - V2 按任务书 §3 第 5、6 条改的收尾**两档收敛**：在打开了的那几条里挑（先要有波形的，
    一条都没有就取第一条打开了）；**一条都没打开**时落 `probe_failed`、`endpoint=None`
    （V1 那两条短路口 —— 单候选直接返回、全失败回落首个候选 —— 都不再照搬）。

端点表与试开函数都是本文件造的假件；真机那一侧不在自动化测试里验（真开设备需要机主点头）。
"""

from __future__ import annotations

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
#:
#: `is_default=True` 标在 **MME #1** 上：本机只读实测的系统默认输入端点
#: （`sd.default.device = [1, 6]`、`query_devices(kind="input")["index"] == 1`）就是它，
#: 而**序号最小**的是 `Microsoft 声音映射器 - Input`（#0，Windows 的 WAVE_MAPPER）。
MACHINE_INPUTS: tuple[DeviceEndpoint, ...] = (
    DeviceEndpoint(
        "Analogue 1 + 2 (2- Focusrite USB Audio)", "Windows WASAPI", "input", index=18,
        channels=2,
    ),
    DeviceEndpoint(
        "Analogue 1 + 2 (2- Focusrite USB Audio)", "Windows DirectSound", "input", index=8,
        channels=4,
    ),
    DeviceEndpoint("Analogue 1 + 2 (2- Focusrite US", "MME", "input", index=1, channels=4,
                   is_default=True),
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
    """排序键：`WASAPI` → `DirectSound` → `MME` → 其余 `HOST_RANK_FALLBACK`。

    钉的是**相对次序**（V1 那张表的性质）；各档的具体取值本身由一处常量给出，
    再抄一遍数字不会多出信息。
    """
    wasapi = devices_module.host_rank("WASAPI")
    direct_sound = devices_module.host_rank("DirectSound")
    mme = devices_module.host_rank("MME")
    unknown = devices_module.host_rank("Windows WDM-KS")

    assert devices_module.host_rank("Windows WASAPI") == wasapi
    assert wasapi < direct_sound < mme < unknown
    assert devices_module.host_rank("Windows DirectSound") == direct_sound
    assert unknown == HOST_RANK_FALLBACK
    assert devices_module.host_rank("") == HOST_RANK_FALLBACK


def test_sort_candidates_uses_host_rank():
    """候选排序就是 `cands.sort(key=host_rank)`（只按 rank，稳定排序）。"""
    ordered = devices_module.sort_candidates(MACHINE_INPUTS)
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
    """抓取型判据 = 两个模块级常量同时命中（`CABLE Output` + `VB-Audio` 牌子）。"""
    assert devices_module.is_pseudo_capture("CABLE Output (VB-Audio Virtual Cable)") is True
    assert devices_module.is_pseudo_capture("CABLE Output (VB-Audio Virtual ") is True
    # `CABLE Input` 是 render 端点（播出去的那一侧），不在抓取型判据里
    assert devices_module.is_pseudo_capture("CABLE Input (VB-Audio Virtual Cable)") is False
    # 真实声卡的端点一条都不该被判成虚拟线
    assert devices_module.is_pseudo_capture("Analogue 1 + 2 (2- Focusrite USB Audio)") is False
    assert devices_module.is_pseudo_capture("立体声混音 (Realtek High Definition Audio)") is False


def test_pseudo_capture_recognises_the_wdm_ks_point_spelling():
    """本机真表里那条 WDM-KS 的 `CABLE Output (VB-Audio Point)`（#25, ch=16）也算抓取型。

    它的名字里**没有** "virtual"：牌子标记只取 `vb-audio`，取长写法会把这条漏掉，于是同一块
    VB-Cable 的抓取侧在这一档上照进候选（任务书 §3.1 要求这类端点「一律不进候选」）。
    表里另有三条带 "Virtual" 的抓取侧（MME #2 / DirectSound #11 / WASAPI #21）作为对照。
    """
    assert devices_module.PSEUDO_CAPTURE_MARKER == "vb-audio"
    assert devices_module.is_pseudo_capture("CABLE Output (VB-Audio Point)") is True
    assert devices_module.is_pseudo_capture("CABLE Output (VB-Audio Virtual Cable)") is True
    # 同一块虚拟线的 render 侧（`CABLE Input`）与 `Input (VB-Audio Point)` 都不是抓取侧
    assert devices_module.is_pseudo_capture("CABLE Input (VB-Audio Virtual Cable)") is False
    assert devices_module.is_pseudo_capture("Input (VB-Audio Point)") is False
    # 真表的四条抓取侧逐条点名（假表的形状照抄本机）
    table = MACHINE_INPUTS + (DeviceEndpoint(
        "CABLE Output (VB-Audio Point)", "Windows WDM-KS", "input", index=25, channels=16,
    ),)
    pseudo = [item.name for item in table if devices_module.is_pseudo_capture(item.name)]
    assert pseudo == [
        "CABLE Output (VB-Audio Virtual Cable)",
        "CABLE Output (VB-Audio Virtual Cable)",
        "CABLE Output (VB-Audio Point)",
    ]
    candidates, explicit_pseudo, _prefix = devices_module.mic_candidates(table, None)
    assert explicit_pseudo is False
    assert all("CABLE Output" not in item.name for item in candidates)
    # 显式配那条 WDM-KS 抓取侧：照办，但提醒一句都不少
    outcome = select_microphone(table, "CABLE Output (VB-Audio Point)",
                               lambda endpoint: open_with(WAVE))
    assert outcome.explicit_pseudo is True
    assert "抓取型虚拟端点" in outcome.detail


def test_marker_tokens_survive_hyphenated_spellings():
    """牌子标记在连字符写法下照样认得出（VB-Audio 的牌子在不同库里写法不一）。

    这一条只管**标记词**（`is_pseudo_capture()` 的那两个常量）：记号词按 `token_form()` 折过。
    loopback 那两级判据走的是 V1 原文的 `in`（原样子串、不折形），见下一条。
    """
    assert devices_module.token_form("CABLE-In-16ch (VB-Audio Virtual Cable)") == (
        "cable in 16ch (vb audio virtual cable)"
    )
    assert devices_module.is_pseudo_capture("CABLE-Output (VB-Audio-Virtual-Cable)") is True
    assert devices_module.is_pseudo_capture("CABLE-Output (VB-Audio-Point)") is True


def test_loopback_tokens_are_v1_literal_substrings():
    """loopback 的两个子串判据**逐字照 V1**（`"CABLE Input" in s.name`，大小写敏感）。

    V1 原文不做折形，V2 也不做：`CABLE-Input` / `cable input` 这些写法在 V1 不命中，在这里
    同样不命中（旧稿曾走 `_contains()` 折大小写与连字符，比 V1 宽，已按 V1 原文收回）。
    """
    render = "CABLE Input (VB-Audio Virtual Cable)"
    hyphenated = "CABLE-Input (VB-Audio Virtual Cable)"
    plain_speaker = "扬声器 (2- Focusrite USB Audio)"

    literal, tier = devices_module.loopback_candidates(
        (
            DeviceEndpoint(plain_speaker, "", "render", index=0),
            DeviceEndpoint(render, "", "render", index=1),
        ),
        None,
    )
    assert tier == "cable"
    assert [item.name for item in literal] == [render]

    # 折形写法不命中 → 落到「系统默认输出」那一档（V1 的 `in` 也不命中）
    folded, tier = devices_module.loopback_candidates(
        (
            DeviceEndpoint(plain_speaker, "", "render", index=0),
            DeviceEndpoint(hyphenated, "", "render", index=1),
        ),
        None,
    )
    assert tier == "default_speaker"
    assert [item.name for item in folded] == [plain_speaker]

    # 排除词同样逐字比：`16ch` 原样命中，`16 ch` 不算（V1 的 `"16ch" not in s.name`）
    spaced, tier = devices_module.loopback_candidates(
        (
            DeviceEndpoint(plain_speaker, "", "render", index=0),
            DeviceEndpoint("CABLE Input (16 ch) (VB-Audio Virtual Cable)", "", "render", index=1),
        ),
        None,
    )
    assert tier == "cable"
    assert [item.name for item in spaced] == [
        "CABLE Input (16 ch) (VB-Audio Virtual Cable)"
    ]


def test_pseudo_capture_endpoints_stay_out_of_the_mic_candidates():
    """抓取型虚拟端点不进候选（任务书 §3.1：不挡就会把远端声音当成麦克风录一遍）。"""
    # 显式点名那条虚拟线：候选收窄到它，并标记成「显式配的抓取型端点」
    candidates, explicit_pseudo, matched_prefix = devices_module.mic_candidates(
        MACHINE_INPUTS, "CABLE Output (VB-Audio Virtual Cable)"
    )
    assert explicit_pseudo is True
    assert matched_prefix is False
    assert [item.name for item in candidates] == ["CABLE Output (VB-Audio Virtual Cable)"] * 2

    # 没配名字时：种子取**系统默认输入**那条（`is_default`，本机 = MME #1 Focusrite），
    # 那条虚拟线一条都不进候选
    candidates, explicit_pseudo, matched_prefix = devices_module.mic_candidates(
        MACHINE_INPUTS, None
    )
    assert explicit_pseudo is False
    assert matched_prefix is False
    assert [item.name for item in candidates] == ["Analogue 1 + 2 (2- Focusrite US"]
    assert all(not devices_module.is_pseudo_capture(item.name) for item in candidates)


# ---------------------------------------------------------------------------
# 麦克风：V1 的骨架
# ---------------------------------------------------------------------------


def test_a_lone_candidate_is_still_tried_once():
    """V1 的单候选短路口**不再照搬**：候选只有一条也试开一次（读回样本才判得出「有没有波形」）。"""
    test_open = opener({"Microsoft 声音映射器 - Input": open_with(WAVE)})
    outcome = select_microphone(
        MACHINE_INPUTS, "Microsoft 声音映射器 - Input", test_open
    )
    assert outcome.status == PROBE_READY
    assert outcome.device_name() == "Microsoft 声音映射器 - Input"
    assert test_open.calls == ["Microsoft 声音映射器 - Input"]  # type: ignore[attr-defined]
    assert "有真实波形" in outcome.detail


def test_a_lone_candidate_that_cannot_be_opened_reports_probe_failed():
    """单候选也开不了 → `probe_failed`、`endpoint=None`（不再直接把它当 `ready` 报上去）。"""
    test_open = opener({"Microsoft 声音映射器 - Input": refuse("被别的程序占着")})
    outcome = select_microphone(
        MACHINE_INPUTS, "Microsoft 声音映射器 - Input", test_open
    )
    assert outcome.status == PROBE_FAILED
    assert outcome.device_name() is None
    assert test_open.calls == ["Microsoft 声音映射器 - Input"]  # type: ignore[attr-defined]
    assert "一条都没打开" in outcome.detail
    assert "被别的程序占着" in outcome.detail


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


def test_all_candidates_silent_takes_the_first_one_that_opened():
    """一条有波形的都没有时：取**第一条打开了**的（`ready`），不是排序后的第一条。

    本机实测的摆法：WASAPI 那条在 16k 直接开不了、DirectSound 那条能开但全是零、MME 那条
    也能开但全是零 —— 中选的是排序后**第一条打开了**的 DirectSound（WASAPI 那条从头到尾
    都没打开过，因此进不了这一档）。
    """
    name = "Analogue 1 + 2 (2- Focusrite USB Audio"
    endpoints = (
        DeviceEndpoint(name, "Windows WASAPI", "input", index=18, channels=2),
        DeviceEndpoint(name, "Windows DirectSound", "input", index=8, channels=4),
        DeviceEndpoint(name, "MME", "input", index=1, channels=4),
    )
    seen: list[str] = []

    def wasapi_refuses_the_rest_silent(endpoint: DeviceEndpoint) -> OpenAttempt:
        seen.append(endpoint.host_api)
        if endpoint.host_api == "Windows WASAPI":
            return refuse()
        return open_with(SILENCE)

    outcome = select_microphone(endpoints, name, wasapi_refuses_the_rest_silent)
    assert outcome.status == PROBE_READY
    assert outcome.device_name() == name
    assert outcome.endpoint is not None
    assert outcome.endpoint.host_api == "Windows DirectSound"
    assert seen == ["Windows WASAPI", "Windows DirectSound", "MME"]
    assert "取第一条**打开了**的候选" in outcome.detail
    # WASAPI 那条的失败留痕照样在（试过但没选上）
    assert "Invalid sample rate" in outcome.detail


def test_no_candidate_opens_reports_probe_failed():
    """**一条都没打开** → `probe_failed` + `endpoint=None`，`detail` 逐条写清各自为什么没成。

    这就是任务书 §3 第 6 条与两态裁定的那一条：V1 在这里返回 `cands[0]`，V2 有 `probe_failed`
    这一档可报，**不许**把一个从没打开过的端点当 `ready` 报上去（那会先显示就绪、真开录立刻
    掉线）。
    """
    name = "Analogue 1 + 2 (2- Focusrite USB Audio"
    endpoints = (
        DeviceEndpoint(name, "Windows WASAPI", "input", index=18, channels=2),
        DeviceEndpoint(name, "Windows DirectSound", "input", index=8, channels=4),
        DeviceEndpoint(name, "MME", "input", index=1, channels=4),
    )
    seen: list[str] = []

    def always_refuses(endpoint: DeviceEndpoint) -> OpenAttempt:
        seen.append(endpoint.host_api)
        return refuse(f"{endpoint.host_api} 这条打不开")

    outcome = select_microphone(endpoints, name, always_refuses)
    assert outcome.status == PROBE_FAILED
    assert outcome.status in SOURCE_STATUS
    assert outcome.endpoint is None
    assert outcome.device_name() is None
    # 三条候选逐条试过，各条的失败原因都写在 detail 里
    assert seen == ["Windows WASAPI", "Windows DirectSound", "MME"]
    assert "3 条候选端点一条都没打开" in outcome.detail
    for host in ("Windows WASAPI", "Windows DirectSound", "MME"):
        assert f"{host} 这条打不开" in outcome.detail
    assert "不回落从没打开过的候选" in outcome.detail
    assert len(outcome.attempts) == 3


def test_no_candidate_reports_probe_failed():
    """候选为空（端点表里没有匹配的输入端点）→ `probe_failed`，`detail` 是人话。"""
    outcome = select_microphone(MACHINE_INPUTS, "一块并不存在的声卡", lambda endpoint: refuse())
    assert outcome.status == PROBE_FAILED
    assert outcome.device_name() is None
    assert outcome.status in SOURCE_STATUS
    assert "没命中" in outcome.detail
    assert "候选为空" in outcome.detail


def test_default_input_is_used_when_nothing_is_configured():
    """没配名字：种子取**系统默认输入端点**（`is_default`），显式配置那一栏的提醒一句都不出现。

    本机那张表的形状：默认输入是 MME `#1` Focusrite（`is_default=True`），而**序号最小**的
    `#0` 是 MME 的 `Microsoft 声音映射器 - Input`（Windows 的 WAVE_MAPPER，跟随系统默认
    输入设备）。C1 点名的就是这两者的区别：按序号最小取会落到 mapper 上，报出来的
    `device_name` 与机主插的那块声卡对不上，而且把「系统默认输入」这条路原样留着。
    """
    test_open = opener({}, default=open_with(WAVE))
    outcome = select_microphone(MACHINE_INPUTS, None, test_open)
    assert outcome.status == PROBE_READY
    assert outcome.device_name() == "Analogue 1 + 2 (2- Focusrite US"
    assert outcome.device_name() != "Microsoft 声音映射器 - Input"
    assert "显式配置" not in outcome.detail


def test_default_seed_is_the_marked_endpoint_not_the_lowest_index():
    """种子的取值口跟着表上标出的 `is_default` 那一条走（C1 的回归钉子）。

    本机那张表把 `is_default` 标在 MME `#1` Focusrite 上；同一张表把它挪到 WASAPI 那条
    （序号 #18）上，种子跟着挪。序号最小的 `#0` 是 MME 的 WAVE_MAPPER，这个桩子钉的就是
    「种子跟着标记走」这件事：谁把取值口改回 `min(index)`，这两条断言当场红。
    """
    candidates, _pseudo, _prefix = devices_module.mic_candidates(MACHINE_INPUTS, None)
    assert [item.index for item in candidates] == [1]
    assert [item.host_api for item in candidates] == ["MME"]

    moved = tuple(
        DeviceEndpoint(item.name, item.host_api, item.role, item.index, item.channels,
                       is_default=(item.index == 18))
        for item in MACHINE_INPUTS
    )
    candidates, _pseudo, _prefix = devices_module.mic_candidates(moved, None)
    # 标记在 WASAPI 那条上，它的名字是全名 —— 同名的 DirectSound 那条一起进候选
    assert [item.index for item in candidates] == [18, 8]
    assert [item.host_api for item in candidates] == ["Windows WASAPI", "Windows DirectSound"]
    assert 1 not in [item.index for item in candidates]


def test_pseudo_default_input_falls_back_to_the_first_real_candidate():
    """系统默认输入端点本身是抓取型（`CABLE Output` 这类）时，它不进候选、也不当种子。

    退回按 host API 排序的第一条非抓取型候选（任务书 §3.1 的落点：默认输入一旦是它，
    mic 与 loopback 就会录同一路）。
    """
    endpoints = (
        DeviceEndpoint("CABLE Output (VB-Audio Virtual Cable)", "MME", "input", index=0,
                       channels=16, is_default=True),
        DeviceEndpoint("Analogue 1 + 2 (2- Focusrite USB Audio)", "Windows WASAPI", "input",
                       index=18, channels=2),
        DeviceEndpoint("Analogue 1 + 2 (2- Focusrite US", "MME", "input", index=1, channels=4),
    )
    candidates, explicit_pseudo, _prefix = devices_module.mic_candidates(endpoints, None)
    assert explicit_pseudo is False
    assert [item.name for item in candidates] == ["Analogue 1 + 2 (2- Focusrite USB Audio)"]

    # 表里一条都没标 `is_default`（老表 / 读不出默认输入）时同样退回按 host API 排序的第一条
    unmarked = tuple(
        DeviceEndpoint(item.name, item.host_api, item.role, item.index, item.channels)
        for item in endpoints
    )
    candidates, _pseudo, _prefix = devices_module.mic_candidates(unmarked, None)
    assert [item.name for item in candidates] == ["Analogue 1 + 2 (2- Focusrite USB Audio)"]


# ---------------------------------------------------------------------------
# 麦克风：显式配置
# ---------------------------------------------------------------------------


def test_explicit_name_is_matched_exactly_first():
    """精确名命中（V1 的 `d.get("name") == name`）：候选就是那几条同名的，别的一条都不碰。"""
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


def test_exact_name_wins_over_a_prefix_match_in_the_candidate_set():
    """显式配的**截断名**精确命中 MME 那条：前缀命中的 WASAPI 那条不进候选。

    这条与下一条一起收掉 C1 复审的 minor 2：名字的两档匹配落在**候选集合**上（先取精确名
    命中的候选，精确命中为空才用前缀兜一次），于是「精确名命中 MME、前缀命中 WASAPI」时
    选中 MME 那条，`matched_prefix` 如实为 `False`。
    """
    truncated = "Analogue 1 + 2 (2- Focusrite US"
    endpoints = (
        DeviceEndpoint(
            "Analogue 1 + 2 (2- Focusrite USB Audio)", "Windows WASAPI", "input",
            index=18, channels=2,
        ),
        DeviceEndpoint(truncated, "MME", "input", index=1, channels=4),
    )
    test_open = opener(
        {
            "Analogue 1 + 2 (2- Focusrite USB Audio)": open_with(WAVE),
            truncated: open_with(SILENCE),
        }
    )

    outcome = select_microphone(endpoints, truncated, test_open)
    # 前缀兜那条（有波形）从头到尾没被试开：候选集合里只有精确命中的 MME 那条
    assert test_open.calls == [truncated]  # type: ignore[attr-defined]
    assert outcome.endpoint is not None
    assert outcome.endpoint.host_api == "MME"
    assert outcome.device_name() == truncated
    assert outcome.matched_prefix is False
    assert "按前缀兜到" not in outcome.detail


def test_explicit_name_falls_back_to_a_prefix_match():
    """MME 端点名会被 Windows 截断：精确名不中时用**双向前缀**兜，报实际命中的那个名字。"""
    full = "Analogue 1 + 2 (2- Focusrite USB Audio)"
    endpoints = (DeviceEndpoint("Analogue 1 + 2 (2- Focusrite US", "MME", "input", index=1),)
    calls: list[str] = []

    def record(endpoint: DeviceEndpoint) -> OpenAttempt:
        calls.append(endpoint.name)
        return open_with(SILENCE)

    outcome = select_microphone(endpoints, full, record)
    assert outcome.device_name() == "Analogue 1 + 2 (2- Focusrite US"
    assert outcome.matched_prefix is True
    assert "按前缀兜到" in outcome.detail
    assert calls == ["Analogue 1 + 2 (2- Focusrite US"]  # 兜到的这条也走一遍试开

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


def test_devices_module_does_not_import_audio_libraries():
    """纯逻辑那一侧的硬要求：这个模块不 import 声卡库（没有声卡库也 import 得动）。"""
    import ast
    import pathlib

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
