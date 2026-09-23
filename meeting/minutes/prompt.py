"""编排提示词的渲染（`41465462` spec「可搬资产」D2 / D3 两条的渲染器一侧）。

这个模块服务 spec 里这一条：主 agent 拿到的那份提示词里，**这一轮有哪几条输入、每条几行、
分了几块、块文件在哪**必须写得清清楚楚，模型照着读就行，不必自己去找文件。五阶段的正文归
`prompts/meeting_final.md`（phase2 写），渲染器只做两件事：把路径与档位填进模板、把清单拼出来。

三条硬口径：

  - **渲染出来的路径一律正斜杠**：DSH 子会话拿到的是一段文本，里面混着反斜杠容易被当成转义
    （`\\t`、`\\n` 这类），路径里出现一个反斜杠就可能让模型读错文件；
  - **模板缺席、占位符对不上、路径为空一律抛错**：半份提示词比没有提示词更坏——模型会拿它去
    跑一条走不通的流程，而人从产物上看不出哪里缺了；
  - **降级档要写在提示词里**（`{{MODE_NOTE}}`）：只喂一条时明写「本次只有 X 稿」，
    读到这份提示词的模型因此知道这一档本来就分不出个人，附录里该如实写这句话。

模板占位符一共六个，其中**五处必须逐处写在模板里**：
`{{SESSION_DIR}}` / `{{MODE}}` / `{{MODE_NOTE}}` / `{{INPUTS}}` / `{{MAX_SEGMENTS}}`。
第六处 `{{PHASE_BODY}}` **可以省略**：五阶段正文写在两块标记（`PHASE_BODY_BEGIN` /
`PHASE_BODY_END`）之间，**标记块本身就是正文的落点**——渲染时把整块就地换成正文，
正文因此只有一份可维护，不必在别处再放一个 `{{PHASE_BODY}}`；写在别处时才填同一份正文。

`{{SESSION_DIR}}` 填的是**模型的工作目录那一点**（`.`）：提示词里给模型看的路径一律相对场次目录，
`working/inputs.json` 里那一格 `session_dir` 才是给程序读的绝对路径，两者不要混。
正文里可以照常写 `{{SESSION_DIR}}` / `{{MAX_SEGMENTS}}` 这类占位符，
它们在填进正文之前先被替换掉。

**档位与说明段都按「逐 source」口径**（`41465462` phase2 第 0 节 M1 的定案）：每一条输入各自
判它有哪几条稿（`both` / `live_only` / `offline_only`），说明段**逐 source 各写一句**它这一路
有什么；整体 `{{MODE}}` 取「两路合并后的公共档位」，两路不同时取 `both`（至少有一条真·离线稿
在，不许再把整场说成降级）。此前那一版在「一路两条都在、另一路只有会中稿」时会渲染出
自相矛盾的提示词：说明段说「本次只有会中临时稿、不要为任何人名给出推断」，同一份提示词的
清单里却写着另一路「带聚类编号」。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from meeting.minutes.chunk import (
    INDEX_NAME,
    MODES,
    WORKING_DIR,
    InputSpec,
    discover_inputs,
    mode_for,
)
from meeting.minutes.lines import count_lines

#: 模板的默认落点（仓库根下的 `prompts/meeting_final.md`，phase2 写）
DEFAULT_TEMPLATE = Path(__file__).resolve().parents[2] / "prompts" / "meeting_final.md"

#: 引擎认识的全部占位符（模板里出现这五个之外的花括号内容一律判错）
PLACEHOLDER_NAMES: tuple[str, ...] = (
    "SESSION_DIR",
    "MODE",
    "MODE_NOTE",
    "INPUTS",
    "MAX_SEGMENTS",
    "PHASE_BODY",
)

#: 五阶段正文在模板里的两块标记：渲染时把整块就地换成正文（正文的落点就是标记块本身，
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

#: 清单里每条输入印哪种稿的说明。**按输入的语义字段判、不按文件名的子串判**
#: （phase2 第 0 节 M2 的定案）：加一层目录、换一种命名都会让「名字里有没有 `offline`」
#: 这个隐式约定说错话，而 `InputSpec` 里本来就分着 `live` / `offline` 两条语义。
KIND_LABELS: dict[str, str] = {
    "live": "会中临时稿",
    "offline": "会后权威稿",
}

#: 两路各自的稿型（判逐 source 档位与列表说明用）
KINDS: tuple[str, ...] = ("live", "offline")

#: 一条输入**可能有哪几种稿**（`PromptInput.kind` 那一格的全部取值）：两条都在 / 只有会中 /
#: 只有会后。清单与说明段都按它说话，`kinds_per_source()` 的分类也落在这三个词上。
INPUT_KINDS: tuple[str, ...] = ("both", "live_only", "offline_only")

#: 稿型 → 说明段里那一句该怎么写：两条都在时把两种形态各说一句（读的人据此知道
#: 这一路的每一份稿长什么样）。
SOURCE_KIND_NOTES: dict[str, tuple[str, ...]] = {
    "both": ("live", "offline"),
    "live_only": ("live",),
    "offline_only": ("offline",),
}


class PromptTemplateError(ValueError):
    """提示词渲染不出来（模板缺席、占位符对不上、某条输入的路径是空）。

    继承 `ValueError`，与 `meeting` 里别的「值不对」错误同档：调用方（phase2 的 runner）
    据此把这一趟记成 `failed` + 原因，**不拿半份提示词去启动子会话**。
    """


@dataclass(frozen=True)
class PromptInput:
    """提示词清单里的一条输入。

    `transcript` 是**相对场次目录**的转写路径，`index` / `chunk_dir` 是分块产物的相对路径。
    路径一律正斜杠（构造时就把反斜杠换掉，见 `_posix()`）：渲染器任何一处都不该把反斜杠漏出去。

    `kind` 是这一条是**哪种稿**（`both` / `live_only` / `offline_only`），由调用方从输入的
    语义字段给出来（`InputSpec.live` / `InputSpec.offline` 两条字段在不在）。它是
    `{{MODE}}` 那一格与清单里那句说话人形态说明的**唯一依据**：清单不再去猜文件名的子串。
    """

    source: str
    transcript: str
    lines: int
    chunk_count: int
    index: str
    chunk_dir: str
    kind: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "transcript", _posix(self.transcript))
        object.__setattr__(self, "index", _posix(self.index))
        object.__setattr__(self, "chunk_dir", _posix(self.chunk_dir))
        if self.kind not in INPUT_KINDS:
            raise PromptTemplateError(f"稿型只认 {INPUT_KINDS}，收到 {self.kind!r}")


@dataclass(frozen=True)
class InputsListing:
    """渲染一份提示词需要的全部输入事实（一到两条）。"""

    session_dir: str
    mode: str
    inputs: tuple[PromptInput, ...]

    @property
    def sources(self) -> tuple[str, ...]:
        return tuple(item.source for item in self.inputs)

    def kinds_per_source(self) -> tuple[tuple[str, str], ...]:
        """逐 source 的稿型：`(("mic", "both"), ("loopback", "live_only"))`。

        每一条输入的 `kind` 本来就是这三个词之一（两条都在 / 只有会中 / 只有会后），
        这里只按出现序答一遍。**同一条 source 出现两次是渲染器的另一条错**
        （`render_prompt()` 拦），这里不做去重、只按顺序答。
        """
        return tuple((item.source, item.kind) for item in self.inputs)


def _posix(value: object) -> str:
    """路径一律正斜杠（DSH 子会话里的文本混着反斜杠容易被当成转义）。"""
    return str(value).replace("\\", "/")


def _note_sentence(source: str, kind: str) -> str:
    """说明段里**某一路**那一句（它这一路有哪几份稿、每一份长什么样）。"""
    kinds = SOURCE_KIND_NOTES[kind]
    if kind == "both":
        shape = "；".join(
            f"{KIND_LABELS[item]}（{SPEAKER_SHAPE_NOTES[item]}）" for item in kinds
        )
        return f"> `{source}` 这一路的稿：{shape}。"
    only = kinds[0]
    if kind == "live_only":
        return (
            f"> `{source}` 这一路本次只有{KIND_LABELS[only]}，{SPEAKER_SHAPE_NOTES[only]}。"
            "附录里如实写明「这一路分不出个人」，不要为任何人名给出推断。"
        )
    return (
        f"> `{source}` 这一路本次只有{KIND_LABELS[only]}，{SPEAKER_SHAPE_NOTES[only]}。"
        "会中临时稿不在这台机器上，逐块对照那一步没有第二条可对。"
    )


def mode_note_text(mode: str, per_source: Sequence[tuple[str, str]]) -> str:
    """档位说明段（`both` 档、且每一路两条都在时给空字符串：那一档没有降级要说）。

    `per_source` 是**逐 source 的稿型**（`InputsListing.kinds_per_source()` 那个形状），
    说明段因此逐 source 各写一句它这一路有哪几份稿。整体 `mode` 决定这一段说的是哪一种话：

      - `both`：两路合并后的公共档位。每一路两条都在时这一段是空的（没有降级要说）；
        某一两路缺稿时逐 source 各写一句它缺的是哪一份 —— **不许出现与清单不符的话**
        （phase2 第 0 节 M1：这个混合形状曾经渲染出自相矛盾的提示词）；
      - `live_only` / `offline_only`：整场只有哪一种稿，那句话把两路的名字一起列出来，
        再嘱咐附录如实写「这一路分不出个人」。

    这一层是**事实陈述**：写出来的每句话都要在清单里找得到依据。
    """
    if mode not in MODES:
        raise PromptTemplateError(f"档位只认 {MODES}，收到 {mode!r}")
    where = "、".join(source for source, _ in per_source) if per_source else "（没有输入）"
    if mode == "both":
        if all(kind == "both" for _, kind in per_source) and per_source:
            return ""
        return "\n".join(_note_sentence(source, kind) for source, kind in per_source)
    if mode == "live_only":
        return (
            f"> 本次只有会中临时稿（{where}），没有说话人标签。"
            "附录里如实写明「这一路分不出个人」，不要为任何人名给出推断。"
        )
    return (
        f"> 本次只有会后权威稿（{where}）。会中临时稿不在这台机器上，"
        "逐块对照那一步没有第二条可对。"
    )


def build_inputs_text(listing: InputsListing) -> str:
    """把「有哪几条输入、每条几行、分几块、块文件在哪」拼成一段清单。

    清单给两条读法：一段人话（一眼看清这一轮吃什么）与一份 JSON（模型与程序都能照字段读）。
    路径都是**相对场次目录**的，模型的工作目录就是那个场次目录。

    每条输入印两种稿里的哪一种，**按它的 `kind` 字段判**（`InputSpec` 的语义字段），
    不拿文件名的子串猜。
    """
    lines = [
        f"本轮输入 {len(listing.inputs)} 条，工作目录是场次目录本身，下面所有路径都相对它：",
        "- 转写文件：各自的名字见下表（会中稿是临时稿，会后稿是权威稿）",
        f"- 分块产物：`{WORKING_DIR}/<source>/{INDEX_NAME}` 是行号区间表，"
        f"块文件在 `{WORKING_DIR}/<source>/chunks/chunk_NN.md`",
        "",
    ]
    table = ["| source | 转写文件 | 行数 | 块数 | 区间表 |", "| --- | --- | --- | --- | --- |"]
    for item in listing.inputs:
        table.append(
            f"| {item.source} | {item.transcript} | {item.lines}"
            f" | {item.chunk_count} | {item.index} |"
        )
    lines.extend(table)
    lines.append("")
    for item in listing.inputs:
        shape = "；".join(
            f"{KIND_LABELS[kind]}，{SPEAKER_SHAPE_NOTES[kind]}"
            for kind in SOURCE_KIND_NOTES[item.kind]
        )
        lines.append(
            f"`{item.source}`：{item.transcript}（{item.lines} 行，{shape}），"
            f"切了 {item.chunk_count} 块，"
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
    """清单的 JSON 读法（字段名与 `working/inputs.json` 的逐条形状对齐）。"""
    return {
        "session_dir": _posix(listing.session_dir),
        "mode": listing.mode,
        "source_count": len(listing.inputs),
        "sources": [
            {
                "source": item.source,
                "transcript": item.transcript,
                "kind": item.kind,
                "lines": item.lines,
                "chunk_count": item.chunk_count,
                "chunk_dir": item.chunk_dir,
                "index": item.index,
            }
            for item in listing.inputs
        ],
    }


def extract_phase_body(template_text: str, template: object, *, token: str = "{{PHASE_BODY}}") -> str:
    """从模板里抽出五阶段正文（两块标记之间的部分），顺手摘掉正文里的那一处 `token`。

    正文**保留** `{{SESSION_DIR}}` / `{{MAX_SEGMENTS}}` 这类占位符：调用方拿走这份正文之后
    先把它们填掉，再把整份正文放回标记块（标记连同标记一起换掉，见 `render_prompt()`）。

    标记缺席就抛错：这一格没有正文可填时硬渲染出来的提示词会让子会话拿着一条没有阶段的编排
    跑一趟，代价是整场会的模型调用。
    """
    begin = template_text.find(PHASE_BODY_BEGIN)
    end = template_text.find(PHASE_BODY_END)
    if begin < 0 or end < 0:
        raise PromptTemplateError(
            f"模板里找不到五阶段正文的标记（要有 {PHASE_BODY_BEGIN} 与 {PHASE_BODY_END}）：{template}"
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
) -> str:
    """把清单与档位填进模板，返回提示词全文。

    缺任何一样都抛 `PromptTemplateError`，绝不交出半份提示词：**五处**占位符必须逐处出现
    （`{{PHASE_BODY}}` 可以省略，正文由两块标记就地替换，见模块头），
    某条输入的转写路径为空、`max_segments` 不是正整数同样当场拦下。
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
    sources = listing.sources
    if len(set(sources)) != len(sources):
        raise PromptTemplateError(f"输入清单里有重复的 source：{list(sources)}")

    #: 占位符的名字与它的字面量（渲染时逐个替换）
    defined = {name: "{{" + name + "}}" for name in PLACEHOLDER_NAMES}
    tokens = tuple(defined.values())

    if not isinstance(template_text, str) or not template_text.strip():
        raise PromptTemplateError(f"模板是空的：{template}")

    # 对不上号的占位符要在**替换之前**从模板原文里找：五阶段正文自己也会用 `{{SESSION_DIR}}`
    # 这类占位符，替换之后再去扫全文，那些合法的用法会被误报成「对不上号」。
    unknown = [name for name in PLACEHOLDER_RE.findall(template_text) if name not in tokens]
    if unknown:
        raise PromptTemplateError(
            f"模板里有对不上号的占位符 {sorted(set(unknown))}（认识的是 {tokens}）：{template}"
        )
    # `{{PHASE_BODY}}` 允许省略：两块标记之间的正文本身就是完整的一份正文，标记块就是它的落点。
    # 其余五处必须逐处写出来，缺一处就说明模板还没写完（省略 `{{PHASE_BODY}}` 不等于少写正文——
    # 真模板走的就是这一条，见模块头与 `prompts/meeting_final.md` 顶部那段说明）。
    for name in PLACEHOLDER_NAMES:
        if name == "PHASE_BODY":
            continue
        if defined[name] not in template_text:
            raise PromptTemplateError(f"模板里缺占位符 {defined[name]}：{template}")

    # 两块标记之间的正文是**唯一一份**正文：先把它自己用的那几个占位符填掉，再用它把整块
    # （连同两块标记）换掉。`{{PHASE_BODY}}` **可以整处不写**（标记块就是落点）；写在别处时，
    # 那一处也一并换成同一份正文。
    body = extract_phase_body(template_text, template, token=defined["PHASE_BODY"])
    values = {
        # 给模型看的那一点一律相对场次目录（见 `MODEL_CWD`）；绝对路径只出现在清单那段 JSON 里
        "SESSION_DIR": MODEL_CWD,
        "MODE": listing.mode,
        "MODE_NOTE": mode_note_text(listing.mode, listing.kinds_per_source()),
        "INPUTS": build_inputs_text(listing),
        "MAX_SEGMENTS": str(max_segments),
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

    `mode` 缺席时按盘上有哪几份转写自己判（`chunk.mode_for`）。**一条都没有时不渲染**：
    spec 明写这一档不启动 DSH，渲染器跟着不给出提示词。
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
    )


def listing_for_session(session_dir: str | Path, *, mode: str | None = None) -> InputsListing:
    """按盘上的转写与分块产物拼出一份 `InputsListing`。

    每条输入的块数与行数从 `working/<source>/00_index.md` 读（`- total_lines:` 与
    `- chunk_count:` 两条 bullet）；区间表还没切出来时块数记 0，行数退回数转写本身的行数 ——
    刚录完、还没切块时渲染一次提示词是正常用法，这一档不该抛错。
    """
    session = Path(session_dir)
    specs = discover_inputs(session)
    resolved = mode or mode_for(specs)
    if not resolved:
        raise PromptTemplateError(
            f"场次目录里没有任何一条转写，不渲染提示词（不启动子会话）：{session}"
        )
    inputs = tuple(_prompt_input(session, spec) for spec in specs)
    return InputsListing(session_dir=str(session), mode=resolved, inputs=inputs)


def _prompt_input(session: Path, spec: InputSpec) -> PromptInput:
    """一条输入的分块产物读数（转写优先取会后那份权威稿）。

    `kind` 走 `_spec_kind()`：这一路有哪几条稿由 `spec` 的语义字段回答
    （`live` / `offline` 两条字段在不在），**不拿文件名的子串猜**。
    选中的那一份转写不在盘上时抛 `PromptTemplateError`（与渲染器其它坏输入同档），
    不把裸 `OSError` 漏给调用方。
    """
    name = spec.offline or spec.live
    if not name:
        raise PromptTemplateError(f"输入 {spec.source!r} 里没有任何一份转写文件名")
    kind = _spec_kind(spec)
    index = session / WORKING_DIR / spec.source / INDEX_NAME
    lines, chunk_count = _read_index(index)
    if lines is None:
        transcript = session / name
        try:
            text = transcript.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise PromptTemplateError(
                f"输入 {spec.source!r} 的转写读不动：{transcript}"
                f"（{type(exc).__name__}: {exc}）"
            ) from exc
        lines = count_lines(text)
        chunk_count = 0
    return PromptInput(
        source=spec.source,
        transcript=name,
        lines=lines,
        chunk_count=chunk_count or 0,
        index=f"{WORKING_DIR}/{spec.source}/{INDEX_NAME}",
        chunk_dir=f"{WORKING_DIR}/{spec.source}/chunks",
        kind=kind,
    )


def _spec_kind(spec: InputSpec) -> str:
    """这一路自己有哪几条稿：`both` / `live_only` / `offline_only`。

    这里问的是**盘上两条都在不在**（`spec` 的两个字段），与 `chunk_session()` 切的是哪一份
    无关：切成会后那份权威稿是为了读，两条都在这件事本身要如实写在清单与说明段里。
    """
    if spec.live and spec.offline:
        return "both"
    return "live_only" if spec.live else "offline_only"


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
    parser.add_argument("session_dir", help="场次目录（<输出根>/YYMMDD）")
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


#: 直接跑 `python -m meeting.minutes.prompt` 时走这里
if __name__ == "__main__":  # pragma: no cover - 进程入口
    sys.exit(main())


__all__ = [
    "DEFAULT_MAX_SEGMENTS",
    "DEFAULT_TEMPLATE",
    "INPUT_KINDS",
    "KINDS",
    "KIND_LABELS",
    "MODEL_CWD",
    "PHASE_BODY_BEGIN",
    "PHASE_BODY_END",
    "PLACEHOLDER_NAMES",
    "PLACEHOLDER_RE",
    "SOURCE_KIND_NOTES",
    "SPEAKER_SHAPE_NOTES",
    "InputsListing",
    "PromptInput",
    "PromptTemplateError",
    "build_inputs_text",
    "extract_phase_body",
    "listing_for_session",
    "main",
    "mode_note_text",
    "render_prompt",
    "render_session_prompt",
]
