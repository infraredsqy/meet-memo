"""会中总结的窗口切片：内存里的追加缓冲 + 按时间戳取连续尾部。

这个模块服务 spec 里那两条冻结裁定：

  - **窗口从哪儿读**：服务侧内存里的一条追加缓冲（由转写行的 sink 喂）。盘上那两份只供人看。
    盘上 `meeting_transcript_<source>.md` 只有分钟级的 `[HH:MM]`（秒被丢掉、同一分钟会撞成
    一个戳）、也没有单调序号；内存里那份带完整的墙钟浮点 `ts` 与入队序 `seq`。
  - **窗口怎么切**：按**行首时间戳**切 —— 取「末行 `ts` 往前 N 秒」为止的**连续尾部**，
    边界**不切进任何一行**；`整场` = 缓冲里全部。事故那一场里时间戳曾经被压进一分钟，
    按行数取窗口就会取错内容（spec「坑与注意」第 1 条）。

三块各管一件事：

  - `BufferedLine` / `TranscriptBuffer`：**收**。缓冲线程安全（转写行的 sink 从 worker 线程来，
    切窗口从服务的事件循环来），`lines()` 交回副本，调用方改不动缓冲里的东西。
  - `select_window()`：**切**。纯函数，只吃一个 `Sequence[BufferedLine]`，不碰缓冲对象。
  - `render_window()`：**装配**。窗口 → 喂给模型的文本（**逐字保留原句**，一行一句）。

`BufferedLine.ts` 与 `meeting.backends.base.TranscriptLine.ts` 是**同一个值**（墙钟秒、epoch）；
`seq` 是入队序、单调递增、从 0 起，同一毫秒上挤着一批行时靠它定先后。

**这里没有任何「什么时候触发」的东西**：V1 那套频率门控（每 10 段或 300 秒）明留在 V1，
本节点的窗口由人按键挑。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from meeting.backends.base import TranscriptLine

#: 缓冲的行数上限：**兜的是内存**，窗口要的是最近那几分钟。一场会一小时约 300–600 行，两万行是几十场的量；
#: 到顶时从头丢最老的那些（`append()` 里的裁剪），时间窗口取的是尾部，丢头不影响它。
DEFAULT_MAX_LINES = 20000

#: 窗口档位 → 人话标签（设计章 2.3 的四个档位逐字对应）
WINDOW_LABELS: dict[str, str] = {
    "5m": "5 分钟",
    "15m": "15 分钟",
    "30m": "30 分钟",
    "full": "整场",
}

#: 两路的人话路径标签（设计章 2.3「行格式：`[HH:MM] 路径标签 · 正文`」，取值 `本地` / `远端`；
#: 渲染窗口文本时照它，行尾标出来源，读窗口的人一眼看得出这一句是本地还是远端）
SOURCE_LABELS: dict[str, str] = {
    "mic": "本地",
    "loopback": "远端",
}

#: 认不出的来源原样标出（不许编一个中文名给它）
UNKNOWN_SOURCE_LABEL = "未知"


@dataclass(frozen=True)
class BufferedLine:
    """缓冲里的一行（**冻结**：切片交出去的是这些行本身，谁都不许就地改）。

    `seq` 是入队序，单调递增、从 0 起；`ts` 是墙钟秒（epoch），与 `TranscriptLine.ts`
    同一个值；`kind` 在生产路径上恒为 `"line"`（`TranscriptLine.kind` 的取值域是
    `line` / `gap`，转写流水线出的每一行都是 `line`）。
    """

    seq: int
    ts: float
    source: str
    text: str
    kind: str = "line"


class TranscriptBuffer:
    """一份追加中的实时转写（**线程安全**）。

    喂它的是转写行的 sink（worker 线程），读它的是切窗口那一下（服务的事件循环），
    因此每一处都得走同一把锁；`lines()` 交回的是**副本**，调用方拿着它随便排序、
    切片都不会动到缓冲里那一份。
    """

    def __init__(self, *, max_lines: int = DEFAULT_MAX_LINES) -> None:
        if int(max_lines) < 1:
            raise ValueError(f"TranscriptBuffer 的 max_lines 至少是 1（收到 {max_lines!r}）")
        self._max_lines = int(max_lines)
        self._lock = threading.Lock()
        self._lines: list[BufferedLine] = []
        self._seq = 0

    def append(self, line: TranscriptLine) -> None:
        """收一行转写（可从任何线程调）。

        `seq` 由缓冲自己发（不取自 `TranscriptLine` —— 那个结构里没有序号），
        因此同一毫秒上挤着一批行时，入队的先后就是 `seq` 的先后。
        """
        with self._lock:
            self._lines.append(
                BufferedLine(
                    seq=self._seq,
                    ts=float(line.ts),
                    source=str(line.source),
                    text=str(line.text),
                    kind=str(getattr(line, "kind", "line") or "line"),
                )
            )
            self._seq += 1
            if len(self._lines) > self._max_lines:
                # 到顶就从头丢：窗口取的是尾部，丢头不影响最近这几分钟
                del self._lines[: len(self._lines) - self._max_lines]

    def lines(self) -> list[BufferedLine]:
        """当前全部行的**副本**（按入队序）。"""
        with self._lock:
            return list(self._lines)

    def clear(self) -> None:
        """清空（**新的一场会从头开始**；`seq` 一起归零，免得读方看见一个跳变）。"""
        with self._lock:
            self._lines.clear()
            self._seq = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._lines)

    @property
    def max_lines(self) -> int:
        """行数上限（构造时定死）。"""
        return self._max_lines


def source_label(source: str) -> str:
    """一路来源的人话标签（`mic` → `本地` / `loopback` → `远端`；认不出就报 `未知`）。"""
    return SOURCE_LABELS.get(str(source), UNKNOWN_SOURCE_LABEL)


def select_window(
    lines: Sequence[BufferedLine],
    *,
    window_seconds: float | None,
    now_ts: float | None = None,
) -> list[BufferedLine]:
    """取窗口：**末行 `ts` 往前 `window_seconds` 秒**的那一段连续尾部。

    - `window_seconds is None`（`整场`）→ 缓冲里的全部行（原样、不重排）；
    - 缓冲非空 → **锚点就是末行的 `ts`**（`cutoff = 末行 ts − window_seconds`）：
      转写停转或说话变稀的时候，末行比此刻早多少都不影响窗口取到那几行 ——
      `now_ts` 在这里**不参与**；拿当前墙钟当锚点会让「末行早就过去了」的缓冲切出空窗口，
      调用方随后交回一个一行都没有的静默成功（spec 冻结的口径见 task.md 的接口契约）；
    - 缓冲为空且给了 `now_ts` → 用 `now_ts` 当锚点：刚开会、一行都还没落下来时，
      窗口交回空的一份，这样调用方拿到的是「还没有材料」这件事实本身；
    - 缓冲为空且不给 `now_ts` → 空窗口（没有锚点，也没有行可取）。

    **边界不切进任何一行**：从尾部向前收到「`ts < cutoff`」为止。同一个 `ts` 上挤着
    一批行时按 `seq` 的先后**整批一起收或一起丢**，绝不从中间断开 —— 事故那一场里
    时间戳被压进同一分钟，按行数取窗口就会把同一分钟的内容切成两半。

    传进来的 `lines` 按入队序（`seq` 升序）给；本函数**不排序**、也不改输入，
    交回的是同一批 `BufferedLine` 对象组成的新列表。
    """
    if window_seconds is None:
        return list(lines)

    span = float(window_seconds)
    if span <= 0:
        # 0 秒窗口没有任何意义，也不该悄悄退化成「一行都不给」或「全给」：
        # 调用方（`run_summary()`）只发四档取值，走到这里说明是直接调本函数的人写错了
        raise ValueError(f"select_window 的 window_seconds 必须是正数（收到 {window_seconds!r}）")

    if not lines:
        return []

    # 锚点：**末行的 ts**（契约口径）。`now_ts` 只服务「缓冲为空」那一格（上面已经返回），
    # 因此走到这里它**不参与计算** —— 这一格是契约里的参数，留着不改签名。
    cutoff = float(lines[-1].ts) - span

    kept: list[BufferedLine] = []
    for line in reversed(lines):
        if float(line.ts) >= cutoff:
            kept.append(line)
            continue
        # 这一行落在窗口外，它前面那些行的 ts 只会更早，一起留下。
        # 同一个 ts 上挤着的一批行因此**整批一起收或一起丢**：边界落在这一批的
        # 前一条与后一条之间，绝不从中间断开。
        break
    kept.reverse()
    return kept


def render_window(lines: Sequence[BufferedLine]) -> str:
    """窗口 → 喂给模型的文本：**一行一句、原样**，行尾标出来源。

    形状：`[HH:MM:SS] 正文 · 本地`。

    - 正文**逐字保留**（识别失败的占位符 `[[识别失败: 超时 20s]]` 也原样在里面 ——
      提示词负责把缺口讲清楚，这一层不替它改写、不替它删）；
    - 时间是那一行的**墙钟时刻**（不是相对秒数）：模型要能说清「讲到哪儿了」；
    - 来源标在行尾，与设计章 2.3 的 `本地` / `远端` 同一套词。
    """
    rendered: list[str] = []
    for line in lines:
        stamp = datetime.fromtimestamp(float(line.ts)).strftime("%H:%M:%S")
        rendered.append(f"[{stamp}] {line.text} · {source_label(line.source)}")
    return "\n".join(rendered)


def now_seconds() -> float:
    """当前墙钟秒（`select_window()` 的 `now_ts` 缺省来源）。"""
    return time.time()


def line_count(lines: Sequence[Any]) -> int:
    """窗口里有多少行（`run_summary()` 报 `lines` 用的那一个数）。"""
    return len(lines)


__all__ = [
    "DEFAULT_MAX_LINES",
    "SOURCE_LABELS",
    "UNKNOWN_SOURCE_LABEL",
    "WINDOW_LABELS",
    "BufferedLine",
    "TranscriptBuffer",
    "line_count",
    "now_seconds",
    "render_window",
    "select_window",
    "source_label",
]
