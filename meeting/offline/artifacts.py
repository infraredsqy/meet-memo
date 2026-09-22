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
    并排读，「两路当成一路」这件事必须在读的那一刻就被拦下；
  - `label_raw` 记引擎原始标签，`local` 记规范化后的 `S01`，两者都留着，改编号规则时可追溯；
  - `id` 一律 `${source}:${local}`：**「这条路的一号等于那条路的一号」在数据结构上表达
    不出来**，跨路不串靠形状（限定名里始终带着 `source`）而不是靠约定。

## 精度分工

转写正文与它的 `[HH:MM]` 按 `ui-design.md` 门槛 10 走**分钟级**，秒级精度只进 JSON 的
`start` / `end`（数据面）。同一个数在两个文件里因此有两种精度，两者各记各的层级。

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
) -> dict[str, Any]:
    """组一份对照表（**纯函数**，不碰盘）：写出去与读回来都以它为准。

    `segments` 每项收 `start` / `end` / `speaker` / `local` / `text` 五格，
    `speakers` 每项收 `id` / `local` / `label_raw` / `segments` / `seconds` 五格。
    """
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
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": str(session_id),
        "source": source,
        "engine": dict(engine),
        "audio": dict(audio),
        "speakers": summary,
        "segments": rows,
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
    """读回转写成 `[{stamp, speaker, text}]`，行格式不合契约即抛 `ArtifactContractError`。"""
    rows: list[dict[str, str]] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
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
    """一路跑完之后留下的度量（报告的那一行与摘要的那一格都从这里取）。"""

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

    **段耗时分布这一项在会后离线路径上落在「每路一次引擎调用」那个粒度**：MOSSTD
    整段跑，没有「一段一段的耗时」可以统计，逐段秒数因此不冒充它（报告里写清这一点，
    不拿零填一格）。
    """
    rows = [item.to_dict() for item in metrics]
    total_audio = sum(float(item["audio_seconds"]) for item in rows)
    total_segments = sum(int(item["segments"]) for item in rows)
    failed = [item for item in rows if item["state"] == "failed"]
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
        f" · 总耗时：{float(total_wall_seconds):.3f}s"
    )
    lines.append("")
    lines.append("## 逐路")
    lines.append("")
    lines.append("| 路 | 音频 | 秒数 | 段数 | 类数 | 类内秒数 | 墙钟 | 实时率 | 状态 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for item in rows:
        rtf = "-" if item["rtf"] is None else f"{item['rtf']:.3f}×"
        lines.append(
            f"| {item['source']} | {item['audio_file'] or '（缺）'} | {item['audio_seconds']:.1f}"
            f" | {item['segments']} | {item['speakers']} | {item['seconds']:.1f}"
            f" | {item['wall_seconds']:.3f}s | {rtf} | {item['state']} |"
        )
    lines.append("")
    lines.append("## 段耗时分布")
    lines.append("")
    lines.append(
        "会后离线路径对每一路**整段调一次**引擎（MOSSTD 的主场是整段批处理），"
        "因此这里给的是每次调用的墙钟，逐段耗时分布在这一层不存在。"
    )
    lines.append("")
    for item in rows:
        lines.append(
            f"- {item['source']}：一次调用 {item['wall_seconds']:.3f}s"
            f"（音频 {item['audio_seconds']:.1f}s）"
        )
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
        lines.append(
            f"- 整场：{float(total_wall_seconds):.3f}×"
            f"（音频合计 {total_audio:.1f}s / 总耗时 {float(total_wall_seconds):.3f}s）"
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
) -> Path:
    """落状态文件（`running` / `done` / `failed` 三档，写盘原子替换）。"""
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
    "V1_RTF_BASELINE",
    "build_speakers_document",
    "format_transcript_line",
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
