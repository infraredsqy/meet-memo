"""真实适配层——`sounddevice` / `soundcard`，函数体内惰性 import。

本模块是唯一碰声卡库的地方：`meeting/capture/devices.py` 拿这里的端点表与试开结果做挑选
（纯逻辑，可单测）；这里负责问系统有哪些端点、以 16k 试开一次并量出样本，以及持续采集的
两路源（`SounddeviceMicSource` / `SoundcardLoopbackSource`）。

惰性 import 是有意的：`import meeting.capture.devices` 必须能在没装声卡库的环境里过。
两个库同时用也有意——`sounddevice 0.5.5` 没有 WASAPI loopback，loopback 那条路只能走
`soundcard`（spec「坑与注意」第 1 条）。打开方式照 V1（`mnotes_core/recorder.py`）：
mic 走 `sd.InputStream`（16k / 1 声道 / int16）；loopback 走
`sc.get_microphone(id=str(speaker.name), include_loopback=True)`；V1 采集线程里那手
`ctypes.windll.ole32.CoInitializeEx(None, 0)` 跟着 loopback 源一起搬过来。
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    import numpy as np  # noqa: F401 - 只进注解（numpy 在函数体内惰性 import，见模块说明）

from meeting.capture.devices import (
    PROBE_CHANNELS,
    PROBE_FRAME_COUNT,
    PROBE_SAMPLE_RATE,
    DeviceEndpoint,
    OpenAttempt,
    measure_samples,
)

_log = logging.getLogger("meeting.capture.sources")

#: `soundcard` 的默认 render 端点在本模块的端点表里恒排第 0 条（`select_loopback()` 的
#: 「系统默认 render 端点」那一档因此不必再知道库的存在）
DEFAULT_RENDER_INDEX = 0

#: int16 的量程（把 float 样本折算成 int16 量级用；判定看的只是量级与相邻差）
_INT16_MAX = 32767

#: 采集流的形状（照 V1 `mnotes_core/config.py` 的 `SAMPLE_RATE` / `CHANNELS`）
SAMPLE_RATE = PROBE_SAMPLE_RATE
CHANNELS = PROBE_CHANNELS

#: loopback 一次读多少帧：0.1 秒 @16k（照 V1 `_loopback_pump` 的 `numframes=1600`）
LOOPBACK_FRAME_COUNT = 1600

#: 读侧队列的积压上限（块）：写侧跟不上时丢最旧的块并留一条 warning（不许静默膨胀）
QUEUE_MAX_CHUNKS = 512

#: mic 开流的输入延迟档（秒，`InputStream` 的 `latency`）。开场每场都在同进程重载
#: 会中 ASR 模型（设计意图第 21 条「用完即关」），加载的 GIL 压力会把麦克风回调饿过
#: 驱动缓冲——不传 `latency` 的原档真机探针实测缓冲 ≈26 ms、最坏饿死 335 ms、
#: 丢 ≈0.3 s 音频；这一档实测缓冲标称 100 ms（环形约 400 ms），同等窗口零溢出零丢失。
#: 实测表与探针脚本见 spec `per-session-model-reload-starves-capture`（2026-11-06
#: 真机探针）。只作用于 mic 这一条路：loopback 是 `soundcard` 的另一套采集，探针
#: 未测，不跟着改。
MIC_INPUT_LATENCY_SECONDS = 2.0


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


# 端点枚举


def _default_input_index(sounddevice: Any) -> int | None:
    """系统默认输入端点的序号（只读，不开流、不采集任何声音）。

    先读 `sd.default.device`（`(输入, 输出)` 一对；`-1` = 按 PortAudio 的默认来），
    读不出或拿到 `-1` 时退到 `sd.query_devices(kind="input")`。本机实测两条都给 `1`
    （MME Focusrite）。这个序号就是 `mic_candidates()` 没配 `devices.mic` 时的种子取值口；
    序号最小的 `#0` 是 WAVE_MAPPER（跟随系统默认输入、报出的是跟着变的别名），不能当种子。
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


# 试开（读回样本）


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
        # 只收开流失败（不从 `Exception` 继承的东西照原样往上抛）：测试里那道「一碰真声卡
        # 就炸」的看门抛的是 `tests/conftest.py` 的 `RealCaptureForbidden`（`BaseException`
        # 直系子类），必须炸到用例上，不许被收成「设备开不了」的业务失败。
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


# 持续采集：两路各自的读侧（这一侧的读法照 V1，交给写侧去落盘）


class _QueueReader:
    """读侧共用的取块口：一个队列 + 最近一块样本 + 收队标志。

    队列形状照 V1 的 `MeetingRecorder.audio_queue`（`:315`），只是一路一个（V1 两路共用、
    按 `(tag, bytes)` 打标签）。满的时候丢最旧的块并留 warning：采集要跟上的是实时流，
    压着旧块只会让延迟越滚越大。
    """

    def __init__(self, label: str) -> None:
        import numpy as np

        self.label = label
        self.queue: queue.Queue[bytes] = queue.Queue(maxsize=QUEUE_MAX_CHUNKS)
        self.stop_event = threading.Event()
        self.dropped = 0
        self._latest = np.empty(0, dtype=np.int16)
        self._lock = threading.Lock()

    def put(self, data: bytes, samples: np.ndarray) -> None:
        """收块：先更新电平素材，再入队（满了丢最旧的一块）。"""
        with self._lock:
            self._latest = samples
        try:
            self.queue.put_nowait(bytes(data))
        except queue.Full:
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(bytes(data))
            except queue.Empty:  # pragma: no cover - 竞态下队列被取空，直接补投
                pass
            self.dropped += 1
            if self.dropped % 50 == 1:
                _log.warning(
                    "%s 这一路的写侧跟不上，已丢掉 %d 块（丢的是最旧的块）",
                    self.label, self.dropped,
                )

    def read(self, timeout: float) -> bytes | None:
        """取下一块；收队时返回 `None`（引擎据此收队并报 `SourceLost`）。"""
        if self.stop_event.is_set():
            return None
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return b""

    def latest_samples(self) -> np.ndarray:
        with self._lock:
            return self._latest

    def clear(self) -> None:
        """收队时把还压着的块丢掉（源已经停了，这些块不再有价值）。"""
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                return


class SounddeviceMicSource:
    """mic 的持续采集：`sd.InputStream` 的回调把块投进队列（§3 的 `mic` 源）。

    搬自 V1 `_open_mic`（`recorder.py:415-426`）与 `_mic_callback`（`:488-493`）；
    接口层替换：`self.mic_device` → 构造时收到的 `endpoint`；`self.recording` → `stop()`；
    `self.audio_queue.put(...)` → `_reader.put(...)`。回调里不做电平计算——电平刻度在
    `engine.py` 的刻度线程里按 `frame_interval` 走，与有没有新块无关；这里只把样本交给
    读侧当素材（V2 新增）。
    """

    #: 这一路的标识（引擎按 `source` 登记）
    name = "mic"

    def __init__(self, endpoint: DeviceEndpoint) -> None:
        self.endpoint = endpoint
        self._reader = _QueueReader("mic")
        self._stream: Any = None

    def start(self) -> None:
        """打开麦克风流（指定设备或默认输入，16k mono；延迟档见 `MIC_INPUT_LATENCY_SECONDS`）。"""
        sounddevice = _import_sounddevice()
        self._reader.stop_event.clear()
        stream = sounddevice.InputStream(
            device=self.endpoint.index,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            latency=MIC_INPUT_LATENCY_SECONDS,
            callback=self._callback,
        )
        stream.start()
        self._stream = stream

    def stop(self) -> None:
        """停流 + 关流（**幂等**，照 V1 `stop_recording` 里的 `stream.stop()` / `stream.close()`）。"""
        self._reader.stop_event.set()
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception:  # noqa: BLE001 - 关流失败不掩盖收尾结论
            _log.warning("mic 的流停不下来（如实登记，不静默）", exc_info=True)

    def read(self, timeout: float) -> bytes | None:
        return self._reader.read(timeout)

    def latest_samples(self) -> np.ndarray:
        return self._reader.latest_samples()

    @property
    def dropped(self) -> int:
        """写侧跟不上时丢掉的块数（如实登记，`/debug` 与测试看它）。"""
        return self._reader.dropped

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        """麦克风回调——放入音频队列（搬自 V1 `_mic_callback`，`recorder.py:488-493`）。

        V1 的三行逐字保留；末尾多一步「把这块样本交给读侧当电平素材」。
        """
        import numpy as np

        if status:
            _log.warning("audio(mic): %s", status)
        if self._reader.stop_event.is_set():
            return
        samples = np.asarray(indata, dtype=np.int16).reshape(-1)
        self._reader.put(samples.tobytes(), samples)


class SoundcardLoopbackSource:
    """loopback 的持续采集：专用线程 + `CoInitializeEx` + `soundcard` 的 recorder。

    搬自 V1 `_open_loopback`（`recorder.py:428-442`）与 `_loopback_pump`（`:467-486`）；
    接口层替换与 mic 源同一档（`endpoint` / `stop()` / `_reader.put(...)`）。
    线程体第一件事仍是 `ctypes.windll.ole32.CoInitializeEx(None, 0)`：`soundcard` 依赖
    COM（WASAPI），子线程不手动初始化 apartment 会报 `0x800401F0`，而那个失败会被
    `except: pass` 吞掉，只能从「采到的是全零」倒推出来（`docs/v1-lineage.md` 的坑 C2）。
    """

    #: 这一路的标识（引擎按 `source` 登记）
    name = "loopback"

    def __init__(self, endpoint: DeviceEndpoint) -> None:
        self.endpoint = endpoint
        self._reader = _QueueReader("loopback")
        self._thread: threading.Thread | None = None
        self._recorder: Any = None
        #: 采集线程死在运行期时留给读侧的那句话（读侧据此如实报一条 `SourceLost`）
        self._dead_note: str | None = None

    def start(self) -> None:
        """取 recorder 并起采集线程（照 V1 `_open_loopback`，`recorder.py:436-442`）。"""
        recorder = loopback_recorder(self.endpoint)
        if recorder is None:
            raise RuntimeError(
                f"soundcard 里没有这条端点的 loopback：{self.endpoint.label()}"
            )
        _log.info("loopback source: %s", self.endpoint.name)
        self._recorder = recorder
        self._reader.stop_event.clear()
        self._thread = threading.Thread(
            target=self._pump, args=(recorder,), name="capture-loopback-pump", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """收队：置停止标志、join 采集线程（照 V1 `stop_recording` 的 `join(timeout=2.0)`）。"""
        self._reader.stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
            if thread.is_alive():
                _log.warning(
                    "loopback 采集线程在 2.0s 内没有退出（daemon 线程，不阻塞进程退出）"
                )
        self._recorder = None
        self._reader.clear()

    def read(self, timeout: float) -> bytes | None:
        """取下一块；采集线程死在运行期时如实报错（引擎据此报一条 `SourceLost`）。"""
        if self._dead_note is not None:
            note, self._dead_note = self._dead_note, None
            raise RuntimeError(note)
        return self._reader.read(timeout)

    def latest_samples(self) -> np.ndarray:
        return self._reader.latest_samples()

    @property
    def dropped(self) -> int:
        """写侧跟不上时丢掉的块数（如实登记）。"""
        return self._reader.dropped

    def _pump(self, recorder: Any) -> None:
        """捕获线程：持续读 loopback（16k float32 stereo）→ 转 mono int16 → 队列。

        搬自 V1 `_loopback_pump`（`recorder.py:467-486`），循环体内逐字保留
        （`record(numframes=1600)`、`mean(axis=1)` 降 mono、clip 到 int16、布尔停止标志）；
        接口层两处替换同上，顺带把样本留作电平素材。退出时 recorder 由 `with` 释放。
        """
        import numpy as np

        try:
            # soundcard 依赖 COM（WASAPI），子线程必须手动初始化 apartment
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
        except Exception:
            pass
        try:
            with recorder.recorder(samplerate=SAMPLE_RATE) as rec:
                while not self._reader.stop_event.is_set():
                    data = rec.record(numframes=LOOPBACK_FRAME_COUNT)  # 0.1s @16k
                    if self._reader.stop_event.is_set():
                        break
                    mono = data.mean(axis=1) if data.ndim > 1 and data.shape[1] > 1 else data
                    audio16 = np.clip(np.rint(mono * 32767), -32768, 32767).astype(np.int16)
                    self._reader.put(audio16.tobytes(), audio16)
        except Exception as exc:
            if not self._reader.stop_event.is_set():
                _log.error("loopback pump error: %s", exc)
                # 采集线程死在运行期：把原因留给读侧，引擎据此报一条 `SourceLost`
                # （V1 这里只有一行日志；「不能静默出事」是 V2 契约的要求）
                self._dead_note = f"loopback 采集线程出错：{type(exc).__name__}: {exc}"


__all__ = [
    "CHANNELS",
    "DEFAULT_RENDER_INDEX",
    "LOOPBACK_FRAME_COUNT",
    "MIC_INPUT_LATENCY_SECONDS",
    "QUEUE_MAX_CHUNKS",
    "SAMPLE_RATE",
    "SoundcardLoopbackSource",
    "SounddeviceMicSource",
    "list_input_endpoints",
    "list_render_endpoints",
    "loopback_recorder",
    "test_open_input",
    "test_open_loopback",
]
