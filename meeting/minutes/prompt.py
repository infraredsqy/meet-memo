"""编排提示词的渲染：把路径、档位与输入清单填进模板，交出主 agent 照着读的提示词全文。

编排正文归 `prompts/meeting_final.md`，渲染器只做两件事：把路径与档位填进模板、把清单
拼出来。各占位符与档位说明段的口径见对应常量与函数的注释。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from meeting.minutes.chunk import (
    CHUNK_DIR_NAME,
    INDEX_NAME,
    MODES,
    WORKING_DIR,
    InputSpec,
    classify,
    discover_inputs,
    mode_for,
)
from meeting.minutes.lines import count_lines
from meeting.speaker_names import identity_rows

#: 模板的默认落点（仓库根下的 `prompts/meeting_final.md`）
DEFAULT_TEMPLATE = Path(__file__).resolve().parents[2] / "prompts" / "meeting_final.md"

#: 引擎认识的全部占位符（模板里出现这些之外的花括号内容一律判错）
PLACEHOLDER_NAMES: tuple[str, ...] = (
    "SESSION_DIR",
    "MODE",
    "MODE_NOTE",
    "INPUTS",
    "MAX_SEGMENTS",
    "SPEAKER_NAMES",
    "PHASE_BODY",
)

#: 编排正文在模板里的两块标记：渲染时把整块就地换成正文（正文的落点就是标记块本身，
#: 模板里因此**可以没有** `{{PHASE_BODY}}`；写在别处时那一处也填同一份正文）
PHASE_BODY_BEGIN = "<!-- PHASE_BODY:BEGIN -->"
PHASE_BODY_END = "<!-- PHASE_BODY:END -->"

#: `{{SESSION_DIR}}` 填给模型的那一点：**模型的工作目录**。
#: 提示词里凡是要模型照着走的路径一律相对场次目录（子会话的 cwd 就是场次目录），
#: 因此这里填 `.` 而不是那段绝对路径——绝对路径会让同一条提示词里出现两种口径
#: （清单里写着「下面所有路径都相对它」，紧接着一行又冒出 `C:/…/场次目录/meeting_final.md`）。
#: 给程序读的那一份（`working/inputs.json` 与清单里那段 JSON 的 `session_dir`）照旧是绝对路径。
MODEL_CWD = "."

#: 模板里任何一处没被填掉的占位符（渲染完还留着的就是「对不上号」）
PLACEHOLDER_RE = re.compile(r"\{\{[^{}]*\}\}")

#: 每段上限的默认值（与 `minutes.max_segments` 配置键的默认值一致）
DEFAULT_MAX_SEGMENTS = 8

#: 两路各自的说话人标签形态（写进提示词，模型据此知道哪一份稿带编号）
SPEAKER_SHAPE_NOTES: dict[str, str] = {
    "live": "无说话人标签（每行形如 `[HH:MM] 正文`）",
    "offline": "带聚类编号（每行形如 `[HH:MM] **S01**: 正文`）",
}

#: 两份稿的人话标签。**只有这两份稿这一个维度，没有排序**：谁也不叫"权威"、谁也不"优先"
#: （`docs/design-intent.md` 第 11 条、R11 / R12 —— 实测并不保证某一份稳定更好）。
KIND_LABELS: dict[str, str] = {
    "live": "会中稿",
    "offline": "会后稿",
}

#: 两份稿的文件名模板（写进清单，读的人据此回原始文件核）
KIND_FILE_TEMPLATES: dict[str, str] = {
    "live": "meeting_transcript_<source>.md",
    "offline": "meeting_offline_transcript_<source>.md",
}

#: 两路各自的稿型（判逐 source 档位与列表说明用）
KINDS: tuple[str, ...] = ("live", "offline")

#: 一条输入**可能有哪几种稿**（`PromptInput.kind` 那一格的全部取值）：两条都在 / 只有会中 /
#: 只有会后。清单与说明段都按它说话，`kinds_per_source()` 的分类也落在这三个词上。
INPUT_KINDS: tuple[str, ...] = ("both", "live_only", "offline_only")

def shape_of(kind: str) -> str:
    """某一份稿的形态说明（`会中稿，无说话人标签（…）`；写进清单与说明段）。"""
    return f"{KIND_LABELS[kind]}，{SPEAKER_SHAPE_NOTES[kind]}"


def kind_for(kinds: Sequence[str]) -> str:
    """这一路**手上有哪几份稿** → `both` / `live_only` / `offline_only`。

    只按「哪几份在」判，不排先后、不说哪一份更好（R11：两份平级）；映射本体在
    `chunk.classify()`（三处共用那一份）。
    """
    present = set(kinds)
    if present - set(KINDS):
        raise PromptTemplateError(f"这一路的稿别说不出（收到 {sorted(present)}）")
    try:
        return classify("live" in present, "offline" in present)
    except ValueError as exc:
        raise PromptTemplateError(f"这一路的稿别说不出（收到 {sorted(present)}）") from exc


def _note_sentence(source: str, kinds: Sequence[str]) -> str:
    """说明段里**某一路**那一句：它这一路手上有哪几份稿、每一份长什么样。

    逐条写清、不做排序（R11）；缺一条时如实写缺的是哪一份，
    **不把缺的那一条说成不可信**。
    """
    kind = kind_for(kinds)
    if kind == "both":
        shape = "；".join(shape_of(item) for item in ("live", "offline"))
        return (
            f"> `{source}` 这一路的稿：{shape}。两份都在，**平级、不合并**，"
            "逐块对照那一步两份都要读。"
        )
    only = kinds[0]
    if kind == "live_only":
        return (
            f"> `{source}` 这一路本次只有{shape_of(only)}。会后稿不在这台机器上，"
            "逐块对照那一步没有第二条可对。附录里如实写明「这一路分不出个人」，"
            "不要为任何人名给出推断。"
        )
    return (
        f"> `{source}` 这一路本次只有{shape_of(only)}。会中稿不在这台机器上，"
        "逐块对照那一步没有第二条可对。附录里如实写明「这一路分不出个人」，"
        "不要为任何人名给出推断。"
    )


class PromptTemplateError(ValueError):
    """提示词渲染不出来（模板缺席、占位符对不上、某条输入的路径是空）。

    继承 `ValueError`，与 `meeting` 里别的「值不对」错误同档：调用方据此把这一趟
    记成 `failed` + 原因，**不拿半份提示词去启动子会话**。
    """


@dataclass(frozen=True)
class PromptInput:
    """提示词清单里的一条：**某一路的某一份稿**（`live` 或 `offline`）。

    `transcript` 是**相对场次目录**的转写路径，`index` / `chunk_dir` 是那一份稿的分块产物
    （`working/<source>/<kind>/…`）；路径一律正斜杠（构造时经 `_posix()`，渲染器任何一处
    都不该把反斜杠漏出去）。**两条都在时它出现两次（两条各一条），谁也不替谁**
    （R11：两份稿平级、一起喂给模型）；`kind` 由调用方给死，不拿文件名的子串猜。
    """

    source: str
    kind: str
    transcript: str
    lines: int
    chunk_count: int
    index: str
    chunk_dir: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "transcript", _posix(self.transcript))
        object.__setattr__(self, "index", _posix(self.index))
        object.__setattr__(self, "chunk_dir", _posix(self.chunk_dir))
        if self.kind not in KINDS:
            raise PromptTemplateError(f"稿别只认 {KINDS}，收到 {self.kind!r}")

    @property
    def label(self) -> str:
        """这一份稿的人话标签（会中稿 / 会后稿）。"""
        return KIND_LABELS[self.kind]


@dataclass(frozen=True)
class InputsListing:
    """渲染一份提示词需要的全部输入事实（一到两路，每路一到两份稿，逐份一条）。"""

    session_dir: str
    mode: str
    inputs: tuple[PromptInput, ...]

    @property
    def sources(self) -> tuple[str, ...]:
        """出现过的 source（按出现序，同一个 source 只答一次）。"""
        seen: list[str] = []
        for item in self.inputs:
            if item.source not in seen:
                seen.append(item.source)
        return tuple(seen)

    def kinds_of(self, source: str) -> tuple[str, ...]:
        """这一路手上有哪几份稿（`("live",)` / `("live", "offline")` …）。"""
        return tuple(item.kind for item in self.inputs if item.source == source)

    def kinds_per_source(self) -> tuple[tuple[str, str], ...]:
        """逐 source 的稿型：`(("mic", "both"), ("loopback", "live_only"))`。

        说明段与整场档位都按它说话。
        """
        return tuple((source, kind_for(self.kinds_of(source))) for source in self.sources)


def _posix(value: object) -> str:
    """路径一律正斜杠（DSH 子会话里的文本混着反斜杠容易被当成转义）。"""
    return str(value).replace("\\", "/")


def mode_note_text(mode: str, per_source: Sequence[tuple[str, str]]) -> str:
    """档位说明段（`{{MODE_NOTE}}` 的正文，`both` 档、且每一路两份都在时也**逐路写一句事实**）。

    `per_source` 是**逐 source 的稿型**（`InputsListing.kinds_per_source()` 那个形状），
    说明段逐 source 各写一句它这一路有哪几份稿。这一层是**事实陈述**：写出来的每句话都要在
    清单里找得到依据，且**不出现任何「哪一份更可信 / 更权威」的排序**（R11、
    `docs/design-intent.md` 第 11 条）——混合形状曾经渲染出自相矛盾的提示词
    （说明段说「本次只有会中稿」，清单里却写着另一路「带聚类编号」）。
    """
    if mode not in MODES:
        raise PromptTemplateError(f"档位只认 {MODES}，收到 {mode!r}")
    where = "、".join(source for source, _ in per_source) if per_source else "（没有输入）"
    if mode == "both":
        if all(kind == "both" for _, kind in per_source) and per_source:
            # 两份都全这一档不再是空的：spec 完成判定第 3 条要求「说明段逐条写清
            # 这一场手上有哪几份」，只有这一档原先什么都没写、那一格渲染成空白行。
            shape = "；".join(shape_of(item) for item in ("live", "offline"))
            return (
                f"> 本次这两路两份都在（{where}）：{shape}。两份**平级、不合并**，"
                "逐块对照那一步两份都要读。"
            )
        return "\n".join(_note_sentence(source, _kinds_of(kind)) for source, kind in per_source)
    if mode == "live_only":
        return (
            f"> 本次两路都只有{shape_of('live')}。会后稿不在这台机器上，"
            "逐块对照那一步没有第二条可对。附录里如实写明「这一路分不出个人」，"
            "不要为任何人名给出推断。"
            f"（这一档的两路：{where}。）"
        )
    return (
        f"> 本次两路都只有{shape_of('offline')}。会中稿不在这台机器上，"
        "逐块对照那一步没有第二条可对。附录里如实写明「这一路分不出个人」，"
        "不要为任何人名给出推断。"
        f"（这一档的两路：{where}。）"
    )


def _kinds_of(kind: str) -> tuple[str, ...]:
    """稿型那一个词 → 它在场的那几份稿（`_note_sentence()` 只收「哪几份在」）。"""
    if kind == "both":
        return ("live", "offline")
    if kind == "live_only":
        return ("live",)
    if kind == "offline_only":
        return ("offline",)
    raise PromptTemplateError(f"稿型只认 {INPUT_KINDS}，收到 {kind!r}")


#: `{{SPEAKER_NAMES}}` 的空态正文：盘上没有可用的确认记录时**如实**渲染这一段，
#: 与 `{{MODE_NOTE}}` 的「这一路分不出个人」同一口径——没有确认过的名字时，
#: 附录里的人名照旧全部由模型推断。
SPEAKER_NAMES_EMPTY = (
    "> 机主还没有确认过任何编号的人名（这一场目录里没有这份记录，或一个名字都还没起）："
    "附录里的人名照旧**全部从上下文推断**、逐行标把握，猜不出的照旧列进「猜不出的」；"
    "某一路分不出个人时照档位说明如实写「这一路分不出个人」。"
)


def speaker_names_section(rows: Sequence[Mapping[str, object]]) -> str:
    """身份集 → `{{SPEAKER_NAMES}}` 的正文（**纯函数**，不碰盘）。

    `rows` 是 `meeting.speaker_names.identity_rows()` 那个形状（每行 `key` / `source` /
    `label` / `name` / `segments` 五格）。**只印确认过的名字**（`name` 非空的行）——表是
    机主逐条确认的记录，附录那一格照抄它、不作任何推断；行形与附录那张表对齐，模型照抄时
    不许再猜一次格。没有任何确认过的名字时渲染 `SPEAKER_NAMES_EMPTY`（如实一句，不是空
    字符串——占位符渲染完不许留下空白段落让模型猜这里本来该有什么）。
    """
    named = [row for row in rows if str(row.get("name") or "").strip()]
    if not named:
        return SPEAKER_NAMES_EMPTY
    lines = [
        "> 机主在界面上确认过的人名（下面这张表由程序照目录里的记录渲染，**照抄、不推断**；"
        "没列出的编号不在这张表里）：",
        ">",
        "> | 路 | 编号 | 人名 |",
        "> | --- | --- | --- |",
    ]
    for row in named:
        key = str(row.get("key") or "")
        source = str(row.get("source") or "")
        label = str(row.get("label") or source)
        local = key.partition(":")[2] if ":" in key else "—"
        name = str(row.get("name") or "").strip()
        lines.append(f"> | {source}（{label}） | {local} | {name} |")
    lines.extend(
        [
            ">",
            "> 写附录时：表里有的编号，人名**照抄「人名」那一列**、把握写「高」、"
            "依据写「机主在界面上确认」——这是确认不是推断，档位说明里「这一路分不出个人、"
            "不要为任何人名给出推断」说的是推断，管不到机主自己确认过的名字；"
            "表里没有的编号照旧从上下文推断、标把握，猜不出的照旧列进「猜不出的」。"
            "两路各自的编号不合并成一行。",
        ]
    )
    return "\n".join(lines)


def speaker_names_text_for_session(session_dir: str | Path) -> str:
    """从盘上取这一场的身份集与确认过的名字，拼 `{{SPEAKER_NAMES}}` 的正文。

    人名表读不出（缺席或坏了）就如实渲染 `SPEAKER_NAMES_EMPTY`，
    **不抛错、不挡渲染**（`meeting.speaker_names` 的读方口径）。
    """
    return speaker_names_section(identity_rows(session_dir))


def build_inputs_text(listing: InputsListing) -> str:
    """把「有哪几条输入、每条几行、分几块、块文件在哪」拼成一段清单。

    清单给两条读法：一段人话（一眼看清这一轮吃什么）与一份 JSON（模型与程序都能照字段读）。
    **逐份稿印一条**：同一路两条稿都在时它出现两行（会中稿一行、会后稿一行）——模型照清单
    读得到两份，不必自己去猜另一份在哪（R11 / R29 的提示词这一侧）。
    """
    lines = [
        f"本轮输入 {len(listing.sources)} 路、共 {len(listing.inputs)} 份稿，"
        "工作目录是场次目录本身，下面所有路径都相对它：",
        "- 转写文件：两份稿**平级**（会中稿 / 会后稿），没有哪一份更可信，"
        "清单里逐份写清在不在、在哪",
        f"- 分块产物：`{WORKING_DIR}/<source>/<kind>/{INDEX_NAME}` 是那一份稿的行号区间表，"
        f"块文件在 `{WORKING_DIR}/<source>/<kind>/chunks/chunk_NN.md`"
        f"（`<kind>` 取 `{'` / `'.join(KINDS)}`）",
        "",
    ]
    table = [
        "| source | 稿 | 转写文件 | 行数 | 块数 | 区间表 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in listing.inputs:
        table.append(
            f"| {item.source} | {item.label} | {item.transcript} | {item.lines}"
            f" | {item.chunk_count} | {item.index} |"
        )
    lines.extend(table)
    lines.append("")
    for item in listing.inputs:
        lines.append(
            f"`{item.source}` 的{item.label}：{item.transcript}（{item.lines} 行，"
            f"{SPEAKER_SHAPE_NOTES[item.kind]}），切了 {item.chunk_count} 块，"
            f"块文件在 `{item.chunk_dir}/`，区间表 `{item.index}`。"
        )
    lines.append("")
    lines.extend(
        [
            "同一份清单的机器读法：",
            "",
            "```json",
            json.dumps(_listing_payload(listing), ensure_ascii=False, indent=2),
            "```",
        ]
    )
    return "\n".join(lines)


def _listing_payload(listing: InputsListing) -> dict:
    """清单的 JSON 读法（逐 source 一格，稿别在 `kinds` 里逐份列出）。"""
    return {
        "session_dir": _posix(listing.session_dir),
        "mode": listing.mode,
        "source_count": len(listing.sources),
        "sources": [
            {
                "source": source,
                "kind": kind_for(listing.kinds_of(source)),
                "kinds": [
                    {
                        "kind": item.kind,
                        "label": item.label,
                        "transcript": item.transcript,
                        "lines": item.lines,
                        "chunk_count": item.chunk_count,
                        "chunk_dir": item.chunk_dir,
                        "index": item.index,
                    }
                    for item in listing.inputs
                    if item.source == source
                ],
            }
            for source in listing.sources
        ],
    }


def extract_phase_body(template_text: str, template: object, *, token: str = "{{PHASE_BODY}}") -> str:
    """从模板里抽出编排正文（两块标记之间的部分），顺手摘掉正文里的那一处 `token`。

    正文**保留** `{{SESSION_DIR}}` / `{{MAX_SEGMENTS}}` 这类占位符：调用方拿走这份正文之后
    先把它们填掉，再把整份正文放回标记块（见 `render_prompt()`）。标记缺席就抛错——
    这一格没有正文可填时硬渲染出来的提示词，会让子会话拿着一条没有阶段的编排跑一趟，
    代价是整场会的模型调用。
    """
    begin = template_text.find(PHASE_BODY_BEGIN)
    end = template_text.find(PHASE_BODY_END)
    if begin < 0 or end < 0:
        raise PromptTemplateError(
            f"模板里找不到编排正文的标记（要有 {PHASE_BODY_BEGIN} 与 {PHASE_BODY_END}）：{template}"
        )
    if end <= begin:
        raise PromptTemplateError(
            f"模板里 {PHASE_BODY_BEGIN} 与 {PHASE_BODY_END} 的次序不对：{template}"
        )
    body = template_text[begin + len(PHASE_BODY_BEGIN) : end]
    return body.replace(token, "").strip()


def render_prompt(
    listing: InputsListing,
    template_text: str,
    *,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
    template: object = "<内存里的模板>",
    speaker_names: str | None = None,
) -> str:
    """把清单与档位填进模板，返回提示词全文；缺任何一样都抛 `PromptTemplateError`。

    绝不交出半份提示词——模型会拿它去跑一条走不通的流程，而人从产物上看不出哪里缺了。
    **五处**占位符必须逐处出现（`{{MAX_SEGMENTS}}` / `{{PHASE_BODY}}` 可以省略，见
    `PHASE_BODY_BEGIN` 的注释）；某条输入的转写路径为空、`max_segments` 不是正整数同样当场拦下。
    `speaker_names` 是 `{{SPEAKER_NAMES}}` 的正文：**本函数不碰盘**（纯渲染），调用方从盘上
    取好了递进来；不传时按「没有确认过的名字」的空态正文 `SPEAKER_NAMES_EMPTY` 填。
    """
    if listing.mode not in MODES:
        raise PromptTemplateError(f"档位只认 {MODES}，收到 {listing.mode!r}")
    if not listing.inputs:
        raise PromptTemplateError("输入清单是空的：一条转写都没有时不该渲染提示词（不启动子会话）")
    if isinstance(max_segments, bool) or not isinstance(max_segments, int) or max_segments < 1:
        raise PromptTemplateError(f"max_segments 要是 ≥1 的整数，收到 {max_segments!r}")
    for item in listing.inputs:
        if not item.transcript.strip():
            raise PromptTemplateError(f"输入 {item.source!r} 的转写路径是空的")
        if not item.index.strip() or not item.chunk_dir.strip():
            raise PromptTemplateError(f"输入 {item.source!r} 的分块产物路径是空的")
    pairs = [(item.source, item.kind) for item in listing.inputs]
    if len(set(pairs)) != len(pairs):
        raise PromptTemplateError(f"输入清单里有重复的 (source, kind)：{pairs}")

    defined = {name: "{{" + name + "}}" for name in PLACEHOLDER_NAMES}
    tokens = tuple(defined.values())

    if not isinstance(template_text, str) or not template_text.strip():
        raise PromptTemplateError(f"模板是空的：{template}")

    # 对不上号的占位符要在**替换之前**从模板原文里找：编排正文自己也会用 `{{SESSION_DIR}}`
    # 这类占位符，替换之后再去扫全文，那些合法的用法会被误报成「对不上号」。
    unknown = [name for name in PLACEHOLDER_RE.findall(template_text) if name not in tokens]
    if unknown:
        raise PromptTemplateError(
            f"模板里有对不上号的占位符 {sorted(set(unknown))}（认识的是 {tokens}）：{template}"
        )
    # `{{MAX_SEGMENTS}}` 允许整处省略：分段方案不再是纪要流程的产物（`{{PHASE_BODY}}`
    # 同理，标记块就是正文的落点，见 `PHASE_BODY_BEGIN` 的注释）。
    for name in PLACEHOLDER_NAMES:
        if name in ("PHASE_BODY", "MAX_SEGMENTS"):
            continue
        if defined[name] not in template_text:
            raise PromptTemplateError(f"模板里缺占位符 {defined[name]}：{template}")

    body = extract_phase_body(template_text, template, token=defined["PHASE_BODY"])
    values = {
        "SESSION_DIR": MODEL_CWD,
        "MODE": listing.mode,
        "MODE_NOTE": mode_note_text(listing.mode, listing.kinds_per_source()),
        "INPUTS": build_inputs_text(listing),
        "MAX_SEGMENTS": str(max_segments),
        "SPEAKER_NAMES": SPEAKER_NAMES_EMPTY if speaker_names is None else speaker_names,
    }
    for name in PLACEHOLDER_NAMES:
        if name != "PHASE_BODY":
            body = body.replace(defined[name], values[name])

    begin = template_text.find(PHASE_BODY_BEGIN)
    end = template_text.find(PHASE_BODY_END)
    rendered = (
        template_text[:begin] + body + template_text[end + len(PHASE_BODY_END) :]
    )
    for name in PLACEHOLDER_NAMES:
        if name != "PHASE_BODY":
            rendered = rendered.replace(defined[name], values[name])
    # 标记块里原本那一处 token 已经随整块被换掉了；写在别处的那一处在这里换成同一份正文
    if defined["PHASE_BODY"] in template_text:
        rendered = rendered.replace(defined["PHASE_BODY"], body)
    return rendered


def render_session_prompt(
    session_dir: str | Path,
    *,
    template_path: str | Path = DEFAULT_TEMPLATE,
    mode: str | None = None,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
) -> str:
    """读盘一遍：认输入、读模板、渲染，返回提示词全文。

    `mode` 缺席时按盘上有哪几份转写自己判（`chunk.mode_for`）；**一条都没有时不渲染**——
    这一档不启动 DSH，渲染器跟着不给出提示词。
    """
    listing = listing_for_session(session_dir, mode=mode)
    template = Path(template_path)
    try:
        template_text = template.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptTemplateError(
            f"提示词模板读不动：{template}（{type(exc).__name__}: {exc}）"
        ) from exc
    return render_prompt(
        listing,
        template_text,
        max_segments=max_segments,
        template=template,
        speaker_names=speaker_names_text_for_session(session_dir),
    )


def listing_for_session(session_dir: str | Path, *, mode: str | None = None) -> InputsListing:
    """按盘上的转写与分块产物拼出一份 `InputsListing`（每条输入的读数细则见 `_prompt_inputs()`）。"""
    session = Path(session_dir)
    specs = discover_inputs(session)
    resolved = mode or mode_for(specs)
    if not resolved:
        raise PromptTemplateError(
            f"场次目录里没有任何一条转写，不渲染提示词（不启动子会话）：{session}"
        )
    inputs = tuple(item for spec in specs for item in _prompt_inputs(session, spec))
    return InputsListing(session_dir=str(session), mode=resolved, inputs=inputs)


def _prompt_inputs(session: Path, spec: InputSpec) -> tuple[PromptInput, ...]:
    """一条输入的分块产物读数：**它手上每一份稿各答一条**（顺序恒为 `live` → `offline`）。

    两份都在时交出两条 —— 清单与说明段因此逐份说清，模型读得到两份（R11 / R29）；
    某一份缺席时那一条不出现在返回值里（不编一份不在的稿）。

    块数与行数从**那一份稿自己的** `working/<source>/<kind>/00_index.md` 读
    （`- total_lines:` 与 `- chunk_count:` 两条 bullet）；区间表还没切出来时块数记 0、
    行数退回数那一份转写本身的行数 —— 刚录完、还没切块时渲染一次提示词是正常用法，
    这一档不该抛错。**某一档缺席时的退路**是这条 source 名下的旧位置（`working/<source>/`，
    两份稿只切了一套时的历史产物）：只在那一份就是这一路**唯一**在手的稿时兜一次，
    免得旧场次目录渲染不出提示词。
    """
    found: list[PromptInput] = []
    for kind, name in (("live", spec.live), ("offline", spec.offline)):
        if not name:
            continue
        base = f"{WORKING_DIR}/{spec.source}/{kind}"
        lines, chunk_count = _read_index(session / base / INDEX_NAME)
        if lines is None and _is_the_only_kind(spec):
            base = f"{WORKING_DIR}/{spec.source}"
            lines, chunk_count = _read_index(session / base / INDEX_NAME)
        if lines is None:
            transcript = session / name
            try:
                text = transcript.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise PromptTemplateError(
                    f"输入 {spec.source!r} 的{kind}转写读不动：{transcript}"
                    f"（{type(exc).__name__}: {exc}）"
                ) from exc
            lines = count_lines(text)
            chunk_count = 0
        found.append(
            PromptInput(
                source=spec.source,
                kind=kind,
                transcript=name,
                lines=lines,
                chunk_count=chunk_count or 0,
                index=f"{base}/{INDEX_NAME}",
                chunk_dir=f"{base}/{CHUNK_DIR_NAME}",
            )
        )
    if not found:
        raise PromptTemplateError(f"输入 {spec.source!r} 里没有任何一份转写文件名")
    return tuple(found)


def _is_the_only_kind(spec: InputSpec) -> bool:
    """这一路是不是只有一份稿在（只有它时才允许退回 `working/<source>/` 的旧位置）。"""
    return bool(spec.live) != bool(spec.offline)


#: 区间表里那两个数（`- total_lines: 738` / `- chunk_count: 5`）
_INDEX_LINE_RE = re.compile(r"^- total_lines: (\d+)$", re.MULTILINE)
_INDEX_COUNT_RE = re.compile(r"^- chunk_count: (\d+)$", re.MULTILINE)


def _read_index(index: Path) -> tuple[int | None, int]:
    """读区间表里的 `(总行数, 块数)`；读不出来时给 `(None, 0)`。

    **不做任何猜测**：区间表不在或那两条 bullet 缺一条，都退回「还没切块」这一档，
    由调用方去数转写本身的行数。
    """
    try:
        text = index.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, 0
    total = _INDEX_LINE_RE.search(text)
    count = _INDEX_COUNT_RE.search(text)
    if total is None or count is None:
        return None, 0
    return int(total.group(1)), int(count.group(1))


def main(argv: Sequence[str] | None = None, *, prog: str = "python -m meeting.minutes.prompt") -> int:
    """`python -m meeting.minutes.prompt <场次目录> [--mode …] [--template …]`。

    **本入口不加载 `meeting.config`**（与 `chunk` 那一支同口径）：模板路径与档位都从 argv 拿，
    免得把一个后端子命令和整条配置链捆在一起。
    """
    parser = argparse.ArgumentParser(
        prog=prog,
        description="按场次目录渲染会后纪要的编排提示词（模板正文归 prompts/meeting_final.md）。",
    )
    parser.add_argument("session_dir", help="场次目录（工作区目录，路径由人给）")
    parser.add_argument("--mode", choices=MODES, default=None, help="档位（默认按盘上的输入自己判）")
    parser.add_argument("--template", default=str(DEFAULT_TEMPLATE), help="提示词模板的路径")
    parser.add_argument("--max-segments", type=int, default=DEFAULT_MAX_SEGMENTS, help="每路分段上限")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        text = render_session_prompt(
            args.session_dir,
            template_path=args.template,
            mode=args.mode,
            max_segments=args.max_segments,
        )
    except PromptTemplateError as exc:
        print(f"提示词渲染失败：{exc}", file=sys.stderr)
        return 1
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - 进程入口
    sys.exit(main())


__all__ = [
    "DEFAULT_MAX_SEGMENTS",
    "DEFAULT_TEMPLATE",
    "INPUT_KINDS",
    "KINDS",
    "KIND_FILE_TEMPLATES",
    "KIND_LABELS",
    "MODEL_CWD",
    "PHASE_BODY_BEGIN",
    "PHASE_BODY_END",
    "PLACEHOLDER_NAMES",
    "PLACEHOLDER_RE",
    "SPEAKER_NAMES_EMPTY",
    "SPEAKER_SHAPE_NOTES",
    "InputsListing",
    "PromptInput",
    "PromptTemplateError",
    "build_inputs_text",
    "extract_phase_body",
    "kind_for",
    "listing_for_session",
    "main",
    "mode_note_text",
    "render_prompt",
    "render_session_prompt",
    "shape_of",
    "speaker_names_section",
    "speaker_names_text_for_session",
]
