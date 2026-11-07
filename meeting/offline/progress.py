"""会后离线转写的**逐块进度记录**（`meeting_offline_progress.jsonl`）与它的现场读数。

每跑完一块就补一行（这一块的文字 + 落点），一块的字当场可读（design-intent 第 12 条、
R27）。三条边界照做别放宽：

1. **正式稿另有一份**：`meeting_offline_transcript_<source>.md` 的写时机不改、也不许
   提前建——`has_offline_transcript()` 只看名目，提前建会让 R29 提前变真；本文件
   前缀刻意对不上正式稿。
2. **它是给人看的读数，不带校验性质**：任何代码不许拿它核对、报错、改结论
   （design-intent 第 6、7 条）；也不参与 cli 幂等闸（那道闸读 `meeting_offline_status.json`）。
3. **落盘失败不带走这一趟**：写不下去只记 log，正片照跑。

形状：第一行表头（`kind: ledger`）+ 一行一块的 JSON Lines；`chunks_total` 是**这一路**
的块数（两路各算各的，永不合并）；`state` 四档（`CHUNK_STATES`），静音跳过与失败的块
**都算"跑过了一块"**——分母来自切块计划，不因成败增减。整份重写 + 原子替换；读方容忍
原子写中间态（空文件、半行如实交回"读不出来"，不抛不猜）。
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

#: 逐块进度记录的文件名。刻意与正式稿分家：`has_offline_transcript()` 按
#: `meeting_offline_transcript_` 前缀判「有没有会后精细稿」，这份对不上那个前缀。
PROGRESS_NAME = "meeting_offline_progress.jsonl"

#: 进度记录自己的模式版本（与转写/对照表/状态文件那三个各自独立）
PROGRESS_SCHEMA_VERSION = 1

#: 表头那一行的 `kind` 字面量（读方据此认出表头）
LEDGER_KIND = "ledger"

#: 一块跑完之后落在记录里的四档结果。**四档都算"跑过了一块"**（分母不因它们增减）。
CHUNK_STATES = ("transcribed", "silent", "failed", "unjudged")

#: 落盘失败时的重试次数（与 `artifacts.write_text_atomic()` 的退避同一条口径）
WRITE_RETRIES = 5


def ledger_path(session_dir: Path | str) -> Path:
    """这一场的逐块进度记录落在哪。"""
    return Path(session_dir) / PROGRESS_NAME


def _round(value: Any) -> float:
    return round(float(value), 3)


def counts_by_state(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """按结果档数一遍（**四档都列出来**，读方不必自己补零）。数块数的唯一实现：
    现场读数与重放工具（`meeting/offline/replay.py`）都从这里数，别另写同构循环。
    """
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
    """把整份进度记录重写一遍（表头 + 逐块那些行，原子替换）。

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
    """读回一份进度记录：`(表头, 逐块那些行)`。读不出来（文件不在、正被原子替换换走、
    半行）就如实交回 `(None, [])`——这份记录是给人看的读数，不必为它抛错。
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
            continue  # 半行（写方正在换文件）：跳过这一行，别的行照收
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
    `source` 给机器读的路名，人话标签归界面（`meeting/state.py` 的 `SOURCE_LABELS`）。
    """
    done = len(rows)
    return {
        "source": str(source),
        "done": done,
        "total": None if total is None else int(total),
        "remaining": None if total is None else max(0, int(total) - done),
        # 正在跑的是下一块；整路跑完如实交回 None
        "current_chunk": (
            None if total is not None and done >= int(total)
            else (done + 1 if running else None)
        ),
        "counts": counts_by_state(rows),
    }


def _per_source_readings(
    names: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    *,
    totals: Mapping[str, int] | None = None,
    current: str | None = None,
    running: bool = False,
) -> list[dict[str, Any]]:
    """逐路读数拼装的**唯一出处**（offline-progress-snapshot-duplicated；形状挂
    `docs/contract.md` 4.9 的 `sources` 数组）：路名收集（记录里多出来的路补在声明的
    后面）、按路过滤、分母逐行回退取 `chunks_total` 都只写这一遍——现场版
    （`snapshot()`）与盘上版（`snapshot_of()`）共用，改一处两处同步。
    """
    collected = [str(name) for name in names]
    for row in rows:
        name = str(row.get("source") or "")
        if name and name not in collected:
            collected.append(name)
    per_source: list[dict[str, Any]] = []
    for source in collected:
        mine = [row for row in rows if str(row.get("source") or "") == source]
        total = None if totals is None else totals.get(source)
        if total is None:
            for row in mine:
                if row.get("chunks_total") is not None:
                    total = int(row["chunks_total"])
                    break
        # 「正在跑第几块」只有本路在跑时才有值：没轮到的路如实 None，
        # "第 1 块 / 共 None 块"读起来像已经开跑了。
        running_here = running and source == current
        per_source.append(_source_progress(source, mine, total, running=running_here))
    return per_source


class OfflineProgressLedger:
    """这一趟离线跑的逐块记录：**每块跑完落一次盘**，同时留一份进程内可读的现场。
    `snapshot()` 给服务端点随取随用（不读盘）；盘上那份给服务重启之后回头翻的人。
    实例**线程安全**：写方是会后后台线程，读方是事件循环里的请求。
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

    # -- 写 ---------------------------------------------------------------

    def start(self, *, started_at: str) -> None:
        """这一趟开跑：记下墙钟起点，并落一份只有表头的记录（盘上这一刻就有「在跑、
        跑哪几路、一块都没跑完」的读数，与状态文件那版 running 同时到位）。
        """
        with self._lock:
            self.started_at = str(started_at)
            self._flush_locked()

    def start_source(self, source: str, total_chunks: int) -> None:
        """轮到这一路：分母（这一路切了几块）在进逐块循环之前定下，不因块成败增减。"""
        with self._lock:
            self._current_source = str(source)
            self._totals[str(source)] = int(total_chunks)

    def record_chunk(self, entry: ChunkProgress) -> None:
        """一块跑完：把这一块的文字与落点补进记录里，**当场落盘**。"""
        with self._lock:
            total = (
                entry.chunks_total
                if entry.chunks_total is not None
                else self._totals.get(str(entry.source))
            )
            self._rows.append(entry.to_row(chunks_total=total))
            self._flush_locked()

    def has_source(self, source: str) -> bool:
        """这一路在记录里有没有行（切法算不出来那一档要据此补一行）。"""
        with self._lock:
            return any(str(row.get("source") or "") == str(source) for row in self._rows)

    def finish(self, *, finished_at: str | None = None) -> None:
        """这一趟跑完（成了、失败、被打断都走这里）：标记终态并把记录再落一次。"""
        with self._lock:
            self.finished_at = None if finished_at is None else str(finished_at)
            self._flush_locked()

    def _flush_locked(self) -> None:
        """落盘（**调用方已经持锁**）。写不下去只记 log：进度记录是附注，不带走这一趟。"""
        try:
            self._writer(
                ledger_path(self.session_dir),
                session_id=self.session_id,
                sources=list(self.sources),
                rows=list(self._rows),
            )
        except Exception:  # pragma: no cover - 盘满/只读盘那一档
            _log.warning(
                "会后离线的逐块进度记录落不下去（不影响这一趟）：%s",
                ledger_path(self.session_dir), exc_info=True,
            )

    # -- 读 ---------------------------------------------------------------

    def elapsed_seconds(self) -> float:
        """这一趟从开跑到此刻的墙钟秒数（**只作附注**，不许拿它核对）。"""
        with self._lock:
            return max(0.0, round(float(self._clock()) - self._started, 3))

    def snapshot(self) -> dict[str, Any]:
        """此刻的现场读数（不读盘）；形状见 `docs/contract.md` 4.9，
        `GET /api/sessions/{id}` 的 `offline` 格就是它。
        """
        with self._lock:
            started_at = self.started_at
            finished_at = self.finished_at
            current = self._current_source
            totals = dict(self._totals)
            rows = list(self._rows)
        running = bool(started_at) and not finished_at
        return {
            "ledger": PROGRESS_NAME,
            "state": "done" if finished_at else ("running" if started_at else "idle"),
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_seconds": self.elapsed_seconds(),
            "current_source": current,
            "sources": _per_source_readings(
                self.sources, rows, totals=totals, current=current, running=running
            ),
        }


def snapshot_of(
    session_dir: Path | str, *, sources: Sequence[str], session_id: str
) -> dict[str, Any]:
    """**从盘上那份进度记录**拼出同一套形状的读数（进程里没有现场时用，如服务重启后）。
    跨进程算不出单调钟差值，`elapsed_seconds` 如实 `None`；`started_at` / `finished_at` /
    `current_source` 不在记录里，同理 `None`，跑没跑完由状态文件回答（`state = "ledger"`）。
    """
    header, rows = read_progress(ledger_path(session_dir))
    return {
        "ledger": PROGRESS_NAME,
        "state": "ledger" if header is not None else "idle",
        "started_at": None,
        "finished_at": None,
        "elapsed_seconds": None,
        "current_source": None,
        "sources": _per_source_readings(sources, rows),
    }


__all__ = [
    "CHUNK_STATES",
    "LEDGER_KIND",
    "PROGRESS_NAME",
    "PROGRESS_SCHEMA_VERSION",
    "ChunkProgress",
    "OfflineProgressLedger",
    "counts_by_state",
    "ledger_path",
    "read_progress",
    "snapshot_of",
    "write_progress",
]
