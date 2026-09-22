"""**dev 采集替身 —— 只给开发与测试用（非产品路径，默认关闭）**（phase2 §3.4 / 方案 §7.4）。

本模块**不是产品代码路径**：默认不启用，只有把 `capture.backend` 显式配成 `dev`
（或 `MEETING_CAPTURE=dev`）才会被解析到。它提供两样东西：

  1. **确定性合成电平**：种子唯一的来源 = 模块级常量 `DEV_LEVEL_SEED`（随源码一起定版）。
     后端构造函数保持无参（phase1 的注入规则），`dev_inject` 也不引入配置键，
     所以种子不来自调用方、不来自配置文件、不来自环境变量。
     **同种子同电平序列**：任意两次 `DevCaptureBackend()` 实例，在相同 `DEV_LEVEL_SEED`
     与相同帧序下产出的电平序列逐帧相同。
  2. **故障注入**（`inject_fault()`，由 `POST /api/actions {"action": "dev_inject"}` 驱动，
     非 dev 后端这个动作回 `action_unavailable`(404)）：三种故障 `silence` / `stall` / `lost`，
     **到点生效、到点自动恢复**；`after_seconds` 到点故障开始、`duration_seconds` 到点故障结束，
     `duration_seconds` 缺省取模块级常量 `DEV_FAULT_DEFAULT_SECONDS`。
     - `silence` = 这一路电平落到 **−80 dBFS 以下并保持**（走 `alerts.py` 的静默累计）；
     - `stall` = 两路的事实帧**整体停发**（HTTP 照常应答，于是 `recording_alive = false`）；
     - `lost` = 直接来一条 `SourceLost`，并且**窗口内这一路整段断流**（一拍电平帧都不发）；
       窗口到点后重新发帧，此后走既有的恢复路径（连着有信号达 `alert.recover_seconds` 回
       `recording`）。这一档与 `stall` 的区别：`stall` 两路都停、一条 `SourceLost` 都不发。
     对同一路再次注入（无论故障是否已恢复）**替换前一次注入并按新的 `after_seconds` /
     `duration_seconds` 重新计时**。三档故障都**不动 `top_state` 的迁移路径**——能不能回
     `recording` 由 `alerts.py` 的判定说了算。

它**不写真实音频**，只在 `finalize()` / `transcode()` 里**记账**（音频字节是 `dca4539b` 的活），
因此收尾第 1、2 步在它身上记 `skipped`（判据只看 `registry` 的解析结论与实现能力）。

## 本模块里的第二件替身：`DevTranscriberBackend`（`9e5db89c` phase2 §3.8）

`transcriber.backend = dev` 按 **key 分流**解析到它（采集那一档照旧是 `DevCaptureBackend`，
两档互不影响 —— 见 `meeting/backends/registry.py` 的 `_target_for()`）。它是一条
**会中能出合成行**的开发替身：起一条线程，每隔 `DEV_TRANSCRIPT_INTERVAL_SECONDS` 秒经
`line_sink` 产一行两路轮换的合成转写，不读盘、不加载模型。

它同时是测试用的稳定替身：节奏由 `interval_seconds` / `initial_delay_seconds` 两个构造参数
给定，`stop()` 之后线程收队、**一行都不再产**；发出去的每一行都是
`meeting.backends.base.TranscriptLine`（`kind="line"`），形状与真后端逐字段同形。

**首发延迟为什么不是 0**（`DEV_TRANSCRIPT_INITIAL_DELAY_SECONDS = 2.0`）：采集侧开场之后
服务要先把两路电平喂满一屏（`ENVELOPE_BAR_COUNT` = 15 根 × dev 的 100ms 帧间隔 ≈ 1.5 秒），
`start` 之后紧接着抓的那一版快照里 `transcript.last_line_at` 因此仍是 `null`；若首行在
开场几十毫秒内就落地，`contract/fixtures/snapshot/recording.json` 那一格会随抓取时机抖动。
延迟取 2.0 秒，这一格就是确定的。
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from meeting.backends.base import (
    SOURCE_IDS,
    LevelFrame,
    SourceFacts,
    SourceLost,
    SourceProbe,
    TranscriptLine,
)

_log = logging.getLogger("meeting.backends.dev")

#: 合成电平的确定性种子（**唯一来源**；随源码一起定版）
DEV_LEVEL_SEED = 20250906

#: dev 替身的帧间隔（秒）：每 100ms 一帧事实
DEV_FRAME_INTERVAL_SECONDS = 0.1

#: 故障注入的默认存活时长（秒）——`duration_seconds` 可省，缺省取它
DEV_FAULT_DEFAULT_SECONDS = 30.0

#: 三种故障（取值域；非其中之一一律 `invalid_setting`(400)）
DEV_FAULT_KINDS: tuple[str, ...] = ("silence", "stall", "lost")

#: `silence` 故障下这一路的电平（−80 dBFS 以下并保持）
DEV_SILENCE_DBFS = -90.0

#: 合成电平的可闻区间（dBFS）
DEV_LEVEL_MIN_DBFS = -60.0
DEV_LEVEL_MAX_DBFS = -6.0

#: dev 转写替身两行之间的间隔（秒）与首发延迟（秒）
#:
#: 首发延迟取 2.0 秒的理由写在模块 docstring 的最后一节（`contract/fixtures/snapshot/
#: recording.json` 那一格的确定性：那一版快照在开场后约 1.5 秒抓取）。
DEV_TRANSCRIPT_INTERVAL_SECONDS = 0.5
DEV_TRANSCRIPT_INITIAL_DELAY_SECONDS = 2.0

#: dev 转写替身的合成台词（两路轮换时带上前缀，一眼看出是哪一路）
DEV_TRANSCRIPT_TEXT = "dev 替身合成的一行转写"


@dataclass
class _FaultWindow:
    """一次注入的时间窗（**有期状态、到点自动恢复**）。"""

    fault: str
    starts_at: float
    ends_at: float
    fired: bool = False

    def active(self, now: float) -> bool:
        return self.starts_at <= now < self.ends_at


class DevCaptureBackend:
    """dev 采集替身：两路合成电平、100ms 一帧、三种可注入故障。"""

    #: 帧间隔的声明位（**类属性形态**，runtime 在 `bind()` 之后读它）
    frame_interval = DEV_FRAME_INTERVAL_SECONDS

    def __init__(self) -> None:
        self.seed = DEV_LEVEL_SEED
        self._emit: Callable[[Any], None] | None = None
        self._config: Any = None
        self._lock = threading.RLock()
        self._faults: dict[str, _FaultWindow] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._frame_index = 0
        self._sources: tuple[str, ...] = ("mic", "loopback")
        self._mode = "both"
        self._session_dir: str | None = None
        #: 记账（不写真实音频）：两次收尾调用各记一笔
        self.finalize_calls = 0
        self.transcode_calls = 0
        self.emitted_frames = 0

    # -- 协议 -------------------------------------------------------------

    def bind(self, emit: Callable[[Any], None], *, config: Any = None) -> None:
        """记下事实出口与只读配置视图（runtime 在 `probe()` 之前调一次）。"""
        self._emit = emit
        self._config = config

    def probe(self) -> list[SourceProbe]:
        """两路都报 `ready`（合成电平随时可用），`device_name` 标明这是替身。"""
        return [
            SourceProbe(
                source=source,
                status="ready",
                device_name=f"dev-{source}",
                detail="dev 采集替身（非产品路径）：合成电平 + 可注入故障",
            )
            for source in self._sources
        ]

    def start(self, mode: str, session_dir: str) -> None:
        """起合成电平：每 `frame_interval` 秒给两路各发一帧事实。"""
        self._mode = mode
        self._session_dir = session_dir
        self._stop.clear()
        self._frame_index = 0
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="dev-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停合成电平（幂等）。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def finalize(self) -> None:
        """**只记账**（不写真实音频）：音频字节的写入归 `dca4539b`。"""
        self.finalize_calls += 1
        _log.info("dev 替身 finalize()：只记账，不写真实音频（session=%s）", self._session_dir)

    def transcode(self) -> None:
        """**只记账**（没有真实音频可转码）。"""
        self.transcode_calls += 1
        _log.info("dev 替身 transcode()：只记账，不写真实音频")

    # -- 故障注入（dev-only 动作的唯一入口） ------------------------------

    def inject_fault(
        self,
        *,
        source: str,
        fault: str,
        after_seconds: float = 0.0,
        duration_seconds: float | None = None,
        now: float | None = None,
    ) -> _FaultWindow:
        """注入一档故障：`after_seconds` 到点生效、`duration_seconds` 到点自动恢复。

        对同一路再次注入**替换前一次注入**并按新的 `after_seconds` / `duration_seconds`
        重新计时（无论故障是否已恢复）。取值域之外的 `source` / `fault` 抛 `ValueError`
        （由动作端点转成 `invalid_setting`(400)）。
        """
        if source not in self._sources:
            raise ValueError(f"未知的采集路：{source!r}（取值域 {self._sources}）")
        if fault not in DEV_FAULT_KINDS:
            raise ValueError(f"未知的故障：{fault!r}（取值域 {DEV_FAULT_KINDS}）")
        duration = DEV_FAULT_DEFAULT_SECONDS if duration_seconds is None else float(duration_seconds)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError(f"duration_seconds 必须是正数，收到 {duration_seconds!r}")
        start_offset = float(after_seconds or 0.0)
        if not math.isfinite(start_offset) or start_offset < 0:
            raise ValueError(f"after_seconds 必须是非负数，收到 {after_seconds!r}")

        base = time.monotonic() if now is None else float(now)
        window = _FaultWindow(
            fault=fault,
            starts_at=base + start_offset,
            ends_at=base + start_offset + duration,
        )
        with self._lock:
            if fault == "stall":
                # `stall` 是「两路的事实帧整体停发」：两路都记上这档故障
                for item in self._sources:
                    self._faults[item] = window
            else:
                self._faults[source] = window
        _log.warning(
            "dev 替身故障注入：source=%s fault=%s after=%.2fs duration=%.2fs"
            "（非产品路径，只给开发与测试用）",
            source, fault, start_offset, duration,
        )
        return window

    def fault_state(self, source: str, now: float | None = None) -> str | None:
        """这一路当前生效的故障（没有就是 `None`）；故障窗口到点自动清掉。"""
        moment = time.monotonic() if now is None else float(now)
        with self._lock:
            window = self._faults.get(source)
            if window is None:
                return None
            if moment >= window.ends_at:
                for key in [key for key, item in self._faults.items() if item is window]:
                    self._faults.pop(key, None)
                return None
            if moment < window.starts_at:
                return None
            return window.fault

    def active_faults(self, now: float | None = None) -> dict[str, str]:
        """当前生效的故障（按路）；`stall` 时两路都在里面。"""
        moment = time.monotonic() if now is None else float(now)
        return {
            source: fault
            for source in self._sources
            if (fault := self.fault_state(source, moment)) is not None
        }

    # -- 合成电平 ---------------------------------------------------------

    def level_sequence(self, source: str, count: int) -> list[float]:
        """按种子算出 `count` 帧的电平序列（**确定性**：同种子同帧序逐帧相同）。"""
        rng = random.Random(f"{self.seed}:{source}")
        values: list[float] = []
        phase = 0.0
        for index in range(int(count)):
            wave = (math.sin(index * 0.31 + phase) + 1.0) / 2.0
            value = DEV_LEVEL_MIN_DBFS + wave * (DEV_LEVEL_MAX_DBFS - DEV_LEVEL_MIN_DBFS)
            value += rng.uniform(-1.5, 1.5)
            values.append(max(DEV_LEVEL_MIN_DBFS, min(DEV_LEVEL_MAX_DBFS, value)))
            phase += 0.07
        return values

    def collect_frames(self, count: int, *, timeout: float = 10.0) -> list[Any]:
        """起一路采集并收集 `count` 条事实（给纯单测用，不起服务）。"""
        received: list[Any] = []
        done = threading.Event()

        def sink(fact: Any) -> None:
            received.append(fact)
            if len(received) >= count:
                done.set()

        self.bind(sink, config=None)
        self.start("both", "<dev-collect>")
        try:
            done.wait(timeout=timeout)
        finally:
            self.stop()
        return received

    # -- 合成线程 ---------------------------------------------------------

    def _run(self) -> None:
        tick = 0
        while not self._stop.is_set():
            now = time.monotonic()
            with self._lock:
                faults = {source: self.fault_state(source, now) for source in self._sources}
            if "stall" in faults.values():
                # `stall` = 两路的事实帧整体停发（HTTP 照常应答，于是 recording_alive = false）
                tick += 1
                time.sleep(DEV_FRAME_INTERVAL_SECONDS)
                continue

            for source in self._sources:
                fault = faults.get(source)
                if fault == "lost":
                    # `lost` 的窗口内这一路**整段断流**：窗口一开始发一条 `SourceLost`（照契约），
                    # 之后一拍电平帧都不发 —— 否则电平帧会把这一路的帧时钟喂活、对端据此把这路
                    # 判回 `restored`，而故障窗口还没到点（窗口到点前它就该一直是断的）。
                    with self._lock:
                        window = self._faults.get(source)
                        first_tick = window is not None and not window.fired
                        if first_tick:
                            window.fired = True
                    if first_tick:
                        self._emit_fact(SourceLost(source=source, reason="dev 替身注入的流中断"))
                    continue
                if fault == "silence":
                    # 电平落到 −80 dBFS 以下并保持（走 alerts.py 的静默累计）
                    self._emit_frame(source, now, DEV_SILENCE_DBFS)
                    continue
                dbfs = self.level_sequence(source, tick + 1)[-1]
                self._emit_frame(source, now, dbfs, peak=min(DEV_LEVEL_MAX_DBFS, dbfs + 3.0))

            # 事实帧里的存活事实（`SourceFacts` **不刷新**帧时钟：只有 `LevelFrame` 刷新）
            for source in self._sources:
                if faults.get(source) in ("stall", "lost"):
                    continue
                self._emit_fact(SourceFacts(source=source, stream_alive=True))

            self._frame_index += 1
            tick += 1
            time.sleep(DEV_FRAME_INTERVAL_SECONDS)

    def _emit_frame(self, source: str, now: float, dbfs: float, peak: float | None = None) -> None:
        peak_value = dbfs if peak is None else peak
        bar = max(dbfs, peak_value)
        self._emit_fact(
            LevelFrame(source=source, ts=now, dbfs=float(dbfs), peak=float(peak_value), bar=float(bar))
        )
        self.emitted_frames += 1

    def _emit_fact(self, fact: Any) -> None:
        emit = self._emit
        if emit is None:
            return
        try:
            emit(fact)
        except Exception:  # pragma: no cover - 出口异常不许把合成线程带走
            _log.exception("dev 替身的事实出口抛错（只记一条，合成继续）")


class DevTranscriberBackend:
    """dev 转写替身：会中每隔 `interval_seconds` 秒产一行两路轮换的合成转写。

    **非产品路径**（`transcriber.backend = dev` 才解析到它，见模块 docstring 的最后一节）。
    它是一条「开发时能看见字幕在动」的稳定替身：不读归档、不加载模型、不写产物文件，
    只把 `TranscriptLine` 交给 `line_sink`（与真后端同一个出口形状）。

    起停语义与真后端同形：`bind()` → `start(mode, session_dir)` → `stop()`；
    `stop()` 之后线程收队、**一行都不再产**（`join` 有上界，超时留一条 warning，不静默）。
    """

    def __init__(
        self,
        *,
        interval_seconds: float | None = None,
        initial_delay_seconds: float | None = None,
        sources: tuple[str, ...] = SOURCE_IDS,
    ) -> None:
        """三个可选参数（都带缺省，registry 的无参实例化因此照常成立）。

        `interval_seconds` / `initial_delay_seconds` 缺省取本模块的两个常量 ——
        用例传小值就能把节奏掐快，不必等 0.5 秒一行。
        """
        self.interval_seconds = (
            DEV_TRANSCRIPT_INTERVAL_SECONDS if interval_seconds is None else float(interval_seconds)
        )
        self.initial_delay_seconds = (
            DEV_TRANSCRIPT_INITIAL_DELAY_SECONDS
            if initial_delay_seconds is None
            else float(initial_delay_seconds)
        )
        self.sources = tuple(sources)
        self._line_sink: Callable[[Any], None] | None = None
        self._config: Any = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._mode = "both"
        self._session_dir: str | None = None
        #: 记账（用例据此断言「产了几行」）
        self.lines_emitted = 0

    # -- 协议 -------------------------------------------------------------

    def bind(self, line_sink: Callable[[Any], None], *, config: Any = None) -> None:
        """记下行出口与只读配置视图（runtime 在 `start()` 之前调一次）。"""
        self._line_sink = line_sink
        self._config = config

    def start(self, mode: str, session_dir: str) -> None:
        """起合成行：先等 `initial_delay_seconds` 秒，之后每 `interval_seconds` 秒一行。"""
        self._mode = str(mode)
        self._session_dir = str(session_dir)
        self._stop.clear()
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="dev-transcriber", daemon=True)
        self._thread.start()
        _log.info(
            "dev 转写替身已起（非产品路径）：mode=%s session=%s 首发 %.1fs 后、每 %.1fs 一行",
            self._mode, self._session_dir, self.initial_delay_seconds, self.interval_seconds,
        )

    def stop(self) -> None:
        """收队（**幂等**）：之后一行都不再产。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
            if thread.is_alive():  # pragma: no cover - daemon 线程，不阻塞进程退出
                _log.warning("dev 转写替身的线程 2s 内没有退出（daemon，不阻塞进程退出）")
        self._thread = None

    def health(self) -> str:
        """`normal`：替身按自己的节奏产行，没有「落后」这件事（积压恒 0）。"""
        return "normal"

    def backlog_depth(self) -> int:
        """替身不排队，积压恒 0。"""
        return 0

    def segment_timeout(self) -> float:
        """替身不走识别，报缺省 20.0 秒（与 `NullTranscriberBackend` 同值）。"""
        return 20.0

    # -- 合成线程 ---------------------------------------------------------

    def _run(self) -> None:
        if self._stop.wait(float(self.initial_delay_seconds)):
            return
        index = 0
        while not self._stop.is_set():
            self._emit_line(index)
            index += 1
            if self._stop.wait(float(self.interval_seconds)):
                return

    def _emit_line(self, index: int) -> None:
        sink = self._line_sink
        if sink is None:
            return
        source = self.sources[index % len(self.sources)]
        line = TranscriptLine(
            ts=time.time(),
            source=source,
            text=f"{DEV_TRANSCRIPT_TEXT}（{source} #{index + 1}）",
            kind="line",
        )
        try:
            sink(line)
        except Exception:  # pragma: no cover - 出口异常不许把合成线程带走
            _log.exception("dev 转写替身的行出口抛错（只记一条，合成继续）")
            return
        self.lines_emitted += 1


__all__ = [
    "DEV_FAULT_DEFAULT_SECONDS",
    "DEV_FAULT_KINDS",
    "DEV_FRAME_INTERVAL_SECONDS",
    "DEV_LEVEL_MAX_DBFS",
    "DEV_LEVEL_MIN_DBFS",
    "DEV_LEVEL_SEED",
    "DEV_SILENCE_DBFS",
    "DEV_TRANSCRIPT_INITIAL_DELAY_SECONDS",
    "DEV_TRANSCRIPT_INTERVAL_SECONDS",
    "DEV_TRANSCRIPT_TEXT",
    "DevCaptureBackend",
    "DevTranscriberBackend",
]
