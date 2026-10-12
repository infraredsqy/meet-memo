"""帧空洞自查记录 —— 发帧线程自己的「与上一拍的间隔」登记（gil-stall-hole 节点）。

**为什么检测点在生产侧**（`.work/issues/specs/opening-window-false-capture-alarm.md:52-53`
的实测）：录中发生 GIL 饿死 / 整进程冻结时，采帧与判停的线程**一起冻住**，解冻后
服务侧的帧时刻立刻刷新，空洞在判停那一侧的记录上不留痕。墙钟（单调钟）不冻 ——
**发帧线程解冻后的第一拍**拿当下与上一拍一减，真实空洞时长就出来了。

口径三条（设计已拍板）：

  1. **先算间隔、再刷新时刻**：`observe()` 每次产帧先与上一拍做差，超过既有窗口
     （`meeting/state.py` 的 `frame_clock_window()`，`max(2.0s, 3 × frame_interval)`，
     公式不新造）才报一次空洞；无论报不报，上一拍都刷新 —— 于是**同一空洞回合
     天然只报一次**，解冻后逐帧连记这条路被源头堵死。
  2. **首帧前无上一帧**：第一次 `observe()` 只立基准、不报 —— 与 `recording_alive`
     的「还没有第一帧」未知档互不干扰。
  3. **时钟可注入**：`observe(now=…)` 收显式时刻，纯逻辑用例毫秒级跑完，不等真实窗口。

窗口公式与常量都从 `meeting/state.py` 来，本模块不复制数值。
"""

from __future__ import annotations

import time
from typing import Callable

from meeting.state import frame_clock_window


class FrameGapDetector:
    """一根发帧线程的上一拍登记：算间隔、超窗报一次、然后刷新。

    `on_gap(seconds, detected)` 在**空洞被发现的那一刻**（≈ 解冻后的第一拍）被调；
    `seconds` 是空洞时长、`detected` 是发现时刻（采集侧单调钟，与 `LevelFrame.ts`
    同一口径）。记录的合并与累计归服务侧，本类一拍只报一次、不做任何累计。
    """

    def __init__(
        self,
        *,
        frame_interval: float,
        on_gap: Callable[[float, float], None],
    ) -> None:
        self._window = frame_clock_window(frame_interval)
        self._on_gap = on_gap
        #: 上一拍（采集侧单调钟）；`None` = 还没有过上一帧（首帧前不记）
        self._last: float | None = None

    def reset(self) -> None:
        """清掉上一拍（后端 / 引擎重新起一场时调 —— 跨场的间隔不是本场的空洞）。"""
        self._last = None

    @property
    def window(self) -> float:
        """判据窗口（秒），与 `recording_alive` 同一把尺。"""
        return self._window

    def observe(self, *, now: float | None = None) -> float | None:
        """产帧时调：先算与上一拍的间隔，**再**刷新上一拍；超窗报一次空洞。

        返回本次报出的空洞秒数（没报就是 `None`）。`now` 供用例注入时钟，
        生产路径一律省略（取 `time.monotonic()`）。
        """
        moment = time.monotonic() if now is None else float(now)
        previous = self._last
        self._last = moment
        if previous is None:
            return None
        gap = moment - previous
        if gap > self._window:
            self._on_gap(gap, moment)
            return gap
        return None


__all__ = ["FrameGapDetector"]
