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

from meeting.backends.base import (
    DEFAULT_FRAME_INTERVAL_SECONDS,
    FrameGap,
    LevelFrame,
    SourceFacts,
    SourceLost,
)
from meeting.capture.archive import PcmArchiveWriter, SAMPLE_RATE, SAMPLE_WIDTH
from meeting.capture.gaps import FrameGapDetector
from meeting.capture.levels import frame_level
from meeting.session import archive_name, next_segment_number, segment_archive_paths
from meeting.state import now_iso

_log = logging.getLogger("meeting.capture.engine")

#: 两路（`mic` = 本地、`loopback` = 远端）；口径与 `meeting.backends.base.SOURCE_IDS` 一致
SOURCE_IDS: tuple[str, ...] = ("mic", "loopback")

#: `start()` 收到的三种模式（未选中的那一路**不读、不发帧**，但按整段时间写静音）
CAPTURE_MODES: tuple[str, ...] = ("both", "mic", "loopback")

#: 读侧一次空转的等待上限（秒）：源没有新块时读线程按这个节奏醒来，`stop()` 因此能及时收队
READ_POLL_SECONDS = 0.05

#: 收尾 join 的确定上界（秒）——join 有上界，超时留一条 warning，不静默、也不挂住进程
JOIN_TIMEOUT_SECONDS = 5.0

#: 一路音频每秒的字节数（16k × int16 = 32000；归档对账与时长换算的同一把尺）
BYTES_PER_SECOND = SAMPLE_RATE * SAMPLE_WIDTH

#: 写静音的节拍（秒）：暂停、用户关一路、没开的那一路都按这个节奏往归档里写数字零，
#: 两路的时间轴因此始终等长（`docs/design-intent.md` 第 8、9 条）
SILENCE_CHUNK_SECONDS = 0.1

#: 写静音的一块多少字节（0.1 秒 = 3200 字节；与真采集一个块的量级对齐）
SILENCE_CHUNK_BYTES = int(SAMPLE_RATE * SILENCE_CHUNK_SECONDS) * SAMPLE_WIDTH

#: 数字零的一块（写线程落盘的就是它；真音频块在写侧被等长替换，内容到不了盘上）
_SILENCE_CHUNK = bytes(SILENCE_CHUNK_BYTES)


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

    `active=False` 的那一路**不读真源、不发帧**，但读线程照样起——跑的是**写静音的节拍器**
    （`docs/design-intent.md` 第 9 条：某一路没开也是「那一路写静音」，两路时间轴等长）。
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
        pause_event: threading.Event | None = None,
    ) -> None:
        self.source = source
        self.source_impl = source_impl
        self.writer = writer
        self.active = active
        self._emit = emit
        self.frame_interval = float(frame_interval)
        #: 本场的暂停信号（`CaptureSession.pause()` / `resume()` 翻动；置位时读线程
        #: 把拿到的真音频**等长替换成数字零**再交给写侧——暂停 = 写静音，不录内容）
        self._pause_event = pause_event if pause_event is not None else threading.Event()
        #: 这一路的流还活着没有（死掉之后 `SourceFacts` 照发，值为假）
        self.alive = bool(active)
        self._stop = threading.Event()
        #: 用户主动关掉这一路（与「这一路真死了」是两回事：不报 `SourceLost`、不进告警）
        self._user_closed = threading.Event()
        #: 写静音的下一拍（绝对时刻，防漂移）
        self._silence_deadline: float | None = None
        #: 帧空洞自查账（gil-stall-hole）：发帧线程每次产帧先与上一拍做差，超窗报一次
        #: `FrameGap` 再发帧 —— 整进程冻结解冻后的第一拍就能算出真实空洞时长，留痕
        #: 不依赖判停那一侧（它与采帧一起冻）。窗口与 `recording_alive` 同一把尺。
        self._gap_detector = FrameGapDetector(
            frame_interval=frame_interval, on_gap=self._report_gap
        )
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    # -- 起停 -------------------------------------------------------------

    def start(self) -> None:
        if self.active:
            self._threads = [
                threading.Thread(target=self._read_loop, name=f"capture-read-{self.source}", daemon=True),
                threading.Thread(target=self._tick_loop, name=f"capture-level-{self.source}", daemon=True),
            ]
        else:
            # 未选中的那一路只有静音节拍器：没有读侧、没有电平刻度
            self._threads = [
                threading.Thread(
                    target=self._silence_loop, name=f"capture-silence-{self.source}", daemon=True
                )
            ]
        for thread in self._threads:
            thread.start()

    def stop(self, *, timeout: float = JOIN_TIMEOUT_SECONDS) -> None:
        """收队：停源、唤醒线程、join（**幂等**）。"""
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

    # -- 关一路（用户的主动选择，不是故障） ---------------------------------

    def deactivate(self) -> None:
        """用户主动关掉这一路：停读真源、改写静音；**不报 `SourceLost`、不进告警**。

        与「这一路真死了」的区分落在这里（`docs/design-intent.md` 第 9 条：那一路写静音，
        两路时间对齐）：真死维持读线程里的 `SourceLost` 语义不动；主动关只翻本地的
        `_user_closed` 信号——读线程随后不再碰源（设备被释放），转由静音节拍器接管。
        """
        if self._user_closed.is_set():
            return
        self._user_closed.set()
        source = self.source_impl
        if source is not None:
            try:
                source.stop()
            except Exception:  # noqa: BLE001 - 关掉时停源失败不改变「用户已关」的事实
                _log.warning("关 %s 这一路时停源失败（写静音照常）", self.source, exc_info=True)
        _log.info("用户关掉 %s 这一路：改写静音（不报故障）", self.source)

    @property
    def user_closed(self) -> bool:
        """这一路是否被用户主动关掉（与「真死」区分开的读数）。"""
        return self._user_closed.is_set()

    def reactivate(self, source_impl: CaptureSource) -> None:
        """用户重新打开这一路：清掉「用户已关」的信号，把**新开的**源交给读线程。

        与 `deactivate()` 严格对称，且**只在源真的开起来之后才调**（调用方先造源、
        先 `start()`，失败就原样上抛）：读线程那一侧不能出现「标志清了、源却是死的」
        这一刻 —— 那会立刻报成 `SourceLost`（用户主动关掉的那一路明令不报故障）。

        读线程此刻正跑在静音节拍器上（用户关掉之后它不碰源），这里只换源 + 清标志，
        它下一拍就自己回到读源那条路（见 `_read_loop()` 每轮现读 `self.source_impl`）。
        """
        with self._lock:
            self.source_impl = source_impl
            self._user_closed.clear()
            # 静音节拍器的绝对时刻从这里重新起算（关掉期间它一直在推进）
            self._silence_deadline = None
            self.alive = True
        _log.info("用户重新打开 %s 这一路：读线程回到读源", self.source)

    # -- 读侧 -------------------------------------------------------------

    def _feed(self, data: bytes) -> None:
        """把拿到的块交给写侧；本场暂停中就**等长替换成静音**（内容到不了写侧队列）。"""
        if self._pause_event.is_set():
            self.writer.feed(data, silence=True)
        else:
            self.writer.feed(data)

    def _read_loop(self) -> None:
        """取一块、写一块（搬自 `pipeline._audio_loop` 的 `pipeline.py:89-95`）。

        一路的源读不出来 / 抛错时：**这一路收队并报一条 `SourceLost`**，另一路的读线程与写线程
        一个都不动（「一路出事另一路照跑」就是这么落地的）。用户主动关掉的那一路**不走**
        这条报障路：读线程转去跑静音节拍器。

        **源每一轮现取**（`self.source_impl`，不在循环外缓存）：用户关掉再重新打开
        （`deactivate()` / `reactivate()`）时读线程一直在原地转，缓存下来的旧源会让它
        一直读那条已经关掉的流 —— 于是「重新打开」这条路上什么都不恢复。
        """
        while not self._stop.is_set():
            source = self.source_impl
            if not self.active or self._user_closed.is_set() or source is None:
                if self._pace_silence():
                    return
                continue
            try:
                data = source.read(READ_POLL_SECONDS)
            except Exception as exc:  # noqa: BLE001 - 采集侧的任何读错都如实报成 SourceLost
                if not self._user_closed.is_set():
                    self._lose(f"{type(exc).__name__}: {exc}")
                return
            if data is None:
                if not self._user_closed.is_set():
                    self._lose("这一路的采集流已经结束")
                return
            if not data:
                continue
            self._feed(data)

    def _silence_loop(self) -> None:
        """未选中那一路的整段时间静音节拍器（没有读侧，纯按真实时长写零）。"""
        while not self._stop.is_set():
            if self._pace_silence():
                return

    def _pace_silence(self) -> bool:
        """往归档写一块数字静音，然后歇到下一拍（绝对时刻，防漂移）。

        交回 `True` 表示收到停机信号，调用方照办退出。
        """
        self.writer.feed(_SILENCE_CHUNK, silence=True)
        now = time.monotonic()
        if self._silence_deadline is None:
            self._silence_deadline = now + SILENCE_CHUNK_SECONDS
        else:
            self._silence_deadline += SILENCE_CHUNK_SECONDS
        delay = self._silence_deadline - time.monotonic()
        return bool(delay > 0 and self._stop.wait(delay))

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
        自己的钟（`time.monotonic()`，与 `alerts.py` 做差的口径一致）。发帧前先过一遍
        帧空洞自查账（`_emit_level()` 的第一步）：与上一拍的间隔超窗就报一次 `FrameGap`
        —— 暂停期间电平帧照发（既有依赖），所以暂停不构成空洞。
        """
        while not self._stop.wait(self.frame_interval):
            self._emit_level()

    def _emit_level(self) -> None:
        """每 `frame_interval` 秒一帧：先记空洞账（超窗报一次 `FrameGap`），再发电平帧。"""
        import numpy as np

        self._gap_detector.observe()
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

    def _report_gap(self, seconds: float, detected: float) -> None:
        """空洞自查账的出口：发一条 `FrameGap` 事实（账的合并归服务侧）。"""
        self._emit_fact(FrameGap(source=self.source, seconds=seconds, detected=detected))

    def _emit_fact(self, fact: Any) -> None:
        try:
            self._emit(fact)
        except Exception:  # noqa: BLE001 - 出口抛错不许把采集线程带走
            _log.exception("事实出口抛错（只记一条，采集继续）")


class CaptureSession:
    """一场会话的采集引擎：两路读侧 / 写侧 / 0.5 秒电平刻度，收尾 `finalize()` + `transcode()`。

    生命周期（`meeting/service/runtime.py` 的调用顺序）：

      - 构造：**先定本段段号、再把两路的段归档文件都建出来**并起两路写线程；此刻
        还没有读侧。段号 = `meeting/session.py` 的 `next_segment_number()`（盘上实有
        段号最大值 + 1，空目录从 1 起）——**次序不能反**：`open_pcm()` 是截断写，
        段号解析晚于打开就会把上一段的 PCM 清成零字节；开场（空目录）建段 1，
        续接（同一工作区再录）建下一个序号的新段文件，旧段一个字节不碰；
      - `start()`：按 `mode` 起该采的几路读侧与电平刻度（未选中的那一路起静音节拍器），
        同时落下**本段**的起点戳——一段 = 一次录制（start→finalize，`docs/design-intent.md`
        第 9 条）；
      - `pause()` / `resume()`：暂停与继续。暂停期间所有路写数字静音、不录内容
        （第 8 条），段**不**切段，暂停区间进段账；
      - `deactivate_source()`：用户主动关掉某一路——那一路改写静音，不报 `SourceLost`；
      - `activate_source()`：用户重新打开某一路——把新开起来的源接回读线程；
      - `finalize()`：停流 + join + 关文件 + `fsync`（收尾第 1 步），落下本段终点戳
        并结出**在录时长**（实际录到的音频时长，不含暂停；权威数只从写侧的字节账来）；
      - `transcode()`：把各段 PCM 转成 MP3（收尾第 2 步）；转不动**原样上抛**，PCM 留着；
      - `stop()`：幂等收场（进程关停时调；已经收过尾就什么都不做）。

    段账（`segment_records()` / `audio_duration_seconds()`）由服务侧落进 `session.json`；
    本类只管把它记对，不做任何墙钟对账（第 6 条：元信息除权威时长外一律不当判据）。
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
        #: 本场的暂停信号（读线程看它决定写内容还是写静音）
        self._pause_event = threading.Event()
        #: 段内暂停区间（墙钟 ISO；`{"start": …, "end": …}`，end 为 None = 还在暂停中）
        self._pause_intervals: list[dict[str, str | None]] = []
        #: **本段段号**（一段 = 一次录制；开场 1，续接取盘上已有段号 + 1）。
        #: 必须在开写侧**之前**定死——`open_pcm()` 是截断写，段号解析晚于打开会把
        #: 上一段的 PCM 清成零字节（本节点的最大坑，见 `next_segment_number()`）。
        self.segment_number = next_segment_number(self.session_dir, SOURCE_IDS)
        #: 本段的起止戳与在录时长（一段 = 一次录制）
        self._segment_started_at: str | None = None
        self._segment_ended_at: str | None = None
        self._segment_duration_seconds: float | None = None
        self.writers: dict[str, PcmArchiveWriter] = {}
        self.workers: dict[str, _SourceWorker] = {}
        # **抛错之前自己拉起来的东西自己收干净**（与 `start()` 同一条纪律，见 `start()`）：
        # 两路逐个 `open()`（建段文件 + 起写线程），第二路抛错时构造原样上抛，已 `open()`
        # 的第一路写线程与句柄不能没人收 —— 上抛之前先把已记账的 writer 逐个 `close()`
        # （`close()` 幂等、容忍半开状态：句柄开了线程没起的那一档也收得干净）。
        # 收尾自己再抛只记 warning：上抛的主因仍是构造失败的那个原异常。
        try:
            for source in SOURCE_IDS:
                writer = PcmArchiveWriter(
                    _archive_path(self.session_dir, source, self.segment_number), label=source
                )
                # 先记账再 `open()`：`open()` 半路抛错时它自己也在收尾名单里
                self.writers[source] = writer
                writer.open()
                self.workers[source] = _SourceWorker(
                    source,
                    self._sources.get(source),
                    writer,
                    emit=emit,
                    frame_interval=self.frame_interval,
                    active=source in self.requested and source in self._sources,
                    pause_event=self._pause_event,
                )
        except Exception:
            for writer in self.writers.values():
                try:
                    writer.close()
                except Exception:  # noqa: BLE001 - 收尾失败不掩盖构造失败的主因
                    _log.warning(
                        "构造失败收尾：关 %s 的归档写侧失败（原异常照上抛）",
                        writer.label,
                        exc_info=True,
                    )
            raise

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
            # 本段从这里起算（一段 = 一次录制；只是元信息戳，不当判据）
            self._segment_started_at = now_iso()
        try:
            for worker in self.workers.values():
                worker.start()
        except Exception:
            self.stop()
            raise

    # -- 暂停 / 关一路（引擎的服务层出口；动作面入口在 `services/routes.py`） ----

    def pause(self) -> None:
        """暂停本场采集：所有路从这一刻起写数字静音，**不录内容**（第 8 条的隐私线）。

        幂等；没在录（没 `start()` 或已 `finalize()`）时什么都不做。段**不**切段：
        暂停只在本段账上记一条区间。
        """
        with self._lock:
            if self._pause_event.is_set() or not self._started or self._finalized:
                return
            self._pause_event.set()
            self._pause_intervals.append({"start": now_iso(), "end": None})
        _log.info("本场已暂停：%s 往归档写数字静音，不录内容", self.session_dir)

    def resume(self) -> None:
        """继续本场采集：结束当前这条暂停区间（**幂等**；没在暂停中时什么都不做）。"""
        with self._lock:
            if not self._pause_event.is_set():
                return
            self._pause_event.clear()
            for interval in reversed(self._pause_intervals):
                if interval.get("end") is None:
                    interval["end"] = now_iso()
                    break
        _log.info("本场已继续：暂停区间结束，归档回到录内容")

    @property
    def paused(self) -> bool:
        """本场是否正处在暂停中（写静音、不录内容）。"""
        return self._pause_event.is_set()

    def deactivate_source(self, source: str) -> None:
        """用户主动关掉某一路：那一路改写静音，**不报 `SourceLost`、不进告警**（幂等）。"""
        worker = self.workers.get(source)
        if worker is None:  # pragma: no cover - 两路的 worker 构造即建
            return
        worker.deactivate()

    def activate_source(self, source: str, source_impl: CaptureSource) -> None:
        """用户重新打开某一路：把**新开起来的源**接回那一台的读线程（幂等）。

        与 `deactivate_source()` 对称。`source_impl` 必须已经 `start()` 过 ——
        造源 / 开流那一步归后端（`CaptureBackend.activate_source()`），引擎这一层只管
        接线；开不起来的那一档由后端原样上抛，快照里那一路照旧如实停在「已关掉」。
        """
        worker = self.workers.get(source)
        if worker is None:  # pragma: no cover - 两路的 worker 构造即建
            return
        worker.reactivate(source_impl)

    # -- 段账 ---------------------------------------------------------------

    def segment_records(self) -> list[dict[str, Any]]:
        """本段那一条段账（`session.json` 的 `segments` 那一格的**新增项**）。

        一场会可以有多个段（续接 = 同一工作区再录一段）：本类只交回**自己这一段**，
        与旧段的合并归 runtime（`_session_record()`）。还在录时 `ended_at` 与
        `duration_seconds` 是 `None`，收尾那一刻由 `finalize()` 结账。
        """
        with self._lock:
            pauses = [dict(interval) for interval in self._pause_intervals]
            started = self._segment_started_at
            ended = self._segment_ended_at
            duration = self._segment_duration_seconds
        return [
            {
                "index": self.segment_number,
                "started_at": started,
                "ended_at": ended,
                "sources": list(self.requested),
                "pauses": pauses,
                "duration_seconds": None if duration is None else round(duration, 3),
            }
        ]

    def audio_duration_seconds(self) -> float:
        """**本段**的在录时长（实际录到的音频，不含暂停）。

        只数**写完的段**（还在录的那段没有账可结）；每个数的来历是写侧的**内容字节**
        （`÷ 32000`），与墙钟无关。整场（各段之和）的合计归 runtime 合并段账后另算。
        """
        with self._lock:
            duration = self._segment_duration_seconds
        return 0.0 if duration is None else round(duration, 3)

    def archives_for(self, source: str) -> list[str]:
        """这一路**本段**写下的段文件名（`session.json` 的 `sources[*].archives` 的新增项）。"""
        return [archive_name(source, self.segment_number)]

    # -- 收尾 -------------------------------------------------------------

    def finalize(self) -> None:
        """停流 + join + 关文件 + `fsync`（**幂等**），并结出本段的在录时长。"""
        with self._lock:
            if self._finalized:
                return
            self._finalized = True
            # 暂停中收尾：把最后一条暂停区间关上（后端 stop 会停源，静音写不到下一拍）
            if self._pause_event.is_set():
                self._pause_event.clear()
                for interval in reversed(self._pause_intervals):
                    if interval.get("end") is None:
                        interval["end"] = now_iso()
                        break
        for worker in self.workers.values():
            worker.stop()
        for writer in self.writers.values():
            writer.close()
        with self._lock:
            # 在录时长 = 该段各路「内容字节」最大的那一条 ÷ 每秒字节数：内容字节只由
            # 真正录到的块累加（暂停静音、没开一路的填充都记在 silence 那一格），
            # 权威数因此从盘上的字节账来，与墙钟无关。
            self._segment_ended_at = now_iso()
            self._segment_duration_seconds = max(
                (
                    self.writers[source].content_bytes / BYTES_PER_SECOND
                    for source in self.requested
                ),
                default=0.0,
            )
        _log.info(
            "收尾落盘完成：%s（两路归档 %s）",
            self.session_dir,
            ", ".join(
                f"{source}={self.writers[source].bytes_written} 字节"
                f"（内容 {self.writers[source].content_bytes}、静音 {self.writers[source].silence_bytes}）"
                for source in SOURCE_IDS
            ),
        )

    def transcode(self) -> None:
        """收尾第 2 步：各段 PCM → MP3。**不做决定论的口径**，转不动就抛（该步记 `failed`）。

        逐段转（`meeting/session.py` 枚举出的**整场**段文件——续接过的会每路多段，
        旧段与新段一并转；旧段 MP3 已在的也照转一遍，产物内容不变）。判据照旧是
        `convert_to_mp3()` 的空文件口径：0 字节的段不产出 MP3；写满静音的段字节非空，
        照转（盘上事实如此，产物里就是一段静音）。
        """
        # 函数内 import：转码这一层是「可以整块换掉」的（测试拿它验失败那一档）
        from meeting.capture.archive import convert_to_mp3, mp3_path_for

        produced: list[str] = []
        for source in SOURCE_IDS:
            for pcm_path in segment_archive_paths(self.session_dir, source):
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
        """这一路真正写下去的字节数（时间轴口径：32000 字节/秒 × 秒数，含静音）。"""
        return self.writers[source].bytes_written

    def content_bytes(self, source: str) -> int:
        """这一路**录到内容**的字节数（权威时长的账本：不含暂停静音与未选一路的填充）。"""
        return self.writers[source].content_bytes

    def silence_bytes(self, source: str) -> int:
        """这一路写下去的静音字节数（暂停 / 用户关一路 / 没开一路的填充）。"""
        return self.writers[source].silence_bytes


def _archive_path(session_dir: str, source: str, segment: int) -> str:
    """场次目录里某路某段的归档路径（文件名由 `meeting/session.py` 定死）。"""
    return os.path.join(session_dir, archive_name(source, segment))


__all__ = [
    "BYTES_PER_SECOND",
    "CAPTURE_MODES",
    "JOIN_TIMEOUT_SECONDS",
    "READ_POLL_SECONDS",
    "SILENCE_CHUNK_BYTES",
    "SILENCE_CHUNK_SECONDS",
    "SOURCE_IDS",
    "CaptureSession",
    "CaptureSource",
    "sources_for_mode",
]
