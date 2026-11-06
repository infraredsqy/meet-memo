"""`meeting/minutes/prompt.py` 档位说明段（`{{MODE_NOTE}}`）的文案钉子。

说明段是**模型可见文案**：只有一份稿的四个分支（`mode_note_text()` 的 live_only /
offline_only 两档、`_note_sentence()` 的 live_only / offline_only 两档）逐字同构、
只差缺席稿的名字，收敛在 `_MISSING_DRAFT_LABELS` 一处——这里的断言把四段输出
**逐字**钉住，改一个字都当场报红。纯函数比对，不读盘、不起进程。
"""

from __future__ import annotations

from meeting.minutes.prompt import mode_note_text

#: 只有会中稿这一档、逐路那句的全文（`_note_sentence()` 的 live_only 分支）
NOTE_LIVE_ONLY = (
    "> `mic` 这一路本次只有会中稿，无说话人标签（每行形如 `[HH:MM] 正文`）。"
    "会后稿不在这台机器上，逐块对照那一步没有第二条可对。"
    "附录里如实写明「这一路分不出个人」，不要为任何人名给出推断。"
)

#: 只有会后稿这一档、逐路那句的全文（`_note_sentence()` 的 offline_only 分支）
NOTE_OFFLINE_ONLY = (
    "> `mic` 这一路本次只有会后稿，带聚类编号（每行形如 `[HH:MM] **S01**: 正文`）。"
    "会中稿不在这台机器上，逐块对照那一步没有第二条可对。"
    "附录里如实写明「这一路分不出个人」，不要为任何人名给出推断。"
)

#: 只有会中稿这一档、整场那句的全文（`mode_note_text()` 的 live_only 分支）
MODE_LIVE_ONLY = (
    "> 本次两路都只有会中稿，无说话人标签（每行形如 `[HH:MM] 正文`）。"
    "会后稿不在这台机器上，逐块对照那一步没有第二条可对。"
    "附录里如实写明「这一路分不出个人」，不要为任何人名给出推断。"
    "（这一档的两路：mic、loopback。）"
)

#: 只有会后稿这一档、整场那句的全文（`mode_note_text()` 的 offline_only 分支）
MODE_OFFLINE_ONLY = (
    "> 本次两路都只有会后稿，带聚类编号（每行形如 `[HH:MM] **S01**: 正文`）。"
    "会中稿不在这台机器上，逐块对照那一步没有第二条可对。"
    "附录里如实写明「这一路分不出个人」，不要为任何人名给出推断。"
    "（这一档的两路：mic、loopback。）"
)


def test_the_per_source_note_for_live_only_is_verbatim() -> None:
    """逐路那句（只有会中稿）逐字不变。"""
    assert mode_note_text("both", (("mic", "live_only"),)) == NOTE_LIVE_ONLY


def test_the_per_source_note_for_offline_only_is_verbatim() -> None:
    """逐路那句（只有会后稿）逐字不变。"""
    assert mode_note_text("both", (("mic", "offline_only"),)) == NOTE_OFFLINE_ONLY


def test_the_mode_note_for_live_only_is_verbatim() -> None:
    """整场那句（只有会中稿）逐字不变。"""
    assert (
        mode_note_text("live_only", (("mic", "live_only"), ("loopback", "live_only")))
        == MODE_LIVE_ONLY
    )


def test_the_mode_note_for_offline_only_is_verbatim() -> None:
    """整场那句（只有会后稿）逐字不变。"""
    assert (
        mode_note_text("offline_only", (("mic", "offline_only"), ("loopback", "offline_only")))
        == MODE_OFFLINE_ONLY
    )


def test_both_and_all_both_note_stays_put() -> None:
    """两份都全那一档与逐路 both 那句也一并钉住（收敛不许碰到它们）。"""
    both_shape = "会中稿，无说话人标签（每行形如 `[HH:MM] 正文`）；会后稿，带聚类编号（每行形如 `[HH:MM] **S01**: 正文`）"
    assert mode_note_text("both", (("mic", "both"), ("loopback", "both"))) == (
        f"> 本次这两路两份都在（mic、loopback）：{both_shape}。"
        "两份**平级、不合并**，逐块对照那一步两份都要读。"
    )
    rendered = mode_note_text("both", (("mic", "both"), ("loopback", "live_only")))
    assert rendered.split("\n")[0] == (
        f"> `mic` 这一路的稿：{both_shape}。两份都在，**平级、不合并**，"
        "逐块对照那一步两份都要读。"
    )
