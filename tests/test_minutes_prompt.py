"""`meeting/minutes/prompt.py` 的契约用例：填模板、拼清单、降级档说明、路径一律正斜杠。

phase1 的那一批用例（本文件上半部分）自己造一份**形状相同**的模板（同样的七处占位符、
同样两块正文标记），把渲染器单独验穿：

  - 五阶段正文从标记之间抽出来填进 `{{PHASE_BODY}}`；
  - 清单里写着这一轮有哪几条输入、每条几行、分几块、块文件在哪，路径都相对场次目录；
  - 渲染出来的文本里**一个反斜杠都没有**（DSH 子会话拿到的是纯文本，反斜杠容易被当成转义）；
  - 模板缺席、占位符对不上、某条输入的路径是空，三档都抛错，绝不交出半份提示词。

phase2（`41465462`）往这个文件里追加了两批：

  - **模板真身的集成用例**（第七节）：`prompts/meeting_final.md` 那份真模板能被渲染器吃下，
    六处必填占位符逐处在、`{{PHASE_BODY}}` 可以省略（正文由两块标记就地替换）、
    两条固定标题逐字在、写作纪律逐条在，渲染完一个 `{{…}}` 都不剩；
  - **第 0 节那三条 minor 的回归**（第八节）：混合形状下说明段逐 source 说话、
    稿型按语义字段判（不拿文件名的子串猜）、选中的转写不在盘上时抛 `PromptTemplateError`。

修复轮（第九节）另加三条：`{{PHASE_BODY}}` 省略时正文照样是完整一份（M2 的口径）、
渲染出来的提示词里给模型看的路径**一律相对场次目录**（绝对路径只留在清单那段 JSON 的
`session_dir` 里，那是给程序读的字段）（M4）、配置级 `null` 让纪要钩子落缺席档的那条因果。

真跑修复轮（第十节）收了真跑暴露的那条主线：提示词里那几条 `python -m meeting.minutes…`
的命令从场次目录里跑不起来——阶段 1 改成「块已经切好了、照着认」，阶段 2 末尾的自查改成
逐条核对的文字单子；两条固定文字与「同步等待」那一句在**渲染产物**上另有一条复查。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from meeting.minutes.chunk import (
    INDEX_NAME,
    InputSpec,
    chunk_session,
    discover_inputs,
    mode_for,
)
from meeting.minutes import prompt as prompt_module
from meeting.minutes.prompt import (
    DEFAULT_TEMPLATE,
    KIND_LABELS,
    MODEL_CWD,
    PHASE_BODY_BEGIN,
    PHASE_BODY_END,
    PLACEHOLDER_NAMES,
    SPEAKER_NAMES_EMPTY,
    InputsListing,
    PromptInput,
    PromptTemplateError,
    SPEAKER_SHAPE_NOTES,
    build_inputs_text,
    extract_phase_body,
    listing_for_session,
    main,
    mode_note_text,
    render_prompt,
    render_session_prompt,
    speaker_names_section,
)
from meeting.offline.artifacts import (
    SPEAKERS_NAME_TEMPLATE,
    build_speakers_document,
    write_speakers,
)
from meeting.speaker_names import NAMES_FILE_NAME, set_speaker_name

#: 一份与 phase2 同形的模板（七处占位符 + 两块正文标记）
TEMPLATE = "\n".join(
    [
        "# 会后纪要编排",
        "",
        f"{PHASE_BODY_BEGIN}",
        "阶段 1 分割：跑 `python -m meeting.minutes.chunk {{SESSION_DIR}}`。",
        "阶段 2 分割方案：每路 ≤ {{MAX_SEGMENTS}} 段，写 working/segmentation.json。",
        "阶段 3 逐段总结：每段一个 sub agent。",
        "阶段 4 合并：写会议纪要正文 + 说话人附录。",
        "机主确认过的人名：{{SPEAKER_NAMES}}",
        "阶段 5 只读检查：幻觉与一致性两个透镜。",
        f"{PHASE_BODY_END}",
        "",
        "本次档位：{{MODE}}",
        "{{MODE_NOTE}}",
        "",
        "## 本轮的输入",
        "",
        "{{INPUTS}}",
        "",
        "## 编排正文",
        "",
        "{{PHASE_BODY}}",
    ]
)


def _listing(*inputs: PromptInput, mode: str = "both", session: str = "C:/out/250919") -> InputsListing:
    return InputsListing(session_dir=session, mode=mode, inputs=inputs)


def _input(source: str = "loopback", kind: str = "offline", **overrides) -> PromptInput:
    """清单里的一条：**某一路的某一份稿**（默认 `loopback` 的会后稿，与旧用例同形）。"""
    name = {
        "live": f"meeting_transcript_{source}.md",
        "offline": f"meeting_offline_transcript_{source}.md",
    }.get(kind, f"meeting_{kind}_transcript_{source}.md")
    values = {
        "source": source,
        "kind": kind,
        "transcript": name,
        "lines": 738,
        "chunk_count": 8,
        "index": f"working/{source}/{kind}/{INDEX_NAME}",
        "chunk_dir": f"working/{source}/{kind}/chunks",
    }
    values.update(overrides)
    return PromptInput(**values)


def _per_source(*pairs: tuple[str, str]) -> tuple[tuple[str, str], ...]:
    """`mode_note_text()` 收的那一 shape：`(("mic", "both"), …)`。"""
    return tuple(pairs)


def _write_session(tmp_path: Path, *, live: bool = True, offline: bool = True) -> Path:
    """造一个场次目录（两条都在 / 只有一条），并真的切一次块。"""
    session = tmp_path / "250919"
    session.mkdir(parents=True, exist_ok=True)
    if live:
        (session / "meeting_transcript_loopback.md").write_text(
            "\n".join(f"[21:{minute:02d}] 会中第 {minute} 句。" for minute in range(15)) + "\n",
            encoding="utf-8",
        )
    if offline:
        (session / "meeting_offline_transcript_loopback.md").write_text(
            "\n".join(f"[21:{minute:02d}] **S01**: 会后第 {minute} 句。" for minute in range(12)) + "\n",
            encoding="utf-8",
        )
    chunk_session(session, discover_inputs(session), chunk_lines=10)
    return session


def _write_speakers_artifact(session: Path, source: str, locals: list[str]) -> Path:
    """给这一路落一份对照表（真实写方 + 真实字段契约）：人名节点读编号就从它来。"""
    document = build_speakers_document(
        session_id=session.name,
        source=source,
        engine={"name": "fake-engine"},
        audio={"seconds": 60.0},
        segments=[],
        speakers=[
            {
                "id": f"{source}:{local}",
                "local": local,
                "label_raw": f"speaker#{local}",
                "segments": 3,
                "seconds": 12.0,
            }
            for local in locals
        ],
    )
    return write_speakers(session / SPEAKERS_NAME_TEMPLATE.format(source=source), document)


def _write_mixed_session(tmp_path: Path) -> Path:
    """造一个**混合形状**的场次目录：`mic` 两条稿都在、`loopback` 只有会中稿。

    这个形状 spec 的三档词表没单列（`both` / `live_only` / `offline_only` 假设整场一致），
    但盘上真的会出现（离线那一趟可能只跑成了一路）。phase2 第 0 节的定案：
    整体档位取两路合并后的公共档位（这里落 `both`），说明段**逐 source 各写一句**。
    """
    session = tmp_path / "250919"
    session.mkdir(parents=True, exist_ok=True)
    (session / "meeting_transcript_mic.md").write_text(
        "\n".join(f"[21:{minute:02d}] mic 会中第 {minute} 句。" for minute in range(8)) + "\n",
        encoding="utf-8",
    )
    (session / "meeting_offline_transcript_mic.md").write_text(
        "\n".join(f"[21:{minute:02d}] **S01**: mic 会后第 {minute} 句。" for minute in range(6)) + "\n",
        encoding="utf-8",
    )
    (session / "meeting_transcript_loopback.md").write_text(
        "\n".join(f"[21:{minute:02d}] loopback 会中第 {minute} 句。" for minute in range(5)) + "\n",
        encoding="utf-8",
    )
    chunk_session(session, discover_inputs(session), chunk_lines=10)
    return session


# ---------------------------------------------------------------------------
# 一、填模板
# ---------------------------------------------------------------------------


def test_render_fills_every_placeholder_and_no_braces_remain() -> None:
    """七处占位符全被填掉，渲染完的文本里没有剩下的 `{{…}}`。"""
    text = render_prompt(_listing(_input()), TEMPLATE)

    assert "{{" not in text and "}}" not in text
    assert "本次档位：both" in text
    assert "每路 ≤ 8 段" in text
    assert "阶段 5 只读检查" in text


def test_phase_body_lives_in_the_marker_block() -> None:
    """五阶段正文写在标记块里，那一块在渲染时就地换成正文。

    这份测试模板把正文印两处（标记块里一份、末尾 `## 编排正文` 下一处 `{{PHASE_BODY}}`），
    渲染之后份数不变：正文没有被复制成第三份。
    """
    text = render_prompt(_listing(_input()), TEMPLATE)

    # 标记是给作者用的两块路标，正文落定之后它们就退场了
    assert PHASE_BODY_BEGIN not in text and PHASE_BODY_END not in text
    assert "{{PHASE_BODY}}" not in text
    assert text.count("阶段 3 逐段总结") == 2
    body = extract_phase_body(TEMPLATE, "t.md")
    assert body.startswith("阶段 1 分割")
    assert "{{PHASE_BODY}}" not in body


def test_a_phase_body_token_left_in_the_body_does_not_print_itself() -> None:
    """正文里再写一处 `{{PHASE_BODY}}` 时那一处被摘掉，产物里印不出字面的 token。"""
    doubled = TEMPLATE.replace(
        "阶段 3 逐段总结：每段一个 sub agent。",
        "阶段 3 逐段总结：每段一个 sub agent。\n{{PHASE_BODY}}",
        1,
    )

    text = render_prompt(_listing(_input()), doubled)

    assert "{{PHASE_BODY}}" not in text
    assert "{{PHASE_BODY}}" not in extract_phase_body(doubled, "t.md")
    # 份数与未改动时相同：那一处 token 被摘掉，正文没有因此多印一份
    assert text.count("阶段 3 逐段总结") == 2


def test_a_phase_body_token_outside_the_markers_gets_the_same_body() -> None:
    """`{{PHASE_BODY}}` 写在标记块之外时，那一处也填同一份正文。"""
    moved = TEMPLATE.replace("{{PHASE_BODY}}", "").replace(
        "## 编排正文\n", "## 编排正文\n\n{{PHASE_BODY}}\n"
    )

    text = render_prompt(_listing(_input()), moved)

    assert "{{PHASE_BODY}}" not in text
    assert text.count("阶段 5 只读检查") == 2


def test_extract_phase_body_needs_both_markers() -> None:
    """两块标记缺一块就抛错（没有正文可填时不许硬渲染）。"""
    with pytest.raises(PromptTemplateError, match="找不到五阶段正文的标记"):
        extract_phase_body("没有任何标记", "t.md")
    with pytest.raises(PromptTemplateError, match="次序不对"):
        extract_phase_body(f"{PHASE_BODY_END}\n正文\n{PHASE_BODY_BEGIN}", "t.md")


def test_render_rejects_a_template_missing_a_placeholder() -> None:
    """模板缺一处占位符 → 抛错，消息里点出缺的是哪一个。"""
    short = TEMPLATE.replace("{{INPUTS}}", "（这一轮没有清单）")

    with pytest.raises(PromptTemplateError, match=r"缺占位符 \{\{INPUTS\}\}"):
        render_prompt(_listing(_input()), short)


def test_render_rejects_an_unknown_placeholder() -> None:
    """模板里多出对不上号的占位符 → 抛错（渲染出半份提示词比不渲染更坏）。"""
    extra = TEMPLATE + "\n{{CHUNK_SIZE}}\n"

    with pytest.raises(PromptTemplateError, match="对不上号的占位符"):
        render_prompt(_listing(_input()), extra)


def test_render_rejects_an_empty_template_text() -> None:
    """模板文本是空的 → 抛错。"""
    with pytest.raises(PromptTemplateError, match="模板是空的"):
        render_prompt(_listing(_input()), "   ")


def test_placeholder_names_are_the_six_plus_phase_body() -> None:
    """认识的名字就是这七个（真模板按它填；`{{SPEAKER_NAMES}}` 是人名节点 91b6752e 加的）。"""
    assert PLACEHOLDER_NAMES == (
        "SESSION_DIR",
        "MODE",
        "MODE_NOTE",
        "INPUTS",
        "MAX_SEGMENTS",
        "SPEAKER_NAMES",
        "PHASE_BODY",
    )


def test_render_rejects_an_empty_input_list() -> None:
    """一条输入都没有 → 抛错（spec：这一档不启动子会话，渲染器跟着不给出提示词）。"""
    with pytest.raises(PromptTemplateError, match="输入清单是空的"):
        render_prompt(_listing(), TEMPLATE)


def test_render_rejects_an_empty_transcript_path() -> None:
    """某条输入的转写路径是空 → 抛错。"""
    with pytest.raises(PromptTemplateError, match="转写路径是空的"):
        render_prompt(_listing(_input(transcript="   ")), TEMPLATE)


def test_render_rejects_an_empty_chunk_path() -> None:
    """分块产物的路径是空 → 抛错。"""
    with pytest.raises(PromptTemplateError, match="分块产物路径是空的"):
        render_prompt(_listing(_input(index="")), TEMPLATE)


def test_render_rejects_a_bad_mode() -> None:
    """档位只认三个词。"""
    with pytest.raises(PromptTemplateError, match="档位只认"):
        render_prompt(_listing(_input(), mode="single"), TEMPLATE)


def test_render_rejects_a_bad_max_segments() -> None:
    """`max_segments` 要是 ≥1 的整数。"""
    with pytest.raises(PromptTemplateError, match="max_segments"):
        render_prompt(_listing(_input()), TEMPLATE, max_segments=0)


def test_render_rejects_a_duplicated_source_and_kind() -> None:
    """同一路同一份稿在清单里出现两次 → 抛错（两份稿本身各占一条，那是合法的）。"""
    with pytest.raises(PromptTemplateError, match="重复的"):
        render_prompt(_listing(_input(), _input()), TEMPLATE)


def test_render_accepts_both_kinds_of_one_source() -> None:
    """同一路的两份稿各占一条 —— 这是本轮的正常形状（两份平级、一起喂）。"""
    text = render_prompt(
        _listing(_input("loopback", "live"), _input("loopback", "offline")), TEMPLATE
    )

    assert "meeting_transcript_loopback.md" in text
    assert "meeting_offline_transcript_loopback.md" in text


# ---------------------------------------------------------------------------
# 二、清单与降级档说明
# ---------------------------------------------------------------------------


def test_listing_says_where_the_chunks_are() -> None:
    """清单写着每条输入几行、几块、块文件在哪（相对场次目录的路径）。"""
    text = build_inputs_text(_listing(_input()))

    assert "本轮输入 1 路、共 1 份稿" in text
    assert "| loopback | 会后稿 | meeting_offline_transcript_loopback.md | 738 | 8 |" in text
    assert "`working/loopback/offline/chunks/`" in text
    assert "`working/loopback/offline/00_index.md`" in text
    assert '"chunk_count": 8' in text


def test_listing_lists_both_kinds_of_one_source() -> None:
    """一路两份都在时清单里**两份各印一条**（模型照清单读得到两份）。"""
    text = build_inputs_text(_listing(_input("mic", "live"), _input("mic", "offline")))

    assert "| mic | 会中稿 | meeting_transcript_mic.md | 738 | 8 |" in text
    assert "| mic | 会后稿 | meeting_offline_transcript_mic.md | 738 | 8 |" in text
    assert "本轮输入 1 路、共 2 份稿" in text


def test_listing_carries_the_speaker_shape_of_each_kind() -> None:
    """两条一起时清单点明哪一份带编号、哪一份没有（这是本流程的输入事实）。"""
    text = build_inputs_text(
        _listing(
            _input("loopback", "live", chunk_count=2),
            _input("mic", "offline"),
        )
    )

    assert "带聚类编号" in text
    assert "无说话人标签" in text


def test_listing_names_the_kind_from_the_semantic_field() -> None:
    """清单印哪一份稿的说明**按 `kind` 字段**，与文件名叫什么无关（M2 的回归）。"""
    text = build_inputs_text(
        _listing(
            _input("loopback", "live", transcript="some/deep/meeting_transcript_loopback.md"),
            _input("mic", "offline"),
        )
    )

    assert KIND_LABELS["live"] in text and KIND_LABELS["offline"] in text
    assert SPEAKER_SHAPE_NOTES["live"] in text
    assert SPEAKER_SHAPE_NOTES["offline"] in text
    assert "权威" not in text


def test_a_both_kind_input_names_both_shapes() -> None:
    """一路两条都在时，清单那一行把两种稿的形态各说一句。"""
    text = build_inputs_text(_listing(_input("mic", "live"), _input("mic", "offline")))

    assert SPEAKER_SHAPE_NOTES["live"] in text
    assert SPEAKER_SHAPE_NOTES["offline"] in text


def test_both_mode_names_what_this_session_holds() -> None:
    """`both` 档且每一路两份都在时，说明段逐条写清这一场手上有哪几份（复审 m9）。

    改之前这一档返回空串、提示词里那一格是空白行 —— spec 完成判定第 3 条要的是
    「三种输入各渲染一次提示词，说明段逐条写清这一场手上有哪几份」，因此这一档同样要有字。
    """
    note = mode_note_text("both", _per_source(("loopback", "both"), ("mic", "both")))

    assert note.strip(), "「两份都全」这一档的说明段不该是空的"
    assert "loopback" in note and "mic" in note, note
    assert SPEAKER_SHAPE_NOTES["live"] in note and SPEAKER_SHAPE_NOTES["offline"] in note, note
    assert "平级" in note and "不合并" in note, note
    assert "权威" not in note and "更可信" not in note


def test_both_mode_note_reaches_the_rendered_prompt() -> None:
    """那一句真的进了渲染出来的提示词（`{{MODE_NOTE}}` 那一格不再留空白行）。"""
    text = render_prompt(
        _listing(_input("mic", "live"), _input("mic", "offline"), mode="both"),
        TEMPLATE,
    )

    assert "本次这两路两份都在" in text, text
    note_line = next(
        line for line in text.splitlines() if "本次这两路两份都在" in line
    )
    assert note_line.strip() != "", "这一格不许是空白行"
    assert "本次档位：both" in text


def test_live_only_note_says_only_the_live_transcript_is_here() -> None:
    """`live_only` 档的说明段明写「只有会中稿」，并嘱咐附录如实写分不出个人。"""
    note = mode_note_text("live_only", _per_source(("loopback", "live_only")))

    assert "只有会中稿" in note
    assert "分不出个人" in note
    assert "权威" not in note


def test_offline_only_note_says_only_the_offline_transcript_is_here() -> None:
    """`offline_only` 档的说明段明写「只有会后稿」。"""
    note = mode_note_text("offline_only", _per_source(("mic", "offline_only")))

    assert "只有会后稿" in note
    assert "分不出个人" in note
    assert "权威" not in note


def test_mode_note_is_rendered_into_the_prompt() -> None:
    """降级档的说明段真的进了提示词（不是只写在函数里）。"""
    text = render_prompt(
        _listing(_input("loopback", "live"), mode="live_only"),
        TEMPLATE,
    )

    assert "只有会中稿" in text
    assert "本次档位：live_only" in text


def test_mode_note_rejects_an_unknown_mode() -> None:
    """档位认不出时说明段同样当场抛错。"""
    with pytest.raises(PromptTemplateError, match="档位只认"):
        mode_note_text("single", _per_source(("loopback", "both")))


# ---------------------------------------------------------------------------
# 三、路径一律正斜杠
# ---------------------------------------------------------------------------


def test_the_legacy_working_dir_index_is_the_fallback_for_a_single_kind(tmp_path: Path) -> None:
    """只切过旧位置（`working/<source>/00_index.md`）时，读数与区间表都指回那里（复审 m5）。

    这是 report ④.7 特意保留的**唯一**一条向后兼容路径：老场次目录的块还停在
    `working/<source>/` 时，提示词仍要渲染得出来。
    """
    session = _write_session(tmp_path, live=True, offline=False)
    live_dir = session / "working" / "loopback" / "live"
    legacy_dir = session / "working" / "loopback"
    assert live_dir.is_dir(), "先确认新位置那一套真的切出来了"
    for item in list(live_dir.iterdir()):
        item.rename(legacy_dir / item.name)
    live_dir.rmdir()

    listing = listing_for_session(session)
    item = listing.inputs[0]

    assert item.kind == "live", item
    assert item.index == f"working/loopback/{INDEX_NAME}", item.index
    assert item.chunk_dir == "working/loopback/chunks", item.chunk_dir
    assert item.lines == 15, item.lines
    assert item.chunk_count == 2, item.chunk_count


def test_the_legacy_index_is_refused_when_two_kinds_are_both_here(tmp_path: Path) -> None:
    """同一路两份稿都在时**不许**退回旧位置（复审 m5 的第二档）。

    `_is_the_only_kind()` 为假的那一档退回旧位置，会把「两条稿各自的块」又混成一套：
    这里两份稿的块都挪到旧位置之后，读数该退回「还没切块」（块数 0、行数数转写本身），
    区间表仍指向新位置 —— 两条稿各自的块因此各归各。
    """
    session = _write_session(tmp_path, live=True, offline=True)
    for kind in ("live", "offline"):
        kind_dir = session / "working" / "loopback" / kind
        legacy_dir = session / "working" / "loopback"
        for item in list(kind_dir.iterdir()):
            item.rename(legacy_dir / f"{kind}-{item.name}")
        kind_dir.rmdir()

    listing = listing_for_session(session)
    by_kind = {item.kind: item for item in listing.inputs}

    assert set(by_kind) == {"live", "offline"}, by_kind
    for kind, item in by_kind.items():
        assert item.index == f"working/loopback/{kind}/{INDEX_NAME}", item.index
        assert item.chunk_count == 0, (kind, item.chunk_count)
    # 行数退回数那一份转写本身（区间表读不出来那一档）
    assert by_kind["live"].lines == 15, by_kind["live"].lines
    assert by_kind["offline"].lines == 12, by_kind["offline"].lines


def test_windows_paths_come_out_with_forward_slashes(tmp_path: Path) -> None:
    """Windows 形状的场次目录渲染出来没有反斜杠（位号也是正斜杠）。"""
    session = _write_session(tmp_path)

    text = render_prompt(listing_for_session(session), TEMPLATE)

    assert "\\" not in text, "渲染结果里出现了反斜杠"
    assert str(session).replace("\\", "/") in text
    assert f"working/loopback/offline/{INDEX_NAME}" in text


def test_prompt_input_normalises_backslashes_at_construction() -> None:
    """`PromptInput` 收下反斜杠路径时当场换成正斜杠（漏不出去）。"""
    item = _input(
        transcript=r"working\meeting_offline_transcript_loopback.md",
        index=r"working\loopback\offline\00_index.md",
        chunk_dir=r"working\loopback\offline\chunks",
    )

    assert "\\" not in (item.transcript + item.index + item.chunk_dir)


# ---------------------------------------------------------------------------
# 四、读盘一遍：认输入、读模板
# ---------------------------------------------------------------------------


def test_listing_for_session_reads_the_index_and_the_lines(tmp_path: Path) -> None:
    """两份稿都在时**清单里两条各答一条**（行数与块数取各自那份的重量）。"""
    session = _write_session(tmp_path)

    listing = listing_for_session(session)

    assert listing.mode == "both"
    assert [(item.kind, item.transcript, item.lines, item.chunk_count) for item in listing.inputs] == [
        ("live", "meeting_transcript_loopback.md", 15, 2),
        ("offline", "meeting_offline_transcript_loopback.md", 12, 2),
    ]
    assert listing.kinds_per_source() == (("loopback", "both"),)


def test_listing_for_session_falls_back_before_chunking(tmp_path: Path) -> None:
    """还没切块时：块数记 0、行数退回数转写本身（刚录完就渲染一次是正常用法）。"""
    session = tmp_path / "250919"
    session.mkdir()
    (session / "meeting_transcript_loopback.md").write_text(
        "\n".join(f"[21:{minute:02d}] 第 {minute} 句。" for minute in range(15)) + "\n",
        encoding="utf-8",
    )

    listing = listing_for_session(session)

    assert listing.mode == "live_only"
    assert (listing.inputs[0].lines, listing.inputs[0].chunk_count) == (15, 0)


def test_listing_for_session_accepts_an_explicit_mode(tmp_path: Path) -> None:
    """档位可以由调用方指定（`live_only` 那一档要能显式压上去）。"""
    session = _write_session(tmp_path)

    assert listing_for_session(session, mode="live_only").mode == "live_only"


def test_listing_for_session_rejects_a_session_without_inputs(tmp_path: Path) -> None:
    """一条转写都没有时不渲染（spec：这一档记 failed，不启动 DSH）。"""
    session = tmp_path / "250919"
    session.mkdir()

    with pytest.raises(PromptTemplateError, match="没有任何一条转写"):
        listing_for_session(session)


def test_render_session_prompt_reads_the_template_from_disk(tmp_path: Path) -> None:
    """模板从盘上读（phase2 的 `prompts/meeting_final.md` 就是这么进去的）。"""
    session = _write_session(tmp_path)
    template = tmp_path / "meeting_final.md"
    template.write_text(TEMPLATE, encoding="utf-8")

    text = render_session_prompt(session, template_path=template)

    assert "本次档位：both" in text
    assert "阶段 1 分割" in text


def test_render_session_prompt_reports_a_missing_template(tmp_path: Path) -> None:
    """模板不在 → 抛错，消息里带着那个路径。"""
    session = _write_session(tmp_path)

    with pytest.raises(PromptTemplateError, match="提示词模板读不动"):
        render_session_prompt(session, template_path=tmp_path / "nowhere" / "meeting_final.md")


def test_the_default_template_lives_in_the_repo_prompts_dir() -> None:
    """默认模板的落点是仓库根下的 `prompts/meeting_final.md`（phase2 写）。"""
    assert DEFAULT_TEMPLATE.parent.name == "prompts"
    assert DEFAULT_TEMPLATE.name == "meeting_final.md"


def test_main_prints_the_prompt(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """命令行入口：把渲染出来的提示词打到 stdout。"""
    session = _write_session(tmp_path)
    template = tmp_path / "t.md"
    template.write_text(TEMPLATE, encoding="utf-8")

    code = main([str(session), "--template", str(template)])

    assert code == 0
    assert "本次档位：both" in capsys.readouterr().out


def test_main_reports_a_missing_template(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """模板缺席时退 1，错误打在 stderr 上。"""
    session = _write_session(tmp_path)

    code = main([str(session), "--template", str(tmp_path / "nowhere.md")])

    assert code == 1
    assert "提示词渲染失败" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 七、模板真身（`prompts/meeting_final.md`）
#
# 前六节的模板都是本文件自己造的；这一节拿**要上生产的那一份**喂同一个渲染器：
# 占位符对不上、正文标记写错、两条固定标题被改动，都在这里当场露出来。
# ---------------------------------------------------------------------------


def test_the_real_template_names_every_placeholder_the_renderer_knows() -> None:
    """真模板里那六处必须逐处出现的占位符都在，正文标记也齐。

    第七处 `{{PHASE_BODY}}` 可以写在两块标记之间（渲染时就地换成正文），也可以整处不写
    —— 标记块里的正文本身就是完整的一份正文。真模板走的是后一条：标记齐、正文在标记之间。
    """
    text = DEFAULT_TEMPLATE.read_text(encoding="utf-8")

    for name in PLACEHOLDER_NAMES:
        if name == "PHASE_BODY":
            continue
        assert "{{" + name + "}}" in text, f"真模板里缺占位符 {{{{{name}}}}}"
    assert PHASE_BODY_BEGIN in text and PHASE_BODY_END in text
    assert extract_phase_body(text, DEFAULT_TEMPLATE).strip()


def test_the_real_template_keeps_the_phase_body_between_the_markers() -> None:
    """五阶段正文逐条写在两块标记之间（标记缺席或正文跑出标记块都当场抛错）。"""
    text = DEFAULT_TEMPLATE.read_text(encoding="utf-8")
    body = extract_phase_body(text, DEFAULT_TEMPLATE)

    assert text.index(PHASE_BODY_BEGIN) < text.index(PHASE_BODY_END)
    assert body.count(PHASE_BODY_BEGIN) == 0 and body.count(PHASE_BODY_END) == 0
    for phase in (
        "阶段 1：分割",
        "阶段 2：分割方案",
        "阶段 3：逐段总结",
        "阶段 4：合并",
        "阶段 5：只读检查",
    ):
        assert phase in body, phase


def test_the_real_template_renders_for_both_a_full_and_a_degraded_session(
    tmp_path: Path,
) -> None:
    """真模板在两个场次上真渲染一遍：两条都在、只有会中稿，两份提示词都完整。"""
    both = _write_session(tmp_path / "both")
    live_only = _write_session(tmp_path / "live_only", offline=False)

    full = render_session_prompt(both)
    degraded = render_session_prompt(live_only)

    for text in (full, degraded):
        assert "{{" not in text and "}}" not in text
        assert "## 说话人附录" in text
        assert "### 猜不出的" in text
        assert "meeting.minutes.chunk" in text
        assert "meeting_final.md" in text
        assert "\\" not in text, "渲染结果里出现了反斜杠"
    assert "本次档位：`both`" in full
    assert "本次档位：`live_only`" in degraded
    assert "只有会中稿" in degraded
    assert "权威" not in full and "权威" not in degraded


def test_the_real_template_renders_a_mixed_shape_without_contradicting_itself(
    tmp_path: Path,
) -> None:
    """混合形状（一路两份都在、一路只有会中稿）也渲染得出来，说明段逐 source 说话。"""
    session = _write_mixed_session(tmp_path)

    text = render_session_prompt(session)

    assert "{{" not in text
    assert "本次档位：`both`" in text
    # 整场那句降级话不许出现（至少有一份真·会后稿在场上）
    assert "本次两路都只有" not in text
    assert text.count("> `mic` 这一路的稿：") == 1
    assert text.count("> `loopback` 这一路本次只有") == 1
    # 两份稿都在的那一路：清单里两份各印一条
    assert "| mic | 会中稿 | meeting_transcript_mic.md |" in text
    assert "| mic | 会后稿 | meeting_offline_transcript_mic.md |" in text


def test_the_real_template_carries_the_three_materials_rules(tmp_path: Path) -> None:
    """资料口径的三处新文字逐处在场、各落各的阶段，有料与没料的场次是同一份模板。

    三处口径（节点 spec 的定案）：逐段总结把相关资料读进来、当会议内容融进总结
    （文件修改时间是时机参考）；合并扫一眼资料、把段总结漏掉的补进正文；只读检查对资料核
    （转写块里找不到不算幻觉）。纪要正文形状零变化：固定骨架一格不改、没有「参考资料」一节。
    """
    with_materials = _write_session(tmp_path / "with")
    (with_materials / "meeting_materials").mkdir()
    (with_materials / "meeting_materials" / "需求草稿.md").write_text(
        "# 需求草稿\n\n会议要讨论的方案 v2，三条结论都写在这里。\n", encoding="utf-8"
    )
    without_materials = _write_session(tmp_path / "without")

    rich = render_session_prompt(with_materials)
    plain = render_session_prompt(without_materials)

    for text in (rich, plain):
        # 三处口径与公共说明都是固定文案：目录不在也照写（有就多看一眼，没有就跳过）
        assert "meeting_materials" in text
        assert "没有就整段跳过" in text
        assert "读进来、当这场会的内容" in text
        assert "文件修改时间是时机参考、不是必要条件" in text
        assert "扫一眼资料" in text
        assert "不算幻觉" in text
        # 正文形状零变化：固定骨架的标题逐个在，没有「参考资料」一节
        assert "## 会议概要" in text
        assert "## 一、核心共识与结论" in text
        assert "## 二、遗留问题与待办" in text
        assert "## 花絮" in text
        assert "## 参考资料" not in text
        assert "{{" not in text and "}}" not in text
        assert "\\" not in text
    # 三处口径各落各的阶段（管道概览图里没有这些词，按阶段标题切开来查）
    phase3 = rich.split("### 阶段 3", 1)[1].split("### 阶段 4", 1)[0]
    phase4 = rich.split("### 阶段 4", 1)[1].split("### 阶段 5", 1)[0]
    phase5 = rich.split("### 阶段 5", 1)[1].split("## 质量目标", 1)[0]
    assert "读进来、当这场会的内容" in phase3
    assert "文件修改时间是时机参考、不是必要条件" in phase3
    assert "扫一眼资料" in phase4
    assert "不算幻觉" in phase5


def test_the_real_template_carries_the_two_fixed_sections_verbatim() -> None:
    """两条固定标题与附录表头在模板里**逐字**在（启动器的产物判据按它们判）。"""
    text = DEFAULT_TEMPLATE.read_text(encoding="utf-8")
    skeleton = "\n".join(
        [
            "## 说话人附录",
            "",
            "| 路 | 编号 | 人名 | 把握 | 依据 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    # 模板把这一段写在引用块里（`> ` 前缀）：逐行摘掉前缀再比对
    flattened = "\n".join(line[2:] if line.startswith("> ") else line.lstrip(">")
                          for line in text.splitlines())

    # 两串标题各出现两次：一次在阶段 4 的指令里（写成产物该有的样子），一次在提醒句里
    assert text.count("## 说话人附录") == 2
    assert text.count("### 猜不出的") == 2
    assert skeleton in flattened, "附录那张表的表头与分隔行要逐字在"
    # 表头之后是数据行（样例），再空一行接「猜不出的」那一节
    tail = flattened[flattened.index(skeleton) + len(skeleton):].lstrip("\n")
    assert tail.splitlines()[0].startswith("| loopback |"), tail[:120]
    assert "\n\n### 猜不出的\n" in tail


def test_the_real_template_keeps_the_synchronous_waiting_rule_verbatim() -> None:
    """同步等待那一条纪律逐字在，而且它**不绑分支**。

    早先这一句写的是「本次运行是一次性 headless 会话…不会有后续唤醒」——web 分支（交付给
    DSH web 服务端那条会话）里那句话不成立，因此改成与分支无关的写法：无论跑在一次性
    headless 子进程里，还是 DSH GUI 里那条会话里，都要同步等所有 sub agent 交回。
    """
    text = DEFAULT_TEMPLATE.read_text(encoding="utf-8")
    sentence = (
        "注意：无论这一次跑在哪一种会话里（一次性的 headless 子进程，还是 DSH GUI 里那条会话），"
        "都要**同步等所有 sub agent 交回**再进下一阶段——绝不把活挂在后台就结束回合；"
        "全部 segment 汇总并写出 meeting_final.md 后才算完成。"
    )

    assert sentence in text
    assert "你的回合结束后进程即退出" not in text, "headless 那个因果不许再写死在模板里"


def test_the_real_template_writes_down_the_reconciliation_rule() -> None:
    """核对口径（R12）写在模板里：不做预先对齐、两份稿同时读、按内容判哪边可信。"""
    text = DEFAULT_TEMPLATE.read_text(encoding="utf-8")

    assert "按内容判哪一边可信" in text
    assert "不做预先的文本对齐" in text
    assert "核对结果" in text
    assert "两份稿都读" in text or "两份稿同时读" in text
    # 两份稿在这一段里平级：没有任何一处把它们排成"可信 / 凑数"
    assert "权威" not in text


def test_the_real_template_writes_down_the_honest_attribution_rules() -> None:
    """说话人归属的诚实口径逐条落在模板里（spec 那一节不得放宽）。"""
    text = DEFAULT_TEMPLATE.read_text(encoding="utf-8")

    for phrase in (
        "默认不写谁说的",
        "依据",
        "把握",
        "猜不出就列进「猜不出的」那一节，不许编",
        "两路各自的 `S01` 不是同一个人",
        "不要把「远端」当成人名",
    ):
        assert phrase in text, phrase


def test_the_real_template_names_the_five_artifacts_and_the_reentry_gate() -> None:
    """产物落点与再入防护都写清：场次目录根的纪要、`working/` 下的分块与方案、不写状态文件。"""
    text = DEFAULT_TEMPLATE.read_text(encoding="utf-8")

    assert "working/<source>/<kind>/chunks/chunk_NN.md" in text
    assert "working/<source>/<kind>/00_index.md" in text
    assert "working/segmentation.json" in text
    assert "working/segment-<id>.md" in text
    assert "/meeting_final.md" in text
    assert "不要写 `meeting_minutes_status.json`" in text


def test_the_real_template_has_no_leftover_v1_paths() -> None:
    """V1 那两种写错的路径字面量只出现在「落点就是这两处」的提醒句里。

    `prompts/meeting_final.md` 里提到它们，是为了写明**本轮不落这两处**
    （V1 三种写法并存，照抄哪一条都是错的）；除此之外的路径一律是
    `working/<source>/<kind>/chunks/chunk_NN.md` 与 `working/<source>/<kind>/00_index.md`。
    """
    text = DEFAULT_TEMPLATE.read_text(encoding="utf-8")
    mentions = [line for line in text.splitlines() if "working/chunk_01.md" in line]

    assert len(mentions) == 1, "那一句提醒只该出现一次"
    assert "不要" in mentions[0] or "没有别的写法" in mentions[0]
    assert "meeting-notes" not in text
    assert "split_chunks.py" not in text
    assert "C:\\Users" not in text
    assert "meeting_raw.md" not in text


# ---------------------------------------------------------------------------
# 八、phase1 那三条 minor 的回归（第 0 节的定案）
# ---------------------------------------------------------------------------


def test_a_mixed_shape_gets_a_per_source_note(tmp_path: Path) -> None:
    """**M1**：`mic` 两份都在 + `loopback` 只有会中稿 → 说明段逐 source 说话。

    整体档位取两路合并后的公共档位（这里是 `both`）：至少有一份真·会后稿在场上，
    整场不再说成降级；而 `loopback` 那一路缺稿这件事照样写在说明段里。
    """
    session = _write_mixed_session(tmp_path)

    listing = listing_for_session(session)
    text = render_prompt(listing, DEFAULT_TEMPLATE.read_text(encoding="utf-8"))

    assert listing.mode == "both"
    assert listing.kinds_per_source() == (("mic", "both"), ("loopback", "live_only"))
    # mic 那一路两份都在：说明段里那一句只该说「有哪几份稿」，不许替它下「分不出个人」的结论
    mic_sentence = [line for line in text.splitlines() if line.startswith("> `mic`")]
    loopback_sentence = [line for line in text.splitlines() if line.startswith("> `loopback`")]
    assert len(mic_sentence) == 1 and len(loopback_sentence) == 1
    assert KIND_LABELS["live"] in mic_sentence[0] and KIND_LABELS["offline"] in mic_sentence[0]
    assert "平级" in mic_sentence[0]
    assert "分不出个人" not in mic_sentence[0]
    # loopback 那一路只有会中稿：它那一句必须说清，并且嘱咐附录如实写
    assert "这一路本次只有" in loopback_sentence[0]
    assert KIND_LABELS["live"] in loopback_sentence[0]
    assert "分不出个人" in loopback_sentence[0]
    # 整场不再说成降级（至少有一份真·会后稿在场上）：整场那一句连着说两路时才出现
    assert "本次两路都只有" not in text
    # 清单里 mic 那一路**两份各印一条**（与说明段不打架）
    assert "| mic | 会中稿 | meeting_transcript_mic.md |" in text
    assert "| mic | 会后稿 | meeting_offline_transcript_mic.md |" in text
    # 「权威」这两个字在整份提示词里一个字都不出现
    assert "权威" not in text


def test_mode_for_answers_the_common_level_when_the_two_sources_differ() -> None:
    """`mode_for()` 在混合形状上取公共档位（`prompt.py` 那一份与它同源）。"""
    mixed = (
        InputSpec(source="mic", live="meeting_transcript_mic.md",
                  offline="meeting_offline_transcript_mic.md"),
        InputSpec(source="loopback", live="meeting_transcript_loopback.md", offline=None),
    )
    live_only = (InputSpec(source="loopback", live="a.md", offline=None),)
    offline_only = (InputSpec(source="mic", live=None, offline="b.md"),)
    both = (
        InputSpec(source="mic", live="a.md", offline="b.md"),
        InputSpec(source="loopback", live="c.md", offline="d.md"),
    )

    assert mode_for(mixed) == "both"
    assert mode_for(live_only) == "live_only"
    assert mode_for(offline_only) == "offline_only"
    assert mode_for(both) == "both"
    assert mode_for(()) is None


def test_the_input_kind_comes_from_the_semantics_not_from_the_file_name() -> None:
    """**M2**：清单印哪一份稿的形态**按 `kind` 判**，与文件名叫什么无关。"""
    text = build_inputs_text(
        _listing(_input("loopback", "live", transcript="some/deep/meeting_transcript_loopback.md"))
    )

    assert SPEAKER_SHAPE_NOTES["live"] in text
    assert SPEAKER_SHAPE_NOTES["offline"] not in text


def test_prompt_input_rejects_a_kind_it_does_not_know() -> None:
    """`kind` 只有两份稿这两个词；写别的当场抛错（免得清单印一句说不清的说明）。"""
    with pytest.raises(PromptTemplateError, match="稿别只认"):
        _input(kind="transcript")


def test_a_missing_selected_transcript_raises_the_renderer_error(tmp_path: Path) -> None:
    """**M6**：某一份稿在盘上不在、区间表也没切 → 抛 `PromptTemplateError`（带那个路径）。

    走 `_prompt_inputs()` 那条路：`listing_for_session()` 按盘上有没有来认输入，
    因此手工构造一条「文件不在」的 `InputSpec` 才触得到这一档（转写在两步之间被删掉、
    或者调用方自己塞了一份 `InputSpec` 时就是它）。
    """
    session = tmp_path / "250919"
    session.mkdir()
    spec = InputSpec(
        source="mic",
        live="meeting_transcript_mic.md",
        offline="meeting_offline_transcript_mic.md",
    )

    with pytest.raises(PromptTemplateError) as info:
        prompt_module._prompt_inputs(session, spec)

    assert "meeting_transcript_mic.md" in str(info.value)
    assert "读不动" in str(info.value)


# ---------------------------------------------------------------------------
# 九、修复轮（复审四条里的 M2 与 M4）
#
# M2：`prompts/meeting_final.md` 里没有 `{{PHASE_BODY}}`，这是**设计**而不是漏写
#     ——正文的落点就是那两块标记，渲染时整块就地替换；口径写在渲染器与模板两处。
# M4：渲染出来的提示词里给模型看的路径一律**相对场次目录**；绝对路径只留在清单那段 JSON
#     的 `session_dir` 里（那是给程序读的字段，不动）。
# ---------------------------------------------------------------------------


def test_a_template_without_the_phase_body_token_renders_the_whole_body() -> None:
    """**M2**：模板里整处不写 `{{PHASE_BODY}}` 也能渲染，正文照样是完整一份。

    这是真模板走的那一条（正文的落点就是标记块本身）。判据两条：渲染不抛错、
    五阶段正文一份不少，且**没有多印第二份**（标记块之外再放一处 token 才会印两份，
    那一条由 `test_a_phase_body_token_outside_the_markers_gets_the_same_body` 盯着）。
    """
    without_token = TEMPLATE.replace("{{PHASE_BODY}}", "")

    assert "{{PHASE_BODY}}" not in without_token
    text = render_prompt(_listing(_input()), without_token)

    assert "{{" not in text and "}}" not in text
    for phase in (
        "阶段 1 分割",
        "阶段 2 分割方案",
        "阶段 3 逐段总结",
        "阶段 4 合并",
        "阶段 5 只读检查",
    ):
        assert text.count(phase) == 1, phase
    assert PHASE_BODY_BEGIN not in text and PHASE_BODY_END not in text


def test_the_six_required_placeholders_are_still_enforced_without_the_seventh() -> None:
    """**M2**：可以省略的只有 `{{PHASE_BODY}}`；其余六处缺一处照样抛错。"""
    without_token = TEMPLATE.replace("{{PHASE_BODY}}", "")
    assert render_prompt(_listing(_input()), without_token)

    for name in PLACEHOLDER_NAMES:
        if name == "PHASE_BODY":
            continue
        token = "{{" + name + "}}"
        missing = without_token.replace(token, "")
        with pytest.raises(PromptTemplateError, match="缺占位符"):
            render_prompt(_listing(_input()), missing)


def test_the_real_template_says_the_optional_placeholder_may_be_left_out() -> None:
    """**M2**：真模板里把这条口径写给人看（后来人才不会照着「七处都要在」去补一个 token）。

    那句话写在**正文标记之前**：标记块里是正文本身，说明句得落在它外面。
    它点名的占位符写成 HTML 实体（`&#123;&#123;PHASE_BODY&#125;&#125;`）——模板源码里
    一旦写了这一处的字面量，渲染器的最后一步替换会把这个「讲解」也当成落点，
    整块正文被塞进这句话里（实测过一次：那一行被截断成半句，正文跟着重复一份）。
    """
    text = DEFAULT_TEMPLATE.read_text(encoding="utf-8")

    assert "&#123;&#123;PHASE_BODY&#125;&#125;" in text, "口径那句话里要点名这一处占位符"
    assert "可以没有" in text, "要明说这一处占位符可以没有、标记块就是正文的落点"
    assert text.index("可以没有") < text.index(PHASE_BODY_BEGIN), (
        "这句话要写在标记之前（标记块里是正文，说明句得落在它外面）"
    )
    assert text.count(PHASE_BODY_BEGIN) == 1 and text.count(PHASE_BODY_END) == 1, (
        "两块标记各只该出现一次：正文的作者说明里抄一遍字面量会把正文切错"
    )
    assert "{{PHASE_BODY}}" not in text, "模板源码里不该出现这一处的字面量"


def test_the_author_note_survives_rendering_in_one_piece() -> None:
    """那句作者说明渲染后是完整一句：既没被当成落点，也没被正文切掉半行。"""
    text = render_prompt(_listing(_input()), DEFAULT_TEMPLATE.read_text(encoding="utf-8"))
    flattened = " ".join(text.split())  # 说明句在模板里断成两行，比对前先拉平

    assert "那一处占位符（本模板走的就是这一条）" in flattened
    note = [row for row in text.splitlines() if "这份模板自己的一点说明" in row]
    assert len(note) == 1, note
    assert "&#123;" in text, "实体在提示词里如实留着（模型读得懂，渲染器不当它占位符）"
    # 正文五阶段一份不少（说明句没把落点抢走）
    assert text.count("### 阶段 1：分割") == 1
    assert text.count("### 阶段 5：只读检查") == 1


def test_the_rendered_prompt_only_shows_model_paths_relative_to_the_session(
    tmp_path: Path,
) -> None:
    """**M4**：渲染出来的提示词里，给模型看的路径一律相对场次目录。

    绝对路径只剩清单那段 JSON 的 `session_dir` 一格——那一格**给程序读**（程序读的那份
    `working/inputs.json` 用自己的口径记场次目录名），不跟提示词里的路径一起改。
    """
    session = _write_session(tmp_path)
    text = render_session_prompt(session)

    absolute = str(session).replace("\\", "/")
    # 给模型看的正文里一处绝对路径都没有：提示词里凡是以盘符开头的那几处，逐个解析一遍，
    # 没有一处落在这一场的场次目录上（清单那段 JSON 是给程序读的，单列在 `prose` 之外）
    prose = text.split("```json")[0]
    assert absolute not in prose, "渲染出来的提示词里出现了场次目录的绝对路径"
    for found in re.findall(r"[A-Za-z]:[/\\][^\s`）)，,。]*", prose):
        assert Path(found).resolve() != session.resolve(), (
            f"正文里出现了这一场的绝对路径：{found}"
        )
    # 给模型看的落点是相对路径：`./meeting_final.md`（模型的工作目录就是场次目录）
    assert f"{MODEL_CWD}/meeting_final.md" in text
    # 切块那条命令**不在这份提示词里**（第十节那条回归：块由编排层切好，子会话的 cwd 是
    # 场次目录、`meeting` 包不在它的模块搜索路径上，那条命令必然报 ModuleNotFoundError）；
    # 块与区间表仍旧以相对路径点名，模型照着认就行
    assert _minutes_command_lines(text) == []
    assert f"working/loopback/live/{INDEX_NAME}" in text
    assert f"working/loopback/offline/{INDEX_NAME}" in text
    # 给程序读的那一格照旧是绝对路径（**两者不要一起改**）：清单那段 JSON 里原位保留
    assert f'"session_dir": "{absolute}"' in text


def test_the_listing_json_keeps_the_absolute_session_dir(tmp_path: Path) -> None:
    """**M4** 的另一半：清单里那段 JSON 的 `session_dir` 仍是绝对路径（程序读的字段）。"""
    session = _write_session(tmp_path)
    listing = listing_for_session(session)

    payload = prompt_module._listing_payload(listing)

    assert payload["session_dir"] == str(session).replace("\\", "/")
    assert payload["session_dir"] != MODEL_CWD
    # 清单里的人话那几行全是相对路径，绝对路径只出现在那段 JSON 里
    prose = build_inputs_text(listing).split("```json")[0]
    assert str(session).replace("\\", "/") not in prose


def test_the_session_dir_placeholder_fills_the_model_working_dir() -> None:
    """`{{SESSION_DIR}}` 填的是模型的工作目录那一点（`.`）：子会话的 cwd 就是场次目录。

    这一份是文件自己造的模板（它那一行命令写着 `{{SESSION_DIR}}`），因此里面照旧有一条
    `python -m meeting.minutes.chunk .`：渲染器把那一处填成 `.` 这件事实在这里看得最清楚。
    真模板里没有这样的命令行，那一条由第十节盯着。
    """
    text = render_prompt(_listing(_input()), TEMPLATE)

    assert f"python -m meeting.minutes.chunk {MODEL_CWD}" in text
    assert f"{MODEL_CWD}/meeting_final.md" not in text  # 这份模板没写那条落点
    prose = text.split("```json")[0]
    assert "C:/out/250919" not in prose


# ---------------------------------------------------------------------------
# 十、修复轮（真跑暴露的那条主线）
#
# 真跑了四遍真实素材（那一轮的一次性证据目录已清）：提示词里那三条
# `python -m meeting.minutes…` 的命令**从场次目录里跑不起来**——子会话的 cwd 就是场次目录
# （在仓库树外），`meeting` 包不在它的模块搜索路径上，裸跑必然 ModuleNotFoundError
# （run3 实测：`Error while finding module specification for 'meeting.minutes'
# (ModuleNotFoundError: No module named 'meeting')`）。
#
# 定案：**别让模型去跑一条它跑不动的命令**。
#
#   - 阶段 1 不再要求模型自己切块：块由编排层在拉起 DSH **之前**就切好了
#     （`runner.run()` → `chunk_session()`），提示词改成「块已经切好了 + 照着认」；
#   - 阶段 2 末尾那条 `check` 自查改成**逐条核对的文字单子**（那一份方案的验收条件写全），
#     不再挂一条跑不通的命令。
#
# 这两条都由下面三条用例钉住；两条固定文字与「同步等待」那一句另有一条**渲染产物**级
# 的复查（它们在模板真身里的逐字断言在第七节）。
# ---------------------------------------------------------------------------

#: 提示词里任何一处要模型跑的 `python -m …`（渲染出来是给模型照着敲的）
PYTHON_COMMAND_RE = re.compile(r"python -m \S+")
#: 阶段 1 提到模块名时用的说法（「编排层已经跑完切块（`meeting.minutes.chunk` 那一步）」）
MINUTES_MODULE_NAME = "meeting.minutes.chunk"
REENTRY_PROHIBITION = "不要跑 `meeting.minutes.runner`"


def _minutes_command_lines(text: str) -> list[str]:
    """提示词里那些**要模型照着敲**的 `python -m …` 命令行（空的才干净）。"""
    return [line.strip() for line in text.splitlines() if PYTHON_COMMAND_RE.search(line)]


def test_the_real_template_phase_one_says_the_chunks_are_already_cut() -> None:
    """阶段 1 明写「块已经切好了」，并把该用的区间表与块文件指清楚。"""
    text = DEFAULT_TEMPLATE.read_text(encoding="utf-8")
    body = extract_phase_body(text, DEFAULT_TEMPLATE)
    phase_one = body.split("### 阶段 2")[0]

    assert "块已经切好了" in phase_one
    assert "不要" in phase_one and "切块" in phase_one, "要明写不要去跑切块命令"
    assert "working/<source>/<kind>/00_index.md" in phase_one
    assert "working/<source>/<kind>/chunks/chunk_NN.md" in phase_one


def test_the_real_template_keeps_no_minutes_command_anywhere(tmp_path: Path) -> None:
    """真模板里**一条**要模型照着敲的 `python -m meeting.minutes…` 都没有。

    三类产物都要复查：模板真身、抽出来的五阶段正文、以及**真渲染一份**出来的提示词
    ——最后这一份才是模型手上那一份。模块名留在两处叙述里是允许的：阶段 1 那句「编排层已经
    跑完切块（`meeting.minutes.chunk` 那一步）」，以及再入防护里写明**不许跑**的那一条。
    """
    text = DEFAULT_TEMPLATE.read_text(encoding="utf-8")
    body = extract_phase_body(text, DEFAULT_TEMPLATE)
    rendered = render_session_prompt(_write_mixed_session(tmp_path))

    for label, candidate in (
        ("模板真身", text),
        ("五阶段正文", body),
        ("渲染出来的提示词", rendered),
    ):
        found = _minutes_command_lines(candidate)
        assert found == [], f"{label} 里还挂着跑不通的命令：{found}"
    # 剩下那两处提到模块名的行，一处是「编排层的既成事实」、一处是「不许跑」
    narrative = [
        line.strip()
        for line in rendered.splitlines()
        if MINUTES_MODULE_NAME in line or "meeting.minutes.runner" in line
    ]
    assert any("已经跑完切块" in line for line in narrative), narrative
    assert REENTRY_PROHIBITION in rendered


def test_the_real_template_phase_two_self_check_is_a_text_checklist() -> None:
    """阶段 2 末尾的自查落在一条**逐条核对的文字单子**上，阶段 2 里不再挂着 `check` 命令。"""
    text = DEFAULT_TEMPLATE.read_text(encoding="utf-8")
    body = extract_phase_body(text, DEFAULT_TEMPLATE)
    phase_two = body.split("### 阶段 3")[0]

    assert "check" not in phase_two, "那一处 `check` 命令不许留在阶段 2 里"
    assert "验收条件" in phase_two, "自查要写成这一份方案的验收条件"
    # 单子逐条在：字段齐 / 同 source 内连续 / 末段盖到 total_lines / 顶层逐 source / 段数上限
    for item in ("line_start", "line_end", "total_lines", "{{MAX_SEGMENTS}}"):
        assert item in phase_two, item
    assert "working/inputs.json" in phase_two, "行数从哪来要指清楚（编排层落的那份清单）"


def test_the_rendered_prompt_keeps_the_fixed_text_and_the_waiting_sentence(
    tmp_path: Path,
) -> None:
    """渲染出来的提示词里，产物校验要认的两段固定文字与「同步等待」那一条纪律**逐字仍在**。

    这两处是产物校验与已有用例的判据——这一条贴着**渲染产物**复查（模型拿到的就是它），
    模板源码上的逐字断言留在第七节。「同步等待」那一条现在写成与分支无关的纪律
    （headless 子进程与 DSH GUI 会话两条都要同步等），因此这里也逐字钉住那一版。
    """
    text = render_session_prompt(_write_mixed_session(tmp_path))
    skeleton = "\n".join(
        [
            "## 说话人附录",
            "",
            "| 路 | 编号 | 人名 | 把握 | 依据 |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    flattened = "\n".join(
        line[2:] if line.startswith("> ") else line.lstrip(">") for line in text.splitlines()
    )
    waiting = (
        "注意：无论这一次跑在哪一种会话里（一次性的 headless 子进程，还是 DSH GUI 里那条会话），"
        "都要**同步等所有 sub agent 交回**再进下一阶段——绝不把活挂在后台就结束回合；"
        "全部 segment 汇总并写出 meeting_final.md 后才算完成。"
    )

    assert skeleton in flattened, "说话人附录那张表的表头与分隔行要逐字在"
    assert "### 猜不出的" in flattened
    assert waiting in text
    assert "{{" not in text and "}}" not in text


# ---------------------------------------------------------------------------
# 十一、块路径与划段分母：三档渲染产物里点到的每一条输入路径都在盘上
# （review-3 C1 / C2 / m1）
#
# C1：阶段 3 的块文件列表少过一层 `chunks/`——主 agent 照它抄出来的路径在盘上不存在；
# C2：`offline_only` 档的划段分母与自检原先都指会中稿，而那一档会中稿根本不在
#     （与 `contract.py` 的既有降级口径分叉：两份都在按会中稿、只有会后稿按会后稿）；
# m1：模板与清单 docstring 里残留旧布局 `working/<source>/chunks/`。
# 三条都按「渲染产物里出现的每一条输入路径，在盘上都能找到」这一口径钉住。
# ---------------------------------------------------------------------------

#: 渲染产物里的 `working/…` 路径（占位符形状含 `<>`，匹配时天然截断在它上面）
WORKING_PATH_RE = re.compile(r"working/[^\s`\"<>，。、）()|]*")


def _concrete_input_paths(text: str) -> list[str]:
    """渲染产物里点到的**具体输入**路径（区间表与块目录）。

    方案（`segmentation.json`）、段总结（`segment-*.md`）与 `inputs.json` 是这一趟的
    产出 / 编排层的东西，渲染时刻不在盘上，不在这一条口径里；`chunk_NN.md` 是模式名。
    """
    paths: list[str] = []
    for raw in WORKING_PATH_RE.findall(text):
        if "chunk_NN" in raw:
            continue
        path = raw.rstrip("./")
        if not (path.endswith(f"/{INDEX_NAME}") or path.endswith("chunks")):
            continue
        if path not in paths:
            paths.append(path)
    return paths


@pytest.mark.parametrize(
    "live, offline",
    [(True, True), (True, False), (False, True)],
    ids=["both", "live_only", "offline_only"],
)
def test_every_input_path_the_render_names_exists_on_disk(
    tmp_path: Path, live: bool, offline: bool
) -> None:
    """三种输入各真渲染一次：产物里点到的每一条区间表 / 块目录路径，在盘上都真存在。"""
    session = _write_session(tmp_path, live=live, offline=offline)
    text = render_session_prompt(session)

    paths = _concrete_input_paths(text)
    assert paths, "渲染产物里至少要点到一条输入路径"
    for path in paths:
        if path.endswith("chunks"):
            assert (session / path).is_dir(), f"块目录不在盘上：{path}"
        else:
            assert (session / path).is_file(), f"区间表不在盘上：{path}"
    # 旧布局（`working/<source>/chunks/`、`working/<source>/00_index.md`）一条都不残留
    assert "working/loopback/chunks" not in text
    assert f"working/loopback/{INDEX_NAME}" not in text
    # 缺的那一份稿，它的具体路径一条都不出现（清单不编不在的稿，正文只写占位符形状）
    if not offline:
        assert "working/loopback/offline/" not in text
    if not live:
        assert "working/loopback/live/" not in text


def test_the_offline_only_render_self_check_points_at_the_index_that_exists(
    tmp_path: Path,
) -> None:
    """`offline_only` 档：划段分母与自检按**在场那份稿**（会后稿）说话，指的区间表在盘上。

    旧文让这一档拿会中稿当分母、照 `working/<source>/live/00_index.md` 自检——两处都取
    不到（清单里只有会后稿一行）。新口径与 `contract.py` 的降级同话：两份都在按会中稿，
    只有会后稿的场次按会后稿。
    """
    session = _write_session(tmp_path, live=False, offline=True)
    text = render_session_prompt(session)
    phase_two = text.split("### 阶段 3")[0].split("### 阶段 2", 1)[1]

    assert "working/<source>/live/00_index.md" not in text, "自检不该再指这一档不存在的区间表"
    assert "划段按会中稿的行号空间来" not in text
    assert "划段按这一路在场那份稿的行号空间来" in phase_two
    assert "只有会后稿的场次按会后稿" in phase_two
    index_rel = f"working/loopback/offline/{INDEX_NAME}"
    assert index_rel in text, "自检核的那份区间表就是清单里这一档真实存在的那一条"
    assert (session / index_rel).is_file(), index_rel


def test_the_real_template_names_chunk_paths_in_the_new_layout() -> None:
    """阶段 3 的块文件列表写新布局（带 `chunks/` 一层），全文不残留旧布局形状（C1 / m1）。"""
    text = DEFAULT_TEMPLATE.read_text(encoding="utf-8")

    assert "working/[source]/<kind>/chunks/chunk_NN.md" in text
    assert "working/[source]/live/chunk_NN.md" not in text, "照这一行抄出来的是盘上没有的路径"
    assert "这一路在场的每一份稿各自照它 `chunks` 那一格给" in text
    assert "working/<source>/chunks/" not in text, "旧布局的块目录位置不残留"
    assert "working/<source>/00_index.md" not in text, "旧布局的区间表位置不残留"


# ---------------------------------------------------------------------------
# 十二、机主确认过的人名（`{{SPEAKER_NAMES}}`，节点 `91b6752e`）
#
# 「改完写回纪要里那张表」的落法是**喂提示词**（R10 第四条：同一场以后再出纪要自动带上）：
# 人名表渲染进附录说明，模型照它把那一格抄成机主确认过的名字。这一节钉三条：
#
#   - 没有确认过的名字时**如实**渲染空态一句（不是空串、也不是编一份空表），
#     与 `{{MODE_NOTE}}`「这一路分不出个人」的既有口径摆在一起不吵架；
#   - 确认过的名字照抄进表（确认不是推断，档位说明管不到它）；
#   - 人名表坏了不挡渲染：读不出就落空态，提示词照常交出去。
# ---------------------------------------------------------------------------


def test_the_rendered_prompt_says_no_confirmed_names_by_default(tmp_path: Path) -> None:
    """没有人名表的场次：`{{SPEAKER_NAMES}}` 渲染成如实的空态一句，一个 `{{…}}` 都不剩。"""
    session = _write_session(tmp_path)
    text = render_session_prompt(session)

    assert "机主还没有确认过任何编号的人名" in text
    assert "全部从上下文推断" in text
    assert "{{SPEAKER_NAMES}}" not in text
    assert "{{" not in text and "}}" not in text


def test_the_rendered_prompt_carries_the_confirmed_names(tmp_path: Path) -> None:
    """改过名之后再渲染：确认过的名字进了提示词那张表，表形与附录那张对齐。"""
    session = _write_session(tmp_path)
    _write_speakers_artifact(session, "loopback", ["S01", "S02"])
    set_speaker_name(session, "loopback:S01", "老王")

    text = render_session_prompt(session)

    assert "| loopback（远端） | S01 | 老王 |" in text
    assert "老王" in text
    # 没确认过的编号不进这张表（表里只存起过名字的编号）
    assert "| loopback（远端） | S02" not in text
    assert "照抄" in text, "口径要写清：表里的名字逐字照抄，推断只管表里没有的编号"
    assert "机主还没有确认过" not in text
    assert "{{" not in text and "}}" not in text


def test_a_road_itself_can_be_named_when_there_is_no_clustering(tmp_path: Path) -> None:
    """完成判定的第 3 条读数：只有会中稿的场次，身份就是路本身，照样能起名。"""
    session = _write_session(tmp_path, offline=False)
    set_speaker_name(session, "loopback", "小李")

    text = render_session_prompt(session)

    assert "| loopback（远端） | — | 小李 |" in text
    assert "{{" not in text and "}}" not in text


def test_rendering_again_after_a_rename_reflects_the_new_name(tmp_path: Path) -> None:
    """完成判定的第 1 条读数：改一个编号的名字，重渲出来的提示词跟着变。"""
    session = _write_session(tmp_path)
    _write_speakers_artifact(session, "loopback", ["S01"])
    set_speaker_name(session, "loopback:S01", "老王")
    before = render_session_prompt(session)
    assert "| loopback（远端） | S01 | 老王 |" in before

    set_speaker_name(session, "loopback:S01", "老王头")
    after = render_session_prompt(session)

    assert "| loopback（远端） | S01 | 老王头 |" in after
    assert "| loopback（远端） | S01 | 老王 |" not in after.replace("老王头", ""), (
        "旧名字不该再出现在表里（同一键覆盖）"
    )


def test_a_broken_roster_renders_the_honest_empty_section(tmp_path: Path) -> None:
    """人名表坏了（不是合法 JSON）：渲染不抛错、如实落空态——读侧只做结构解析、不挡出纪要。"""
    session = _write_session(tmp_path)
    (session / NAMES_FILE_NAME).write_text("{ 不是 json", encoding="utf-8")

    text = render_session_prompt(session)

    assert "机主还没有确认过任何编号的人名" in text
    assert "{{" not in text and "}}" not in text


def test_render_prompt_without_the_speaker_names_argument_fills_the_empty_text() -> None:
    """`render_prompt()` 不碰盘：不传 `speaker_names` 时按空态正文填，不留没填的占位符。"""
    text = render_prompt(_listing(_input()), TEMPLATE)

    assert "机主还没有确认过任何编号的人名" in text
    assert "{{SPEAKER_NAMES}}" not in text
    assert "{{" not in text and "}}" not in text


def test_render_prompt_carries_a_passed_speaker_names_section() -> None:
    """调用方从盘上取好了递进来的正文原样落进占位处（纯渲染的另一半）。"""
    section = ">> 这一行是调用方递进来的人名表"

    text = render_prompt(_listing(_input()), TEMPLATE, speaker_names=section)

    assert section in text


def test_speaker_names_section_prints_only_the_confirmed_rows() -> None:
    """表格只印确认过的行：路 + 编号 + 人名三格与附录那张表对齐，没起名的编号不出现。"""
    rows = [
        {"key": "mic:S01", "source": "mic", "label": "本地", "name": "老王", "segments": 3},
        {"key": "mic:S02", "source": "mic", "label": "本地", "name": None, "segments": 1},
        {
            "key": "loopback",
            "source": "loopback",
            "label": "远端",
            "name": "小李",
            "segments": None,
        },
    ]

    text = speaker_names_section(rows)

    assert "| mic（本地） | S01 | 老王 |" in text
    assert "| loopback（远端） | — | 小李 |" in text
    assert "S02" not in text, "没起过名的编号不进这张表"
    assert "照抄" in text and "猜不出的" in text


def test_speaker_names_section_is_the_honest_empty_text_without_names() -> None:
    """一行确认过的名字都没有 → 恰好是空态正文（不是空串、不是一张空表）。"""
    rows = [
        {"key": "mic:S01", "source": "mic", "label": "本地", "name": None, "segments": 3},
    ]

    assert speaker_names_section(rows) == SPEAKER_NAMES_EMPTY
    assert speaker_names_section([]) == SPEAKER_NAMES_EMPTY
