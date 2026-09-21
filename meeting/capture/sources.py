"""真实适配层 —— `sounddevice` / `soundcard`，**函数内惰性 import**（`dca4539b` phase1 §3）。

本模块是**唯一**碰声卡库的地方，分工照 `meeting/capture/devices.py` 的模块说明：

  - `meeting/capture/devices.py` 拿这里的端点表与试开结果做挑选（纯逻辑，可单测）；
  - 这里只负责「问系统有哪些端点」与「以 16k 试开一次并把读回的样本量出来」。

**为什么惰性 import**：`import meeting.capture.devices` 必须能在没装声卡库的环境里过；
`sounddevice` / `soundcard` 只在下面这些函数体内 import，模块级一行都没有。两个库同时用是
有意的 —— `sounddevice 0.5.5` 没有 WASAPI loopback，loopback 那条路只能走 `soundcard`
（spec 的「坑与注意」第 1 条）。

**打开方式照 V1**（`meeting-notes/mnotes_core/recorder.py`）：

  - mic 走 `sd.InputStream`（`_open_mic`，`:415-426`）：16k / 1 声道 / int16；
  - loopback 走 `sc.get_microphone(id=str(speaker.name), include_loopback=True)`
    拿到的录音器（`_open_loopback`，`:428-442`）。
  这里只做**试开一次并读回一小段样本**（试开判定的输入），持续采集归下一个执行单元；
  V1 在采集线程里那一手 `ctypes.windll.ole32.CoInitializeEx(None, 0)`（`_loopback_pump`，
  `:469-474`）属于那条线程的事，也归下一段。
"""

from __future__ import annotations

from typing import Any, Iterable

from meeting.capture.devices import (
    PROBE_CHANNELS,
    PROBE_FRAME_COUNT,
    PROBE_SAMPLE_RATE,
    DeviceEndpoint,
    OpenAttempt,
    measure_samples,
)

#: `soundcard` 的默认 render 端点在本模块的端点表里恒排第 0 条（`select_loopback()` 的
#: 「系统默认 render 端点」那一档因此不必再知道库的存在）
DEFAULT_RENDER_INDEX = 0

#: int16 的量程（把 float 样本折算成 int16 量级用；判定看的只是量级与相邻差）
_INT16_MAX = 32767


def _import_sounddevice() -> Any:
    import sounddevice  # noqa: PLC0415 - 惰性 import 是有意的（见模块说明）

    return sounddevice


def _import_soundcard() -> Any:
    import soundcard  # noqa: PLC0415 - 惰性 import 是有意的（见模块说明）

    return soundcard


def _error_text(exc: BaseException) -> str:
    """把库抛出来的异常收成一句人话（`detail` 里要能看懂是哪一档错）。"""
    text = str(exc).strip() or exc.__class__.__name__
    return f"{exc.__class__.__name__}: {text}"


def _to_int16(frames: Iterable[Any]) -> list[int]:
    """把库交回来的样本收成 int16 量级的整数表（`soundcard` 交回的是 -1..1 的 float）。"""
    values: list[int] = []
    for frame in frames:
        try:
            value = float(frame)
        except (TypeError, ValueError):
            continue
        scaled = int(round(value * _INT16_MAX))
        values.append(max(-_INT16_MAX - 1, min(_INT16_MAX, scaled)))
    return values


def _flatten(data: Any) -> list[Any]:
    """把样本块摊平成一维。

    `sounddevice` 的 `stream.read()` 与 `soundcard` 的 `record()` 都交回
    `(块数, 声道数)` 的二维形状，两种都收；已经是平的就照原样交给 `list()`。
    收不动（元素既不是序列也不是数）一律抛 `TypeError`，由调用方记成「样本读不出来」。
    """
    if hasattr(data, "reshape"):
        try:
            return list(data.reshape(-1))
        except Exception:  # noqa: BLE001 - 摊不动就走下面那条逐层收
            pass
    flat: list[Any] = []
    for item in data:
        if isinstance(item, (list, tuple)):
            flat.extend(item)
        elif hasattr(item, "__len__") and not isinstance(item, (str, bytes)):
            flat.extend(list(item))
        else:
            flat.append(item)
    return flat


# ---------------------------------------------------------------------------
# 端点枚举
# ---------------------------------------------------------------------------


def _default_input_index(sounddevice: Any) -> int | None:
    """系统默认输入端点的序号（**只读**，不开流、不采集任何声音）。

    先读 `sd.default.device`（`(输入, 输出)` 一对；`-1` 表示「按 PortAudio 的默认来」），
    读不出来或拿到 `-1` 时退到 `sd.query_devices(kind="input")`，由库把默认值解析成一个真实
    序号。两条都是只读枚举。本机实测两条都给 `1`（MME `Analogue 1 + 2 (2- Focusrite US`）。

    这个序号就是 `mic_candidates()` 在没配 `devices.mic` 时的种子取值口。本机读数：
    `#1` 是 Focusrite；序号最小的 `#0` 是 `Microsoft 声音映射器 - Input`（Windows 的
    WAVE_MAPPER，跟随系统默认输入设备），把它当成种子报出来的是一个 mapper 别名，
    并且会把「系统默认输入当下是什么」原样带进 mic 那一侧。
    """
    index: int | None = None
    try:
        index = int(sounddevice.default.device[0])
    except Exception:  # noqa: BLE001 - 读不出来按「没有默认输入」处理，退到下面那条
        index = None
    if index is not None and index >= 0:
        return index
    try:
        return int(sounddevice.query_devices(kind="input")["index"])
    except Exception:  # noqa: BLE001 - 同上的口径：退不出来就当没有默认输入端点
        return None


def list_input_endpoints() -> list[DeviceEndpoint]:
    """`sounddevice` 的输入端点表（抓取型；含各 host API 下的同名端点）。

    `max_input_channels < 1` 的条目在这里就被滤掉（V1 的 `d.get("max_input_channels", 0) > 0`
    那一判），系统默认输入那一条按 `_default_input_index()` 标成 `is_default=True`。
    """
    sounddevice = _import_sounddevice()
    host_apis = [str(item.get("name") or "") for item in sounddevice.query_hostapis()]
    default_index = _default_input_index(sounddevice)
    endpoints: list[DeviceEndpoint] = []
    for index, device in enumerate(sounddevice.query_devices()):
        try:
            channels = int(device.get("max_input_channels") or 0)
        except (TypeError, ValueError):
            channels = 0
        if channels < 1:
            continue
        try:
            host_api = host_apis[int(device.get("hostapi"))]
        except (IndexError, TypeError, ValueError):
            host_api = ""
        endpoints.append(
            DeviceEndpoint(
                name=str(device.get("name") or ""),
                host_api=host_api,
                role="input",
                index=index,
                channels=channels,
                is_default=index == default_index,
            )
        )
    return endpoints


def list_render_endpoints() -> list[DeviceEndpoint]:
    """`soundcard` 的 render 端点表（**系统默认那条恒在第一**，见 `DEFAULT_RENDER_INDEX`）。"""
    soundcard = _import_soundcard()
    default_name = ""
    try:
        speaker = soundcard.default_speaker()
        default_name = "" if speaker is None else str(speaker.name)
    except Exception:  # noqa: BLE001 - 取默认设备失败不该让枚举整体失败
        default_name = ""

    names: list[str] = []
    try:
        for speaker in soundcard.all_speakers():
            name = str(getattr(speaker, "name", "") or "")
            if name and name not in names:
                names.append(name)
    except Exception:  # noqa: BLE001 - 同上：枚举失败按「没有端点」如实报出
        return []

    if default_name in names:
        names.remove(default_name)
        names.insert(DEFAULT_RENDER_INDEX, default_name)
    return [
        DeviceEndpoint(name=name, host_api="", role="render", index=index)
        for index, name in enumerate(names)
    ]


# ---------------------------------------------------------------------------
# 试开（读回样本）
# ---------------------------------------------------------------------------


def test_open_input(endpoint: DeviceEndpoint) -> OpenAttempt:
    """以 16k / 1 声道 / int16 试开一条输入端点，**并读回一小段样本**。

    试开这一手搬自 V1 `_resolve_mic_device`（`mnotes_core/recorder.py:403-410`）里的
    `with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16", device=i)`；
    读回样本那一步是本段加的（V1 那里 `with` 顺利退出就算能开）。

    读回这一步是必须的：本机实测 DirectSound 的 Focusrite 端点能以 16k 打开，但交回的是
    精确的全零（静默失败），光看「能打开」会把麦克风挑到一条没有声音的端点上。
    """
    sounddevice = _import_sounddevice()
    try:
        with sounddevice.InputStream(
            device=endpoint.index,
            samplerate=PROBE_SAMPLE_RATE,
            channels=PROBE_CHANNELS,
            dtype="int16",
            blocksize=PROBE_FRAME_COUNT,
        ) as stream:
            chunk, _overflowed = stream.read(PROBE_FRAME_COUNT)
    except Exception as exc:  # noqa: BLE001 - 任何开流失败都如实留痕（含 PortAudioError）
        # **只收开流失败**（不从 `Exception` 继承的东西照原样往上抛）：测试里那道「一碰真声卡
        # 就炸」的看门抛的是 `tests/conftest.py` 的 `RealCaptureForbidden`（`BaseException`
        # 直系子类），它必须炸到用例上，不许在这里被收成一条「设备开不了」的业务失败。
        return OpenAttempt(opened=False, error=_error_text(exc))

    try:
        # `sounddevice` 交回的是 (块数, 声道数) 的二维数组，**先摊平**再量：摊平之前每个元素
        # 是一行（单元素数组），`abs()` / `max()` 会落到数组上，判定当场失真。
        frames = [int(item) for item in _flatten(chunk)]
    except (TypeError, ValueError) as exc:
        return OpenAttempt(
            opened=True,
            sample=None,
            error=f"流开起来了，但读回的样本读不出来（{type(exc).__name__}: {exc}）",
        )
    return OpenAttempt(opened=True, sample=measure_samples(frames))


def loopback_recorder(endpoint: DeviceEndpoint) -> Any:
    """取 render 端点的 loopback 录音器。

    搬自 V1 `_open_loopback`（`mnotes_core/recorder.py:437`）：
    `mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)`。
    取不到（名字对不上 / 系统里没有这条端点）时返回 `None`。
    """
    soundcard = _import_soundcard()
    try:
        return soundcard.get_microphone(id=str(endpoint.name), include_loopback=True)
    except Exception:  # noqa: BLE001 - 取不到按「这条端点打不开」处理
        return None


def test_open_loopback(endpoint: DeviceEndpoint) -> OpenAttempt:
    """以 16k / 1 声道试开一条 render 端点的 loopback，并读回一小段样本。

    `soundcard` 打开的一定是有波形的（虚拟线没在走音频时交回精确的全零，那条路照样算
    「能打开」），所以这里的样本只用于 `detail` 里报出峰值，不参与「能不能用」的判定。
    """
    recorder = loopback_recorder(endpoint)
    if recorder is None:
        return OpenAttempt(opened=False, error="soundcard 里没有这条端点的 loopback")
    try:
        with recorder.recorder(
            samplerate=PROBE_SAMPLE_RATE, channels=PROBE_CHANNELS, blocksize=PROBE_FRAME_COUNT
        ) as handle:
            data = handle.record(numframes=PROBE_FRAME_COUNT)
    except Exception as exc:  # noqa: BLE001 - 同上：开流失败如实留痕
        return OpenAttempt(opened=False, error=_error_text(exc))

    try:
        flat = _flatten(data)
    except TypeError as exc:
        return OpenAttempt(
            opened=False, error=f"读回的样本读不出来（{type(exc).__name__}: {exc}）"
        )
    return OpenAttempt(opened=True, sample=measure_samples(_to_int16(flat)))


__all__ = [
    "DEFAULT_RENDER_INDEX",
    "list_input_endpoints",
    "list_render_endpoints",
    "loopback_recorder",
    "test_open_input",
    "test_open_loopback",
]
