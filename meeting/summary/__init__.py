"""会中总结内核：按一下出结果的那一整条（跟界面无关的那一段）。

`run_summary()` 是这一整条的入口：它把读窗口、渲染、装配消息、调模型、解析、分小段串起来，
并且守住**一条墙钟 deadline** —— 预算用尽就停，并如实报出等了多久、试了几次。

内核是一批可注入、可独立调用的函数与类：**不注册路由、不碰快照、不起线程、不写文件**
（落文件一处都不落）。
"""

from __future__ import annotations

from meeting.summary.client import (
    DEFAULT_TIMEOUT_SECONDS,
    ERROR_REASONS,
    HttpRequest,
    HttpResponse,
    ModelClient,
    SummaryError,
    endpoint_url,
)
from meeting.summary.parse import (
    SECTION_LABELS,
    SECTION_ORDER,
    all_sections_unlabeled,
    parse_bullets,
    split_sections,
)
from meeting.summary.prompt import build_messages, build_retry_messages
from meeting.summary.runner import (
    WINDOW_CHOICES,
    SummaryOutcome,
    run_summary,
    sections_for,
)
from meeting.summary.settings import (
    DEFAULT_WINDOW,
    SETTING_KEYS,
    SummarySettings,
    normalize_window,
    parse_extra_body,
    window_seconds_of,
)
from meeting.summary.window import (
    BufferedLine,
    TranscriptBuffer,
    render_window,
    select_window,
    source_label,
)

__all__ = [
    "BufferedLine",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_WINDOW",
    "ERROR_REASONS",
    "HttpRequest",
    "HttpResponse",
    "ModelClient",
    "SECTION_LABELS",
    "SECTION_ORDER",
    "SETTING_KEYS",
    "SummaryError",
    "SummaryOutcome",
    "SummarySettings",
    "TranscriptBuffer",
    "WINDOW_CHOICES",
    "all_sections_unlabeled",
    "build_messages",
    "build_retry_messages",
    "endpoint_url",
    "normalize_window",
    "parse_bullets",
    "parse_extra_body",
    "render_window",
    "run_summary",
    "sections_for",
    "select_window",
    "source_label",
    "split_sections",
    "window_seconds_of",
]
