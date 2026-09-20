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
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from meeting.backends.base import LevelFrame, SourceFacts, SourceLost, SourceProbe

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


__all__ = [
    "DEV_FAULT_DEFAULT_SECONDS",
    "DEV_FAULT_KINDS",
    "DEV_FRAME_INTERVAL_SECONDS",
    "DEV_LEVEL_MAX_DBFS",
    "DEV_LEVEL_MIN_DBFS",
    "DEV_LEVEL_SEED",
    "DEV_SILENCE_DBFS",
    "DevCaptureBackend",
]
