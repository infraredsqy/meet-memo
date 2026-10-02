"""会中总结内核：按一下，十到十五秒出结果的那一整条（跟界面无关的那一段）。

## 这个包在干什么

会议进行中，有人跟丢了，按下「总结一下」：把最近这段时间的**实时临时稿**读一遍，
给回三小段——刚才在讲什么 / 讲到哪儿了 / 丢掉了什么，几条就够，是一块救生板。
整条链在这里：

    TranscriptBuffer（内存里追加的实时转写）
        → select_window()      按行首时间戳取连续尾部（5m / 15m / 30m / 整场）
        → render_window()      窗口 → 喂给模型的文本（原句逐字保留，缺口也留着）
        → build_messages()     装配成一次 chat 调用要的消息
        → ModelClient.complete()  OpenAI 兼容端点（标准库 urllib，extra_body 并入）
        → parse_bullets()      抠出 {"bullets": [...]}（容忍 ```json 围栏、空数组合法）
        → split_sections()     按固定标签归到三小段
        → SummaryOutcome       一次收场：那几条 + 等了多久 + 试了几次

`run_summary()` 是这一整条的入口：它把上面几步串起来，并且守住**一条墙钟 deadline**
（默认 25 秒）——预算用尽就停，并如实报出等了多久、试了几次。

## 跟会后纪要层的分工

两层的名字里的「纪要」容易混，它们服务的是会议的两头：

| | 本包（会中） | `meeting/minutes/`（会后） |
|---|---|---|
| 什么时候跑 | 人按一下，随叫随到 | 人按才跑：「直接出纪要」按钮或 `meeting post` |
| 吃什么 | 内存里的实时转写窗口 | 盘上的两条权威转写 |
| 模型怎么调 | 本包自己调 OpenAI 兼容端点 | 外包给一个 headless DSH 子会话 |
| 预算 | 十到十五秒（默认 deadline 25 秒） | 四小时（`minutes.timeout_seconds`） |
| 产物 | 几条，出现在应答体里，关掉即消失 | 一份 `meeting_final.md` 落在场次目录 |
| 落文件 | **一处都不落** | 状态文件 + 纪要 + 中间产物 |

**不搬 V1 的增量节奏**：每 10 段或 300 秒那一套自动总结留在 V1（spec 明文裁定），
本包里没有「什么时候触发」这件事——窗口由人按键挑。

## 边界

内核是一批可注入、可独立调用的函数与类：**不注册路由、不碰快照、不起线程、不写文件**。
服务接线（转写行的 sink 喂进缓冲、`summarize` 动作、`GET /api/summaries` 记录端点）
归 phase2；界面归 phase3。
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
