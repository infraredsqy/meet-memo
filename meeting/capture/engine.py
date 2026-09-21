"""会话采集引擎 —— 每路一套「读侧 + 写侧 + 0.5 秒电平刻度」（`dca4539b` phase2 §3）。

**这一段的骨架来自 V1 的双录**（`C:/Users/<user>/tools/meeting-notes`）：V1 那套在真机上跑通过
整场整场的会，所以这里不另起一套，只把它按 V2 的契约重排：

  | V1 的东西 | 本模块的去处 |
  |---|---|
  | `MeetingRecorder._mic_callback`（`recorder.py:488-493`）里入队那一句 | 读侧的落点（入队那一句在 `sources.py` 的 mic 源里） |
  | `MeetingRecorder._loopback_pump`（`recorder.py:467-486`）的循环 | `sources.py` 的 loopback 源（专用线程 + `CoInitializeEx`） |
  | `pipeline._audio_loop` 里按 tag 取块再写（`pipeline.py:89-95`） | `archive.PcmArchiveWriter`（取一块、写一块） |

**与 V1 的结构差别只有三条，每条都有契约上的理由**（§3 的取舍，别把它扩大成重构）：

  1. **两路各自一条写线程**（V1 是两路共用一条队列 + 一条写线程）：契约要求「一路出事另一路
     照跑」，分开写更直接；
  2. **单路失败报 `SourceLost`**（V1 里 loopback 死在运行期只有一行日志）：R2 要的是「不能静默
     出事」；一路死掉**不影响另一路**，`start()` 也不因此抛错；
  3. **每路一条电平刻度线程**：每 `frame_interval` 秒**无条件**发一帧 `LevelFrame`
     （一路真的静下来时发的是 `-120 dBFS` 的帧）。V1 没有电平事实，这一块是 V2 契约新增的；
     帧必须按节奏持续发，不能「有声音才发」——服务侧的心跳窗口是
     `max(2.0s, 3 × frame_interval)`，停发会把「静音」误判成「采集停了」。

**假源注入是这一段的验收手段**：`CaptureSession` 的 `sources` 参数收任何满足
`CaptureSource` 协议的对象（`read()` 逐块给字节、`latest_samples()` 给这一路的电平素材），
测试因此走的是**生产代码这条路径**：读线程、写线程、电平刻度、收尾落盘全部是真的，
换掉的只有最外面那一层声卡库。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Protocol, runtime_checkable

import numpy as np

from meeting.backends.base import (
    DEFAULT_FRAME_INTERVAL_SECONDS,
    LevelFrame,
    SourceFacts,
    SourceLost,
)
from meeting.capture.archive import PcmArchiveWriter
from meeting.capture.levels import frame_level
from meeting.session import archive_name

_log = logging.getLogger("meeting.capture.engine")

#: 两路（`mic` = 本地、`loopback` = 远端）；口径与 `meeting.backends.base.SOURCE_IDS` 一致
SOURCE_IDS: tuple[str, ...] = ("mic", "loopback")

#: `start()` 收到的三种模式（未选中的那一路**不读、不发帧**，但归档文件照样建出来）
CAPTURE_MODES: tuple[str, ...] = ("both", "mic", "loopback")

#: 读侧一次空转的等待上限（秒）：源没有新块时读线程按这个节奏醒来，`stop()` 因此能及时收队
READ_POLL_SECONDS = 0.05

#: 收尾 join 的确定上界（秒）——join 有上界，超时留一条 warning，不静默、也不挂住进程
JOIN_TIMEOUT_SECONDS = 5.0


def sources_for_mode(mode: str) -> tuple[str, ...]:
    """本场该采哪几路（`both` / `mic` / `loopback`）。"""
    if mode not in CAPTURE_MODES:
        raise ValueError(f"未知的采集模式：{mode!r}（取值域 {CAPTURE_MODES}）")
    if mode == "both":
        return SOURCE_IDS
    return (mode,)


@runtime_checkable
class CaptureSource(Protocol):
    """一路采集源的读侧（生产实现见 `meeting/capture/sources.py`，测试用假源）。

    - `read(timeout)` —— 取下一块 int16 裸 PCM 字节。**返回 `None` = 这一路已经读不出东西了**
      （流到头的正常收场），引擎据此收队并发一条 `SourceLost`；**抛错**同样收队并如实报原因。
    - `latest_samples()` —— 这一路最近一块样本（电平刻度的素材；还没有块时给空数组）。
    - `stop()` —— 收队（幂等）。读侧与它拉的线程/流都在这里收干净。
    """

    name: str

    def read(self, timeout: float) -> bytes | None: ...

    def latest_samples(self) -> np.ndarray: ...

    def stop(self) -> None: ...


class _SourceWorker:
    """一路的全套件：一条读线程（拿块→喂写侧）、一条电平刻度线程、一条写线程。

    `active=False` 的那一路只有归档文件（0 字节），**不读、不发帧**（§5「未选中的那一路」）。
    """

    def __init__(
        self,
        source: str,
        source_impl: CaptureSource | None,
        writer: PcmArchiveWriter,
        *,
        emit: Callable[[Any], None],
        frame_interval: float,
        active: bool,
    ) -> None:
        self.source = source
        self.source_impl = source_impl
        self.writer = writer
        self.active = active
        self._emit = emit
        self.frame_interval = float(frame_interval)
        #: 这一路的流还活着没有（死掉之后 `SourceFacts` 照发，值为假）
        self.alive = bool(active)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    # -- 起停 -------------------------------------------------------------

    def start(self) -> None:
        if not self.active:
            return
        self._threads = [
            threading.Thread(target=self._read_loop, name=f"capture-read-{self.source}", daemon=True),
            threading.Thread(target=self._tick_loop, name=f"capture-level-{self.source}", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self, *, timeout: float = JOIN_TIMEOUT_SECONDS) -> None:
        """收队：停源、唤醒两条线程、join（**幂等**）。"""
        self._stop.set()
        source = self.source_impl
        if source is not None:
            try:
                source.stop()
            except Exception:  # noqa: BLE001 - 停源失败不掩盖收尾结论
                _log.warning("停 %s 这一路的源失败（如实记账，不静默）", self.source, exc_info=True)
        for thread in self._threads:
            if thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=timeout)
                if thread.is_alive():
                    _log.warning(
                        "%s 这一路的线程 %s 在 %ss 内没有退出（daemon，不阻塞进程退出）",
                        self.source, thread.name, timeout,
                    )
        self._threads = []

    # -- 读侧 -------------------------------------------------------------

    def _read_loop(self) -> None:
        """取一块、写一块（搬自 `pipeline._audio_loop` 的 `pipeline.py:89-95`）。

        一路的源读不出来 / 抛错时：**这一路收队并报一条 `SourceLost`**，另一路的读线程与写线程
        一个都不动（「一路出事另一路照跑」就是这么落地的）。
        """
        source = self.source_impl
        if source is None:  # pragma: no cover - active 为真时源一定在
            return
        while not self._stop.is_set():
            try:
                data = source.read(READ_POLL_SECONDS)
            except Exception as exc:  # noqa: BLE001 - 采集侧的任何读错都如实报成 SourceLost
                self._lose(f"{type(exc).__name__}: {exc}")
                return
            if data is None:
                self._lose("这一路的采集流已经结束")
                return
            if not data:
                continue
            self.writer.feed(data)

    def _lose(self, reason: str) -> None:
        """这一路没了：如实报一条 `SourceLost`（**不判告警、不报静默秒数**，那是服务侧的事）。"""
        if self._stop.is_set():
            return
        self.mark_lost(reason)

    def mark_lost(self, reason: str) -> None:
        """如实报一条 `SourceLost`（读线程读崩时调它，后端「这一路没起来」时也调它）。"""
        self.alive = False
        _log.error("%s 这一路的流断了：%s", self.source, reason)
        self._emit_fact(SourceLost(source=self.source, reason=reason))

    # -- 电平刻度 ---------------------------------------------------------

    def _tick_loop(self) -> None:
        """每 `frame_interval` 秒**无条件**发一帧电平事实（外加一条流存活事实）。

        帧节奏由这里保证：与有没有新块无关，静下来时发的是 `-120 dBFS` 的帧。`ts` 用采集侧
        自己的钟（`time.monotonic()`，与 `alerts.py` 做差的口径一致）。
        """
        while not self._stop.wait(self.frame_interval):
            self._emit_level()

    def _emit_level(self) -> None:
        source = self.source_impl
        samples: np.ndarray
        if source is None:
            samples = np.empty(0, dtype=np.int16)
        else:
            try:
                samples = source.latest_samples()
            except Exception:  # noqa: BLE001 - 取电平素材失败按「这一拍没有素材」处理
                samples = np.empty(0, dtype=np.int16)
        dbfs, peak, bar = frame_level(samples)
        self._emit_fact(
            LevelFrame(
                source=self.source,
                ts=time.monotonic(),
                dbfs=dbfs,
                peak=peak,
                bar=bar,
            )
        )
        self._emit_fact(SourceFacts(source=self.source, stream_alive=self.alive))

    def _emit_fact(self, fact: Any) -> None:
        try:
            self._emit(fact)
        except Exception:  # noqa: BLE001 - 出口抛错不许把采集线程带走
            _log.exception("事实出口抛错（只记一条，采集继续）")


class CaptureSession:
    """一场会话的采集引擎：两路读侧 / 写侧 / 0.5 秒电平刻度，收尾 `finalize()` + `transcode()`。

    生命周期（`meeting/service/runtime.py` 的调用顺序）：

      - 构造：**把两路的归档文件都建出来**（未选中的那一路是 0 字节，`session.json` 的
        `sources[*].archive` 因此条条落到实处），并起两路写线程；此刻还没有读侧；
      - `start()`：按 `mode` 起该采的几路读侧与电平刻度；
      - `finalize()`：停流 + join + 关文件 + `fsync`（收尾第 1 步）；
      - `transcode()`：把两路 PCM 转成 MP3（收尾第 2 步）；转不动**原样上抛**，PCM 留着；
      - `stop()`：幂等收场（进程关停时调；已经收过尾就什么都不做）。
    """

    def __init__(
        self,
        *,
        mode: str,
        session_dir: str,
        sources: dict[str, CaptureSource],
        emit: Callable[[Any], None],
        frame_interval: float = DEFAULT_FRAME_INTERVAL_SECONDS,
        requested: tuple[str, ...] | None = None,
    ) -> None:
        self.mode = mode
        self.session_dir = str(session_dir)
        self.frame_interval = float(frame_interval)
        #: 本场该采的路（`mode` 决定；传 `requested` 是为了让「源没起来的那一路」也如实记账）
        self.requested = tuple(sources_for_mode(mode) if requested is None else requested)
        self._emit = emit
        self._sources = dict(sources)
        self._lock = threading.Lock()
        self._started = False
        self._finalized = False
        self._stopped = False
        self.writers: dict[str, PcmArchiveWriter] = {}
        self.workers: dict[str, _SourceWorker] = {}
        for source in SOURCE_IDS:
            writer = PcmArchiveWriter(
                _archive_path(self.session_dir, source), label=source
            )
            writer.open()
            self.writers[source] = writer
            self.workers[source] = _SourceWorker(
                source,
                self._sources.get(source),
                writer,
                emit=emit,
                frame_interval=self.frame_interval,
                active=source in self.requested and source in self._sources,
            )

    # -- 起 ---------------------------------------------------------------

    def begin_source(self, source: str, source_impl: CaptureSource) -> None:
        """把某一路的采集源接进来（`start()` 之前由后端调；接进来的这一路随即成为要采的路）。

        后端是**先挑设备、再取源、再开会话**的分步形状：挑不出来 / 开不起来的那一路也要如实
        记账（`mark_lost()`），所以这里允许逐个把源接上。
        """
        self._sources[source] = source_impl
        worker = self.workers[source]
        worker.source_impl = source_impl
        worker.active = source in self.requested

    def mark_lost(self, source: str, reason: str) -> None:
        """如实报一条 `SourceLost`（这一路本场没起来；另一路不受影响）。"""
        self.workers[source].mark_lost(reason)

    def start(self) -> None:
        """起该采的那几路。**抛错之前自己拉起来的东西自己收干净**（runtime 那条 abort 路不调 `stop()`）。"""
        with self._lock:
            if self._started:
                return
            self._started = True
        try:
            for worker in self.workers.values():
                worker.start()
        except Exception:
            self.stop()
            raise

    # -- 收尾 -------------------------------------------------------------

    def finalize(self) -> None:
        """停流 + join + 关文件 + `fsync`（**幂等**）。"""
        with self._lock:
            if self._finalized:
                return
            self._finalized = True
        for worker in self.workers.values():
            worker.stop()
        for writer in self.writers.values():
            writer.close()
        _log.info(
            "收尾落盘完成：%s（两路归档 %s）",
            self.session_dir,
            ", ".join(
                f"{source}={self.writers[source].bytes_written} 字节" for source in SOURCE_IDS
            ),
        )

    def transcode(self) -> None:
        """收尾第 2 步：两路 PCM → MP3。**不做决定论的口径**，转不动就抛（该步记 `failed`）。

        未选中的那一路是 0 字节，`convert_to_mp3()` 按空文件处理、**不产出 MP3**（照 V1）。
        """
        # 函数内 import：转码这一层是「可以整块换掉」的（测试拿它验失败那一档）
        from meeting.capture.archive import convert_to_mp3, mp3_path_for

        produced: list[str] = []
        for source in SOURCE_IDS:
            pcm_path = _archive_path(self.session_dir, source)
            if convert_to_mp3(pcm_path, mp3_path_for(pcm_path)):
                produced.append(mp3_path_for(pcm_path).name)
        _log.info("收尾转码完成：%s（产出 %s）", self.session_dir, produced or "无")

    def stop(self) -> None:
        """幂等收场：没落过盘就先 `finalize()`，再什么都不做（进程关停时调它）。"""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        self.finalize()

    # -- 读数（测试与 /debug 用） -----------------------------------------

    def bytes_written(self, source: str) -> int:
        """这一路真正写下去的字节数（对账口径：32000 字节/秒 × 秒数）。"""
        return self.writers[source].bytes_written


def _archive_path(session_dir: str, source: str) -> str:
    """场次目录里的归档路径（文件名由 `meeting/session.py` 定死）。"""
    return os.path.join(session_dir, archive_name(source))


__all__ = [
    "CAPTURE_MODES",
    "JOIN_TIMEOUT_SECONDS",
    "READ_POLL_SECONDS",
    "SOURCE_IDS",
    "CaptureSession",
    "CaptureSource",
    "sources_for_mode",
]
