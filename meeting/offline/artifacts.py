"""会后产物的写入与字段契约。

一场会落下四份文件（都住场次目录，每路各一份不合并）：转写 `meeting_offline_transcript_<source>.md`
（行格式 `[HH:MM] **S01**: 正文`，分钟级）、对照表 `meeting_offline_speakers_<source>.json`
（秒级浮点，机器读）、运行报告 `meeting_offline_report.md`、状态文件 `meeting_offline_status.json`。

字段契约（键表、拒收判据、五档状态、`headcount` 口径、四档留痕）的**正本在 `docs/state.md`
的会后产物一节**；逐块结论词词表（`REASON_*`）住本模块；门尺子词表见 `meeting/offline/runner/pcm.py` 的 `GATE_METHOD_*`。
本模块是写入与读取的唯一实现。

纯标准库；原子写共用 `meeting/atomicio.py`。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

# 原子写实现只住在 meeting.atomicio 一处，这里留同名转发别名，调用方照旧可用。
from meeting.atomicio import write_text_atomic

#: 三份按路落盘的产物的文件名模板
TRANSCRIPT_NAME_TEMPLATE = "meeting_offline_transcript_{source}.md"
SPEAKERS_NAME_TEMPLATE = "meeting_offline_speakers_{source}.json"
REPORT_NAME = "meeting_offline_report.md"
STATUS_NAME = "meeting_offline_status.json"

#: 对照表的模式版本。**主版本号读方认识才收**：结构变了就抬它，读方据此拒收。
SCHEMA_VERSION = 1

#: 本实现认识的主版本号（读方按它拒收）
KNOWN_SCHEMA_MAJOR = 1

#: 状态文件自己的模式版本
STATUS_SCHEMA_VERSION = 1

#: V1 会后回放的实测实时率（`v1-lineage` B16）：报告里拿它当对照基线
V1_RTF_BASELINE = 0.39

#: `[HH:MM]` 的形状：5 个字符的分钟级时间戳（`HH` 在超过 99 分钟时照样是数字，长度会变长）
_LINE_RE = re.compile(r"^\[(?P<stamp>\d{2}:\d{2})\] \*\*(?P<speaker>[^*]+)\*\*: (?P<text>.*)$")

#: 转写第一行那条注释（缺了一段的说明）的判据：整行以它开头就算注释，`read_transcript_lines()`
#: 跳过它。用 HTML 注释的形状是因为转写正文里出现 `<!--` 的概率可以忽略，而
#: 「一个空格 + 正文」那类形状会与真正的一行正文分不开。
TRANSCRIPT_NOTE_PREFIX = "<!--"
TRANSCRIPT_NOTE_SUFFIX = "-->"

#: 合法状态。`partial` 是**一路整条没交回、另一路跑成了**那一档（见 `write_status()`）：
#: 从前这一档与「两路都成」在顶层这一格上都写 `done`，读方只看 `state` 分不开。
#: `interrupted` 是**跑到块边界被人停**那一档（「打断会后」节点）：它也是终态
#: （这一趟不会再自己往下跑了），与 `running`（还有东西在跑）分开写 —— 「杀了进程就
#: 永远停在在跑」那种状态里，「被打断」这一格是治它的那一半。
STATES = ("running", "done", "partial", "failed", "interrupted")

#: 「盘上停着一版已落定的记录」的三档（`mark_failed_if_unset()` 的沿用判据；与
#: `meeting/backends/post.py` 的 `STATE_FAILED` / `STATE_PARTIAL` / `STATE_INTERRUPTED`
#: 同值）：`interrupted`（人停的）也算 —— 后来逃出来的异常不许把它盖成 `failed`。
_SETTLED_STATES = ("failed", "partial", "interrupted")

#: 失败原因词表（报告与状态文件都用这几个名字，下游按它分档）
REASON_NO_AUDIO = "no_audio"
REASON_EMPTY_PCM = "empty_pcm"
REASON_ENGINE_ERROR = "engine_error"
#: 引擎**加载**失手：一次引擎调用都没发生，与「跑某一块时抛错」的 `engine_error`
#: 分开记——「模型没起来」与「起来了、某块没跑成」是两件要分别动手的事。
REASON_ENGINE_LOAD_ERROR = "engine_load_error"
#: 这一路压根没跑（`_write_failed_status()` 给没轮到的路现开的格）：`reasons` 空、
#: `error` 为 null。兜成 `engine_error` 会让「按原因筛引擎的锅」把没跑的算进去。
REASON_NOT_RUN = "not_run"
REASON_NO_SEGMENTS = "no_segments"
#: 分块下每一块都失败：一段都没留下来（与「一块坏、别的块还在」的 `partial` 分开）
REASON_ALL_CHUNKS_FAILED = "all_chunks_failed"
#: 跑到块边界被取消旗标停：已跑块保留，未跑块进 `missing_spans`
#: （缺口只供人读，不进任何判定——design-intent 第 6 条）。
REASON_INTERRUPTED = "interrupted"
#: 数字静音门跳过的块。不是失败：只进 `reasons` 分布，不进缺失区间。
REASON_DIGITAL_SILENCE = "digital_silence"
#: 引擎跑成但交回的段全是零时长：有字却落在没有长度的区间上（实测假正文每段都是
#: 零时长），零段与真内容都读不出来时这一格是唯一分辨依据。
REASON_ZERO_SPAN_SEGMENTS = "zero_span_segments"
#: 引擎跑成、交回的段都有真实时长（正常档）
REASON_TRANSCRIBED = "transcribed"
#: 门判不了这段的静音与否（解不开、读不全），照常送引擎；必须与「门判过、放行」
#: 分得开，否则没门可用的运行与健康运行同形。
REASON_GATE_UNJUDGED = "gate_unjudged"

#: 一块产物都没落的那一路按次序挑一个 `reason` 词：模型没起来 > 没音频 > 抛错 > 没交回。
#: `not_run` / `interrupted` 不在表里——按词自身认（见 `_failed_source_row()`）。
_REASON_PRIORITY = (
    REASON_ENGINE_LOAD_ERROR,
    REASON_NO_AUDIO,
    REASON_EMPTY_PCM,
    REASON_ENGINE_ERROR,
    REASON_NO_SEGMENTS,
    REASON_ALL_CHUNKS_FAILED,
)


class ArtifactContractError(ValueError):
    """读到的产物不满足字段契约（`schema_version` / `source` 缺失或版本不认识）：
    拿不准的文件一律拒收，读方不许猜。继承 `ValueError`，与项目里别的「值不对」同档。
    """


def minute_stamp(seconds: float) -> str:
    """秒 → `HH:MM`（分钟级，向下取整；负数收成 `00:00`，口径同 `ui-design.md` 门槛 10）。"""
    total = int(max(0.0, float(seconds)) // 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def format_transcript_line(start: float, speaker: str, text: str) -> str:
    """一行正文：`[HH:MM] **S01**: 正文`（说话人为空时不加粗，沿用项目既有行格式）。"""
    stamp = minute_stamp(start)
    name = str(speaker or "").strip()
    body = str(text)
    if name:
        return f"[{stamp}] **{name}**: {body}"
    return f"[{stamp}] {body}"


def incomplete_note(spans: Sequence[Sequence[float]]) -> str:
    """转写第一行那条注释：写明哪几段没有转写（秒级）。是注释行不是正文，
    `read_transcript_lines()` 认它并跳过、不占段号。
    """
    listed = "、".join(f"{float(item[0]):.1f}–{float(item[1]):.1f}s" for item in spans)
    return (
        f"{TRANSCRIPT_NOTE_PREFIX} 这一路缺 {len(spans)} 段音频的转写：{listed}"
        f"（分块跑时那几块没成，详见 {REPORT_NAME} 与 {STATUS_NAME}）"
        f" {TRANSCRIPT_NOTE_SUFFIX}"
    )


def silent_note(spans: Sequence[Sequence[float]]) -> str:
    """转写第一行那条注释：写明哪几段**没声**、因此没送进引擎。与 `incomplete_note()`
    是两条注释各说一件事（那条 = 块没跑成，这条 = 本来就没有音频），读方两条都不会当正文。
    """
    listed = "、".join(f"{float(item[0]):.1f}–{float(item[1]):.1f}s" for item in spans)
    return (
        f"{TRANSCRIPT_NOTE_PREFIX} 这一路有 {len(spans)} 段音频没声（样本全为零），"
        f"没有送进引擎：{listed}（不是失败，那一段本来就没有内容；详见 {REPORT_NAME} 与"
        f" {STATUS_NAME}） {TRANSCRIPT_NOTE_SUFFIX}"
    )


def is_transcript_note(line: str) -> bool:
    """这一行是不是转写的注释行（第一行那条缺段说明、或那条没声说明）。"""
    return line.strip().startswith(TRANSCRIPT_NOTE_PREFIX)


def _round(value: Any) -> float:
    """秒数落盘前统一收成 3 位小数（读数稳定、核对可复算）。"""
    return round(float(value), 3)


def default_source_gate() -> dict[str, Any]:
    """逐路 `gate` 格的**缺省形状**（唯一出处，offline-gate-default-duplicated）：一路还
    没跑出门结论时（更早的一步已经落了失败）就落这一个值。`SourceMetrics.gate` 的
    缺省值与 `runner._chunk_gate()` 的空 runs 分支都从这里取——两份手写各漂一份
    （缺 `method` 键）的事从此长不出来。键表见 `docs/state.md` 的会后产物一节
    （逐路 `gate` 格：`applied` / `method` / 各计数）。
    """
    return {
        "applied": False,
        "method": None,
        "spans_total": 0,
        "spans_judged": 0,
        "spans_unjudged": 0,
        "unjudged_spans": [],
        "unjudged_reason": None,
        "detail": "这一路没有可判的音频（更早的一步已经落了失败）",
    }


def chunk_row(run: Mapping[str, Any]) -> dict[str, Any]:
    """对照表逐块行的 **11 键契约投影**（唯一出处，offline-chunk-row-projection-redundant）：
    从逐块登记（`runner.ChunkRun.to_dict()` 的形状）挑对照表要带的键。
    `build_speakers_document()` 与 runner 落对照表那一次投影都走这一条——
    新增逐块字段只改这一处。
    """
    return {
        "index": int(run["index"]),
        "offset_seconds": _round(run["offset_seconds"]),
        "seconds": _round(run["seconds"]),
        "segments": int(run["segments"]),
        "error": None if run.get("error") is None else str(run["error"]),
        "silent": bool(run.get("silent")),
        "reason": str(run.get("reason") or ""),
        "gate": str(run.get("gate") or ""),
        "gate_method": (
            None if run.get("gate_method") is None else str(run["gate_method"])
        ),
        "gate_detail": (
            None if run.get("gate_detail") is None else str(run["gate_detail"])
        ),
        "zero_span_segments": int(run.get("zero_span_segments") or 0),
    }


def build_speakers_document(
    *,
    session_id: str,
    source: str,
    engine: dict[str, Any],
    audio: dict[str, Any],
    segments: Sequence[dict[str, Any]],
    speakers: Sequence[dict[str, Any]],
    chunks: Sequence[dict[str, Any]] | None = None,
    state: str = "done",
    error: str | None = None,
) -> dict[str, Any]:
    """组一份对照表（**纯函数**，不碰盘）：写出去与读回来都以它为准。

    `state` / `error` 记这一路终态：每一块都没跑成的那一路留下空表，`state = "failed"`
    是读方分辨「一段都没转出来」与「真的没人说话」的唯一依据。`headcount` 每趟现算：
    分块跑时 `speakers` 长度是各块类数之和、读它当人数会错得离谱；`chunk_count` 记
    **实际块数**（`chunks` 条数，单趟记 1），不是「总时长 ÷ 块长」那个商。
    `source` 与读方 `parse_speakers_document()` 用同一条判据当场拦（空 source 会让
    限定名退化成 `:S01`），别让不合契约的形状先落盘。
    """
    if not isinstance(source, str) or not source.strip():
        raise ArtifactContractError(
            f"source 该是非空字符串，收到 {source!r}："
            "说不出这一份出自哪一路的对照表不许写出去（限定名会退化成 `:S01`）"
        )
    source = str(source)
    rows: list[dict[str, Any]] = []
    for item in segments:
        local = str(item["local"])
        rows.append(
            {
                "start": _round(item["start"]),
                "end": _round(item["end"]),
                # 限定名从 source 与 local 现算，绝不接受调用方递进来的裸 id
                "speaker": f"{source}:{local}",
                "local": local,
                "text": str(item["text"]),
            }
        )
    summary: list[dict[str, Any]] = []
    for item in speakers:
        local = str(item["local"])
        summary.append(
            {
                "id": f"{source}:{local}",
                "local": local,
                "label_raw": str(item["label_raw"]),
                "segments": int(item["segments"]),
                "seconds": _round(item["seconds"]),
            }
        )
    chunk_rows: list[dict[str, Any]] = [
        chunk_row(item) for item in chunks or []
    ]
    chunk_count = len(chunk_rows) or 1
    chunked = chunk_count > 1
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": str(session_id),
        "source": source,
        "state": str(state),
        "error": None if error is None else str(error),
        "engine": dict(engine),
        "audio": dict(audio),
        "headcount": {
            "meaningful": not chunked,
            "class_count": len(summary),
            "chunk_count": chunk_count,
            "basis": "sum_of_chunk_classes" if chunked else "single_pass_classes",
        },
        "speakers": summary,
        "segments": rows,
        "chunks": chunk_rows,
    }


def parse_speakers_document(document: Any) -> dict[str, Any]:
    """读一份对照表（dict 或 JSON 文本）：契约不满足即抛 `ArtifactContractError`。
    拒收面：顶层不是对象 / `schema_version` 缺失或主版本不认识 / `source` 缺失或空白 /
    `speakers` `segments` 缺失或段缺必填键。通过后原样交回整份 dict（不裁剪、不补默认值）。
    """
    if isinstance(document, (str, bytes, bytearray)):
        try:
            document = json.loads(document)
        except (ValueError, TypeError) as exc:
            raise ArtifactContractError(f"对照表不是合法 JSON：{exc}") from None
    if not isinstance(document, dict):
        raise ArtifactContractError(
            f"对照表顶层该是对象，收到 {type(document).__name__}"
        )

    version = document.get("schema_version")
    if version is None:
        raise ArtifactContractError("对照表缺 schema_version（读方拒绝，不许猜）")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ArtifactContractError(f"schema_version 该是整数，收到 {version!r}")
    if version != KNOWN_SCHEMA_MAJOR:
        raise ArtifactContractError(
            f"对照表的主版本号不认识：{version}（本实现只认 {KNOWN_SCHEMA_MAJOR}）"
        )

    source = document.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ArtifactContractError(
            "对照表缺 source：说不出这一份出自哪一路，读方拒绝（不许把两路当一路合并）"
        )

    for key in ("speakers", "segments"):
        if not isinstance(document.get(key), list):
            raise ArtifactContractError(f"对照表缺 {key}（该是列表）")

    for index, item in enumerate(document["segments"]):
        if not isinstance(item, dict):
            raise ArtifactContractError(f"segments[{index}] 该是对象，收到 {type(item).__name__}")
        missing = [key for key in ("start", "end", "speaker", "text") if key not in item]
        if missing:
            raise ArtifactContractError(
                f"segments[{index}] 缺必填键：{' / '.join(missing)}"
            )
    return document


# write_text_atomic() 住在 meeting/atomicio.py（与 minutes/inputs.py 共用），
# 上面留了同名转发别名，artifacts.write_text_atomic 照旧可用。


def write_transcript(path: Path | str, lines: Iterable[str]) -> Path:
    """落一份转写：**恰好行数行**，每行末尾一个换行（空转写就是 0 字节文件）。
    不做「末尾多一个空行」的花样：空行会被读方当成一段空正文。
    """
    body = "".join(f"{line}\n" for line in lines)
    return write_text_atomic(path, body)


def write_speakers(path: Path | str, document: dict[str, Any]) -> Path:
    """落一份对照表（先过一遍读方契约：写出去的必须读得回来）。"""
    parse_speakers_document(document)
    return write_text_atomic(
        path, json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    )


def read_speakers_artifact(path: Path | str) -> dict[str, Any]:
    """读回一份对照表（契约不满足即抛 `ArtifactContractError`）。"""
    raw = Path(path).read_text(encoding="utf-8")
    return parse_speakers_document(raw)


def read_transcript_lines(path: Path | str) -> list[dict[str, str]]:
    """读回转写成 `[{stamp, speaker, text}]`，行格式不合契约即抛 `ArtifactContractError`。

    空行与**注释行**（第一行那条缺段说明，见 `incomplete_note()`）跳过：它们不是正文，
    也不该让整份转写读不出来。
    """
    rows: list[dict[str, str]] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or is_transcript_note(line):
            continue
        matched = _LINE_RE.match(line)
        if matched is None:
            raise ArtifactContractError(f"转写第 {number} 行不合行格式：{line!r}")
        rows.append(
            {
                "stamp": matched.group("stamp"),
                "speaker": matched.group("speaker"),
                "text": matched.group("text"),
            }
        )
    return rows


@dataclass
class SourceMetrics:
    """一路跑完的度量（报告那一行与摘要那一格都从这里取）。

    三批区间分开记、互不相交：`missing_spans`（块没跑成，`partial` 立）、
    `silent_spans`（没声跳过，state 仍 done / partial 仍 false）、
    `unjudged_spans`（门判不了、照常送了引擎，同时进 `gate.applied = false`）。
    `zero_span_segments` 由逐块登记数出来，与对照表 `audio.zero_span_segments` 同源。
    """

    source: str
    audio_file: str | None = None
    audio_seconds: float = 0.0
    state: str = "running"
    segments: int = 0
    speakers: int = 0
    seconds: float = 0.0
    wall_seconds: float = 0.0
    rtf: float | None = None
    error: str | None = None
    reasons: dict[str, int] = field(default_factory=dict)
    #: 逐块的登记（单趟跑时是空列表）
    chunks: list[dict[str, Any]] = field(default_factory=list)
    #: 没跑成的那几块覆盖的区间（秒，原录音时间轴）
    missing_spans: list[tuple[float, float]] = field(default_factory=list)
    #: 这一路是不是缺了区间
    partial: bool = False
    #: 没声而跳过引擎的区间（秒，原录音时间轴；每条 = 一块）
    silent_spans: list[tuple[float, float]] = field(default_factory=list)
    #: 门判不了静音与否、照常送了引擎的区间（秒，原录音时间轴）
    unjudged_spans: list[tuple[float, float]] = field(default_factory=list)
    #: 这一路的门做成没做成判断（`applied` / `method` / `spans_judged` / `unjudged_spans`…，
    #: 缺省形状唯一出处 `default_source_gate()`；跑完由 `runner._chunk_gate()` 落结论）
    gate: dict[str, Any] = field(default_factory=default_source_gate)
    #: 这一路产物里 `end` 不晚于 `start` 的段数（零时长段，见模块头；逐块数出来再加总）
    zero_span_segments: int = 0

    @property
    def silent(self) -> bool:
        return bool(self.silent_spans)

    @property
    def gate_applied(self) -> bool:
        return bool(self.gate.get("applied"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "audio_file": self.audio_file,
            "audio_seconds": _round(self.audio_seconds),
            "state": self.state,
            "segments": self.segments,
            "speakers": self.speakers,
            "seconds": _round(self.seconds),
            "wall_seconds": _round(self.wall_seconds),
            "rtf": None if self.rtf is None else round(float(self.rtf), 3),
            "error": self.error,
            "reasons": dict(self.reasons),
            "partial": bool(self.partial),
            "missing_spans": [[_round(a), _round(b)] for a, b in self.missing_spans],
            "silent": self.silent,
            "silent_spans": [[_round(a), _round(b)] for a, b in self.silent_spans],
            "gate": dict(self.gate),
            "unjudged_spans": [[_round(a), _round(b)] for a, b in self.unjudged_spans],
            "zero_span_segments": int(self.zero_span_segments),
            "chunks": [dict(item) for item in self.chunks],
        }


def speaker_rows(
    spans: Sequence[Any], *, raw_labels: Sequence[Any] | None = None
) -> list[dict[str, Any]]:
    """类编号 → 段数与秒数，并把引擎原始标签（`label_raw`，编号前从分段上取）一并留下。
    原始标签按**编号**对齐（某个 `Sxx` 首次出现那段的标签），不按位置——汇总行序走
    限定名首次出现顺序，与分段的时间顺序是两种序。
    """
    from meeting.offline.speakers import speaker_summary

    first_raw: dict[str, str] = {}
    for span, raw in zip(spans, raw_labels or []):
        first_raw.setdefault(str(span.local), str(raw))
    rows = speaker_summary(spans)
    for row in rows:
        row["label_raw"] = first_raw.get(str(row["local"]), "")
    return rows


def _engine_calls(metric: dict[str, Any]) -> int:
    """这一路真的调了几次引擎（单趟记 1）：没声跳过的块不算（一次调用都没发生），
    所以跳过发生时它与块数不是同一个数——报告报的是这个，块数另报在逐路表。
    """
    chunks = metric["chunks"]
    if not chunks:
        return 1
    return sum(1 for item in chunks if not item.get("silent"))


def _gate_headline(rows: Sequence[dict[str, Any]]) -> str:
    """报告开头那一行「数字静音门做成没做成判断」，按这一趟实际发生的事写：
    一段都没跳过时如实写出来，真跳过时写出跳过几段（与 runner._chunk_gate 同一套说法）。
    """
    total = sum(int(item["gate"].get("spans_total", 0)) for item in rows)
    judged = sum(int(item["gate"].get("spans_judged", 0)) for item in rows)
    skipped = sum(len(item["silent_spans"]) for item in rows)
    methods = sorted(
        {
            str(item["gate"]["method"])
            for item in rows
            if item["gate"].get("method")
        }
    )
    how = f"，用的尺子是 {'/'.join(methods)}" if methods else ""
    if total and judged == total:
        if skipped:
            return (
                f"在 {total} 段音频上都给出了结论{how}"
                f"（其中 {skipped} 段是数字静音，跳过了引擎）"
            )
        return (
            f"在 {total} 段音频上都给出了结论{how}"
            "（没有一段是数字静音，一段都没跳过引擎）"
        )
    blocked = [
        f"{item['source']} 第 "
        + "、".join(str(index) for index in _unjudged_indexes(item))
        + " 块"
        for item in rows
        if item["unjudged_spans"]
    ]
    detail = "；".join(blocked) if blocked else "没有可判的音频"
    return f"⚠ **没做成判断**：{judged} / {total} 段判过，其余几段照常送了引擎（{detail}）"


def _unjudged_indexes(item: dict[str, Any]) -> list[int]:
    """这一路门判不了的那几块**是第几块**（逐块登记里的 `gate == "unjudged"`）。"""
    return [
        int(run["index"])
        for run in item["chunks"]
        if run.get("gate") == "unjudged"
    ]


def _chunk_reason(run: dict[str, Any]) -> str:
    """逐块那一行末尾的结论：机器可读的词 + 一句人话（读报告的是人、读表的是程序）。"""
    reason = str(run.get("reason") or "")
    return f"{reason}（{_CHUNK_REASON_TEXT.get(reason, '见逐块登记')}）"


#: 逐块结论词 → 一句人话（报告里那一行用）
_CHUNK_REASON_TEXT = {
    "digital_silence": "样本全零，没声、跳过引擎",
    "engine_error": "引擎抛错，这一段没跑成",
    "no_segments": "引擎跑成了但一段都没交回",
    "zero_span_segments": "跑成了，交回的段每一段都是零时长",
    "transcribed": "跑成了",
    "gate_unjudged": "跑成了，但这一段的静音与否没人判过",
}


def write_report(
    path: Path | str,
    *,
    session_id: str,
    session_dir: Path | str,
    engine: dict[str, Any],
    metrics: Sequence[SourceMetrics],
    total_wall_seconds: float,
) -> Path:
    """落运行报告：B16 的三项（段耗时分布 / 失败原因分布 / 实时率）+ 本场基本事实。

    实时率两格两种口径：逐路是 `SourceMetrics.rtf`（墙钟 ÷ 音频，与 V1 的 0.39× 基线
    同向）；整场是「音频合计 ÷ 总耗时」，算式逐字写在括号里。两格互为倒数，方向写在
    括号里免得被当成矛盾；总耗时为 0 落「算不出」。段耗时分布落在「每次引擎调用」粒度
    （MOSSTD 主场是整段批处理，逐段耗时在这一层不存在）。没声跳过、零时长段、缺段
    各单列一节：报告是人读这一场跑成什么样的地方，混在一处会读成「这一场坏过」。
    """
    rows = [item.to_dict() for item in metrics]
    total_audio = sum(float(item["audio_seconds"]) for item in rows)
    total_segments = sum(int(item["segments"]) for item in rows)
    total_calls = sum(_engine_calls(item) for item in rows)
    failed = [item for item in rows if item["state"] == "failed"]
    incomplete = [item for item in rows if item["missing_spans"]]
    muted = [item for item in rows if item["silent_spans"]]
    unjudged = [item for item in rows if item["unjudged_spans"]]
    zero_counts = {str(item["source"]): int(item["zero_span_segments"]) for item in rows}
    reasons: dict[str, int] = {}
    for item in rows:
        for name, count in item["reasons"].items():
            reasons[name] = reasons.get(name, 0) + int(count)

    lines: list[str] = []
    lines.append("# 会后离线转写报告")
    lines.append("")
    lines.append(f"- 场次：`{session_id}`（`{session_dir}`）")
    lines.append(
        f"- 模型：`{engine.get('model')}` · 档位：`{engine.get('dtype')}`"
        f" · 设备：`{engine.get('device')}` · 引擎选择：`{engine.get('selector')}`"
    )
    lines.append(
        f"- 音频：{total_audio:.1f}s（{len(rows)} 路）· 分段：{total_segments}"
        f" · 引擎调用：{total_calls} 次 · 总耗时：{float(total_wall_seconds):.3f}s"
    )
    lines.append(f"- 数字静音门：{_gate_headline(rows)}")
    if incomplete:
        listed = "、".join(
            f"{item['source']} 缺 {len(item['missing_spans'])} 段" for item in incomplete
        )
        lines.append(f"- ⚠ **这一场的转写不完整**：{listed}（逐段区间见下面那一节）")
    if muted:
        listed = "、".join(
            f"{item['source']} {len(item['silent_spans'])} 段" for item in muted
        )
        lines.append(
            f"- 这一场有 **没声而跳过** 的音频：{listed}"
            "（样本全为零，没有送进引擎；这一档什么都没出错，逐段区间见下面那一节）"
        )
    if unjudged:
        listed = "、".join(
            f"{item['source']} {len(item['unjudged_spans'])} 段" for item in unjudged
        )
        lines.append(
            f"- ⚠ **这一场的门在 {listed}上没做成判断**（音频解不开或读不全，那几段照常送了"
            "引擎；逐段区间见下面那一节）"
        )
    lines.append("")
    lines.append("## 逐路")
    lines.append("")
    lines.append("| 路 | 音频 | 秒数 | 块数 | 段数 | 类数 | 类内秒数 | 墙钟 | 实时率 | 状态 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for item in rows:
        rtf = "-" if item["rtf"] is None else f"{item['rtf']:.3f}×"
        lines.append(
            f"| {item['source']} | {item['audio_file'] or '（缺）'} | {item['audio_seconds']:.1f}"
            f" | {len(item['chunks']) or 1} | {item['segments']} | {item['speakers']}"
            f" | {item['seconds']:.1f}"
            f" | {item['wall_seconds']:.3f}s | {rtf} | {item['state']} |"
        )
    lines.append("")
    lines.append("## 类数与人数")
    lines.append("")
    lines.append(
        "`speakers` 数组的长度只在**整段一次跑**那一档等于「模型在这段音频里分出的类数」；"
        "分块跑的时候它等于**各块类数之和**，同一个人在两块里各拿一个编号。"
        "读这个数当人数之前先看这一节，或直接读对照表里的 `headcount`。"
    )
    lines.append("")
    for item in rows:
        calls = len(item["chunks"]) or 1
        if calls > 1:            lines.append(
                f"- {item['source']}：切成 {calls} 块跑，`speakers` 的长度（{item['speakers']}）"
                f"等于**各块类数之和**，它读不出这一场有几个人"
                f"（对照表 `headcount.meaningful = false`，`basis = sum_of_chunk_classes`）。"
            )
        else:
            lines.append(
                f"- {item['source']}：整段一次跑（1 次引擎调用），`speakers` 的长度"
                f"（{item['speakers']}）等于模型在这段音频里分出的类数"
                f"（对照表 `headcount.meaningful = true`）；它能不能当人数用，"
                "仍取决于这段音频里分得清几个人。"
            )
    lines.append("")
    lines.append("## 段耗时分布")
    lines.append("")
    lines.append(
        "会后离线路径对每一路**按块调**引擎（MOSSTD 的主场是整段批处理）：块数 1 就是"
        "整段一次调用，块数多于一的时候逐块调用。因此这里给的是每次调用的墙钟，"
        "逐段耗时分布在这一层不存在。"
    )
    lines.append("")
    for item in rows:
        if item["chunks"]:
            lines.append(
                f"- {item['source']}：{_engine_calls(item)} 次调用"
                f"（音频 {item['audio_seconds']:.1f}s）"
                f"，合计 {item['wall_seconds']:.3f}s"
            )
            for run in item["chunks"]:
                lines.append(
                    f"  - 第 {run['index']} 块（{run['offset_seconds']:.1f}s 起，"
                    f"{run['seconds']:.1f}s）：{run['segments']} 段，"
                    f"墙钟 {run['wall_seconds']:.3f}s，{_chunk_reason(run)}"
                )
        else:
            lines.append(
                f"- {item['source']}：一次调用 {item['wall_seconds']:.3f}s"
                f"（音频 {item['audio_seconds']:.1f}s）"
            )
    lines.append("")
    lines.append("## 数字静音门（这一段音频的静音与否，判过没有）")
    lines.append("")
    lines.append(
        "每一段音频在调引擎**之前**都过这道闸（单趟跑与分块跑一样，判据见 `docs/state.md`）："
        "样本全零的那一段根本不送进引擎。这一节记的是**门在每一路上做成没做成判断**——"
        "逐块那一行末尾的词与这里的数同源。"
    )
    lines.append("")
    for item in rows:
        gate = item["gate"]
        verdict = "做成判断" if gate.get("applied") else "⚠ 没做成判断"
        lines.append(
            f"- {item['source']}：{verdict}"
            f"（判过 {int(gate.get('spans_judged', 0))} / "
            f"{int(gate.get('spans_total', 0))} 段；"
            f"尺子 {gate.get('method') or '—'}；{gate.get('detail') or '—'}）"
        )
        if item["unjudged_spans"]:
            spans = "、".join(
                f"{float(span[0]):.1f}–{float(span[1]):.1f}s" for span in item["unjudged_spans"]
            )
            lines.append(
                f"  - 判不了的那 {len(item['unjudged_spans'])} 段（{spans}）："
                f"{gate.get('unjudged_reason') or '—'}；那几段照常送了引擎，"
                "转写里有没有字由引擎说了算"
            )
    lines.append("")
    if muted:
        lines.append("## 没声而跳过的音频（这一档什么都没出错）")
        lines.append("")
        lines.append(
            "下面这几段音频的样本**全为零**（实测那一档是 `volumedetect` 读 `-91.0 dB` 的"
            "全零样本），因此一**次引擎调用都没发生**：引擎在这种输入上有编出整段假正文的"
            "实测记录（见节点 的 `设计/fullrun-250906.md`）。跳过之后转写里"
            "没有这几段，那是如实的结果；对照表的 `audio.silent_spans`、转写第一行的注释、"
            "状态文件的 `silent` 三处记的是同一件事。**这一档与 `partial` 分得开**："
            "`partial` 说的是「有块没跑成」，`silent` 说的是「那块本来就没有音频」。"
        )
        lines.append("")
        for item in muted:
            spans = "、".join(
                f"{float(span[0]):.1f}–{float(span[1]):.1f}s" for span in item["silent_spans"]
            )
            lines.append(
                f"- {item['source']}：跳过 {len(item['silent_spans'])} 段（{spans}）"
            )
        lines.append("")
    lines.append("## 零时长的段")
    lines.append("")
    lines.append(
        "`end` 不晚于 `start` 的段逐路记在下面（对照表 `audio.zero_span_segments` 是同一个数）。"
        "解析层对「模型漏写了 `[结束]`」的那一段故意留着正文、把 `end` 退回 `start`，因此这种"
        "段本身是正常形状；实测那台引擎在数字静音上编出来的假正文**每一段都是零时长**，"
        "这一格于是同时是个可核对的异常信号。**只记不改**：正文一个字都没动。"
    )
    lines.append("")
    for item in rows:
        lines.append(
            f"- {item['source']}：{zero_counts.get(str(item['source']), 0)} 段"
            f"（共 {item['segments']} 段）"
        )
    lines.append("")
    if incomplete:
        lines.append("## 缺了哪几段（这一路的转写不完整）")
        lines.append("")
        lines.append(
            "分块跑的时候有块没成，下面这几段音频**没有转写**；转写文件与对照表里也留着"
            "同一件事（转写第一行的注释、对照表的 `audio.missing_spans`）。"
        )
        lines.append("")
        for item in incomplete:
            spans = "、".join(
                f"{float(span[0]):.1f}–{float(span[1]):.1f}s" for span in item["missing_spans"]
            )
            lines.append(f"- {item['source']}：缺 {len(item['missing_spans'])} 段（{spans}）")
        lines.append("")
    lines.append("## 失败原因分布")
    lines.append("")
    if reasons:
        for name, count in sorted(reasons.items()):
            lines.append(f"- {name}：{count}")
    else:
        lines.append("- （无失败）")
    if failed:
        lines.append("")
        for item in failed:
            lines.append(f"- {item['source']}：{item['error']}")
    lines.append("")
    lines.append("## 实时率")
    lines.append("")
    for item in rows:
        if item["rtf"] is None:
            lines.append(f"- {item['source']}：算不出（音频时长为 0）")
        else:
            lines.append(
                f"- {item['source']}：{item['rtf']:.3f}×"
                f"（音频 {item['audio_seconds']:.1f}s / 墙钟 {item['wall_seconds']:.2f}s）"
            )
    if total_audio > 0:
        wall = float(total_wall_seconds)
        if wall > 0:
            lines.append(
                f"- 整场：{total_audio / wall:.3f}×"
                f"（音频合计 {total_audio:.1f}s / 总耗时 {wall:.3f}s；"
                "逐路那一格是「墙钟 ÷ 音频」，与它互为倒数）"
            )
        else:
            lines.append(
                f"- 整场：算不出（总耗时 {wall:.3f}s，音频合计 {total_audio:.1f}s）"
            )
    lines.append(
        f"- 对照基线：V1 会后回放实测 {V1_RTF_BASELINE:.2f}×（归档 PCM 可以离线全速重放）"
    )
    lines.append("")
    return write_text_atomic(path, "\n".join(lines))


def write_status(
    path: Path | str,
    *,
    session_id: str,
    state: str,
    started_at: str,
    sources: Sequence[str],
    artifacts: dict[str, dict[str, str]],
    finished_at: str | None = None,
    error: str | None = None,
    partial: bool = False,
    silent: bool = False,
    silent_spans: Sequence[Sequence[float]] | None = None,
    gate: dict[str, Any] | None = None,
) -> Path:
    """落状态文件（原子替换，五档）。`state` 与 `partial` 是两个键各说一件事：
    `state = "partial"` 看「哪一路整条没交回」（至少一路跑成、也至少一路整条没交回；
    缺哪路看 `artifacts.<source>.state/reason/error`），`partial = true` 看「某一路缺了
    区间」（某块没成，产物照落、该路仍 done——少了这个键，少 450 秒与完整转写同形）。
    `interrupted` 也是终态：取消旗标块边界命中收场，缺口注记进 `error`
    只供人读；重按按钮不用 force 就能从头再跑（幂等闸只拦 running）。
    `silent` / `silent_spans` 记没声跳过（state 照样 done、partial 照样 false）；
    `gate` 记闸自己做成没做成判断，缺了它没门可用的运行与健康运行同形。
    """
    if state not in STATES:
        raise ValueError(f"状态只认 {STATES}，收到 {state!r}")
    document = {
        "schema_version": STATUS_SCHEMA_VERSION,
        "session_id": str(session_id),
        "state": state,
        "started_at": started_at,
        "finished_at": finished_at,
        "sources": [str(item) for item in sources],
        "artifacts": {str(key): dict(value) for key, value in artifacts.items()},
        "partial": bool(partial),
        "silent": bool(silent),
        "silent_spans": [
            [_round(item[0]), _round(item[1])] for item in (silent_spans or [])
        ],
        "gate": dict(gate) if gate is not None else {},
        "error": error,
    }
    return write_text_atomic(
        path, json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    )


def read_status(path: Path | str) -> dict[str, Any]:
    """读回状态文件（缺失或不是对象即抛 `ArtifactContractError`）。"""
    target = Path(path)
    if not target.exists():
        raise ArtifactContractError(f"状态文件不存在：{target}")
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (ValueError, TypeError) as exc:
        raise ArtifactContractError(f"状态文件不是合法 JSON：{exc}") from None
    if not isinstance(document, dict):
        raise ArtifactContractError("状态文件顶层该是对象")
    return document


def _now_iso() -> str:
    """现在的 ISO 时刻（与 `meeting/offline/runner` 编排主模块的 `_now_iso()` 同形）。"""
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def mark_failed_if_unset(session_dir: Path | str, error: str) -> Path:
    """把状态文件补成 `failed` + 原因；盘上停着一版已落定的记录（failed / partial /
    interrupted，判据 `_SETTLED_STATES`）时沿用那一版字段——`engine_load_error` 这类词档
    只在 runner 现场写的版本里、比调用方手里的通用串准，`interrupted`（人停的）不许被
    后来逃出的异常盖成 `failed`。补记录口径只能有这一处：幂等闸、启动核对
    与读回侧都读这份文件。读不动盘上那一版按空记录补；写不下去异常原样抛给调用方记 log。
    """
    directory = Path(session_dir)
    path = directory / STATUS_NAME
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = read_status(path)
        except ArtifactContractError:
            existing = {}
    settled = str(existing.get("state") or "") in _SETTLED_STATES
    return write_status(
        path,
        session_id=str(existing.get("session_id") or directory.name),
        state=str(existing.get("state")) if settled else "failed",
        started_at=str(existing.get("started_at") or _now_iso()),
        sources=list(existing.get("sources") or []),
        artifacts=dict(existing.get("artifacts") or {}),
        finished_at=_now_iso(),
        error=str(existing.get("error") or error) if settled else str(error),
        partial=bool(existing.get("partial")) if settled else False,
        silent=bool(existing.get("silent")) if settled else False,
        silent_spans=list(existing.get("silent_spans") or []) if settled else None,
        gate=dict(existing.get("gate") or {}) if settled else None,
    )


def _status_artifact_row(row: dict[str, Any], *, report: str | None) -> dict[str, Any]:
    """状态文件 `artifacts.<source>` 的唯一形状（三处写方共用，键的算法只有一处）。
    `reason` 跑成留空（`state` 说完了），没跑成带词档，一个都没有时兜 `engine_error`。
    """
    return {
        "transcript": row.get("transcript"),
        "speakers": row.get("speakers"),
        "report": report,
        "segments": int(row.get("segments") or 0),
        "speaker_count": int(row.get("speaker_count") or 0),
        "audio_seconds": row.get("audio_seconds"),
        "state": row.get("state") or "failed",
        "reason": None if row.get("state") == "done" else (row.get("reason") or REASON_ENGINE_ERROR),
        "partial": bool(row.get("partial")),
        "missing_spans": [list(item) for item in (row.get("missing_spans") or [])],
        "silent": bool(row.get("silent")),
        "silent_spans": [list(item) for item in (row.get("silent_spans") or [])],
        "gate": dict(row.get("gate") or {}),
        "error": row.get("error"),
    }


def _failed_source_row(metric: SourceMetrics) -> dict[str, Any]:
    """没落产物的那一路在状态文件 `artifacts.<source>` 里的格：与落产物那路**同形**
    （缺的只是产物名与读数）。`reason` 按 interrupted > not_run > 次序表挑最要紧的
    一个词，`partial` / `missing_spans` 照度量原样带。
    """
    if metric.reasons.get(REASON_INTERRUPTED):
        # 打断档按词自身认：不在次序表里，兜成 engine_error 会把「被人停下」说成「引擎的锅」。
        reason = REASON_INTERRUPTED
    elif metric.reasons.get(REASON_NOT_RUN):
        reason = REASON_NOT_RUN
    elif metric.error is None and not metric.reasons:
        # 一个词档都没有：兜 not_run（产品路径到不了，跑过的每一路至少记一笔，此为守卫）。
        reason = REASON_NOT_RUN
    else:
        reason = next(
            (name for name in _REASON_PRIORITY if metric.reasons.get(name)),
            REASON_ENGINE_ERROR,
        )
    return _status_artifact_row(
        {
            "transcript": None,
            "speakers": None,
            "segments": 0,
            "speaker_count": 0,
            "audio_seconds": round(float(metric.audio_seconds), 3),
            "state": metric.state,
            "reason": reason,
            "partial": bool(metric.partial),
            "missing_spans": [list(item) for item in metric.missing_spans],
            "silent": False,
            "silent_spans": [],
            "gate": dict(metric.gate),
            "error": metric.error,
        },
        report=None,
    )


__all__ = [
    "ArtifactContractError",
    "KNOWN_SCHEMA_MAJOR",
    "REASON_ALL_CHUNKS_FAILED",
    "REASON_DIGITAL_SILENCE",
    "REASON_EMPTY_PCM",
    "REASON_ENGINE_ERROR",
    "REASON_ENGINE_LOAD_ERROR",
    "REASON_GATE_UNJUDGED",
    "REASON_INTERRUPTED",
    "REASON_NO_AUDIO",
    "REASON_NO_SEGMENTS",
    "REASON_NOT_RUN",
    "REASON_TRANSCRIBED",
    "REASON_ZERO_SPAN_SEGMENTS",
    "REPORT_NAME",
    "SCHEMA_VERSION",
    "SPEAKERS_NAME_TEMPLATE",
    "STATES",
    "STATUS_NAME",
    "STATUS_SCHEMA_VERSION",
    "SourceMetrics",
    "TRANSCRIPT_NAME_TEMPLATE",
    "TRANSCRIPT_NOTE_PREFIX",
    "TRANSCRIPT_NOTE_SUFFIX",
    "V1_RTF_BASELINE",
    "build_speakers_document",
    "chunk_row",
    "default_source_gate",
    "format_transcript_line",
    "incomplete_note",
    "is_transcript_note",
    "mark_failed_if_unset",
    "minute_stamp",
    "parse_speakers_document",
    "read_speakers_artifact",
    "read_status",
    "read_transcript_lines",
    "silent_note",
    "speaker_rows",
    "write_report",
    "write_speakers",
    "write_status",
    "write_text_atomic",
    "write_transcript",
]
