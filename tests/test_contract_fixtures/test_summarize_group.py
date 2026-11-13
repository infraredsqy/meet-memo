from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest


from meeting import config as config_module
from meeting.backends.base import ReadOnlyConfig, TranscriptLine
from meeting.service import runtime as runtime_module
from meeting.service.hub import SUBSCRIBER_QUEUE_SIZE
from meeting.state import ENVELOPE_BAR_COUNT
from tests import _contract
from tests import _drain_fakes as fakes
from tests import _helpers as helpers
from tests._contract import emit, normalize, normalize_frame
from tests._helpers import wait_until
from tests._service import SseClient, parse_sse_frame, post_action

from tests.test_contract_fixtures._common import (
    _act,
    _action_record,
    contract_regen,
    make_client,
)

#: 慢档标记（收尾轮）：真服务/真样本采集的收尾验收，不进每次迭代圈。
pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# 会中总结：动作样本 + 本场记录端点（设计）
# ---------------------------------------------------------------------------


class _ContractTranscriber:
    """样本采集用的假转写后端：`bind()` 记下出口、`health()` 恒 `normal`。

    与 `tests/_contract.py` 写出到子进程的那个假后端同形，区别只在它活在**进程内**
    （这一档走 TestClient，不另起服务子进程）。`start()` 什么都不起：喂行由用例自己经
    `bind_transcriber()` 那个 sink 做，样本的形状因此完全确定。
    """

    def __init__(self) -> None:
        self.line_sink = None

    def bind(self, line_sink, *, config=None) -> None:
        del config
        self.line_sink = line_sink

    def start(self, mode: str, session_dir: str) -> None:
        del mode, session_dir

    def stop(self) -> None:
        pass

    def health(self) -> str:
        return "normal"

    def backlog_depth(self) -> int:
        return 0

    def segment_timeout(self) -> float:
        return 20.0


def test_capture_summarize_group(tmp_path, make_client, contract_regen):
    """`summarize` 成功体 + `GET /api/summaries` 两份样本（假总结客户端注入，不发真请求）。

    窗口里铺 12 行（`ts` 取固定值：`render_window()` 会把它印成 `[HH:MM:SS]`，固定值让两次
    采集逐字一致），三小段由 `_contract.SummaryStub` 的固定输出给。**总结是只读的**：
    这一档前后都不提交，`revision` 因此停在 `start` 那一版上。

    `summary.api_key` 交一个**显眼的假值**：这一格就是 `SummarySettings.missing` 点名的那一格，
    空着的话动作会在出门前停下，而这一档采的是成功体。假客户端从注入点进
    （`runtime.summary_client`，把传输层整个替掉），这个假值不上任何线路。
    """
    client = make_client(
        {"capture.backend": "dev", "summary.api_key": "contract-fixture-fake-key"}
    )
    runtime = client.app.state.runtime

    transcriber = _ContractTranscriber()
    runtime.transcriber_backend = transcriber
    transcriber.bind(runtime.bind_transcriber(transcriber), config=runtime._config_view())
    stub = _contract.SummaryStub()
    runtime.summary_client = stub

    assert _act(client, "start", {"mode": "both"}).status_code == 200
    for index in range(12):
        transcriber.line_sink(
            TranscriptLine(ts=1.5, source="mic", text=f"样本第 {index} 行", kind="line")
        )

    summarized = _act(client, "summarize", {"window": "15m"})
    assert summarized.status_code == 200, summarized.text
    body = summarized.json()
    assert set(body) == {"ok", "revision", "summary"}
    summary = body["summary"]
    assert set(summary) == {
        "window", "window_label", "at", "elapsed_seconds", "lines",
        "what", "where", "lost",
    }
    assert summary["window"] == "15m"
    assert summary["window_label"] == "15 分钟"
    assert summary["lines"] == 12
    assert summary["what"] and summary["where"] and summary["lost"]
    assert stub.calls == 1

    # 记录端点：读的是**内存里**那一份（`session_id` 与 `session.id` 同值）
    listing = client.get("/api/summaries")
    assert listing.status_code == 200
    listed = listing.json()
    assert set(listed) == {"session_id", "entries"}
    assert listed["session_id"] == runtime.snapshot.session.id
    assert len(listed["entries"]) == 1
    entry = listed["entries"][0]
    assert set(entry) == {
        "window", "window_label", "at", "elapsed_seconds", "what", "where", "lost",
    }
    assert entry["at"] == summary["at"]

    # 两个新 `code` 各采一份失败体：`detail` 的形状与 HTTP 码都在样本里看得见。
    # ① `transcript_unavailable`(503)：把转写后端拿掉（健康度读不出 = 没在跑那一档）
    runtime.transcriber_backend = None
    transcript_down = _act(client, "summarize", {"window": "15m"})
    assert transcript_down.status_code == 503, transcript_down.text
    assert transcript_down.json()["code"] == "transcript_unavailable"
    assert transcript_down.json()["detail"] == {"health": "stopped"}
    runtime.transcriber_backend = transcriber

    # ② `summary_failed`(502) 的缺键那一档：把只读配置视图换成一份缺端点的（真配置文件不动）
    real_view = runtime._config_view
    runtime._config_view = lambda: ReadOnlyConfig(
        {"summary.base_url": "   ", "summary.api_key": "   ", "summary.model": "test-model"}
    )
    try:
        endpoint_missing = _act(client, "summarize", {"window": "15m"})
    finally:
        runtime._config_view = real_view
    assert endpoint_missing.status_code == 502, endpoint_missing.text
    assert endpoint_missing.json()["code"] == "summary_failed"
    assert endpoint_missing.json()["detail"] == {
        "missing": ["summary.base_url", "summary.api_key"],
    }

    emit(
        {
            "actions/summarize-ok.json": _action_record(
                summarized, "summarize", {"window": "15m"}
            ),
            "actions/fail-transcript-unavailable.json": _action_record(
                transcript_down, "summarize", {"window": "15m"}
            ),
            "actions/fail-summary-failed-missing.json": _action_record(
                endpoint_missing, "summarize", {"window": "15m"}
            ),
            "summaries/after-one.json": normalize(listed),
        },
        regen=contract_regen,
    )


