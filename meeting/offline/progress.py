"""会后离线转写的**逐块进度账**（`meeting_offline_progress.jsonl`）与它的现场读数。

## 这份文件是什么

每跑完一块就往场次目录里那份进度账上补一行：**这一块的文字 + 这一块的落点**。
一块跑完，那一块的字当场就能从盘上读到，不必等整路跑完（`docs/design-intent.md`
第 12 条「每跑完一块就落一次盘」、R27）。

一份进度账、两路分节（`source` 那一格）。**两路各算各的块数与分母**（第 9 条：
两路永远不合并），合成一个整场分母就是在造一个不存在的量。

## 三条边界（照做，别放宽）

1. **正式稿另有一份**。正式稿 `meeting_offline_transcript_<source>.md` 的写时机一个字
   不改（整路跑完一次写）、**也不许提前建它**：`meeting/workspace.py` 的
   `has_offline_transcript()` 只看名目，提前建正式文件会让 R29 那一格提前变真。
   进度账的名字与正式稿不同（`meeting_offline_progress.jsonl`），前缀也对不上，
   两者互不冒充。
2. **它是一份给人看的读数，不带校验性质的含义**。墙钟跨度、块计数这类错了就错了，
   任何代码不许拿它去对账、报错、改结论（`docs/design-intent.md` 第 6、7 条）。
   它也不参与 `meeting/cli.py` 的幂等闸（那道闸读的是 `meeting_offline_status.json`
   的 `state` 那一格）。
3. **落盘失败不带走这一趟**。进度账是附注，写不下去只记一条 log；正片照跑。

## 形状

第一行是表头，之后一行一块（JSON Lines，一行一个对象）：

```
{"kind": "ledger", "schema_version": 1, "session_id": "250919", "sources": ["mic", "loopback"]}
{"source": "mic", "chunk": 1, "chunks_total": 2, "offset_seconds": 0.0, "seconds": 450.0,
 "span": [0.0, 450.0], "state": "transcribed", "text": "[00:00] **c01:S01**: 甲",
 "segments": 3, "wall_seconds": 45.247, "error": null}
```

- `chunk` 从 1 起，`chunks_total` 是**这一路**的块数（`plan_chunks()` 切出来的块数）；
- `state` 四档：`transcribed`（跑成了）/ `silent`（数字静音、跳过引擎）/ `failed`（这块
  没成）/ `unjudged`（跑成了但门没在这块上做成判断）。**静音跳过的块与失败的块都算
  "跑过了一块"**：分母来自切块计划，不因成败增减；`state` 如实记下这一块的结果档。
- `text` 是这一块的文字（`[HH:MM] **c01:S01**: 正文` 一行一条，`\n` 相连）；静音与失败
  那两档没有文字，如实留空串。**读它的是人**：块内的说话人编号带 `cNN:` 命名空间，
  与正式稿的编号（整路内按首次出现顺序）不是同一个东西。
- 两路的行按**这一趟实际跑的顺序**交错（两路串行），读方按 `source` 分节即可。

## 写法

整份重写 + 原子替换（`artifacts.write_text_atomic()`）。写方是会后那条后台线程，
读方是界面随时打过来的请求；一篇写到一半的 JSON 比慢一点更坏，而原子替换正是这一族
既有产物在用的写法（`meeting_offline_status.json` 同款）。代价是每块重写整份 —— 一份账
按每行约 200 字节算，几十块也就几十 KB，相对一次几十秒的模型推理可以忽略。

**读方容忍原子写的中间态**：读到空文件、半行、或文件正被换走时，如实交回"读不出来"，
不抛、不猜。
"""

from __future__ import annotations

import json
import logging
import threading
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from meeting.offline import artifacts

_log = logging.getLogger("meeting.offline.progress")

#: 逐块进度账的文件名。刻意与正式稿分家：`has_offline_transcript()` 按
#: `meeting_offline_transcript_` 前缀判「有没有会后精细稿」，这份对不上那个前缀。
PROGRESS_NAME = "meeting_offline_progress.jsonl"

#: 进度账自己的模式版本（与转写/对照表/状态文件那三个各自独立）
PROGRESS_SCHEMA_VERSION = 1

#: 表头那一行的 `kind` 字面量（读方据此认出表头）
LEDGER_KIND = "ledger"

#: 一块跑完之后落在账上的四档结果。**四档都算"跑过了一块"**（分母不因它们增减）。
CHUNK_STATES = ("transcribed", "silent", "failed", "unjudged")

#: 落盘失败时的重试次数（与 `artifacts.write_text_atomic()` 的退避同一条口径）
WRITE_RETRIES = 5


def ledger_path(session_dir: Path | str) -> Path:
    """这一场的逐块进度账落在哪。"""
    return Path(session_dir) / PROGRESS_NAME


def _round(value: Any) -> float:
    return round(float(value), 3)


def _counts_by_state(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """按结果档数一遍（**四档都列出来**，读方不必自己补零）。"""
    counts = {name: 0 for name in CHUNK_STATES}
    for row in rows:
        state = str(row.get("state") or "")
        if state in counts:
            counts[state] = counts[state] + 1
    return counts


@dataclass
class ChunkProgress:
    """一块跑完之后要落下来的那一格（`text` 是这一块的文字）。"""

    source: str
    index: int
    offset_seconds: float
    seconds: float
    state: str
    text: str = ""
    segments: int = 0
    wall_seconds: float = 0.0
    error: str | None = None
    chunks_total: int | None = None

    def to_row(self, *, chunks_total: int | None = None) -> dict[str, Any]:
        total = self.chunks_total if chunks_total is None else chunks_total
        return {
            "source": str(self.source),
            "chunk": int(self.index),
            "chunks_total": None if total is None else int(total),
            "offset_seconds": _round(self.offset_seconds),
            "seconds": _round(self.seconds),
            "span": [
                _round(self.offset_seconds),
                _round(float(self.offset_seconds) + float(self.seconds)),
            ],
            "state": str(self.state),
            "text": str(self.text),
            "segments": int(self.segments),
            "wall_seconds": _round(self.wall_seconds),
            "error": None if self.error is None else str(self.error),
        }


def write_progress(
    path: Path | str,
    *,
    session_id: str,
    sources: Sequence[str],
    rows: Iterable[Mapping[str, Any]],
) -> Path:
    """把整份进度账重写一遍（表头 + 逐块那些行，原子替换）。

    JSON Lines、行尾一个换行、末行也有换行：读方按行读，半行由 `read_progress()` 挡在
    外面（它只收能被 `json.loads` 认下来的行）。
    """
    header = {
        "kind": LEDGER_KIND,
        "schema_version": PROGRESS_SCHEMA_VERSION,
        "session_id": str(session_id),
        "sources": [str(item) for item in sources],
    }
    lines = [json.dumps(header, ensure_ascii=False)]
    for row in rows:
        lines.append(json.dumps(dict(row), ensure_ascii=False))
    return artifacts.write_text_atomic(path, "\n".join(lines) + "\n", retries=WRITE_RETRIES)


def read_progress(path: Path | str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """读回一份进度账：`(表头, 逐块那些行)`。

    **读不出来就如实交回 `(None, [])`**：文件不在、正被原子替换换走、写到一半的半行，
    都走这一档。这份账是给人看的读数，读方不必为它抛错，安安静静答一句"这会儿读不到"
    就够了。
    """
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return None, []
    header: dict[str, Any] | None = None
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(raw.splitlines()):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except (ValueError, TypeError):
            # 半行（写方正在换这一份文件）：跳过这一行，别的行照收
            continue
        if not isinstance(item, dict):
            continue
        if number == 0 and item.get("kind") == LEDGER_KIND:
            header = item
            continue
        if "source" in item and "chunk" in item:
            rows.append(item)
    return header, rows


def _source_progress(
    source: str, rows: Sequence[Mapping[str, Any]], total: int | None, *, running: bool
) -> dict[str, Any]:
    """一路的读数（哪一路 / 跑过几块 / 共几块 / 此刻在第几块 / 四档各几条）。

    `source` 那一格给的是**路名**（`mic` / `loopback`，机器读）；人话标签（本地 / 远端）
    归界面（`meeting/state.py` 的 `SOURCE_LABELS`），这一层不渲染。
    """
    done = len(rows)
    return {
        "source": str(source),
        "done": done,
        "total": None if total is None else int(total),
        "remaining": None if total is None else max(0, int(total) - done),
        # 「正在跑的是第几块」：这一路已经跑过几块，此刻在跑的就是下一块；
        # 整路跑完了就如实交回 `None`
        "current_chunk": (
            None if total is not None and done >= int(total)
            else (done + 1 if running else None)
        ),
        "counts": _counts_by_state(rows),
    }


class OfflineProgressLedger:
    """这一趟离线跑的逐块账：**每块跑完落一次盘**，同时留一份能在进程内读的现场。

    两个读法各有各的用处：

      - `snapshot()` 给服务侧那只端点（`GET /api/sessions/{id}`）随取随用：它把已经
        跑过几块、正在跑哪一路、已经跑了多久一次交回，**不读盘**；
      - 盘上那份 `meeting_offline_progress.jsonl` 给"页面之外"的读方（命令行、复审、
        几天后回头翻这一场的人）：一块一块攒起来，服务重启之后照旧在那儿。

    实例是**线程安全**的：写方是会后那条后台线程，读方是事件循环里的请求。
    """

    def __init__(
        self,
        session_dir: Path | str,
        *,
        session_id: str,
        sources: Sequence[str],
        clock: Callable[[], float] = _time.monotonic,
        writer: Callable[..., Path] = write_progress,
    ) -> None:
        self.session_dir = Path(session_dir)
        self.session_id = str(session_id)
        self.sources: tuple[str, ...] = tuple(str(item) for item in sources)
        self._clock = clock
        self._writer = writer
        self._lock = threading.Lock()
        #: 起点（单调钟）：只用来算「已经跑了多久」，这一格只作附注
        #: （`docs/design-intent.md` 第 6、7 条）
        self._started = float(clock())
        self._current_source: str | None = None
        self._totals: dict[str, int] = {}
        self._rows: list[dict[str, Any]] = []
        #: `started_at` / `finished_at` 是**墙钟**（跨进程的读方只能靠它），`None` = 还没开跑
        self.started_at: str | None = None
        self.finished_at: str | None = None

    @classmethod
    def for_session(cls, session_dir: Path | str) -> "OfflineProgressLedger":
        """按场次目录立一份账：`session_id` 与路清单都**现问 `offline.runner`**。

        `meeting/service/runtime.py` 的 `_run_post_offline()` 与 `meeting/cli.py::run_post()`
        （`progress` 缺省分支）从前各抄一遍同一个三参构造（场次目录 + `runner.session_id_of()`
        + `runner.declared_sources()`），现在只有这一份。`runner` 在**函数体内** import：
        它反过来在模块顶层 import 本模块，顶层 import 会绕成环。
        """
        from meeting.offline import runner as offline_runner

        directory = Path(session_dir)
        return cls(
            directory,
            session_id=str(offline_runner.session_id_of(directory)),
            sources=list(offline_runner.declared_sources(directory)),
        )

    # -- 写 ---------------------------------------------------------------

    def start(self, *, started_at: str) -> None:
        """这一趟开跑：记下墙钟起点，并把一份**只有表头**的账落下去。

        先落表头那一下有用：盘上从这一刻起就有「这一场在跑、跑的是哪几路、一块都还没
        跑完」这份读数，与 `meeting_offline_status.json` 开头那一版 `running` 同时到位。
        """
        with self._lock:
            self.started_at = str(started_at)
            self._flush_locked()

    def start_source(self, source: str, total_chunks: int) -> None:
        """轮到这一路了：分母（这一路切了几块）在**进逐块循环之前**就定下来。

        分母来自切块计划，因此它**不因某一块失败或跳过而增减** —— 读数是「第 N 块 /
        共 M 块」那个 M，与跑成了几块分开说。
        """
        with self._lock:
            self._current_source = str(source)
            self._totals[str(source)] = int(total_chunks)

    def record_chunk(self, entry: ChunkProgress) -> None:
        """一块跑完：把这一块的文字与落点补进账里，**当场落盘**。"""
        with self._lock:
            total = (
                entry.chunks_total
                if entry.chunks_total is not None
                else self._totals.get(str(entry.source))
            )
            self._rows.append(entry.to_row(chunks_total=total))
            self._flush_locked()

    def has_source(self, source: str) -> bool:
        """这一路在账上有没有行（切法算不出来那一档要据此补一行）。"""
        with self._lock:
            return any(str(row.get("source") or "") == str(source) for row in self._rows)

    def finish(self, *, finished_at: str | None = None) -> None:
        """这一趟跑完（成了、失败、被打断都走这里）：标记终态并把账再落一次。"""
        with self._lock:
            self.finished_at = None if finished_at is None else str(finished_at)
            self._flush_locked()

    def _flush_locked(self) -> None:
        """落盘（**调用方已经持锁**）。写不下去只记 log：进度账是附注，不带走这一趟。"""
        try:
            self._writer(
                ledger_path(self.session_dir),
                session_id=self.session_id,
                sources=list(self.sources),
                rows=list(self._rows),
            )
        except Exception:  # pragma: no cover - 盘满/只读盘那一档
            _log.warning(
                "会后离线的逐块进度账落不下去（不影响这一趟）：%s",
                ledger_path(self.session_dir), exc_info=True,
            )

    # -- 读 ---------------------------------------------------------------

    def elapsed_seconds(self) -> float:
        """这一趟从开跑到此刻的墙钟秒数（**只作附注**，不许拿它对账）。"""
        with self._lock:
            return max(0.0, round(float(self._clock()) - self._started, 3))

    def snapshot(self) -> dict[str, Any]:
        """此刻的现场读数（跑过几块 / 正在跑哪一路 / 已经跑了多久）。

        形状见模块头与 `docs/contract.md` 第 4.9 节；`GET /api/sessions/{id}` 的
        `offline` 那一格就是它。**不读盘**。
        """
        with self._lock:
            started_at = self.started_at
            finished_at = self.finished_at
            current = self._current_source
            totals = dict(self._totals)
            rows = list(self._rows)
        running = bool(started_at) and not finished_at
        per_source: list[dict[str, Any]] = []
        names = list(self.sources)
        for row in rows:
            name = str(row.get("source") or "")
            if name and name not in names:
                names.append(name)
        for source in names:
            mine = [row for row in rows if str(row.get("source") or "") == source]
            total = totals.get(source)
            if total is None:
                for row in mine:
                    if row.get("chunks_total") is not None:
                        total = int(row["chunks_total"])
                        break
            # 「正在跑的是第几块」只有**本路**在跑时才有值：那一趟还没轮到的路如实交回
            # `None`，"第 1 块 / 共 None 块"这种半截读数读起来像是它已经开跑了。
            running_here = running and source == current
            per_source.append(_source_progress(source, mine, total, running=running_here))
        return {
            "ledger": PROGRESS_NAME,
            "state": "done" if finished_at else ("running" if started_at else "idle"),
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_seconds": self.elapsed_seconds(),
            "current_source": current,
            "sources": per_source,
        }


def snapshot_of(
    session_dir: Path | str, *, sources: Sequence[str], session_id: str
) -> dict[str, Any]:
    """**从盘上那份进度账**拼出同一套形状的读数（进程里没有现场时用它）。

    服务重启之后、或命令行那一趟跑完回头翻这一场，走的就是这一条。跨进程算不出单调钟
    的差值，`elapsed_seconds` 因此如实交回 `None`（它本来就只作附注）；`started_at`
    照旧只在这份账的落盘内容里出现……它不在账里（表头记的是场次与两路），于是交给
    调用方去问状态文件。`finished_at` / `current_source` 同理交回 `None`，
    `state` 那一格答 `"ledger"`：盘上有这份账，跑没跑完由状态文件回答。
    """
    header, rows = read_progress(ledger_path(session_dir))
    names = list(sources)
    for row in rows:
        name = str(row.get("source") or "")
        if name and name not in names:
            names.append(name)
    per_source: list[dict[str, Any]] = []
    for source in names:
        mine = [row for row in rows if str(row.get("source") or "") == source]
        total = None
        for row in mine:
            if row.get("chunks_total") is not None:
                total = int(row["chunks_total"])
                break
        per_source.append(_source_progress(source, mine, total, running=False))
    return {
        "ledger": PROGRESS_NAME,
        "state": "ledger" if header is not None else "idle",
        "started_at": None,
        "finished_at": None,
        "elapsed_seconds": None,
        "current_source": None,
        "sources": per_source,
    }


__all__ = [
    "CHUNK_STATES",
    "LEDGER_KIND",
    "PROGRESS_NAME",
    "PROGRESS_SCHEMA_VERSION",
    "ChunkProgress",
    "OfflineProgressLedger",
    "ledger_path",
    "read_progress",
    "snapshot_of",
    "write_progress",
]
