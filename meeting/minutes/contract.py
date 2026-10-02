"""分块 → 逻辑段的契约校验。

分段方案落在 `working/segmentation.json`——**一份**覆盖这场会的一到两条输入
（每条段自带 `source`；两路的行号空间互不相干，所以界面上把它写成
`working/<source>/…` 会让人以为一条一路，那是错的）。

这个模块服务 spec 里这一条：分块方案由 sub agent 交回一份 JSON，主 agent 在拿它往下走之前
必须先证明**它把每一条输入的每一行都盖住了、一行不漏也不重叠**——否则后面按段写摘要时，
没人说得清漏掉的那几十行去哪了。

判定照归档 `260725/scripts/pipeline_utils.py`（A6）重写，形状按本轮定案改：

  - 一份 `segmentation.json` 覆盖**一到两条**输入，每条段带 `source`；
  - 连续性**按 `source` 各判一遍**：该 source 的段按 `line_start` 排好之后要
    `start == prev_end + 1`、从 1 起、末段 `end == 该 source 的总行数`；
  - **跨 source 不判连续性**：两路的行号空间互不相干，`mic` 的第 200 行与 `loopback`
    的第 200 行没有任何关系，拼成一条链去判是错的；
  - 段数**按 source 各自** ≤ `max_segments`；
  - 每段要有 `id` / `title` / `source` / `line_start` / `line_end`，且 `line_end >= line_start`；
  - `chunks` 若在，要是整数列表。

**`total_lines` 这一格有两种合法形状**（两种都要求「每一条 source 都被核到」）：

  - **整数**：只有**一条** source 的场次上，它要是那一条的真实行数（单路场次照样能自证）。
    多 source 场次上写整数**一律拒收**：一个数说不了两条行数不同的转写，而它一旦被当成
    「每条 source 的末段期望」用（本文件此前就是这么读的），多 source 场次就**没有合法取值**
    ——填谁都会把另一条判错，不写则整段末段核对被跳过、连「每行都落进某一段」都不再核；
  - **对象**（`{"loopback": 120, "mic": 63}`）：逐 source 声明，每条 source 按**自己那一格**
    与该 source 的末段 `line_end` 比对，**每一条 source 都要在里面**（少一条就报错，
    不许悄悄退化成「不核」）；
  - 别的形状（字符串、列表、负数、成员不是整数）一律抛 `SegmentationContractError`，
    消息里说清是哪一格不对。

`expected_lines`（调用方从 `working/inputs.json` 读出来的逐 source 真实行数）在场时**它是唯一
权威**：那一路是「读方拿独立事实核方案」，比方案自己声明的数可信；这一档下 `total_lines` 只
按形状校验、**不参与比对**。

字段名照 V1 的 `SEGMENT_SCHEMA`（`:23-43`）**一个都不改名**：提示词的正文与这份校验器若各用
一套字段名，两边就会在「哪条段算什么」上慢慢分家。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

#: 分段方案的模式版本。**主版本号读方认识才收**：结构变了就抬它，读方据此拒收。
SCHEMA_VERSION = 1

#: 本实现认识的主版本号
KNOWN_SCHEMA_MAJOR = 1

#: 一份方案里每个 source 的段数上限（`minutes.max_segments` 配置键的取值域是 1–20）
DEFAULT_MAX_SEGMENTS = 8

#: 方案的文件名（落在 `working/` 那一层，spec 交付物表钉死的）
SEGMENTATION_NAME = "segmentation.json"

#: 输入清单的文件名（`working/inputs.json`，spec 交付物表钉死的）。
#: 这一格是本模块自己那份清单的真源：`check` 子命令靠它在方案旁边找到逐 source 的真实行数。
#: 名字与 `inputs.INPUTS_NAME` 逐字相同，但**不从 `inputs` 模块 import**——`minutes.contract`
#: 只依赖标准库，契约校验不因为「输入清单长什么样」而多一条依赖边。
INPUTS_NAME = "inputs.json"

#: 段里逐段可选的字段（在就一定按类型校验，不在也不报错）
OPTIONAL_SEGMENT_FIELDS: tuple[str, ...] = ("chunks", "key_topics", "is_off_topic", "summary")

#: 段里的必填字段（顺序照 V1 的 `SEGMENT_SCHEMA`，报错时按它列）
REQUIRED_SEGMENT_FIELDS: tuple[str, ...] = ("id", "title", "source", "line_start", "line_end")


class SegmentationContractError(ValueError):
    """分段方案不满足契约。

    **这一档记的是读方的拒绝**，与文件不存在、JSON 坏掉分开（`load_segmentation()` 对三种情形
    给三种能分辨的错误）。消息里一定点得出**哪一条 source 的哪一段**不对：只说「契约不过」
    的报告没人能照着改。
    """


def _where(source: object, segment_id: object) -> str:
    """错误消息的前缀：`source 'mic' 段 3`（说不出段号时退成 `source 'mic' 第 ? 段`）。"""
    return f"source {source!r} 段 {segment_id!r}"


def _require_mapping(value: object, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SegmentationContractError(f"{what} 要是 JSON 对象，收到 {type(value).__name__}")
    return value


def _require_int(value: object, where: str, field: str) -> int:
    """整数那一格：`bool` 不收（`True` 是 `int` 的子类，混进来会算出一个荒唐的行号）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise SegmentationContractError(f"{where} 的 {field} 要是整数，收到 {value!r}")
    return value


def _entry_of(record: Mapping[str, Any]) -> tuple[str, int] | None:
    """`inputs.json` 里一条输入的某一格（`{"file": …, "lines": …}` 那种）。

    取不到就是 `None`：那一格里没有可用的行数，调用方另想办法。
    """
    if not isinstance(record, Mapping):
        return None
    name = record.get("file")
    lines = record.get("lines")
    if not name or isinstance(lines, bool) or not isinstance(lines, int):
        return None
    return str(name), lines


def expected_lines_from_inputs(document: Mapping[str, Any]) -> dict[str, int]:
    """从 `working/inputs.json` 的内容里取「每条 source 的真实行数」（**会中稿那一份**）。

    划段的自检（`validate_segmentation()` 的 `expected_lines` 那条路）只认真实行数，而
    「这一路该盖到第几行」的分母是**会中稿**那一份：会中稿每一路都有（离线那一趟是可选的，
    R9），两份稿的行号空间又各算各的，拿会后那一份的数去核会中稿划出来的段必然对不上。
    这一条与提示词阶段 2 那句「划段按这一路在场那份稿的行号空间来：两份都在按会中稿，
    只有会后稿的场次按会后稿」是同一个口径。

    **说不出行数的那一条 source 整个不出现**（那一格缺 `lines`、或会中稿不在）：契约校验在
    这一档下只核说得出真实行数的那几条，剩下的几条退回方案自己声明的 `total_lines`（对象形状），
    而不是拿一个猜的数去核。一路只有会后稿、没有会中稿时退回会后稿那一格的行数 ——
    那一档下它是这一路唯一在手的稿，划段的分母只能是它。
    """
    sources = document.get("sources")
    if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes)):
        raise SegmentationContractError(
            f"输入清单的 sources 要是列表，收到 {type(sources).__name__}"
        )
    result: dict[str, int] = {}
    for index, record in enumerate(sources, start=1):
        if not isinstance(record, Mapping):
            raise SegmentationContractError(
                f"输入清单 sources 第 {index} 条要是对象，收到 {type(record).__name__}"
            )
        source = record.get("source")
        if not isinstance(source, str) or not source:
            raise SegmentationContractError(f"输入清单 sources 第 {index} 条说不出 source")
        entry = _entry_of(record.get("live")) or _entry_of(record.get("offline"))
        if entry is not None:
            result[source] = entry[1]
    return result


def _validate_segment(source: str, segment: Mapping[str, Any], index: int) -> int:
    """校验一段，返回它的 `line_end`（调用方据此判连续性）。"""
    segment_id = segment.get("id")
    # 段号说不出时的兜底：`id` 缺失本身就是一条契约失败，这里报「第几段」，别报一个 `None`
    where = _where(source, segment_id if segment_id is not None else f"第 {index} 条（id 缺失）")
    for field in REQUIRED_SEGMENT_FIELDS:
        if field not in segment:
            raise SegmentationContractError(f"{where} 缺字段 {field!r}")
    if segment.get("source") != source:
        raise SegmentationContractError(
            f"{where} 的 source 是 {segment.get('source')!r}，与分组用的 {source!r} 不一致"
        )
    title = segment.get("title")
    if not isinstance(title, str) or not title.strip():
        raise SegmentationContractError(f"{where} 的 title 要是非空字符串，收到 {title!r}")
    start = _require_int(segment.get("line_start"), where, "line_start")
    end = _require_int(segment.get("line_end"), where, "line_end")
    if end < start:
        raise SegmentationContractError(f"{where} 的行号区间倒挂：{start}-{end}")
    if start < 1:
        raise SegmentationContractError(f"{where} 的 line_start 从 1 起，收到 {start}")

    chunks = segment.get("chunks")
    if chunks is not None:
        if not isinstance(chunks, Sequence) or isinstance(chunks, (str, bytes)):
            raise SegmentationContractError(f"{where} 的 chunks 要是列表，收到 {chunks!r}")
        for item in chunks:
            if isinstance(item, bool) or not isinstance(item, int):
                raise SegmentationContractError(
                    f"{where} 的 chunks 里每一项要是整数，收到 {item!r}"
                )

    topics = segment.get("key_topics")
    if topics is not None and (
        not isinstance(topics, Sequence) or isinstance(topics, (str, bytes))
    ):
        raise SegmentationContractError(f"{where} 的 key_topics 要是列表，收到 {topics!r}")

    off_topic = segment.get("is_off_topic")
    if off_topic is not None and not isinstance(off_topic, bool):
        raise SegmentationContractError(f"{where} 的 is_off_topic 要是布尔值，收到 {off_topic!r}")

    summary = segment.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise SegmentationContractError(f"{where} 的 summary 要是字符串，收到 {summary!r}")

    return end


def _declared_limits(
    payload: Mapping[str, Any],
    *,
    expected_lines: Mapping[str, int] | None,
    sources: Sequence[str],
) -> dict[str, tuple[int, str]]:
    """决定「每条 source 的末段该盖到第几行」，返回 `{source: (行数, 这个数的来路)}`。

    两条路，`expected_lines` 优先：

      - **`expected_lines` 在场**（调用方从 `working/inputs.json` 读出来的真实行数）：
        它是**唯一权威**，逐 source 返回它；`total_lines` 这一档在这一轮只按形状校验
        （坏形状照样拒收），**不参与比对**——独立事实与方案自述矛盾时，以独立事实为准；
      - **`expected_lines` 缺席**：退回方案自己声明的 `total_lines`，它有两种合法形状：

        * **对象**：逐 source 声明，每条 source 按自己那一格比对；**每一条 source 都要在
          里面**（少一条就报错，不许悄悄退化成「不核」）；
        * **整数**：只有**一条** source 的场次上合法（那一条按它比对，单路场次照样能自证）。
          多 source 场次上报错：一个数说不了两条行数不同的转写，而这里的错法不是「判错」
          而是「静默不核」——那会把「每一行都落进某一段」这条最要紧的判据一起丢掉。

    返回的映射里**没有**哪条 source，就说明那一条这一轮没有依据可核（`total_lines` 整个
    缺席，或那一格缺这一条 source），调用方据此跳过末段比对、而不是拿一个猜的数去核。
    """
    declared = payload.get("total_lines")
    if expected_lines is not None:
        if declared is not None:
            _total_lines_shape(declared, sources)
        return {
            source: (int(expected_lines[source]), "该 source 的真实行数")
            for source in sources
            if source in expected_lines
        }
    if declared is None:
        return {}
    return _total_lines_shape(declared, sources)


def _total_lines_shape(value: object, sources: Sequence[str]) -> dict[str, tuple[int, str]]:
    """按 `total_lines` 的两种合法形状解出 `{source: (行数, 来路)}`，别的形状当场拒收。"""
    if isinstance(value, Mapping):
        limits: dict[str, tuple[int, str]] = {}
        for source, declared in value.items():
            if not isinstance(source, str) or not source:
                raise SegmentationContractError(
                    f"total_lines 的键要是 source 名（非空字符串），收到 {source!r}"
                )
            limits[source] = (
                _require_int(declared, f"total_lines 的 {source!r} 那一格", "行数"),
                "方案自己声明的 total_lines",
            )
        missing = [source for source in sources if source not in limits]
        if missing:
            raise SegmentationContractError(
                f"total_lines 里少了 {missing} 这（几）条 source：逐 source 声明的对象要求"
                f"每一条 source 都在里面（segmentation 里有 {list(sources)}，"
                f"它里面只有 {sorted(limits)}）"
            )
        return {source: limits[source] for source in sources}

    if isinstance(value, bool) or not isinstance(value, int):
        raise SegmentationContractError(
            f"total_lines 要是整数（只用于单 source 场次）或逐 source 的对象"
            f"（形如 {{\"loopback\": 120, \"mic\": 63}}），收到 {value!r}"
        )
    if value < 1:
        raise SegmentationContractError(f"total_lines 要是 ≥1 的整数，收到 {value!r}")
    if len(sources) > 1:
        raise SegmentationContractError(
            f"total_lines 是整数 {value}，但这份 segmentation 覆盖了 {len(sources)} 条 source"
            f"（{list(sources)}）：整数说不了两条行数不同的转写，请把 total_lines 写成"
            f"逐 source 的对象（形如 {{\"loopback\": 120, \"mic\": 63}}），"
            f"或者让调用方传 expected_lines（从 working/inputs.json 读到逐 source 的真实行数）"
        )
    return {sources[0]: (value, "方案自己声明的 total_lines")} if sources else {}


def validate_segmentation(
    payload: Mapping[str, Any],
    *,
    expected_lines: Mapping[str, int] | None = None,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
) -> bool:
    """校验一份分段方案，通过返回 `True`，不通过抛 `SegmentationContractError`。

    两样「这一份方案说得对不对」的外部事实由调用方交进来：

      - `expected_lines`：每条 source 的**真实行数**（`working/inputs.json` 里那两条稿的
        行数，与切块那一步同一条取法，见 `expected_lines_from_inputs()`）。**它在场时是唯一
        权威**：逐 source 拿它与末段 `line_end` 比对，方案自己声明的 `total_lines` 这一档
        只按形状校验、不参与比对（两者故意矛盾时以 `expected_lines` 为准）；
      - `max_segments`：每条 source 的段数上限（`minutes.max_segments`）。

    `expected_lines` 缺席时退回方案自己声明的 `total_lines`（**对象**逐 source 声明，
    或**单 source 场次**上的一个整数）：这一档保证「每条 source 的末段 `end` == 那一格说的
    行数」成立。**多 source 场次上写整数、又没给 `expected_lines`，一律报错**（见下面那一段
    的注释）：这一档下没有任何独立事实可核，而一个整数说不了两条行数不同的转写，
    静默跳过末段核对会把「每行都落进某一段」这条判据一起丢掉。
    """
    if isinstance(max_segments, bool) or not isinstance(max_segments, int) or max_segments < 1:
        raise SegmentationContractError(f"max_segments 要是 ≥1 的整数，收到 {max_segments!r}")

    version = payload.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise SegmentationContractError(f"schema_version 要是整数，收到 {version!r}")
    if version != KNOWN_SCHEMA_MAJOR:
        raise SegmentationContractError(
            f"schema_version {version} 读方不认识（本实现认识 {KNOWN_SCHEMA_MAJOR}）"
        )

    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, Sequence) or isinstance(raw_segments, (str, bytes)):
        raise SegmentationContractError(f"segments 要是列表，收到 {type(raw_segments).__name__}")
    if not raw_segments:
        raise SegmentationContractError("segments 是空的：一份方案至少要有一段")

    # 按 source 分组（组内按出现序，下面再排序），跨 source 不判连续性
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for index, raw in enumerate(raw_segments, start=1):
        segment = _require_mapping(raw, f"segments 第 {index} 条")
        missing = [field for field in REQUIRED_SEGMENT_FIELDS if field not in segment]
        if missing:
            # 必填字段缺一个就到此为止：排序与分组都要读它们，缺了会抛一个与现场无关的类型错
            raise SegmentationContractError(
                f"{_where(segment.get('source'), segment.get('id') or f'第 {index} 条')}"
                f" 缺字段 {missing}"
            )
        source = segment["source"]
        if not isinstance(source, str) or not source:
            raise SegmentationContractError(
                f"segments 第 {index} 条的 source 要是非空字符串，收到 {source!r}"
            )
        grouped.setdefault(source, []).append(segment)

    declared = _declared_limits(
        payload, expected_lines=expected_lines, sources=sorted(grouped)
    )

    for source, segments in sorted(grouped.items()):
        if len(segments) > max_segments:
            raise SegmentationContractError(
                f"source {source!r} 有 {len(segments)} 段，超过上限 {max_segments}"
            )

        ordered = sorted(
            segments,
            key=lambda item: _require_int(
                item["line_start"], _where(source, item["id"]), "line_start"
            ),
        )
        seen_ids: set[Any] = set()
        prev_end = 0
        for position, segment in enumerate(ordered, start=1):
            segment_id = segment.get("id")
            if segment_id in seen_ids:
                raise SegmentationContractError(f"source {source!r} 的段 id {segment_id!r} 重复")
            seen_ids.add(segment_id)

            end = _validate_segment(source, segment, position)
            start = segment["line_start"]
            if start != prev_end + 1:
                raise SegmentationContractError(
                    f"{_where(source, segment_id)} 的 line_start 是 {start}，"
                    f"按上一条盖到第 {prev_end} 行算应当是 {prev_end + 1}（漏行或重叠）"
                )
            prev_end = end

        if expected_lines is not None and source not in expected_lines:
            raise SegmentationContractError(
                f"segmentation 里的 source {source!r} 不在输入清单里"
                f"（清单里有 {sorted(expected_lines)}）"
            )
        if source not in declared:
            # 「末段核到哪一行」这一条这一轮没有依据可核：不许静默跳过别的判据，
            # 静默跳过就等于把「每一行都落进某一段」这条最要紧的判据一起丢了。
            continue
        want, how = declared[source]
        if prev_end != want:
            raise SegmentationContractError(
                f"source {source!r} 的末段盖到第 {prev_end} 行，与{how} {want} 不符"
                f"（{'少了' if prev_end < want else '多了'} {abs(want - prev_end)} 行）"
            )

    if expected_lines is not None:
        missing = [source for source in expected_lines if source not in grouped]
        if missing:
            raise SegmentationContractError(
                f"输入清单里的 {missing} 在 segmentation 里一段都没有"
            )
    return True


def load_segmentation(
    path: str | Path,
    *,
    expected_lines: Mapping[str, int] | None = None,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
) -> dict[str, Any]:
    """读盘入口：读出并通过校验的分段方案（`dict`）。

    三种情形给三种能分辨的错误：

      - 文件不在 → `FileNotFoundError`（调用方据此报「方案还没交回来」）；
      - JSON 坏掉、顶层不是对象 → `SegmentationContractError`，消息里带 `JSON` 两个字；
      - 契约不过 → `SegmentationContractError`，消息里点出哪一条 source 的哪一段。
    """
    target = Path(path)
    text = target.read_text(encoding="utf-8")  # 文件不在就抛 FileNotFoundError，这一档留给调用方
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise SegmentationContractError(f"JSON 解析失败：{target}（{exc}）") from exc
    if not isinstance(payload, Mapping):
        raise SegmentationContractError(
            f"JSON 顶层要是对象：{target}（收到 {type(payload).__name__}）"
        )
    validate_segmentation(payload, expected_lines=expected_lines, max_segments=max_segments)
    return dict(payload)


def expected_lines_from_chunks(chunks: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """从 `chunk_session()` 的返回值里取「每一路每一份稿的真实行数」。

    `chunk_session()` 的返回值形状是 `{source: {kind: SourceChunks}}`（两份稿各一套块），
    交回来的账因此也逐份回答：`{"mic": {"live": 25, "offline": 22}}`。
    「真实行数」只有一种取法（`SourceChunks.total_lines`，也就是 `lines.count_lines()` 的数法）。

    划段那一层用的分母是**会中稿**那一份（提示词的阶段 2 写着这一条）；
    本函数只如实报手上两份各几行，选哪一份由调用方自己说。
    """
    result: dict[str, dict[str, int]] = {}
    for source, reports in chunks.items():
        if not isinstance(reports, Mapping):
            raise SegmentationContractError(f"source {source!r} 的切块结果该是逐份稿的对象")
        per_kind: dict[str, int] = {}
        for kind, report in reports.items():
            total = getattr(report, "total_lines", None)
            if total is None:
                raise SegmentationContractError(
                    f"source {source!r} 的 {kind!r} 切块结果里没有 total_lines"
                )
            per_kind[str(kind)] = int(total)
        result[str(source)] = per_kind
    return result


__all__ = [
    "DEFAULT_MAX_SEGMENTS",
    "INPUTS_NAME",
    "KNOWN_SCHEMA_MAJOR",
    "OPTIONAL_SEGMENT_FIELDS",
    "REQUIRED_SEGMENT_FIELDS",
    "SCHEMA_VERSION",
    "SEGMENTATION_NAME",
    "SegmentationContractError",
    "expected_lines_from_chunks",
    "expected_lines_from_inputs",
    "load_segmentation",
    "validate_segmentation",
]
