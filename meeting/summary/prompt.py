"""会中提示词：把窗口文本装配成一次 chat 调用要的消息（system 的硬规则 +
user 的分段写法）。内容三件事：

  - **三小段**：刚才在讲什么 / 讲到哪儿了 / 丢掉了什么；
  - **缺口要讲清楚**：窗口里的 `[[识别失败: 超时 20s]]` 是一条**信息**，人正是想知道
    「丢掉了什么」。提示词因此明写：缺口要点名、说清大概位置，**不许猜内容填空**；
  - **窗口有边界**：窗口是从整场里切出来的尾巴，开头那句话可能是半截的，
    末尾还是活的（后面还有内容）。提示词明写这一条，模型就不会把半句当成一个结论。

输出契约：**强制合法 JSON、固定 `{"bullets": [...]}` 形状、元素是字符串**；
每条 bullet **以固定标签开头**（`【讲什么】` / `【讲到哪】` / `【丢掉什么】`），
解析层靠标签把它们归到三小段（见 `parse.py`）。标签写在这里、认在那里，两处同源。
"""

from __future__ import annotations

from collections.abc import Sequence

from meeting.summary.parse import SECTION_LABELS

SYSTEM_RULES: tuple[str, ...] = (
    "只输出合法 JSON，形状固定为 {\"bullets\": [\"...\", \"...\"]}，"
    "禁止任何 JSON 以外的文字、解释、注释、代码围栏",
    "bullets 是字符串数组；每条一句话，行首写三个固定标签之一，标签后面直接接正文",
    "转写里有识别错误、重复、口头禅、互相打断：抓意思，可以轻度顺句；"
    "拿不准的说话人归属就不写",
    "窗口开头那句可能是半截的、末尾的内容还在往下走：别把半句当结论，"
    "也别把没说完的事说成已经定了",
    "窗口里没有的内容一律不写：不许猜缺口里可能说了什么，也不许编造没出现在窗口里的内容；"
    "一个要点都没有时交回 {\"bullets\": []}",
)

#: 窗口那一段的标题行（`整场` 与带窗口的两档各一句，读窗口的人一眼看出范围）
FULL_WINDOW_TITLE = "【本场转写（整场）】"
WINDOWED_WINDOW_TITLE_TEMPLATE = "【最近 {window_label}的转写】"


def _system_text() -> str:
    """system 那一段：身份 + 三件事 + 硬性规则。"""
    body = "\n".join(f"- {rule}" for rule in SYSTEM_RULES)
    return (
        "你是会议助理。会议进行中，有人刚跟丢了、按了一下按钮："
        "把最近的实时转写读一遍，告诉他刚才在讲什么、讲到哪儿了、丢掉了什么。\n"
        "他要的是几块救生板，够他重新跟上就行。\n"
        "硬性规则：\n"
        f"{body}"
    )


def _time_hint(started_at: str | None) -> str:
    """会议开始时刻那一格（给了就写出来，没给就明写「不知道」）。"""
    text = str(started_at or "").strip()
    if not text:
        return "会议开始时刻：不知道（没有传进来）"
    return f"会议开始时刻：{text}"


def build_messages(
    window_text: str,
    *,
    window_label: str,
    started_at: str | None,
    line_count: int,
) -> list[dict[str, str]]:
    """把窗口文本装配成一次 chat 调用要的两条消息（system + user）。

    `window_label` 是窗口的人话标签（`5 分钟` / `15 分钟` / `30 分钟` / `整场`），
    它进 user 那一段的标题与任务句；`整场` 那一档标题相应地改成「整场」。
    `line_count` 是窗口里的行数，写在任务句里给模型一个规模感。
    `started_at` 是这场会的开始时刻（ISO 串或 `HH:MM`），拿不到就传 `None`。
    """
    label = str(window_label or "").strip()
    if not label:
        raise ValueError("build_messages 需要 window_label（窗口的人话标签），收到空值")

    title = FULL_WINDOW_TITLE if label == "整场" else WINDOWED_WINDOW_TITLE_TEMPLATE.format(
        window_label=label
    )
    scope = "整场" if label == "整场" else f"最近 {label}"
    text = str(window_text or "")
    user = (
        f"{title}\n"
        f"{text}\n\n"
        "【本次任务】\n"
        "只回答三件事，每件事一到三条：\n"
        f"1. 刚才在讲什么 —— 每条以 {SECTION_LABELS['what']} 开头\n"
        f"2. 讲到哪儿了 —— 每条以 {SECTION_LABELS['where']} 开头\n"
        f"3. 丢掉了什么 —— 每条以 {SECTION_LABELS['lost']} 开头\n"
        f"本条窗口：{scope}，共 {int(line_count)} 行。\n"
        f"{_time_hint(started_at)}\n"
        f"这次窗口里识别失败的缺口按原样标着（形如 [[识别失败: 超时 20s]]）："
        f"见到几个就在 {SECTION_LABELS['lost']} 里写几条，说清什么时候、哪一路、大概位置；"
        "一个缺口都没有就写一条说明「这段窗口里没有识别失败的缺口」。\n"
        "只输出 JSON。"
    )
    return [
        {"role": "system", "content": _system_text()},
        {"role": "user", "content": user},
    ]


def build_retry_messages(
    messages: Sequence[dict[str, str]],
    *,
    attempt: int,
) -> list[dict[str, str]]:
    """第 2 次起在末尾追加一句提示。

    第 1 次（`attempt == 1`）原样交回入参。第 2 次起，在**最后一条 user 消息**后面
    追加一条 user 消息，把「上一次输出不是合法 JSON」这件事说清楚；前面几条一个字不动。
    """
    if int(attempt) <= 1:
        return [dict(message) for message in messages]

    hint = (
        "注意：上一次的输出不是合法 JSON（或者一条固定标签都没用上）。\n"
        "本次只输出 JSON，不要任何多余文字："
        "{\"bullets\": [\"【讲什么】…\", \"【讲到哪】…\", \"【丢掉什么】…\"]}"
    )
    rebuilt = [dict(message) for message in messages]
    if rebuilt and rebuilt[-1].get("role") == "user":
        rebuilt[-1] = {
            "role": "user",
            "content": f"{rebuilt[-1].get('content', '')}\n\n{hint}",
        }
        return rebuilt
    rebuilt.append({"role": "user", "content": hint})
    return rebuilt


__all__ = [
    "FULL_WINDOW_TITLE",
    "SYSTEM_RULES",
    "WINDOWED_WINDOW_TITLE_TEMPLATE",
    "build_messages",
    "build_retry_messages",
]
