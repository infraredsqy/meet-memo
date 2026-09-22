"""每路说话人的编号规范化与汇总。

**规则定了就不改**：按该路内**首次出现顺序**把引擎原始标签规范化成 `S01`、`S02`…，
限定名一律 `${source}:${local}`。理由是分块模式下引擎标签会按块重启，需要一个与块无关的
确定性规则；同一条规则在单块下也成立，于是「同一路内自洽」可验。

**跨路合并是禁区**（R10）：任何情况下都不许把另一路的标签映射到本路编号上。这条不靠约定，
靠形状——限定名里始终带着 `source`，"这条路的一号等于那条路的一号"在数据结构上不可表达。

纯标准库，无重依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from meeting.offline.engine import OfflineSegment


@dataclass(frozen=True, slots=True)
class SpeakerSpan:
    """一个说话人区间：引擎标签经本路编号规范化之后的形态。"""

    source: str       # "mic" | "loopback"
    local: str        # "S01"
    qualified: str    # "loopback:S01"
    start: float
    end: float
    text: str


def number_speakers(source: str, segments: Sequence[OfflineSegment]) -> list[SpeakerSpan]:
    """把一路的分段按**首次出现顺序**编号成 `S01`、`S02`…，返回区间列表。

    同一批输入反复跑给出同一份编号（顺序确定、无随机）。`source` 原样进限定名，
    调用方给 `mic` 就给 `mic:S01`，给 `loopback` 就给 `loopback:S01`。
    """
    source = str(source)
    seen: dict[str, str] = {}
    spans: list[SpeakerSpan] = []
    for segment in segments:
        raw = str(segment.speaker)
        local = seen.get(raw)
        if local is None:
            local = f"S{len(seen) + 1:02d}"
            seen[raw] = local
        spans.append(
            SpeakerSpan(
                source=source,
                local=local,
                qualified=f"{source}:{local}",
                start=float(segment.start),
                end=float(segment.end),
                text=segment.text,
            )
        )
    return spans


def speaker_summary(spans: Sequence[SpeakerSpan]) -> list[dict]:
    """按限定名汇总：每类的**段数**与**总秒数**（秒数按段区间加总，段间静默不计）。

    返回顺序是限定名在 `spans` 里首次出现的顺序（与编号顺序一致，便于人读）。
    """
    order: list[str] = []
    totals: dict[str, dict] = {}
    for span in spans:
        row = totals.get(span.qualified)
        if row is None:
            row = {
                "source": span.source,
                "local": span.local,
                "qualified": span.qualified,
                "segments": 0,
                "seconds": 0.0,
            }
            totals[span.qualified] = row
            order.append(span.qualified)
        row["segments"] += 1
        row["seconds"] = round(row["seconds"] + max(0.0, span.end - span.start), 3)
    return [totals[name] for name in order]
