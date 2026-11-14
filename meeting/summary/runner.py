"""会中总结的一次运行：切窗口 → 装配 → 调端点 → 解析 → 归三小段，带一条墙钟 deadline。

预算是一条 deadline（默认 25 秒，`summary.deadline_seconds`）；尝试次数上限 3 只是上限
——每次请求的超时取剩余预算，预算用尽就停，如实报出等了多久、试了几次（成功与失败两条路
都填 `elapsed_seconds` / `attempts`）。重试只由解析失败触发；端点的四档错误（`timeout` /
`bad_status` / `bad_json` / `no_content`）当场收工，不再重放同一个错误。

三种收场：成功（`ok=True`，五格都填）；窗口里一行都没有（同样 `ok=True`、三段空、
一个请求都不发，空数组合法）；失败（`ok=False`，`error` 人话、`detail` 机器可读）。
`monotonic` / `wall_clock` 都是注入点，用例塞假钟，deadline 那几档不必真等。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from meeting.summary.client import ModelClient, SummaryError
from meeting.summary.parse import (
    SECTION_ORDER,
    all_sections_unlabeled,
    parse_bullets,
    split_sections,
)
from meeting.summary.prompt import build_messages, build_retry_messages
from meeting.summary.settings import (
    DEFAULT_WINDOW,
    WINDOW_CHOICES,
    SummarySettings,
    window_seconds_of,
)
from meeting.summary.window import BufferedLine, WINDOW_LABELS, render_window, select_window

#: 剩余预算小于这个数（秒）就不再发下一次请求：比它更短的超时没机会拿到应答
MIN_ATTEMPT_SECONDS = 0.05

#: 窗口档位 → 人话标签（**引用本体**：`meeting/summary/window.py` 的 `WINDOW_LABELS`，
#: 不再拷贝 —— 表只此一份，新档位加进那里两边同时生效）
WINDOW_LABELS_BY_CHOICE = WINDOW_LABELS


@dataclass(frozen=True)
class SummaryOutcome:
    """一次会中总结的收场（形状冻结）。"""

    ok: bool
    window: str
    window_seconds: float | None
    window_label: str
    lines: int
    attempts: int
    elapsed_seconds: float
    bullets: tuple[str, ...] = ()
    what: tuple[str, ...] = ()
    where: tuple[str, ...] = ()
    lost: tuple[str, ...] = ()
    unlabeled: int = 0
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def sections(self) -> dict[str, tuple[str, ...]]:
        return {"what": self.what, "where": self.where, "lost": self.lost}


def _fail(
    *,
    window: str,
    window_seconds: float | None,
    lines: int,
    attempts: int,
    elapsed: float,
    error: str,
    detail: dict[str, Any],
) -> SummaryOutcome:
    return SummaryOutcome(
        ok=False,
        window=window,
        window_seconds=window_seconds,
        window_label=WINDOW_LABELS_BY_CHOICE.get(window, WINDOW_LABELS["full"]),
        lines=lines,
        attempts=int(attempts),
        elapsed_seconds=round(max(0.0, float(elapsed)), 3),
        error=error,
        detail=dict(detail),
    )


def run_summary(
    lines: Sequence[BufferedLine],
    *,
    window: str,
    settings: SummarySettings,
    client: ModelClient | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
    started_at: str | None = None,
) -> SummaryOutcome:
    choice = str(window) if str(window) in WINDOW_CHOICES else settings.default_window
    choice = choice if choice in WINDOW_CHOICES else DEFAULT_WINDOW
    span = window_seconds_of(choice)
    label = WINDOW_LABELS_BY_CHOICE.get(choice, WINDOW_LABELS["full"])

    started = float(monotonic())

    # 一、配置缺键：**一个请求都不发出去**，先把缺哪几个键说清楚
    missing = tuple(settings.missing)
    if missing:
        return _fail(
            window=choice,
            window_seconds=span,
            lines=0,
            attempts=0,
            elapsed=float(monotonic()) - started,
            error=f"模型端点没配齐，缺这些配置键：{'、'.join(missing)}",
            detail={"missing": list(missing)},
        )

    # 二、切窗口（按行首时间戳取连续尾部，边界不切进任何一行）
    # 锚点是**缓冲末行的 ts**；`now_ts` 只在缓冲为空时
    # 起作用，这里照旧把当前墙钟传进去给它兜那一格，**不拿它当锚点**。
    selected = select_window(lines, window_seconds=span, now_ts=float(wall_clock()))
    count = len(selected)

    # 三、窗口里一行都没有：空数组合法，不算失败（界面给空态文案）
    if count == 0:
        return SummaryOutcome(
            ok=True,
            window=choice,
            window_seconds=span,
            window_label=label,
            lines=0,
            attempts=0,
            elapsed_seconds=round(float(monotonic()) - started, 3),
            detail={},
        )

    # 四、装配 → 调端点 → 解析（解析失败才重试）
    window_text = render_window(selected)
    messages = build_messages(
        window_text, window_label=label, started_at=started_at, line_count=count
    )
    active = ModelClient.from_settings(settings) if client is None else client
    deadline = started + float(settings.deadline_seconds)

    attempts = 0
    last_error = "解析连试带败"
    last_detail: dict[str, Any] = {}

    for attempt in range(1, int(settings.max_attempts) + 1):
        remaining = deadline - float(monotonic())
        if remaining <= MIN_ATTEMPT_SECONDS:
            break
        attempts = attempt
        try:
            content = active.complete(
                build_retry_messages(messages, attempt=attempt),
                timeout_seconds=max(MIN_ATTEMPT_SECONDS, remaining),
            )
        except SummaryError as exc:
            # 端点的四档错误当场收工（API 报错不再重放一遍）
            return _fail(
                window=choice,
                window_seconds=span,
                lines=count,
                attempts=attempts,
                elapsed=float(monotonic()) - started,
                error=f"模型端点没答上来（{exc.reason}）：{exc.detail}",
                detail={
                    "elapsed_seconds": round(float(monotonic()) - started, 3),
                    "attempts": attempts,
                },
            )

        bullets = parse_bullets(content)
        if bullets is None:
            last_error = "模型输出的不是合法的 {\"bullets\": [...]} JSON"
            last_detail = {"reason": "bad_json"}
            continue

        sections, unlabeled = split_sections(bullets)
        if all_sections_unlabeled(sections, unlabeled):
            # 一条固定标签都没认出来：那几条放进哪一段都是猜的，算解析失败、再试一次
            last_error = "模型输出里一条固定标签都没有（【讲什么】/【讲到哪】/【丢掉什么】）"
            last_detail = {"reason": "unlabeled"}
            continue

        return SummaryOutcome(
            ok=True,
            window=choice,
            window_seconds=span,
            window_label=label,
            lines=count,
            attempts=attempts,
            elapsed_seconds=round(float(monotonic()) - started, 3),
            bullets=tuple(bullets),
            what=tuple(sections["what"]),
            where=tuple(sections["where"]),
            lost=tuple(sections["lost"]),
            unlabeled=int(unlabeled),
            detail={},
        )

    # 五、走到这里 = 预算用尽，或者解析连试带败
    elapsed = float(monotonic()) - started
    exhausted = elapsed >= float(settings.deadline_seconds) - MIN_ATTEMPT_SECONDS
    if exhausted:
        message = (
            f"等了 {elapsed:.1f} 秒（预算 {settings.deadline_seconds:g} 秒）还没有结果，"
            f"试了 {attempts} 次"
        )
    else:
        message = f"{last_error}（试了 {attempts} 次）"
    return _fail(
        window=choice,
        window_seconds=span,
        lines=count,
        attempts=attempts,
        elapsed=elapsed,
        error=message,
        detail={"elapsed_seconds": round(elapsed, 3), "attempts": attempts},
    )


def sections_for(outcome: SummaryOutcome) -> dict[str, list[str]]:
    return {name: list(getattr(outcome, name)) for name in SECTION_ORDER}


__all__ = [
    "MIN_ATTEMPT_SECONDS",
    "WINDOW_CHOICES",
    "WINDOW_LABELS_BY_CHOICE",
    "SummaryOutcome",
    "run_summary",
    "sections_for",
]
