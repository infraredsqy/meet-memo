"""`meeting/summary/prompt.py` 的契约用例：会中提示词的两条消息。

盯的是四件事：

  - **组装方式照 V1**：两条消息（`system` + `user`），system 里那七条硬规则一条不少；
  - **缺口要讲清楚**：提示词明写「占位符按原样标着、见到几个就写几条」，
    并且明写「不许猜缺口里说了什么」——人正是想知道丢掉了什么；
  - **输出契约**：三个固定标签的清单在提示词里逐字出现（解析层靠它们归段）；
  - **窗口有边界**：`整场` 那一档的标题与四档窗口的标题各说各的，会议开始时刻给了就写、
    没给就明说不知道。
"""

from __future__ import annotations

from meeting.summary.parse import SECTION_LABELS
from meeting.summary.prompt import (
    FULL_WINDOW_TITLE,
    SYSTEM_RULES,
    build_messages,
    build_retry_messages,
)

#: 一份窗口文本样本（含一条占位符 —— 它正是提示词要讲清楚的那个缺口）
WINDOW_TEXT = "\n".join(
    [
        "[21:32:05] 我们先把这一段记下来 · 本地",
        "[21:33:10] [[识别失败: 超时 20s]] · 远端",
        "[21:34:02] 接着说到下个季度的排期 · 本地",
    ]
)


def test_build_messages_returns_system_and_user_only() -> None:
    """两条消息：system 在前、user 在后；两边都是非空字符串（V1 的两段式）。"""
    messages = build_messages(
        WINDOW_TEXT, window_label="15 分钟", started_at="2025-09-19T21:30:00", line_count=3
    )

    assert [message["role"] for message in messages] == ["system", "user"]
    assert all(message["content"].strip() for message in messages)


def test_system_carries_every_hard_rule() -> None:
    """system 一段把七条硬规则逐条带上，并且报出身份与那三件事。"""
    system = build_messages(
        WINDOW_TEXT, window_label="15 分钟", started_at=None, line_count=3
    )[0]["content"]

    assert len(SYSTEM_RULES) == 7
    for rule in SYSTEM_RULES:
        assert rule in system
    assert "刚才在讲什么" in system
    assert "讲到哪儿了" in system
    assert "丢掉了什么" in system


def test_system_forbids_guessing_and_asks_for_a_fixed_json_shape() -> None:
    """强制 JSON 的信封与「不许猜缺口内容」这两条都在 system 里。"""
    system = build_messages(
        WINDOW_TEXT, window_label="5 分钟", started_at=None, line_count=3
    )[0]["content"]

    assert '{"bullets": ["...", "..."]}' in system
    assert "不许猜缺口里可能说了什么" in system


def test_user_asks_for_the_three_labels_one_by_one() -> None:
    """user 一段把三件事与三条固定标签一一对上（解析层靠标签归段）。"""
    user = build_messages(
        WINDOW_TEXT, window_label="15 分钟", started_at=None, line_count=3
    )[1]["content"]

    for name in ("what", "where", "lost"):
        assert SECTION_LABELS[name] in user
    assert "1. 刚才在讲什么" in user
    assert "2. 讲到哪儿了" in user
    assert "3. 丢掉了什么" in user


def test_user_puts_the_window_text_under_a_windowed_title() -> None:
    """带窗口的那一档：标题写「最近 15 分钟的转写」，窗口正文逐字在标题下面。"""
    user = build_messages(
        WINDOW_TEXT, window_label="15 分钟", started_at=None, line_count=3
    )[1]["content"]

    assert user.startswith("【最近 15 分钟的转写】\n")
    assert WINDOW_TEXT in user


def test_user_uses_the_full_window_title_for_the_whole_session() -> None:
    """`整场` 那一档：标题换成「本场转写（整场）」，范围句写成「整场，共 N 行」。"""
    user = build_messages(
        WINDOW_TEXT, window_label="整场", started_at=None, line_count=3
    )[1]["content"]

    assert user.startswith(f"{FULL_WINDOW_TITLE}\n")
    assert "本条窗口：整场，共 3 行。" in user
    assert "最近 整场" not in user


def test_user_reports_scale_and_the_start_time() -> None:
    """条目数、窗口范围、会议开始时刻三样都写进任务句（给了就写出来）。"""
    user = build_messages(
        WINDOW_TEXT,
        window_label="30 分钟",
        started_at="2025-09-19T21:30:00",
        line_count=42,
    )[1]["content"]

    assert "最近 30 分钟，共 42 行" in user
    assert "会议开始时刻：2025-09-19T21:30:00" in user


def test_user_says_so_when_the_start_time_is_missing() -> None:
    """拿不到开始时刻时明写「不知道」，让模型别把这一格当成一个真实时刻。"""
    user = build_messages(
        WINDOW_TEXT, window_label="15 分钟", started_at=None, line_count=1
    )[1]["content"]

    assert "会议开始时刻：不知道" in user


def test_user_tells_the_model_what_to_do_about_the_placeholders() -> None:
    """缺口那一句明写：占位符原样标着，见到几个就在「丢掉什么」里写几条。"""
    user = build_messages(
        WINDOW_TEXT, window_label="15 分钟", started_at=None, line_count=3
    )[1]["content"]

    assert "[[识别失败: 超时 20s]]" in user
    assert "见到几个就" in user
    assert "一个缺口都没有就写一条说明" in user


def test_build_messages_refuses_an_empty_window_label() -> None:
    """窗口标签是必填的：空值当场报错（免得提示词里出现「最近 的转写」）。"""
    try:
        build_messages(WINDOW_TEXT, window_label="  ", started_at=None, line_count=1)
    except ValueError as exc:
        assert "window_label" in str(exc)
    else:  # pragma: no cover - 走不到这里
        raise AssertionError("空 window_label 该被拦下")


def test_retry_messages_leave_the_first_attempt_alone() -> None:
    """第 1 次原样：消息条数与内容都不动。"""
    messages = build_messages(
        WINDOW_TEXT, window_label="15 分钟", started_at=None, line_count=3
    )

    assert build_retry_messages(messages, attempt=1) == messages


def test_retry_messages_append_the_hint_from_the_second_attempt() -> None:
    """第 2 次起在末尾追加那句「上一次不是合法 JSON」，前一条 user 的正文照旧在前面。"""
    messages = build_messages(
        WINDOW_TEXT, window_label="15 分钟", started_at=None, line_count=3
    )

    retried = build_retry_messages(messages, attempt=2)

    assert len(retried) == len(messages)
    assert retried[0] == messages[0]
    assert "上一次的输出不是合法 JSON" in retried[-1]["content"]
    assert WINDOW_TEXT in retried[-1]["content"]
    # 入参那份没有被就地改掉
    assert "上一次的输出不是合法 JSON" not in messages[-1]["content"]
