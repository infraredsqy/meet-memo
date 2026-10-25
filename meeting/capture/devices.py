"""设备模型与挑选策略——纯逻辑，不 import 声卡库（`dca4539b` phase1 §3）。

挑选内核整段拷自 V1（`mnotes_core/recorder.py`）：`_resolve_mic_device`（`:371-413`）→
`select_microphone()`，`_resolve_loopback_speaker`（`:444-465`）→ `select_loopback()`。
拷过来只动接口层四件事：`self.*` 配置值 → `configured` 形参；方法 → 模块级函数；
两个库的查询产物 → 调用方注入的端点表（`DeviceEndpoint`）；交回端点对象与人话
（`SelectionOutcome`），好让 `probe()` 如实报三档。候选筛选、`host_rank` 的
`WASAPI → DirectSound → MME` 次序、精确名优先再前缀兜、loopback 三级——一律照 V1 原文。

两处收尾有意不照搬 V1（`phase1/task.md` §3 第 5、6 条）：单候选也试开一次；「全试完不成 →
回落首个候选」换成两档收敛——在打开了的候选里挑（先要有波形的），一条都没打开落
`probe_failed`、`endpoint=None`，绝不回落从没打开过的候选（V1 没有 `probe_failed` 可报，V2 有）。

唯一预批准的补强：试开读回一小段样本（`_has_sound()`），把「能打开」与「真的出声音」分开。
「DirectSound 端点开得起来、交回的却是一整块精确全零」是本机实测读数（阈值标定见
`SOURCE_PEAK_THRESHOLD`）；V1 那句注释讲的是另一件事（WASAPI 端点常不支持 16k，
`recorder.py:374-377`），文档侧见 `docs/v1-lineage.md` 的 C3 条目。

候选排序的由来（V1 原话，`recorder.py:374-377`）：Focusrite 等设备在 DirectSound/WASAPI
多个 host API 下同名，sounddevice 按名字匹配会抛 'Multiple input devices found'；WASAPI
端点常不支持 SAMPLE_RATE（如 48k 下 16k 打开失败），故按 host API 优先级逐个试开。

本机实测（原始输出见 `phase1/report.md` 与 `phase1/fix3.md`）：系统默认输入是
`Analogue 1 + 2 (2- Focusrite US`（MME #1）；序号最小的 `#0` 是 MME 的
`Microsoft 声音映射器 - Input`——Windows 的 WAVE_MAPPER，跟随系统默认输入、报出的是
跟着变的别名（旧稿按序号最小取种子落到的就是它）。VB-Cable 的抓取侧 `CABLE Output`
是抓取型端点，抓的是「往 CABLE Input 播出去的声音」＝远端那一路；当麦克风录下来 mic 与
loopback 会抓到同一路。因此在 V1 候选筛选之上多一条：抓取型虚拟端点
不进候选（`is_pseudo_capture()`）；用户显式配了这类端点时照办，只在 `detail` 里提醒。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

#: 本项目自己的 logger（V1 用的是 `logging.getLogger("meeting")`）
log = logging.getLogger("meeting.capture.devices")

# 冻结常量（试开口径）

#: 试开用的采样率（Hz）——与 V1 的 `SAMPLE_RATE`、归档格式（16k 单声道 int16）同一档
PROBE_SAMPLE_RATE = 16000
#: 试开用的声道数（V1 的 `CHANNELS`）
PROBE_CHANNELS = 1
#: 试开读回的样本个数（`PROBE_SAMPLE_RATE` 下 = 0.5 秒，够看出一块波形）
PROBE_FRAME_COUNT = 8000

#: 「样本里有真实波形」的峰值下界（int16 满量程 32767 的千分之一）。
#: 取 3 依据本机三种真实读数：有信号的两个 MME 端点峰值 215 / 231，安静房间的 Realtek
#: 立体声混音 5，虚拟线 MME 残留 1——「虚拟线没在走音频」判成没波形，「安静但有底噪」不误判。
SOURCE_PEAK_THRESHOLD = 3

#: 判定「波形在动」需要的相邻样本差（int16）。取 2 跳过 ±1 的直流偏置与量化抖动：
#: 没在走音频的虚拟线交回的是一整块「1 / -1 / 0 组成」的常数抖动。
WAVEFORM_MIN_STEP = 2

#: V1 `host_rank` 的取值表（搬自 `mnotes_core/recorder.py:396`）
HOST_RANK_KEYS: tuple[str, ...] = ("WASAPI", "DirectSound", "MME")
#: V1 没命中上面三条时的 rank（搬自 `recorder.py:399` 的 `return 9`）
HOST_RANK_FALLBACK = 9

#: 抓取型虚拟端点的排除词（小写子串）：名字里带它的是这条虚拟线的抓取侧
PSEUDO_CAPTURE_EXCLUDE_TOKEN = "cable output"
#: 抓取型虚拟端点的牌子标记（小写子串），两个都命中才算抓取型。只取 `vb-audio` 不取
#: 更长的 `vb-audio virtual`：WDM-KS 下抓取侧叫 `CABLE Output (VB-Audio Point)`
#: （本机实测 #25），名字里没有 "virtual"，取长写法会漏掉它。
PSEUDO_CAPTURE_MARKER = "vb-audio"

#: loopback 候选的选中词（V1 的 `"CABLE Input" in s.name`：原样子串，不折形）
LOOPBACK_PREFERRED_TOKEN = "CABLE Input"
#: loopback 候选的排除词（V1 的 `"16ch" not in s.name`：原样子串，不折形）
LOOPBACK_EXCLUDED_TOKEN = "16ch"

#: 两档探测结论（后端 `probe()` 的 `SourceProbe.status`）
PROBE_READY = "ready"
PROBE_FAILED = "probe_failed"

#: 端点方向（`input` = 抓取，`render` = 播放；loopback 候选只能从 render 里挑）
ENDPOINT_ROLES: tuple[str, ...] = ("input", "render")


# 端点模型（V1 那一侧 `sd.query_devices()` 的一行 / `sc.all_speakers()` 的一条）


@dataclass(frozen=True)
class DeviceEndpoint:
    """端点表里的一条（唯一标识用 `name`，两个库都按名字取设备）。

    `name` 端点名（MME 的名字会被 Windows 截断，本机形如
    `Analogue 1 + 2 (2- Focusrite US`，照原样留着，它就是 `device_name` 要报的值）；
    `host_api` host API 名；`index` sounddevice 的设备序号（试开时当 `device=`）；
    `channels` 端点声明的通道数（**筛选不用它**：通道过滤由 `sources.list_input_endpoints()`
    造表时做完，留在对象上只是让读数跟着走）；`is_default` 是不是系统默认输入端点
    （`sources.list_input_endpoints()` 按只读的 `sd.default.device[0]` 标出）——没配
    `devices.mic` 时种子取值口就是它（见 `mic_candidates()`），本机落在 MME `#1`。
    """

    name: str
    host_api: str
    role: str
    index: int | None = None
    channels: int = 0
    is_default: bool = False

    def __post_init__(self) -> None:
        if self.role not in ENDPOINT_ROLES:
            raise ValueError(f"端点方向只能是 {ENDPOINT_ROLES}，收到 {self.role!r}")

    def label(self) -> str:
        """给人看的一句话（带 host API 与序号；render 端点来自 `soundcard`，没有 host API）。"""
        if self.index is None:
            return self.name
        where = f"{self.host_api} #{self.index}" if self.host_api else f"#{self.index}"
        return f"{self.name}（{where}）"


# 试开结果（`sources.py` 交回来的那一份）


@dataclass(frozen=True)
class TestSample:
    """一次试开读回来的样本（判定只看这两个数）。

    `peak` 绝对值最大的样本；`max_step` 相邻样本差的最大绝对值。全零 / 常数抖动的块对
    这两个数都交回 0 或 1。
    """

    peak: int = 0
    max_step: int = 0
    frames: int = 0


@dataclass(frozen=True)
class OpenAttempt:
    """一次试开的结果：开成了没有、读回的样本是什么、没开成的原因。"""

    opened: bool
    sample: TestSample | None = None
    error: str | None = None

    def peak(self) -> int:
        return 0 if self.sample is None else int(self.sample.peak)

    def max_step(self) -> int:
        return 0 if self.sample is None else int(self.sample.max_step)


def measure_samples(frames: Sequence[int]) -> TestSample:
    """从一串整型样本里量出判定需要的两个数（纯函数，试开函数与单测共用）。"""
    values = [int(item) for item in frames]
    if not values:
        return TestSample()
    peak = max(abs(item) for item in values)
    max_step = max(
        (abs(values[index + 1] - values[index]) for index in range(len(values) - 1)),
        default=0,
    )
    return TestSample(peak=peak, max_step=max_step, frames=len(values))


def _has_sound(attempt: OpenAttempt) -> bool:
    """这一块样本里有没有真实波形（加在 V1 骨架上的那一步，判定是 V2 自己的）。

    两个条件同时成立：峰值到 `SOURCE_PEAK_THRESHOLD`、相邻样本差到 `WAVEFORM_MIN_STEP`。
    V1 试开只判「开起来了没有」；「能打开 ≠ 有声音」按本机实测补：DirectSound 端点能顺利
    打开、交回的却是精确全零，所以多读一小段样本再判。
    """
    if not attempt.opened or attempt.sample is None:
        return False
    return attempt.peak() >= SOURCE_PEAK_THRESHOLD and attempt.max_step() >= WAVEFORM_MIN_STEP


def attempt_reason(attempt: OpenAttempt) -> str:
    """这次试开为什么没成（写进 `detail` 的人话）。"""
    if attempt.opened:
        if attempt.sample is None:
            return "能打开，但一块样本都没读回来"
        return (
            f"能打开，但样本里没有波形（峰值 {attempt.peak()} / 相邻最大变化 "
            f"{attempt.max_step()}）"
        )
    if attempt.error:
        return f"开不了（{attempt.error}）"
    return "开不了"


# 挑选结果


@dataclass(frozen=True)
class AttemptRecord:
    """一条「试过哪几路、各自为什么没成」的留痕（`detail` 的来源）。"""

    endpoint: DeviceEndpoint
    reason: str


@dataclass(frozen=True)
class SelectionOutcome:
    """一路的挑选结果（`device_name` 与 `SourceProbe` 的输入）。

    `status` 只有 `ready` / `probe_failed` 两档，与 `meeting/backends/base.py` 的
    `SOURCE_STATUS` 对得上（`unprobed` 是「还没接采集后端」，真后端不会报它）。
    """

    status: str
    endpoint: DeviceEndpoint | None = None
    detail: str = ""
    attempts: tuple[AttemptRecord, ...] = ()
    notes: tuple[str, ...] = ()
    #: 用户显式配的名字命中得与端点名不一致（前缀匹配兜的那一次）
    matched_prefix: bool = False
    #: 用户显式配的是抓取型虚拟端点
    explicit_pseudo: bool = False

    def device_name(self) -> str | None:
        return None if self.endpoint is None else self.endpoint.name


# 名字匹配


def normalize_name(name: str) -> str:
    """名字的比对形态：去首尾空白、折小写（设备名的匹配一律走它）。"""
    return str(name).strip().casefold()


def token_form(name: str) -> str:
    """认标记词用的形态：在 `normalize_name()` 之上把连字符折成空格、连续空白收一个。

    VB-Audio 的牌子在不同库里写法不一（`CABLE Input (VB-Audio Virtual Cable)` 与
    `CABLE-In-16ch (…)`），折一下才能用同一组常量认出来。设备名的匹配不走这条。
    """
    text = normalize_name(name).replace("-", " ").replace("_", " ")
    return " ".join(text.split())


def _contains(name: str, token: str) -> bool:
    """`name` 里含不含标记词 `token`（先按 `token_form()` 折过，再按去标点形态兜一次）。

    只给 `is_pseudo_capture()` 的两个常量用；loopback 的两个子串判据走 V1 原文的 `in`，
    不经过这里。
    """
    folded = token_form(name)
    if token in folded:
        return True
    compact_name = "".join(char for char in folded if char.isalnum())
    compact_token = "".join(char for char in token_form(token) if char.isalnum())
    return bool(compact_token) and compact_token in compact_name


def name_matches(configured: str, endpoint_name: str) -> tuple[bool, bool]:
    """显式配置的名字命中了没有 → `(命中, 是否靠前缀兜的)`。

    精确名就是 V1 的 `d.get("name") == name`（`recorder.py:386`）。前缀兜是 V2 加的：
    Windows 会把 MME 端点名截断（用户配的通常是完整名），两个方向都要认。
    """
    target = normalize_name(configured)
    candidate = normalize_name(endpoint_name)
    if not target or not candidate:
        return (False, False)
    if target == candidate:
        return (True, False)
    if candidate.startswith(target) or target.startswith(candidate):
        return (True, True)
    return (False, False)


# 抓取型虚拟端点


def is_pseudo_capture(name: str) -> bool:
    """这条输入端点是不是抓取型虚拟端点（判据 = 模块级两个常量同时命中）。

    V1 没有这一条；V2 必须挡住：抓取侧 `CABLE Output` 抓的是远端那一路，当麦克风录下来
    mic 与 loopback 就是同一路内容（任务书 §3.1）。本机真表命中四条：MME `#2`、
    DirectSound `#11`、WASAPI `#21` 的 `CABLE Output (VB-Audio Virtual Cable)`，
    与 WDM-KS `#25` 的 `CABLE Output (VB-Audio Point)`（最后这条牌子标记里没有 "virtual"）。
    """
    return _contains(name, PSEUDO_CAPTURE_EXCLUDE_TOKEN) and _contains(
        name, PSEUDO_CAPTURE_MARKER
    )


# host API 次序（V1 `_resolve_mic_device` 里的 `host_rank`）


def host_rank(host_api: str) -> int:
    """`WASAPI` 0 / `DirectSound` 1 / `MME` 2 / 其余 `HOST_RANK_FALLBACK`（搬自 V1）。"""
    for rk, key in enumerate(HOST_RANK_KEYS):
        if key in host_api:
            return rk
    return HOST_RANK_FALLBACK


#: 兼容别名（`host_rank` 就是 V1 那个名字）
host_api_rank = host_rank


def sort_candidates(endpoints: Iterable[DeviceEndpoint]) -> list[DeviceEndpoint]:
    """候选排序：按 host rank（V1 的 `cands.sort(key=host_rank)`，`recorder.py:401`）。"""
    return sorted(endpoints, key=lambda endpoint: host_rank(endpoint.host_api))


# 麦克风：`_resolve_mic_device` 的搬法（`mnotes_core/recorder.py:371-413`）


def mic_candidates(
    endpoints: Iterable[DeviceEndpoint], configured: str | None
) -> tuple[list[DeviceEndpoint], bool, bool]:
    """麦克风候选 → `(候选表, 是否显式配了抓取型端点, 是否靠前缀兜的)`。

    V1 原文（`recorder.py:379-390`）按 `d.get("name") == name` 查全表、空了回落默认输入。
    V2 三处差异：

      - `if not name` 那一档换成「拿系统默认输入端点的名字当种子」：交付要求是快照报得出
        两路真实设备名，`device=None` 报不出名字。种子取 `is_default=True` 那条（本机 = MME
        `#1` Focusrite）；`#0` 那个 WAVE_MAPPER 别名与机主插的声卡对不上、还会把「系统默认
        输入当下是什么」原样带进 mic 一侧。默认输入本身是抓取型、或表里一条都没标时，
        退回按 host API 排序的第一条非抓取型候选；
      - 候选里滤掉抓取型虚拟端点（`is_pseudo_capture()`），不滤会把远端当麦克风录一遍；
      - 名字两档匹配落到候选集合上：先精确命中，为空才前缀兜一次——两档混在一个集合里
        会让「精确命中的 MME 那条」被「前缀命中的 WASAPI 那条」按 host rank 挤掉，
        第三格返回值是这件事的如实留痕。

    第三个返回值只对显式配置有意义（没配名字时种子是端点表里的原名，一律精确命中）。
    """
    inputs = [endpoint for endpoint in endpoints if endpoint.role == "input"]
    name = (configured or "").strip()
    matched_prefix = False

    if not name:
        # 没配名字：种子取系统默认输入端点（要报得出真名）；抓取型的默认输入不进候选，
        # 那样 mic 与 loopback 会录同一路（任务书 §3.1）。
        seed = next(
            (
                endpoint
                for endpoint in inputs
                if endpoint.is_default and not is_pseudo_capture(endpoint.name)
            ),
            None,
        )
        if seed is None:
            real = [endpoint for endpoint in inputs if not is_pseudo_capture(endpoint.name)]
            if not real:
                return ([], False, False)
            seed = sort_candidates(real)[0]
        name = seed.name

    # 第一档：精确名（V1 的 `d.get("name") == name`）命中几条就是几条
    cands = [
        endpoint
        for endpoint in inputs
        if name_matches(name, endpoint.name) == (True, False)
    ]
    if not cands:
        # 第二档：精确名一条都不中，才用前缀兜一次（MME 端点名会被 Windows 截断）
        cands = [
            endpoint
            for endpoint in inputs
            if name_matches(name, endpoint.name) == (True, True)
        ]
        matched_prefix = bool(cands)
    if not cands:
        # 不回落系统默认输入（V1 的 `using default input` 会把排查引到「已经在用默认输入录」
        # 那条死路上）：候选空表交回 `select_microphone()`，落 `probe_failed`。
        log.warning(
            "mic device '%s' not found（不回落系统默认输入，本路落 probe_failed）", name
        )
        return ([], False, False)
    if not any(is_pseudo_capture(endpoint.name) for endpoint in cands):
        return (cands, False, matched_prefix)
    # 落到抓取型端点上（显式配的，或种子正好是它）：V2 照办并留一句提醒
    log.warning(
        "mic device '%s' 是抓取型虚拟端点（CABLE Output 这类），"
        "它抓的是往 CABLE Input 播出去的声音",
        name,
    )
    return (
        [endpoint for endpoint in cands if is_pseudo_capture(endpoint.name)],
        True,
        matched_prefix,
    )


def select_microphone(
    endpoints: Iterable[DeviceEndpoint],
    configured: str | None,
    test_open: Callable[[DeviceEndpoint], OpenAttempt],
) -> SelectionOutcome:
    """挑麦克风端点：`_resolve_mic_device` 的骨架 + 读回样本那一步。

    加的那一步：循环里的「能开」判定换成 `_has_sound()`——开起来之后读回一小段样本，
    有波形才算数，「能打开但全是零」的端点不会中选；V1 的排序骨架原样不动。

    收尾按任务书 §3 的裁定改过（V1 的 `return cands[0]` 不照搬），收敛分两档：

      - 在打开了的候选里挑：第一条有波形的落 `ready`；一条有波形的都没有时取第一条
        打开了的。波形只用来在候选之间挑，不判这一路生死——试开只看得见零点几秒，
        房间当时安静、远端没出声时读到的就是全零，拿它报故障等于每次安静都亮故障灯；
      - 一条都没打开时落 `probe_failed` 且 `endpoint=None`，`detail` 逐条写清试过哪几条。
        不回落任何从没打开过的候选：报一个打不开的端点上去，界面先显示就绪、真开录时
        立刻掉线（`base.py:46` 与 `phase1/task.md:87` 定的口径）。

    V1 的短路口（`if len(cands) == 1: return cands[0]`）也不再照搬：单候选照样试开，
    「候选只有一条、那条开不了」同样落 `probe_failed`。
    """
    configured_text = (configured or "").strip()
    candidates, explicit_pseudo, matched_prefix = mic_candidates(endpoints, configured_text)
    notes: list[str] = []

    if configured_text:
        if matched_prefix:
            notes.append(
                f"显式配置的 devices.mic = {configured_text!r} 精确名没命中，"
                f"按前缀兜到 {candidates[0].name!r}"
                "（Windows 会把 MME 端点名截断，显式配置因此允许前缀兜）"
            )
        elif not candidates:
            notes.append(
                f"显式配置的 devices.mic = {configured_text!r} 在端点表里没命中"
                "（精确名与前缀都没兜上）"
            )
        if explicit_pseudo:
            notes.append(
                f"显式配置的 devices.mic = {configured_text!r} 是抓取型虚拟端点："
                "它抓的是往 CABLE Input 播出去的声音（远端那一路），"
                "照配不误，但 mic 与 loopback 会录到同一路内容"
            )

    # V1：`if not cands: log.warning(...); return None`（这里如实报「没挑到」）
    if not candidates:
        notes.append("候选为空：端点表里没有匹配的输入端点")
        return SelectionOutcome(
            status=PROBE_FAILED,
            endpoint=None,
            detail=_join(notes, (), None),
            notes=tuple(notes),
            matched_prefix=matched_prefix,
            explicit_pseudo=explicit_pseudo,
        )

    # V1：`cands.sort(key=host_rank)` 后逐个试开、取第一个能开且有波形的
    candidates = sort_candidates(candidates)
    attempts: list[AttemptRecord] = []
    outcomes: list[tuple[DeviceEndpoint, OpenAttempt]] = []
    for endpoint in candidates:
        attempt = test_open(endpoint)
        outcomes.append((endpoint, attempt))
        if attempt.opened and _has_sound(attempt):
            notes.append(
                f"选中 {endpoint.label()}：试开 {PROBE_SAMPLE_RATE} Hz / "
                f"{PROBE_CHANNELS} 声道 / int16 成功，样本峰值 {attempt.peak()}、"
                f"相邻最大变化 {attempt.max_step()}（有真实波形）"
            )
            return SelectionOutcome(
                status=PROBE_READY,
                endpoint=endpoint,
                detail=_join(notes, attempts, endpoint),
                attempts=tuple(attempts),
                notes=tuple(notes),
                matched_prefix=matched_prefix,
                explicit_pseudo=explicit_pseudo,
            )
        attempts.append(AttemptRecord(endpoint, attempt_reason(attempt)))

    # 一条有波形的都没有：取第一条打开了（V1 的 `cands[0]` 只在「打开了」这一档里成立）
    for endpoint, attempt in outcomes:
        if attempt.opened:
            notes.append(
                f"没有一条候选端点交回真实波形，取第一条**打开了**的候选 {endpoint.label()}"
                f"（样本峰值 {attempt.peak()} / 相邻最大变化 {attempt.max_step()}，"
                "这一路仍算 ready：远端没在出声时读到的就是全零，不拿波形判生死）"
            )
            return SelectionOutcome(
                status=PROBE_READY,
                endpoint=endpoint,
                detail=_join(notes, attempts, endpoint),
                attempts=tuple(attempts),
                notes=tuple(notes),
                matched_prefix=matched_prefix,
                explicit_pseudo=explicit_pseudo,
            )

    # 一条都没打开：不回落（V1 在这里返回 `cands[0]`，那是因为它没有 `probe_failed` 可报）
    notes.append(
        f"{len(candidates)} 条候选端点一条都没打开，"
        "没有选中端点（不回落从没打开过的候选：那会先显示就绪、真开录时立刻掉线）"
    )
    return SelectionOutcome(
        status=PROBE_FAILED,
        endpoint=None,
        detail=_join(notes, attempts, None),
        attempts=tuple(attempts),
        notes=tuple(notes),
        matched_prefix=matched_prefix,
        explicit_pseudo=explicit_pseudo,
    )


# loopback：`_resolve_loopback_speaker` 的搬法（`mnotes_core/recorder.py:444-465`）


def loopback_candidates(
    endpoints: Iterable[DeviceEndpoint], configured: str | None
) -> tuple[list[DeviceEndpoint], str]:
    """loopback 候选与它的来源档位 → `(候选表, 档位名)`。

    三级照 V1（`recorder.py:451-465`）：显式名 → `CABLE Input` 且不含 `16ch` → 默认输出
    （默认输出那一档由端点表第一条承担，`sources.list_render_endpoints()` 把
    `default_speaker()` 排在第一位）。两个子串判据逐字用 V1 的 `in` 表达式（大小写敏感的
    原样子串）；不做折形——旧稿曾走 `_contains()` 折大小写与连字符，比 V1 宽，已收回。
    """
    renders = [endpoint for endpoint in endpoints if endpoint.role == "render"]
    name = (configured or "").strip()
    if name:
        for endpoint in renders:
            if endpoint.name == name:
                return ([endpoint], "explicit")
        log.warning("loopback device '%s' not found, using default", name)
    cable = next(
        (
            endpoint
            for endpoint in renders
            if LOOPBACK_PREFERRED_TOKEN in endpoint.name
            and LOOPBACK_EXCLUDED_TOKEN not in endpoint.name
        ),
        None,
    )
    if cable is not None:
        log.info("loopback default: CABLE Input (VB-Audio) -> %s", cable.name)
        return ([cable], "cable")
    if renders:
        return ([renders[0]], "default_speaker")
    return ([], "none")


def select_loopback(
    endpoints: Iterable[DeviceEndpoint],
    configured: str | None,
    test_open: Callable[[DeviceEndpoint], OpenAttempt],
) -> SelectionOutcome:
    """挑 loopback（render）端点：显式名 → `CABLE Input`（不含 16ch）→ 系统默认输出。

    取到的 render 端点由调用方用
    `soundcard.get_microphone(id=str(speaker.name), include_loopback=True)` 打开
    （搬自 `recorder.py:437`；真实那一次在 `meeting/capture/sources.py`）。
    候选同样试开一次并读回样本：`soundcard` 打开的 loopback 在虚拟线没在走音频时交回精确
    全零，那条路照样算「能打开」，所以判定只卡在「能不能打开」这一档上。
    """
    candidates, source_tier = loopback_candidates(endpoints, configured)
    notes: list[str] = []

    if source_tier == "none":
        notes.append("系统里一个 render（播放）端点都没有，loopback 这一路没有可挑的设备")
        return SelectionOutcome(
            status=PROBE_FAILED, endpoint=None, detail=_join(notes, (), None), notes=tuple(notes)
        )

    if (configured or "").strip() and source_tier != "explicit":
        notes.append(
            f"显式配置的 devices.loopback = {configured!r} 在 render 端点表里没命中，"
            f"回退到 {'CABLE Input（不含 16ch）' if source_tier == 'cable' else '系统默认输出'}"
        )
    if source_tier == "cable":
        notes.append(
            "按名字含 'CABLE Input' 且不含 '16ch' 挑（VB-Cable 有两个端点，飞书走 2ch，"
            "选 16ch 会静音）"
        )
    if source_tier == "default_speaker":
        notes.append(
            "没有命中 CABLE Input 类端点，取系统默认输出"
            "（本机装着 VB-Cable 时，远端声音通常要经它才抓得到）"
        )

    attempts: list[AttemptRecord] = []
    for endpoint in candidates:
        attempt = test_open(endpoint)
        if attempt.opened:
            notes.append(
                f"选中 {endpoint.label()}：以 {PROBE_SAMPLE_RATE} Hz 经 "
                "soundcard.get_microphone(include_loopback=True) 打开成功"
                f"（样本峰值 {attempt.peak()}）"
            )
            return SelectionOutcome(
                status=PROBE_READY,
                endpoint=endpoint,
                detail=_join(notes, attempts, endpoint),
                attempts=tuple(attempts),
                notes=tuple(notes),
            )
        attempts.append(AttemptRecord(endpoint, attempt_reason(attempt)))

    notes.append("挑中的 render 端点打不开（都试过了，结果见下面）")
    return SelectionOutcome(
        status=PROBE_FAILED,
        endpoint=None,
        detail=_join(notes, attempts, None),
        attempts=tuple(attempts),
        notes=tuple(notes),
    )


# detail 拼装


def _join(
    notes: Sequence[str], attempts: Sequence[AttemptRecord], chosen: DeviceEndpoint | None
) -> str:
    """把「结论 + 试过哪几条」拼成一句人话（`SourceProbe.detail`）。"""
    parts = [note for note in notes if note]
    skipped = [
        f"{record.endpoint.label()}：{record.reason}"
        for record in attempts
        if record.endpoint != chosen
    ]
    if skipped:
        parts.append("试过但没选上：" + "；".join(skipped))
    return "。".join(parts) if parts else ""


__all__ = [
    "ENDPOINT_ROLES",
    "HOST_RANK_FALLBACK",
    "HOST_RANK_KEYS",
    "LOOPBACK_EXCLUDED_TOKEN",
    "LOOPBACK_PREFERRED_TOKEN",
    "PROBE_CHANNELS",
    "PROBE_FAILED",
    "PROBE_FRAME_COUNT",
    "PROBE_READY",
    "PROBE_SAMPLE_RATE",
    "PSEUDO_CAPTURE_EXCLUDE_TOKEN",
    "PSEUDO_CAPTURE_MARKER",
    "SOURCE_PEAK_THRESHOLD",
    "WAVEFORM_MIN_STEP",
    "AttemptRecord",
    "DeviceEndpoint",
    "OpenAttempt",
    "SelectionOutcome",
    "TestSample",
    "attempt_reason",
    "host_api_rank",
    "host_rank",
    "is_pseudo_capture",
    "loopback_candidates",
    "measure_samples",
    "mic_candidates",
    "name_matches",
    "normalize_name",
    "select_loopback",
    "select_microphone",
    "sort_candidates",
    "token_form",
]
