"""输入清单（`41465462` spec「交付物形状」里的 `working/inputs.json`）。

这一层回答一个问题：**这一场会有哪几条转写、每条几行、走的是哪一档**。名字按 spec 那张
上游冻结的表找（会中 `meeting_transcript_<source>.md`、会后
`meeting_offline_transcript_<source>.md`，`<source> ∈ mic / loopback`），**不猜**；两条都
不在的 `<source>` 不进清单。

产出那份 `inputs.json` 的读者有两类：程序（phase2 的 runner 拿它判「要不要启动 DSH」、
phase3 的验收脚本拿它核「这一轮喂了几条」）与人（打开场次目录想知道这一场到底有什么）。
因此形状是**两份都在一份文件里**说清的：`mode` 那一格说明整场走的是哪一档，
`sources[*].live` / `sources[*].offline` 两格分别说明每一条转写在不在、几行。

四条口径：

  - **行数一律走 `lines.count_lines()`**（`splitlines()`，空行计入）：行号区间表与分段契约的
    分母都是这一个数，全项目只能有一种数法；
  - **档位逐 source 判、整体取公共档位**（`chunk.mode_for()`，同一条口径）；
  - **两个来源一条都没有 → `mode = "none"`**：调用方据此不启动 DSH、状态文件记 `failed` +
    原因。**不许静默出一份空纪要** —— 一份没有输入的纪要会以「这场会什么都没定」的样子交出去；
  - **写盘原子**：先写一份本调用独占的临时文件再 `os.replace` 换上去。读方（界面、下游程序）
    随时可能来读，半份 JSON 比晚一点落地坏得多。这一份实现与 `meeting.offline.artifacts`
    那一份逐行同构，因此提成了**两包共用**的小模块 `meeting.atomicio`（实现只留一份，
    这里留同名转发别名，本包内外的调用方一个都不用改）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from meeting.atomicio import REPLACE_RETRIES, write_text_atomic
from meeting.minutes.chunk import (
    MODES,
    WORKING_DIR,
    InputSpec,
    classify,
    discover_inputs,
    mode_for,
    read_transcript_text,
)
from meeting.minutes.lines import count_lines

#: 清单的模式版本。**主版本号读方认识才收**：结构变了就抬它，读方据此拒收。
SCHEMA_VERSION = 1

#: 本实现认识的主版本号
KNOWN_SCHEMA_MAJOR = 1

#: 一条输入都没有那一档（`MODES` 之外的第四个词：整场一条转写都没有）
MODE_NONE = "none"

#: `mode` 这一格的全部取值
INPUT_MODES: tuple[str, ...] = (*MODES, MODE_NONE)

#: 清单落在工作目录下（相对场次目录）
INPUTS_NAME = "inputs.json"


class InputsError(ValueError):
    """输入清单这一层自己的错误（输入清单坏掉、写盘失败）。

    继承 `ValueError`，与 `meeting` 里别的「值不对」错误同档：调用方（phase2 的 runner）
    据此把这一趟记成 `failed` + 原因。
    """


@dataclass(frozen=True)
class TranscriptEntry:
    """一条输入里**某一路稿**的读数（`{"file": …, "lines": …}` 那一格）。"""

    file: str
    lines: int

    def as_dict(self) -> dict[str, Any]:
        return {"file": self.file, "lines": self.lines}


@dataclass(frozen=True)
class SourceEntry:
    """一条输入（一个 `<source>`）的读数：这一路有哪几份稿、各几行。"""

    source: str
    live: TranscriptEntry | None
    offline: TranscriptEntry | None

    @property
    def kind(self) -> str:
        """这一路自己有哪几条稿（`both` / `live_only` / `offline_only`）。

        映射本体在 `chunk.classify()`（三处共用那一份）。两份都不在的条目照旧答
        `offline_only`：本层从不构造这样的条目（`entries_for_session()` 只收盘上真有的稿），
        这里的空态答案沿用从前那一版的原行为，一个字不变。
        """
        if not self.live and not self.offline:
            return "offline_only"
        return classify(bool(self.live), bool(self.offline))

    def as_dict(self) -> dict[str, Any]:
        """逐条的形状：**不在的那一条整个不写**（`null` 与「没这一份」是两件事，不写更清楚）。"""
        payload: dict[str, Any] = {"source": self.source, "kind": self.kind}
        if self.live is not None:
            payload["live"] = self.live.as_dict()
        if self.offline is not None:
            payload["offline"] = self.offline.as_dict()
        return payload


def entries_for_session(session_dir: str | Path) -> tuple[SourceEntry, ...]:
    """读盘一遍：按 spec 的表找出这一场有哪几条转写、各几行。

    只回答「盘上有哪几份」，不判档位（档位由 `mode_for()` 拿着同一份清单回答）。
    """
    session = Path(session_dir)
    entries: list[SourceEntry] = []
    for spec in discover_inputs(session):
        entries.append(
            SourceEntry(
                source=spec.source,
                live=_entry_of(session, spec.live),
                offline=_entry_of(session, spec.offline),
            )
        )
    return tuple(entries)


def _entry_of(session: Path, name: str | None) -> TranscriptEntry | None:
    """一份转写的读数（文件不在就是 `None`；读得动要按 `errors="replace"` 的口径数行）。"""
    if not name:
        return None
    return TranscriptEntry(file=name, lines=count_lines(read_transcript_text(session / name)))


def build_inputs_document(
    session_dir: str | Path,
    entries: Sequence[SourceEntry],
    *,
    mode: str | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """组一份 `inputs.json` 的内容（**纯函数**，不碰盘）。

    `mode` 缺席时按 `entries` 现判（`chunk.mode_for()` 那一条口径）；一条输入都没有时是
    `"none"`，调用方据此不启动 DSH。
    """
    session = Path(session_dir)
    specs = tuple(
        InputSpec(
            source=item.source,
            live=item.live.file if item.live else None,
            offline=item.offline.file if item.offline else None,
        )
        for item in entries
    )
    resolved = mode or mode_for(specs) or MODE_NONE
    if resolved not in INPUT_MODES:
        raise InputsError(f"档位只认 {INPUT_MODES}，收到 {resolved!r}")
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": session.name,
        "mode": resolved,
        "generated_at": generated_at or datetime.now().isoformat(timespec="seconds"),
        "working_dir": WORKING_DIR,
        "sources": [item.as_dict() for item in entries],
    }


def write_inputs(path: str | Path, document: Mapping[str, Any]) -> Path:
    """落一份 `inputs.json`（原子替换），返回落点。"""
    return write_text_atomic(
        path, json.dumps(dict(document), ensure_ascii=False, indent=2) + "\n"
    )


def read_inputs(path: str | Path) -> dict[str, Any]:
    """读回一份 `inputs.json`（**读方拒收不认识的模式版本**，与别的产物同档）。"""
    target = Path(path)
    if not target.exists():
        raise InputsError(f"输入清单不在：{target}")
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise InputsError(f"输入清单读不动：{target}（{type(exc).__name__}: {exc}）") from exc
    if not isinstance(document, dict):
        raise InputsError(f"输入清单顶层该是对象：{target}")
    return parse_inputs_document(document)


def parse_inputs_document(document: Any) -> dict[str, Any]:
    """校验一份输入清单（缺字段、版本不认识一律拒收，**不猜**）。"""
    if not isinstance(document, Mapping):
        raise InputsError(f"输入清单顶层该是对象，收到 {type(document).__name__}")
    version = document.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise InputsError(f"输入清单的 schema_version 要是整数，收到 {version!r}")
    if version != KNOWN_SCHEMA_MAJOR:
        raise InputsError(
            f"输入清单的主版本号不认识：{version}（本实现只认 {KNOWN_SCHEMA_MAJOR}）"
        )
    if document.get("mode") not in INPUT_MODES:
        raise InputsError(f"输入清单的 mode 只认 {INPUT_MODES}，收到 {document.get('mode')!r}")
    sources = document.get("sources")
    if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes)):
        raise InputsError(f"输入清单的 sources 要是列表，收到 {type(sources).__name__}")
    for index, item in enumerate(sources, start=1):
        if not isinstance(item, Mapping):
            raise InputsError(f"sources 第 {index} 条该是对象，收到 {type(item).__name__}")
        if not item.get("source"):
            raise InputsError(f"sources 第 {index} 条缺 source")
    return dict(document)


def write_inputs_for_session(
    session_dir: str | Path,
    entries: Sequence[SourceEntry] | None = None,
) -> dict[str, Any]:
    """找输入 → 组清单 → 落盘，返回那份清单（`runner` 的第一步走的就是它）。

    落点：`<场次目录>/working/inputs.json`（spec 交付物表钉死的）。

    `mode` 落 `none` 时清单**照落**：读方要看得见「这一场查过了、确实什么都没有」；
    但「这一档不启动 DSH、状态记 `failed` + 原因，不许静默出一份空纪要」的闸门
    在 `meeting/minutes/runner.py` 的 `run()` 里 —— 本函数只落清单、不发车。
    """
    session = Path(session_dir)
    found = tuple(entries) if entries is not None else entries_for_session(session)
    document = build_inputs_document(session, found)
    write_inputs(session / WORKING_DIR / INPUTS_NAME, document)
    return document


def inputs_path(session_dir: str | Path) -> Path:
    """`inputs.json` 的落点（相对场次目录）。"""
    return Path(session_dir) / WORKING_DIR / INPUTS_NAME


def source_ids_with_transcripts(session_dir: str | Path) -> tuple[str, ...]:
    """这一场有两份稿的 `<source>` 顺序（`SOURCE_IDS` 的序，缺席的不出现）。"""
    return tuple(item.source for item in entries_for_session(session_dir))


#: `os.replace` 的瞬时失败判定、独占临时文件名与退避重试：与 `meeting.offline.artifacts`
#: 共用同一份实现，住在 `meeting/atomicio.py`（`REPLACE_RETRIES` 是它的重试次数常量，
#: 这里转发导出，`__all__` 与既有调用方一个不动）。


__all__ = [
    "INPUTS_NAME",
    "INPUT_MODES",
    "KNOWN_SCHEMA_MAJOR",
    "MODE_NONE",
    "REPLACE_RETRIES",
    "SCHEMA_VERSION",
    "InputsError",
    "SourceEntry",
    "TranscriptEntry",
    "build_inputs_document",
    "entries_for_session",
    "inputs_path",
    "parse_inputs_document",
    "read_inputs",
    "source_ids_with_transcripts",
    "write_inputs",
    "write_inputs_for_session",
    "write_text_atomic",
]
