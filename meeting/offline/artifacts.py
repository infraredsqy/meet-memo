"""会后产物的写入与字段契约（`7c6952e5` phase2）。

一场会的会后离线路径落下**四份文件**，都住场次目录：

| 文件 | 作用 | 读者 |
|---|---|---|
| `meeting_offline_transcript_<source>.md` | 权威转写，每行 `[HH:MM] **S01**: 正文`（**分钟级**） | 人 + 下游对账 |
| `meeting_offline_speakers_<source>.json` | 类编号 → 每段区间对照表（**秒级浮点**） | 下游程序 |
| `meeting_offline_report.md` | 运行报告（段耗时分布 / 失败原因分布 / 实时率 + 本场基本事实） | 人 |
| `meeting_offline_status.json` | 状态文件（`running` / `done` / `failed`） | `phase3` 与将来的界面 |

**每路各一份，两路不合并**：转写与对照表两张表都按 `source` 分别落盘，文件名里带
`<source>`。混成一份就再也说不出「这一行出自哪一路」了。

## 字段契约（写入方是本模块，读方是下游程序与对账节点）

对照表是机器读的那一份，字段契约落在这里：

  - 必填顶层键：`schema_version` / `source` / `speakers` / `segments`；
    `segments` 每一项必填 `start` / `end` / `speaker` / `text`；
  - `schema_version` **缺失**，或主版本号**读方不认识** → 读方拒绝该文件并如实报错
    （`read_speakers_artifact()` 抛 `ArtifactContractError`），任何情况下都不许猜；
  - `source` **缺失** → 读方拒绝：一份说不出自己出自哪一路的对照表**没法**与另一路
    并排读，「两路当成一路」这件事必须在读的那一刻就被拦下；**写入侧的公开入口
    `build_speakers_document()` 用同一条判据当场拦**（空 `source` 会让限定名退化成
    `:S01`），写方与读方因此不会对「什么算合契约」有两种口径；
  - `label_raw` 记引擎原始标签，`local` 记规范化后的 `S01`，两者都留着，改编号规则时可追溯；
    **分块模式下 `label_raw` 带块内命名空间**（`c01:S01`），`local` 仍是本路规范化编号 ——
    同一个人的两段落在两块里时故意拿到两个编号（spec：「宁可同一个人被算成两个编号，
    也不要两个人被合并」），而 `cNN` 让下游想合并时自己算得出来；
  - `id` 一律 `${source}:${local}`：**「这条路的一号等于那条路的一号」在数据结构上表达
    不出来**，跨路不串靠形状（限定名里始终带着 `source`）而不是靠约定；
  - `state` 记这一路跑完时的终态（`done` / `failed`），`error` 记失败原因（成功时 `null`）。
    读方**一律先看这一格**：一份空表配 `state = "failed"` 说的是「这一路一段都没转出来」，
    与「这一路真的没人说话」是两件事；
  - `headcount` 说清 `speakers` 数组长度**该怎么读**（见下面「类数与人数」那一节）。

## 类数与人数（`headcount`）

`speakers` 数组的长度在两种跑法下含义不同，机器读方靠 `headcount` 分辨：

  | 跑法 | `headcount.meaningful` | `speakers` 长度的含义 |
  |---|---|---|
  | 整段一次跑（1 次引擎调用） | `true` | 模型在这段音频里分出的类数 |
  | 分块跑（≥2 块） | `false` | **各块类数之和**：同一个人在两块里各拿一个编号 |

分块模式下这个数在真实长度的会议上必然大于人数（`phase3` 实测：900 秒切两块，3 类变 6 类；
5943 秒的素材切 14 块，类数是各块之和）。`headcount` 因此把「这个数从几次引擎调用、
几块里来」一并落在产物上，下游不必回头读报告才知道这件事。

`headcount.chunk_count` 记的是**本路跑了几块**：一块一次引擎调用，因此它也就是**引擎调用
次数**；单趟跑没有 `chunks` 数组、记 `1`，因为那一次同样是一次引擎调用。它是**数出来的**，
不是「总时长 ÷ 块长」算出来的——块长除不尽时这两个数不是一回事：5943.3 秒配 1200 秒块长，
商是 4.95 而实际是 5 块（末块只有 1143.3 秒）。读方拿它对齐的是「这个类数从几块里来」，
按实际块数读，别按商读。

## 精度分工

转写正文与它的 `[HH:MM]` 按 `ui-design.md` 门槛 10 走**分钟级**，秒级精度只进 JSON 的
`start` / `end`（数据面）。同一个数在两个文件里因此有两种精度，两者各记各的层级。

## 缺了一段的时候

分块跑的时候某一块可能没成（OOM、单块抛错、一段都没交回）。那一块的音频因此没有转写，
下游拿到的是一份**短了一段**的权威底本，所以这件事在三处都留痕：

  - 转写文件**第一行**是一条注释行（`incomplete_note()` 造，`<!-- ... -->` 形状），
    写明缺的是哪一段；`read_transcript_lines()` 认这条注释并跳过，它不占段号、不是正文；
  - 对照表的 `audio` 那一格带 `partial` 与 `missing_spans`，`chunks` 数组逐块记成功与失败；
  - 状态文件与运行报告写明整场 `partial` 与缺失区间。

纯标准库，无重依赖（`json` / `re` / `os`）。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time as _time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

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

#: V1 会后回放的实测实时率（`legacy-assets` B16）：报告里拿它当对照基线
V1_RTF_BASELINE = 0.39

#: `[HH:MM]` 的形状：5 个字符的分钟级时间戳（`HH` 在超过 99 分钟时照样是数字，长度会变长）
_LINE_RE = re.compile(r"^\[(?P<stamp>\d{2}:\d{2})\] \*\*(?P<speaker>[^*]+)\*\*: (?P<text>.*)$")

#: 转写第一行那条注释（缺了一段的说明）的判据：整行以它开头就算注释，`read_transcript_lines()`
#: 跳过它。用 HTML 注释的形状是因为转写正文里出现 `<!--` 的概率可以忽略，而
#: 「一个空格 + 正文」那类形状会与真正的一行正文分不开。
TRANSCRIPT_NOTE_PREFIX = "<!--"
TRANSCRIPT_NOTE_SUFFIX = "-->"

#: 合法状态
STATES = ("running", "done", "failed")


class ArtifactContractError(ValueError):
    """读到的产物不满足字段契约（`schema_version` / `source` 缺失或版本不认识）。

    这一档记的是**读方的拒绝**，与写到一半的意外分开：拿不准的文件一律拒收，读方不许猜。
    继承 `ValueError`，与 `meeting` 里别的「值不对」错误同档。
    """


# ---------------------------------------------------------------------------
# 转写正文里的时间戳（分钟级）
# ---------------------------------------------------------------------------


def minute_stamp(seconds: float) -> str:
    """秒 → `HH:MM`（分钟级，**向下取整**；负数收成 `00:00`）。

    取整口径与 `ui-design.md` 门槛 10 一致：分钟级那一档只报「第几分钟」，
    第 3 分 59 秒与第 3 分 0 秒落同一个 `00:03`。
    """
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
    """转写第一行那条注释：写明这段音频有哪几段没有转写（秒级，落在原录音时间轴上）。

    只有分了块、且某一块没成的时候才会有这一行。**它是一行注释**：
    `read_transcript_lines()` 认它并跳过，读方按行格式读正文时不会把它当成一段。
    """
    listed = "、".join(f"{float(item[0]):.1f}–{float(item[1]):.1f}s" for item in spans)
    return (
        f"{TRANSCRIPT_NOTE_PREFIX} 这一路缺 {len(spans)} 段音频的转写：{listed}"
        f"（分块跑时那几块没成，详见 {REPORT_NAME} 与 {STATUS_NAME}）"
        f" {TRANSCRIPT_NOTE_SUFFIX}"
    )


def is_transcript_note(line: str) -> bool:
    """这一行是不是转写的注释行（第一行那条缺段说明）。"""
    return line.strip().startswith(TRANSCRIPT_NOTE_PREFIX)


# ---------------------------------------------------------------------------
# 两个纯函数：JSON 形状 ↔ 内存形状
# ---------------------------------------------------------------------------


def _round(value: Any) -> float:
    """秒数落盘前统一收成 3 位小数（秒级浮点，读数稳定、对账可复算）。"""
    return round(float(value), 3)


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

    `segments` 每项收 `start` / `end` / `speaker` / `local` / `text` 五格，
    `speakers` 每项收 `id` / `local` / `label_raw` / `segments` / `seconds` 五格，
    `chunks` 每项收 `index` / `offset_seconds` / `seconds` / `segments` / `error` 五格
    （单趟跑时是空数组：没有块这件事本身也要看得出来）。

    `state` / `error` 记这一路的终态与失败原因：**每一块都没跑成的那一路**留下的是一份
    空表，`state = "failed"` 与 `error` 是读方分辨「一段都没转出来」与「这段音频真的没人
    说话」的唯一依据。有产物的那一档传 `done`，缺了区间的那些块由 `audio.partial` 与
    `audio.missing_spans` 说话。

    `headcount` 每一趟都现算（见模块头「类数与人数」）：块数多于一的时候 `speakers` 数组的
    长度是**各块类数之和**，读它当人数会在真实长度的会议上错得离谱，因此这一格必须带上
    「这个数从几次引擎调用里来」。`chunk_count` 记的是**实际块数 = 引擎调用次数**
    （`chunks` 的条数；单趟跑记 `1`），不是「总时长 ÷ 块长」那个商——口径见模块头。

    **`source` 与 `parse_speakers_document()` 用同一条判据**（非空字符串）：限定名
    `${source}:${local}` 是这一层的形状保证，`source` 为空时它会长成 `:S01` —— 那种
    形状正是字段契约要拒的东西。写入方在这里当场拦下，别让不合契约的形状先落盘、
    再等读方拒收（`write_speakers()` 也会过一遍读方契约，这里是自己成一道）。
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
                # 限定名从 `source` 与 `local` 现算：绝不接受调用方递进来的裸 id
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
    chunk_rows: list[dict[str, Any]] = []
    for item in chunks or []:
        chunk_rows.append(
            {
                "index": int(item["index"]),
                "offset_seconds": _round(item["offset_seconds"]),
                "seconds": _round(item["seconds"]),
                "segments": int(item["segments"]),
                "error": None if item.get("error") is None else str(item["error"]),
            }
        )
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

    三条拒绝面，逐条来自字段契约：

      1. 顶层不是对象 → 拒；
      2. `schema_version` 缺失、不是整数、或**主版本号不认识** → 拒；
      3. `source` 缺失或空白 → 拒（一份说不出出处的对照表没法与另一路并排读）；
      4. `segments` / `speakers` 缺失或不是列表、`segments` 某项缺必填键 → 拒。

    通过之后就原样交回整份 dict（不裁剪、不补默认值）：读方拿到的就是写入方落盘的那份。
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


# ---------------------------------------------------------------------------
# 写盘（原子替换：半份文件不会被读方读到）
# ---------------------------------------------------------------------------


#: `os.replace` 的瞬时失败里，Windows 那一档的 `winerror`：拒绝访问 / 共享冲突 / 锁冲突
_TRANSIENT_WINERRORS = frozenset({5, 32, 33})


def _tmp_path_for(target: Path) -> Path:
    """这次调用**独占**的临时文件路径（`<目标名>.<pid>.<线程 id>.<随机串>.tmp`）。

    三段都留着：pid 与线程 id 让目录列出来时一眼看得出是谁写的，随机串兜住
    「线程 id 被复用」（线程退出之后它的 ident 会被下一条线程接着用）与「pid 被复用」
    这两档 —— 只靠 pid + ident 的写法在长跑的收尾线程池里仍会撞名。
    """
    return target.with_name(
        f"{target.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
    )


def _is_transient_replace_error(exc: OSError) -> bool:
    """`os.replace` 的这次失败是不是**瞬时**那一档（退避重试还有意义）。

    收尾是后台线程在跑，界面与下游随时可能来读同一份文件，两边的窗口都很短：
      - `PermissionError`（`WinError 32` / `5` 一档）：目标文件正被别人拿在手里；
      - `FileNotFoundError`：目标那一格正处在「被换走 / 被删」的瞬间，或者并发写方
        刚把文件名拿在手里 —— 这一类重试一次即可；
      - 别的 `winerror`（`32` / `33` 那些共享与锁冲突）在部分路径上落的是裸 `OSError`。
    其余异常（磁盘满、路径不存在、只读盘）一次都不重试，原样抛给调用方记账。
    """
    if isinstance(exc, (PermissionError, FileNotFoundError)):
        return True
    return getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS


def write_text_atomic(path: Path | str, text: str, *, retries: int = 5) -> Path:
    """写一份文本产物：先写一份**本调用独占的**临时文件，再 `os.replace` 换上去。

    收尾是后台线程在跑，界面与下游随时可能来读；半份文件因此比慢一点更坏。

    临时文件名**每个写方各不相同**（见 `_tmp_path_for()`）：同一个目标上并发两个写方时，
    共用一个 `<名字>.tmp` 会让先落地的 `os.replace` 把那份临时文件消费掉，另一边的
    `os.replace` 随即抛 `FileNotFoundError` —— 会后主链与 `meeting/backends/post.py` 的
    `_mark_failed` 正是这种并发写方。各写各的临时文件之后，两个写方都能落地，
    目标那一格任何时刻读到的都是一份完整的内容。

    Windows 上一份**别人正拿在手里**的文件会让 `os.replace` 抛 `PermissionError`
    （`WinError 32`）：状态文件恰好在收尾那几十秒里被反复重写、又被界面轮询读。
    这一档与上面那种瞬时失败共用同一条短退避（见 `_is_transient_replace_error()`）；
    重试用尽时把异常抛出去，由调用方如实记账。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path_for(target)
    tmp.write_text(text, encoding="utf-8")
    attempts = max(1, int(retries))
    try:
        for attempt in range(attempts):
            try:
                os.replace(tmp, target)
            except OSError as exc:
                if attempt + 1 >= attempts or not _is_transient_replace_error(exc):
                    raise
                _time.sleep(0.02 * (attempt + 1))
            else:
                return target
        return target  # pragma: no cover - 上面那一支已经返回或抛出
    finally:
        # 重试用尽那一支会留下这次调用自己的临时文件：收拾掉，不给目标目录留残骸
        # （成功那一支已经把它换走了，这里是空操作）。
        with suppress(OSError):
            tmp.unlink(missing_ok=True)


def write_transcript(path: Path | str, lines: Iterable[str]) -> Path:
    """落一份转写：**恰好 `行数` 行**，每行末尾一个换行（空转写就是 0 字节文件）。

    行尾不做「末尾多一个空行」那种花样：读方按行读，空行会被当成一段空正文。
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


# ---------------------------------------------------------------------------
# 逐路的度量（报告与摘要都读它）
# ---------------------------------------------------------------------------


@dataclass
class SourceMetrics:
    """一路跑完之后留下的度量（报告的那一行与摘要的那一格都从这里取）。

    `partial` 立起来表示这一路**缺了区间**（分块跑时某一块没成）：产物照落、`state` 仍是
    `done`，但缺的那几段列在 `missing_spans` 里。整场因此不会把「少了 450 秒」当成
    「整段都转好了」。
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
    #: 逐块的记账（单趟跑时是空列表）
    chunks: list[dict[str, Any]] = field(default_factory=list)
    #: 没跑成的那几块覆盖的区间（秒，原录音时间轴）
    missing_spans: list[tuple[float, float]] = field(default_factory=list)
    #: 这一路是不是缺了区间
    partial: bool = False

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
            "chunks": [dict(item) for item in self.chunks],
        }


def speaker_rows(
    spans: Sequence[Any], *, raw_labels: Sequence[Any] | None = None
) -> list[dict[str, Any]]:
    """类编号 → 段数与秒数，**并把引擎原始标签一并留下来**。

    汇总那一半照 `meeting/offline/speakers.py` 的 `speaker_summary()`（段数按段数、
    秒数按段区间加总，段间静默不计）。

    `label_raw` 逐段记引擎原始标签，由调用方在**编号之前**从分段上取。两者按**编号**
    对齐（某个 `Sxx` 第一次出现的那一段，它的引擎标签就是这一类的原始标签），
    不按位置对齐 —— 汇总表的行序走「限定名首次出现的顺序」这条口径，分段序走时间顺序，
    两种顺序各管各的。
    """
    from meeting.offline.speakers import speaker_summary

    first_raw: dict[str, str] = {}
    for span, raw in zip(spans, raw_labels or []):
        first_raw.setdefault(str(span.local), str(raw))
    rows = speaker_summary(spans)
    for row in rows:
        row["label_raw"] = first_raw.get(str(row["local"]), "")
    return rows


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

    「实时率」那一节有两格、两种口径：逐路那一格是 `SourceMetrics.rtf`（**墙钟 ÷ 音频**，
    与 V1 的 0.39× 基线同向，小于 1 表示比录音快）；「整场」那一格是**音频合计 ÷ 总耗时**，
    括号里逐字写出这条算式，读的人当场能复核。两格互为倒数，方向各自写在括号里。
    总耗时为 0 时整场那一格落「算不出」，不拿 0 当除数。

    **段耗时分布这一项在会后离线路径上落在「每次引擎调用」那个粒度**：MOSSTD 的主场是
    整段批处理，没有「一段一段的耗时」可以统计；分了块之后每次调用对应一块，逐块的
    墙钟因此列得出来（块数 1 就是整段一次调用）。分块、缺段这两件事各有一节：
    报告是人读这一场跑成什么样子的地方，缺一段却不写出来的话，读报告的人看不出来。
    """
    rows = [item.to_dict() for item in metrics]
    total_audio = sum(float(item["audio_seconds"]) for item in rows)
    total_segments = sum(int(item["segments"]) for item in rows)
    total_calls = sum(len(item["chunks"]) or 1 for item in rows)
    failed = [item for item in rows if item["state"] == "failed"]
    incomplete = [item for item in rows if item["missing_spans"]]
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
    if incomplete:
        listed = "、".join(
            f"{item['source']} 缺 {len(item['missing_spans'])} 段" for item in incomplete
        )
        lines.append(f"- ⚠ **这一场的转写不完整**：{listed}（逐段区间见下面那一节）")
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
        if calls > 1:
            lines.append(
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
                f"- {item['source']}：{len(item['chunks'])} 次调用，合计 {item['wall_seconds']:.3f}s"
                f"（音频 {item['audio_seconds']:.1f}s）"
            )
            for run in item["chunks"]:
                state = "没跑成" if run["error"] else "成"
                lines.append(
                    f"  - 第 {run['index']} 块（{run['offset_seconds']:.1f}s 起，"
                    f"{run['seconds']:.1f}s）：{run['segments']} 段，"
                    f"墙钟 {run['wall_seconds']:.3f}s，{state}"
                )
        else:
            lines.append(
                f"- {item['source']}：一次调用 {item['wall_seconds']:.3f}s"
                f"（音频 {item['audio_seconds']:.1f}s）"
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
            # **整场那一格是「音频合计 ÷ 总耗时」**：与括号里的算式同源，可当复核口径。
            # 逐路那一格走的是另一条口径（`SourceMetrics.rtf` = 墙钟 ÷ 音频，与 V1 的
            # 0.39× 基线同向），两者互为倒数，因此这里把关系写在括号里，
            # 免得读的人把两个方向相反的「×」当成互相矛盾。
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


# ---------------------------------------------------------------------------
# 状态文件
# ---------------------------------------------------------------------------


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
) -> Path:
    """落状态文件（`running` / `done` / `failed` 三档，写盘原子替换）。

    `partial` 是这一场有没有**缺了区间**（分块跑时某块没成）：`state` 仍落 `done`
    （产物可用、这一格是终态），而 `partial = true` 加上 `error` 里那句「缺了哪一段」
    让读状态文件的人与程序都看得出这份转写短了一截。没有这个键的话，一份少了 450 秒的
    转写与一份完整的转写在状态文件里长得一模一样。
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


__all__ = [
    "ArtifactContractError",
    "KNOWN_SCHEMA_MAJOR",
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
    "format_transcript_line",
    "incomplete_note",
    "is_transcript_note",
    "minute_stamp",
    "parse_speakers_document",
    "read_speakers_artifact",
    "read_status",
    "read_transcript_lines",
    "speaker_rows",
    "write_report",
    "write_speakers",
    "write_status",
    "write_text_atomic",
    "write_transcript",
]
