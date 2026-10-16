"""`tests/_capture_faulty.py` —— 端到端用例专用的「问题采集后端」挂件（**pytest 不收集本文件**）。

定位与挂件方式照 `tests/_drain_fakes.py`（跨文件共用假件的唯一落点、旋钮走环境变量、
经既有配置键挂进真运行时），有两处按本单元的实际用法收窄：

  - **挂进进程内真 `ServiceRuntime`，不起子进程**（任务书 §5 的验证口径与本单元的时间预算
    共同裁定）：配置键写 `capture.backend = "tests._capture_faulty:FaultyCaptureBackend"`
    —— `module:Class` 是 `meeting/backends/registry.py` 的既有取值口径，pytest 进程的
    `sys.path` 里有仓库根，`tests._capture_faulty` 因此可直接导入。`tests/_drain_fakes.py`
    需要「把模块写进临时目录 + PYTHONPATH」是因为它的使用者是**真服务子进程**；本文件的
    使用者全在进程内，配置键这一条通道原样够用，不必再复制一份写盘工厂。
  - **本文件带进场的是「真后端 + 假源」这一对搭档**：`FaultyCaptureBackend` 继承生产类
    `meeting.backends.capture.CaptureBackend`，只把「端点表 / 试开 / 造源」三个接缝换成
    假件；`probe()` / `start()` / 归档 / 电平刻度 / 收尾各步走的都是生产代码。`registry`
    的 `capabilities_of()` 按实现类的 `__module__` 判替身（只认 `meeting.backends.null` /
    `meeting.backends.dev`），本类的 `__module__` 是 `tests._capture_faulty`，两项收尾能力
    如实为 `True` —— 收尾第 1、2 步因此真的调它。

**可预期样本序列**（核对的根基）：每一路的每一块内容由 `chunk_bytes(source, index)` 一处
定死 —— 样本常值携带**块号**（mic 基值 2000、loopback 基值 12000，避免两路混ReadStream时
难以分辨），且按「每 5 块（= 0.5 秒电平帧）交替有声区 / 静音区」开关。于是：

  - 归档的期望字节就是 `expected_archive_bytes(source, fed)`：逐字节可比，丢一块即错位，
    第一处分歧的样本序号直接指认被丢的块；
  - 每 5 块一区让 0.5 秒帧整帧落在同一段（整帧有声或整帧静音），「有声帧 / 静音帧」两个
    计数因此有确定的期望值，不随喂块节奏漂。

**旋钮（环境变量，`start()` 造源那一刻现读**，与 `_drain_fakes.py` 同一手法：registry
无参实例化，构造参数进不了配置键那条路）：

  ================================  ==========================================
  `MEETING_TEST_CAPTURE_ERROR_AT_MIC`       mic 在喂完第 N 块后的下一次 `read()` 抛错（不设 = 不抛）
  `MEETING_TEST_CAPTURE_ERROR_AT_LOOPBACK`  loopback 同上
  `MEETING_TEST_CAPTURE_PACE_SECONDS`       交一块之后歇多久（缺省 0.1：与真采集一个块 0.1 秒的节奏对齐；0 = 能多快给多快）
  `MEETING_TEST_CAPTURE_CHUNKS`             最多喂多少块（不设 = 喂到收尾为止）
  `MEETING_TEST_CAPTURE_DELIVER_CHUNKS_MIC`        mic 喂满 N 块后活着但不再出块（音频终点钉在确定块边界）
  `MEETING_TEST_CAPTURE_DELIVER_CHUNKS_LOOPBACK`   loopback 同上
  `MEETING_TEST_CAPTURE_REOPEN_ERROR`              点名的那几路在**重新打开**时开不起来（`mic` / `mic,loopback`）
  ================================  ==========================================

「第 N 块后抛错」的语义与 `tests/test_capture_engine.py` 的 `FakeCaptureSource.error_at`
一致：第 N 块**正常交付并写入归档**，之后那一次 `read()` 才抛 ——「运行期掉一路」验的是
这条路（那一路真的起来、真的在采之后才死），不是「启动失败」。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import numpy as np

from meeting.backends.capture import CaptureBackend
from meeting.capture.devices import DeviceEndpoint, OpenAttempt, measure_samples

# ---------------------------------------------------------------------------
# 假端点表与试开结果（`probe()` 与 `start()` 的挑选步骤都用它们；一个真声卡入口都不碰）
# ---------------------------------------------------------------------------

#: 假端点表（形状照本机实测：同名端点在不同 host API 下并存；名字带 Faulty 以便在
#: 快照 `sources[*].device_name` 里一眼认出「本场挂的是这份假件」）
FAKE_MIC = DeviceEndpoint("假-麦克风 (Faulty)", "MME", "input", index=1, is_default=True)
FAKE_RENDER = DeviceEndpoint("假-扬声器 (Faulty)", "", "render", index=0)


def wave_attempt() -> OpenAttempt:
    """能打开、也有波形的试开结果（形状照 `tests/test_capture_backend.py` 的同名词）。"""
    return OpenAttempt(
        opened=True,
        sample=measure_samples([0, 120, -300, 44, 900, -12, 7, -400, 220, 0]),
    )


# ---------------------------------------------------------------------------
# 可预期样本序列：一处定死，假源喂什么、核对比什么，都从这几个函数取
# ---------------------------------------------------------------------------

#: 一块 0.1 秒的 16k / 单声道 / int16 样本（3200 字节；与真采集同速）
CHUNK_FRAMES = 1600
CHUNK_BYTES = CHUNK_FRAMES * 2

#: 几块凑成一个 0.5 秒电平帧（0.5 / 0.1）
FRAME_CHUNKS = 5

#: 有声区各路的样本常值基值（块号叠加在其上；两路基值拉开，混了也能认出是哪一路）
_MIC_BASE = 2000
_LOOPBACK_BASE = 12000


def chunk_value(source: str, index: int) -> int:
    """第 `index` 块的样本常值：块号可辨认，且每 5 块（0.5 秒）交替有声 / 静音。"""
    if (index // FRAME_CHUNKS) % 2 == 1:
        return 0  # 静音区：整块全零（重放电平 = -120 dBFS 的整帧）
    return (_MIC_BASE if source == "mic" else _LOOPBACK_BASE) + index


def chunk_bytes(source: str, index: int) -> bytes:
    """第 `index` 块的字节（核对口径：归档里第 `index` 块就该是这一段）。"""
    return np.full(CHUNK_FRAMES, chunk_value(source, index), dtype=np.int16).tobytes()


def expected_archive_bytes(source: str, fed: int) -> bytes:
    """喂进 `fed` 块之后，这一路归档**应有的全部字节**（逐字节核对的期望侧）。"""
    return b"".join(chunk_bytes(source, index) for index in range(fed))


# ---------------------------------------------------------------------------
# 旋钮（环境变量，造源那一刻现读）
# ---------------------------------------------------------------------------

ERROR_AT_MIC_ENV = "MEETING_TEST_CAPTURE_ERROR_AT_MIC"
ERROR_AT_LOOPBACK_ENV = "MEETING_TEST_CAPTURE_ERROR_AT_LOOPBACK"
PACE_SECONDS_ENV = "MEETING_TEST_CAPTURE_PACE_SECONDS"
CHUNKS_ENV = "MEETING_TEST_CAPTURE_CHUNKS"
#: 交付上限：喂满 N 块之后这一路**继续活着但不再出块**（`read()` 给空）。
#: 与 `CHUNKS_ENV` 的区别：那个只停喂不封顶（用例收尾时还可能再来块），这个把音频
#: 的终点钉在**确定的块边界**上 —— 端到端的墙钟核对靠它做到确定性：取墙钟读数之前
#: 最后一块已经落定、之后不可能再有块落进来，`|样本数 − 墙钟 × 16000|` 因此稳定地
#: 落在一个采集块以内，不会随「stop 赶没赶上下一块」翻转。
DELIVER_MIC_ENV = "MEETING_TEST_CAPTURE_DELIVER_CHUNKS_MIC"
DELIVER_LOOPBACK_ENV = "MEETING_TEST_CAPTURE_DELIVER_CHUNKS_LOOPBACK"
#: 「重新打开这一路时打不开」：值是 `mic` / `loopback`（或逗号分隔的两个），
#: 这一段里**第二次及以后**为那一路造的源一 `start()` 就抛错（第一次造源照常成功，
#: 否则这一场根本开不起来）。用来验 `set_source(active=true)` 的失败口径。
REOPEN_ERROR_ENV = "MEETING_TEST_CAPTURE_REOPEN_ERROR"

#: 不设 `PACE_SECONDS_ENV` 时的块节奏（与真采集一个块 0.1 秒同速；端到端用例靠它让
#: 「喂进去的块数 × 每块样本数」与墙钟秒数可换算）
DEFAULT_PACE_SECONDS = 0.1


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return None
    return int(raw)


def _reopen_error_sources() -> set[str]:
    """`REOPEN_ERROR_ENV` 里点名的那些路（空串 = 一路都不点）。"""
    raw = os.environ.get(REOPEN_ERROR_ENV, "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    return float(raw)


# ---------------------------------------------------------------------------
# 带剧本的假源
# ---------------------------------------------------------------------------


class ScriptedSource:
    """一路按剧本给块的假源（内容带块号、第 N 块后抛错、块数封顶、块节奏可调）。

    满足 `meeting.capture.engine.CaptureSource` 协议的三个方法；`read()` 的三种回法
    （给块 / 给空「这一拍没有新块」/ 抛错「这一路出事」）与
    `tests/test_capture_engine.py` 的 `FakeCaptureSource` 同形 —— 不直接复用它，是因为
    本单元的每块内容必须按块号变化（核对要逐字节可比），那一份假源是「每块同一内容」。
    """

    def __init__(
        self,
        name: str,
        *,
        error_at: int | None = None,
        pace_seconds: float = DEFAULT_PACE_SECONDS,
        chunks: int | None = None,
        deliver_chunks: int | None = None,
        fail_start: bool = False,
    ) -> None:
        self.name = name
        self.error_at = error_at
        self.pace_seconds = pace_seconds
        self.chunks = chunks
        self.deliver_chunks = deliver_chunks
        #: 造出来就开不起来这一档（「重新打开某一路时设备被别的程序占着」用它）
        self.fail_start = bool(fail_start)
        self.fed = 0
        self.reads = 0
        self.started = False
        self.stopped = False
        #: 抛错前交付了多少块（第 N 块**已经写进归档**之后才抛，见模块说明）
        self.delivered_before_error = 0
        self._stop = threading.Event()
        self._latest = np.empty(0, dtype=np.int16)

    # -- CaptureSource 协议 ------------------------------------------------

    def start(self) -> None:
        if self.fail_start:
            raise RuntimeError(
                f"假源 {self.name} 按要求开不起来（设备被别的程序占着这一档）"
            )
        self.started = True

    def read(self, timeout: float) -> bytes | None:
        self.reads += 1
        if self._stop.is_set():
            return None
        if self.error_at is not None and self.fed >= self.error_at:
            raise RuntimeError(
                f"假源 {self.name} 按剧本在第 {self.error_at} 块后抛错（运行期掉一路）"
            )
        exhausted = (
            (self.chunks is not None and self.fed >= self.chunks)
            or (self.deliver_chunks is not None and self.fed >= self.deliver_chunks)
        )
        if exhausted:
            time.sleep(min(timeout, 0.005))
            return b""
        data = chunk_bytes(self.name, self.fed)
        self._latest = np.frombuffer(data, dtype=np.int16)
        self.fed += 1
        if self.pace_seconds > 0:
            time.sleep(self.pace_seconds)
        return data

    def latest_samples(self) -> np.ndarray:
        return self._latest

    def stop(self) -> None:
        self.stopped = True
        self._stop.set()

    # -- 给用例看的读数 ----------------------------------------------------

    @property
    def bytes_fed(self) -> int:
        return self.fed * CHUNK_BYTES


# ---------------------------------------------------------------------------
# 挂进配置键的后端
# ---------------------------------------------------------------------------

#: 配置键 `capture.backend` 的取值（`module:Class` 形状；`FaultyCaptureBackend` 无参可构造，
#: 满足 registry 的无参实例化口径）
BACKEND_TARGET = "tests._capture_faulty:FaultyCaptureBackend"


class FaultyCaptureBackend(CaptureBackend):
    """真采集后端 + 假端点表 + 带剧本的假源。

    生产路径上只换三样：端点表（本模块的两条假端点）、试开（能打开且有波形的结果）、
    造源（`ScriptedSource`：一路可在第 N 块后抛错）。其余 —— `probe()` 的挑选策略、
    `start()` 的抛错口径、归档写线程、电平刻度、收尾 `finalize()` / `transcode()` ——
    全是生产代码。

    `created` 记下造出来的每一路假源（`fed` 是核对里「喂进去的块数」的来源）；
    `endpoints` 记下每一路挑到的假端点（对快照 `device_name` 的交叉核对用）。
    """

    def __init__(self) -> None:
        super().__init__(
            list_inputs=lambda: [FAKE_MIC],
            list_renders=lambda: [FAKE_RENDER],
            test_open_input=lambda endpoint: wave_attempt(),
            test_open_loopback=lambda endpoint: wave_attempt(),
            source_factory=self._create_source,
        )
        self.created: dict[str, ScriptedSource] = {}
        self.endpoints: dict[str, DeviceEndpoint] = {}

    # -- 造源（后端在 `start()` 里逐路调它） ---------------------------------

    def _create_source(self, source: str, endpoint: DeviceEndpoint) -> ScriptedSource:
        error_at = _env_int(ERROR_AT_MIC_ENV if source == "mic" else ERROR_AT_LOOPBACK_ENV)
        deliver = _env_int(DELIVER_MIC_ENV if source == "mic" else DELIVER_LOOPBACK_ENV)
        # 第二次及以后为这一路造源 = 用户在会中重新打开它（第一次是开场那一次）
        reopened = source in self.created
        source_impl = ScriptedSource(
            source,
            error_at=error_at,
            pace_seconds=_env_float(PACE_SECONDS_ENV, DEFAULT_PACE_SECONDS),
            chunks=_env_int(CHUNKS_ENV),
            deliver_chunks=deliver,
            fail_start=reopened and source in _reopen_error_sources(),
        )
        self.created[source] = source_impl
        self.endpoints[source] = endpoint
        return source_impl

    # -- 给用例看的读数 ----------------------------------------------------

    def fed_chunks(self, source: str) -> int:
        """这一路实际喂出去的块数（核对「预期样本数 = 块数 × 每块样本数」的实测侧）。"""
        return self.created[source].fed


__all__ = [
    "BACKEND_TARGET",
    "CHUNKS_ENV",
    "CHUNK_BYTES",
    "CHUNK_FRAMES",
    "DELIVER_LOOPBACK_ENV",
    "DELIVER_MIC_ENV",
    "ERROR_AT_LOOPBACK_ENV",
    "ERROR_AT_MIC_ENV",
    "FAKE_MIC",
    "FAKE_RENDER",
    "FRAME_CHUNKS",
    "PACE_SECONDS_ENV",
    "REOPEN_ERROR_ENV",
    "FaultyCaptureBackend",
    "ScriptedSource",
    "chunk_bytes",
    "chunk_value",
    "expected_archive_bytes",
    "wave_attempt",
]
