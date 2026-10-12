"""按行切块 + 行号区间表（`41465462` spec「交付物形状」里的
`working/<source>/<kind>/chunks/chunk_NN.md` 与 `working/<source>/<kind>/00_index.md`）。

这个模块服务 spec 里这一条：主 agent 不读原始转写，由子 agent 分工读**块**；块与行号的对应
关系落在 `00_index.md` 的区间表里，读块的人因此随时能说出「这一句在原文件的第几行」。

**落盘路径本轮钉死一套**（V1 三种写法并存全是错的）：块在
`working/<source>/<kind>/chunks/chunk_NN.md`，区间表在 `working/<source>/<kind>/00_index.md`。

**`<kind>` 那一层是「两份稿子对拍」的承载**（`docs/design-intent.md` 第 11 条、R11）：
`live` 是会中稿（`meeting_transcript_<source>.md`）、`offline` 是会后稿
（`meeting_offline_transcript_<source>.md`）。两条都在时**两份都切、都留在盘上**，
两路永不合并、两份稿也永不合并；某一档缺席时那一档不切（不伪造空块目录）。
两路的行号空间各算各的，两份稿的行号空间同样各算各的（各自数自己那一路的行）。

三条与 V1 脚本（`split_chunks.py`，A5）不同的地方，各自都有理由：

  - **块文件里只有原始行**，不加任何头。V1 早那份会写一行 `<!-- source: … -->`，250906 那份写
    三行头，两者都让「块文件的行号」与「源文件的行号」错位；区间表已经把这件事说清楚了。
  - **先清本源的块目录再写**：重跑时会议变短会让上一次跑剩下的 `chunk_09.md` 留在盘上，
    子 agent 照 `chunk_*.md` 通配去读就会读到一段不存在的会议。V1 只做 `"w"` 覆盖，没有这一步。
  - **切块不解析行格式**：块就是原始行的搬运（与 V1 同口径）。格式解析归 `meeting.minutes.lines`，
    它服务契约校验与提示词那两处读者。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from meeting.minutes.lines import count_lines

#: 转写文件名的两个模板（上游已冻结的契约，本模块**按名字找、不猜**）。
#: 会中临时稿由 `meeting/asr/pipeline.py` 写，会后稿由 `meeting/offline/artifacts.py` 写。
LIVE_TRANSCRIPT_TEMPLATE = "meeting_transcript_{source}.md"
OFFLINE_TRANSCRIPT_TEMPLATE = "meeting_offline_transcript_{source}.md"

#: 两路的取值（spec：这一段落的 `<source>` 只有这两个值）
SOURCE_IDS: tuple[str, ...] = ("mic", "loopback")

#: 两份稿的取值（**平级**，没有任何一份更可信的排序）：`live` 会中稿 / `offline` 会后稿。
#: `working/<source>/<kind>/` 的那一层目录名就是它。
KIND_IDS: tuple[str, ...] = ("live", "offline")

#: 输入档位：两条一起 / 只有会中稿 / 只有会后稿
MODES: tuple[str, ...] = ("both", "live_only", "offline_only")

#: `working/` 下每一条输入的落点（相对场次目录）
WORKING_DIR = "working"

#: 块目录名与区间表名
CHUNK_DIR_NAME = "chunks"
INDEX_NAME = "00_index.md"

#: 块文件的通配（清理与列举都按它）
CHUNK_GLOB = "chunk_*.md"

#: 每块行数的默认值与允许区间（与 `minutes.chunk_lines` 配置键的取值域一致）
DEFAULT_CHUNK_LINES = 100
MIN_CHUNK_LINES = 10
MAX_CHUNK_LINES = 1000


class ChunkError(ValueError):
    """切块这一层自己的错误（行数不合法、转写在、读不动）。

    继承 `ValueError`，与 `meeting` 里别的「值不对」错误同档：调用方（phase2 的 runner）
    据此把这一趟记成 `failed` + 原因，读盘的人不必去猜异常类型。
    """


@dataclass(frozen=True)
class ChunkRecord:
    """一块的行号区间（表里的一行）。

    `line_start` / `line_end` 是**源文件里 1 起、闭区间**的行号；`lines` 是这一块的行数，
    三者满足 `line_end - line_start + 1 == lines`。
    """

    index: int
    file: str
    line_start: int
    line_end: int
    lines: int


@dataclass(frozen=True)
class SourceChunks:
    """**一份稿**切完之后的结果：哪一路、哪一份稿、转写文件名、总行数、每块的区间、区间表全文。"""

    source: str
    kind: str
    transcript: str
    total_lines: int
    chunk_lines: int
    chunks: tuple[ChunkRecord, ...]
    index_text: str

    def __post_init__(self) -> None:
        if self.kind not in KIND_IDS:
            raise ChunkError(f"稿别只认 {KIND_IDS}，收到 {self.kind!r}")

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    @property
    def chunk_dir(self) -> str:
        """块目录（相对本源工作目录），区间表 `- chunk_dir:` 那一格用的就是它。"""
        return f"{CHUNK_DIR_NAME}/"

    @property
    def base_dir(self) -> str:
        """这一份稿的工作目录（相对场次目录）：`working/<source>/<kind>`。"""
        return f"{WORKING_DIR}/{self.source}/{self.kind}"

    def as_dict(self) -> dict:
        """给 `json.dumps()` 用的形状（两条稿的记录各答一份，调用方按 source + kind 收）。"""
        return {
            "source": self.source,
            "kind": self.kind,
            "transcript": self.transcript,
            "total_lines": self.total_lines,
            "chunk_lines": self.chunk_lines,
            "chunk_count": self.chunk_count,
            "chunk_dir": f"{self.base_dir}/{CHUNK_DIR_NAME}",
            "index": f"{self.base_dir}/{INDEX_NAME}",
            "chunks": [
                {
                    "file": record.file,
                    "line_start": record.line_start,
                    "line_end": record.line_end,
                    "lines": record.lines,
                }
                for record in self.chunks
            ],
        }


@dataclass(frozen=True)
class InputSpec:
    """一条输入的候选转写：哪一路、会中稿叫什么、会后稿叫什么。

    文件名按上游契约算出来（`meeting_transcript_<source>.md` /
    `meeting_offline_transcript_<source>.md`），`None` 表示那个文件**不在盘上**。
    一个名字都没有的输入不会进分块流程（spec：「一条都没有」这一档不启动 DSH）。
    """

    source: str
    live: str | None
    offline: str | None

    def line_count(self, session_dir: str | Path, kind: str) -> int:
        """数这一路某一份转写的行数（`splitlines()` 口径）。

        `kind` 取 `live` / `offline`；那一份不在盘上时按 `0` 计。读不动（权限、编码之外的
        意外）不在这里吞掉：调用方要在自己的错误路径上如实报出来。
        """
        name = self.live if kind == "live" else self.offline
        if not name:
            return 0
        return count_lines(read_transcript_text(Path(session_dir) / name))


def read_transcript_text(path: str | Path) -> str:
    """读一份转写（`utf-8` + `errors="replace"`，与 V1 脚本同口径）。

    `errors="replace"` 是**有意**的：转写里混进半个坏字节时，整场会的纪要流程不该因此停摆；
    替换字符会如实出现在产物里，比一条 `UnicodeDecodeError` 更容易被人看见。
    """
    target = Path(path)
    try:
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ChunkError(f"转写读不动：{target}（{type(exc).__name__}: {exc}）") from exc


def _validate_chunk_lines(chunk_lines: int) -> int:
    """每块行数当场校验（配错的值要在切第一块之前就拦下，别切出半份产物）。"""
    if isinstance(chunk_lines, bool) or not isinstance(chunk_lines, int):
        raise ChunkError(f"chunk_lines 要是整数，收到 {chunk_lines!r}")
    if not (MIN_CHUNK_LINES <= chunk_lines <= MAX_CHUNK_LINES):
        raise ChunkError(
            f"chunk_lines 要落在 {MIN_CHUNK_LINES}-{MAX_CHUNK_LINES} 之间，收到 {chunk_lines}"
        )
    return chunk_lines


def build_index_text(
    *,
    transcript: str,
    total_lines: int,
    chunk_lines: int,
    chunks: Sequence[ChunkRecord],
) -> str:
    """拼 `00_index.md` 的全文（`# Chunk Index` + 五条 bullet + 区间表）。

    形状照 V1 的 `split_chunks.py:34-43`：读块的人先看 bullet 就知道「切的是哪一份、一共几行、
    几行一块、共几块、块在哪个目录」，再照表逐块读。
    """
    rows = [
        "# Chunk Index",
        "",
        f"- source: {transcript}",
        f"- total_lines: {total_lines}",
        f"- chunk_size: {chunk_lines}",
        f"- chunk_count: {len(chunks)}",
        f"- chunk_dir: {CHUNK_DIR_NAME}/",
        "",
        "| file | line_start | line_end | lines |",
        "| --- | --- | --- | --- |",
    ]
    for record in chunks:
        rows.append(
            f"| {CHUNK_DIR_NAME}/{record.file} | {record.line_start}"
            f" | {record.line_end} | {record.lines} |"
        )
    return "\n".join(rows) + "\n"


def chunk_source(
    source: str,
    transcript_path: str | Path,
    out_dir: str | Path,
    *,
    chunk_lines: int = DEFAULT_CHUNK_LINES,
    kind: str = "live",
) -> SourceChunks:
    """把**一份稿**切成 `out_dir/chunks/chunk_NN.md`，并把区间表写到 `out_dir/00_index.md`。

    读盘用 `errors="replace"`；块文件里**只有原始行**（`"\\n".join(块的行)` 加一个结尾换行）；
    写之前**先删掉本目录里既有的 `chunk_*.md`**（重跑时会议变短，旧块留在盘上会让子 agent 读到
    一段不存在的会议）。

    `out_dir` 由调用方给：产品路径是 `working/<source>/<kind>`（见 `chunk_session()`），
    两份稿因此各占一个目录、块文件名不会互撞。`kind` 记进返回值（`SourceChunks.kind`），
    只影响记录，不影响切法。
    """
    size = _validate_chunk_lines(chunk_lines)
    if kind not in KIND_IDS:
        raise ChunkError(f"稿别只认 {KIND_IDS}，收到 {kind!r}")
    target = Path(transcript_path)
    lines = read_transcript_text(target).splitlines()
    total = len(lines)

    out = Path(out_dir)
    chunk_dir = out / CHUNK_DIR_NAME
    chunk_dir.mkdir(parents=True, exist_ok=True)
    # 先清本目录：只动 `chunks/` 里 `chunk_*.md` 这一族，别的文件（含另一路、另一份稿的
    # 产物）一个字节不碰
    for stale in sorted(chunk_dir.glob(CHUNK_GLOB)):
        stale.unlink()

    records: list[ChunkRecord] = []
    for start in range(0, total, size):
        block = lines[start : start + size]
        name = f"chunk_{len(records) + 1:02d}.md"
        (chunk_dir / name).write_text("\n".join(block) + "\n", encoding="utf-8")
        records.append(
            ChunkRecord(
                index=len(records) + 1,
                file=name,
                line_start=start + 1,
                line_end=start + len(block),
                lines=len(block),
            )
        )

    index_text = build_index_text(
        transcript=target.name,
        total_lines=total,
        chunk_lines=size,
        chunks=records,
    )
    out.mkdir(parents=True, exist_ok=True)
    (out / INDEX_NAME).write_text(index_text, encoding="utf-8")
    return SourceChunks(
        source=source,
        kind=kind,
        transcript=target.name,
        total_lines=total,
        chunk_lines=size,
        chunks=tuple(records),
        index_text=index_text,
    )


def discover_inputs(session_dir: str | Path) -> tuple[InputSpec, ...]:
    """按名字在场次目录里找两条转写（按 `SOURCE_IDS` 的序，两路各自一条 `InputSpec`）。

    只回答「盘上有哪几份」，不判档位：档位由调用方拿着一到两条输入去定
    （一条都没有 → `none`，这条在 spec「只喂一条也退化工作」一节里定死了）。
    两路都没有的 `InputSpec` 不会出现在返回值里。
    """
    session = Path(session_dir)
    found: list[InputSpec] = []
    for source in SOURCE_IDS:
        live = LIVE_TRANSCRIPT_TEMPLATE.format(source=source)
        offline = OFFLINE_TRANSCRIPT_TEMPLATE.format(source=source)
        live_name = live if (session / live).is_file() else None
        offline_name = offline if (session / offline).is_file() else None
        if live_name is None and offline_name is None:
            continue
        found.append(InputSpec(source=source, live=live_name, offline=offline_name))
    return tuple(found)


def classify(live: bool, offline: bool) -> str:
    """这一路手上有哪几条稿的**唯一**映射（原来三处各存一份，现在都折到这里）。

    两条都在 → `both`、只有会中稿 → `live_only`、只有会后稿 → `offline_only`；两份都不在
    是**不存在的组合** → `ValueError`。三处调用方（`chunk.source_kind()` /
    `inputs.SourceEntry.kind` / `prompt.kind_for()`）把各自的输入折算成这两个布尔后调它，
    不存在的组合按各自的原行为处理（抛自己那层的错误，或像 `SourceEntry` 那样保持原答案）。
    """
    if live and offline:
        return "both"
    if live:
        return "live_only"
    if offline:
        return "offline_only"
    raise ValueError(
        f"live / offline 两份稿都不在：没有可判的稿型（live={live!r}, offline={offline!r}）"
    )


def source_kind(spec: InputSpec | Mapping[str, object]) -> str:
    """这一条输入自己有哪几条稿：`both` / `live_only` / `offline_only`。

    **按输入的语义字段判**（`InputSpec.offline` / `InputSpec.live` 在不在）：文件名里有没有
    `offline` 这个子串不是判据（`phase2` 第 0 节 M2 的定案）。两种输入形状都收，与
    `_transcript_of()` 同一套：`InputSpec` 直接读字段，`inputs.json` 的逐条映射按
    `live` / `offline` 两格在不在判。映射本体在 `classify()`（三处共用那一份）；
    两份都不在时照原话抛 `ChunkError`。
    """
    if isinstance(spec, InputSpec):
        live, offline = spec.live, spec.offline
    else:
        live, offline = spec.get("live"), spec.get("offline")
    try:
        return classify(bool(live), bool(offline))
    except ValueError as exc:
        raise ChunkError(f"输入清单里有一条说不出 source 的记录：{spec!r}") from exc


def mode_for(inputs: Sequence[InputSpec]) -> str | None:
    """这份输入清单落在哪一档（**逐 source 判、再取两路合并后的公共档位**）。

    规则（spec「只喂一条也退化工作」+ `phase2` 第 0 节 M1 的定案）：

      - 每一条输入各自判它自己有哪几条稿（`source_kind()`）；
      - 整体档位取「两路合并后的公共档位」：两路都 `both` → `both`；只有会中稿 → `live_only`；
        只有会后稿 → `offline_only`；**两路不同（一路两条都在、另一路只有一条） → `both`**
        —— 至少有一条真·离线稿在场上，不许再把整场说成降级；
      - 一条都没有 → `None`（调用方据此**不启动** DSH，状态文件记 `failed` + 原因）。

    逐 source 的那一层事实由提示词的说明段逐条写出来（`prompt.mode_note_text()`）：
    整体档位只说「整场算不算降级」，某一路缺稿这件事在那里逐 source 各写一句。
    """
    if not inputs:
        return None
    kinds = {source_kind(spec) for spec in inputs}
    if len(kinds) > 1:
        return "both"
    return next(iter(kinds))


def chunk_session(
    session_dir: str | Path,
    inputs: Iterable[InputSpec | Mapping[str, object]],
    *,
    chunk_lines: int = DEFAULT_CHUNK_LINES,
) -> dict[str, dict[str, SourceChunks]]:
    """给每一条输入**手上的每一份稿**各切一套块，返回 `{source: {kind: SourceChunks}}`。

    落盘：`<场次目录>/working/<source>/<kind>/chunks/chunk_NN.md` 与
    `<场次目录>/working/<source>/<kind>/00_index.md`（`<kind>` 取 `live` / `offline`）。
    **本函数不写 `inputs.json`**，那是 phase2 的活（它才判得出档位、才数得清两条各自的行数）。

    `inputs` 既收 `InputSpec`，也收 `inputs.json` 里那种逐条映射（`{"source": …, "live": {"file": …}}`），
    因此 phase2 可以直接把自己写下的那份清单交进来。

    **两条稿都在时两份都切、都留在盘上**（R11 / `docs/design-intent.md` 第 11 条）：
    这一层不再挑「哪一份更可信」，挑了就等于替模型下结论，而实测并不保证某一份稳定更好。
    某一份不在盘上时那一格不出现在返回值里（不建空目录、不伪造一份空块）。
    """
    session = Path(session_dir)
    working = session / WORKING_DIR
    result: dict[str, dict[str, SourceChunks]] = {}
    for spec in inputs:
        for source, kind, name in _transcripts_of(spec):
            result.setdefault(source, {})[kind] = chunk_source(
                source,
                session / name,
                working / source / kind,
                chunk_lines=chunk_lines,
                kind=kind,
            )
    return result


def _transcripts_of(spec: InputSpec | Mapping[str, object]) -> list[tuple[str, str, str]]:
    """从 `InputSpec` 或 `inputs.json` 的逐条映射里取 `[(source, kind, 转写文件名), …]`。

    **两份稿都取、一个不落**（会中稿在前、会后稿在后，与会中那条链的写法次序一致）。
    只取在盘上写着名字的那几份：某一份文件名缺席时它不出现在返回值里 —— 「切一份」与
    「这一份不在」是两件事，只有后者才该少切一套。

    `offline` 优先的旧口径（`spec.offline or spec.live`）在这里退场：它把「会后那份是权威」
    写死在代码里，而 R11 要的是两份放在一起对拍、由模型按内容判。
    """
    if isinstance(spec, InputSpec):
        source = spec.source
        found = [("live", spec.live), ("offline", spec.offline)]
    else:
        source = str(spec.get("source") or "")
        found = [("live", _name_in(spec.get("live"))), ("offline", _name_in(spec.get("offline")))]
    if not source:
        raise ChunkError(f"输入清单里有一条说不出 source 的记录：{spec!r}")
    picked = [(source, kind, str(name)) for kind, name in found if name]
    if not picked:
        raise ChunkError(f"输入 {source!r} 里没有任何一份转写文件名（live / offline 都是空）")
    return picked


def _name_in(entry: object) -> str | None:
    """从 `inputs.json` 的 `live` / `offline` 那一格里取文件名（字符串或 `{"file": …}` 两种都认）。"""
    if isinstance(entry, str):
        return entry or None
    if isinstance(entry, Mapping):
        value = entry.get("file")
        return str(value) if value else None
    return None


def main(argv: Sequence[str] | None = None, *, prog: str = "python -m meeting.minutes.chunk") -> int:
    """`python -m meeting.minutes.chunk <场次目录> [--chunk-lines N]`。

    **本入口不加载 `meeting.config`**：一个后端子命令不该把整条配置链（家目录、日志）
    捆在自己身上，用户在一场会的目录里手动切一次块时也不必先有配置。
    """
    parser = argparse.ArgumentParser(
        prog=prog,
        description="按行把场次目录里的转写切成块，并写出行号区间表。",
    )
    parser.add_argument("session_dir", help="场次目录（工作区目录，路径由人给）")
    parser.add_argument(
        "--chunk-lines",
        type=int,
        default=DEFAULT_CHUNK_LINES,
        help=f"每块行数（默认 {DEFAULT_CHUNK_LINES}）",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    session = Path(args.session_dir)
    if not session.is_dir():
        print(f"场次目录不存在：{session}", file=sys.stderr)
        return 2
    inputs = discover_inputs(session)
    if not inputs:
        print(
            f"这个场次目录里没有任何一条转写（找的是 "
            f"{LIVE_TRANSCRIPT_TEMPLATE.format(source='<source>')} 与 "
            f"{OFFLINE_TRANSCRIPT_TEMPLATE.format(source='<source>')}）：{session}",
            file=sys.stderr,
        )
        return 1
    mode = mode_for(inputs)
    try:
        chunks = chunk_session(session, inputs, chunk_lines=args.chunk_lines)
    except ChunkError as exc:
        print(f"切块失败：{exc}", file=sys.stderr)
        return 1
    for source, per_kind in chunks.items():
        for kind, report in per_kind.items():
            print(
                f"{source}/{kind}: {report.transcript} {report.total_lines} 行 → "
                f"{report.chunk_count} 块（每块 {report.chunk_lines} 行），"
                f"区间表 {WORKING_DIR}/{source}/{kind}/{INDEX_NAME}"
            )
    print(f"档位：{mode}")
    return 0


#: 直接跑 `python -m meeting.minutes.chunk` 时走这里
if __name__ == "__main__":  # pragma: no cover - 进程入口
    sys.exit(main())


__all__ = [
    "CHUNK_DIR_NAME",
    "CHUNK_GLOB",
    "DEFAULT_CHUNK_LINES",
    "INDEX_NAME",
    "KIND_IDS",
    "LIVE_TRANSCRIPT_TEMPLATE",
    "MAX_CHUNK_LINES",
    "MIN_CHUNK_LINES",
    "MODES",
    "OFFLINE_TRANSCRIPT_TEMPLATE",
    "SOURCE_IDS",
    "WORKING_DIR",
    "ChunkError",
    "ChunkRecord",
    "InputSpec",
    "SourceChunks",
    "build_index_text",
    "chunk_session",
    "chunk_source",
    "classify",
    "discover_inputs",
    "main",
    "mode_for",
    "read_transcript_text",
    "source_kind",
]
