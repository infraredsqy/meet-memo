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
    CAPTURE_POLL_TIMEOUT_SECONDS,
    SSE_READ_TIMEOUT_SECONDS,
    TOP_LEVEL_KEYS,
    TRANSCRIPT_LINE_KEYS,
    contract_regen,
)

pytestmark = pytest.mark.slow


def _read_frames_until(client, names: set[str], *, timeout: float) -> dict[str, dict]:
    """读到 `names` 里每一个 event 名都到手为止，返回 `{event 名: 帧}`。

    **不依赖帧的到达次序**。`levels` 是
    高频通道，它在 `start` 之后随时会来，谁先谁后由服务侧的广播节奏决定；按次序逐个
    `read_until()` 会把「哪一帧先到」写进断言，读起来像在验契约、实际在验时序。
    `revision` 的确定性因此改由另一条**与次序无关**的判据钉住：每一帧都自报它落在哪一版，
    这里逐帧比对「帧自报的 revision」与「帧内容里的 revision」必须一致（见调用点）。
    """
    import time as _time

    wanted = set(names)
    found: dict[str, dict] = {}
    deadline = _time.monotonic() + float(timeout)
    while wanted:
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"{timeout}s 内没读齐这些 event：{sorted(wanted)}（已到手 {sorted(found)}）"
            )
        frame = client.read_frame(timeout=remaining)
        name = frame.get("event")
        if name in wanted:
            wanted.discard(name)
            found[name] = frame
    return found


def test_capture_sse_frames(tmp_path, service_process, meeting_env, contract_regen):
    """snapshot 首帧、state、levels、transcript 四个帧样本，全部出自真服务的 `/api/events`。

    读帧**不依赖到达次序**（`_read_frames_until()`）：四类帧都在同一次 `start` 之后的
    广播节奏里先后到达，断言只看内容。`revision` 的确定性由帧自己的两处自报钉住 ——
    `snapshot` / `state` 的 `id:` 等于它 `data.revision`，`levels` / `transcript` 不带
    `id:` 但带当前 `revision` 作参照，四帧因此落在同一版上，与它们谁先到无关。
    """
    stub_dir = tmp_path / "contract-transcriber-stub"
    selector = _contract.write_transcriber_stub(stub_dir)
    meeting_env.write_config({"capture.backend": "dev", "transcriber.backend": selector})
    proc = service_process(
        home=meeting_env.home,
        extra_env=meeting_env.env,
        pythonpath=os.pathsep.join([str(helpers.REPO_ROOT), str(stub_dir)]),
    )
    url = proc.wait_ready()
    assert post_action(
        url, "start", {"mode": "both", "dir": helpers.allocate_start_dir(meeting_env.out_root)}
    ).status_code == 200

    # 转写行与包络都落稳之后才连 SSE：首帧的 transcript.last_line_at 与包络长度因此确定
    wait_until(
        lambda: (lambda snap: snap if snap["transcript"]["last_line_at"] else None)(
            proc.snapshot()
        ),
        timeout=CAPTURE_POLL_TIMEOUT_SECONDS,
    )
    wait_until(
        lambda: (
            lambda snap: snap
            if all(
                len(item["envelope"]) == ENVELOPE_BAR_COUNT for item in snap["sources"]
            )
            else None
        )(proc.snapshot()),
        timeout=CAPTURE_POLL_TIMEOUT_SECONDS,
    )

    with SseClient(proc.base_url, timeout=SSE_READ_TIMEOUT_SECONDS) as client:
        first = client.read_event_frame(timeout=SSE_READ_TIMEOUT_SECONDS)
        assert first["event"] == "snapshot"
        assert first["id"] is not None
        assert int(first["id"]) == first["data"]["revision"]
        assert set(first["data"]) == TOP_LEVEL_KEYS
        assert first["data"]["top_state"] == "recording"
        assert [item["id"] for item in first["data"]["sources"]] == ["mic", "loopback"]

        # 三帧里 `levels` 与 `transcript` 的到达先后由服务侧的广播节奏决定，读齐即可；
        # `state` 帧单独在下面用 mute 触发（它是一次真动作的产物，不该混进这条等待里）。
        others = _read_frames_until(
            client, {"levels", "transcript"}, timeout=SSE_READ_TIMEOUT_SECONDS
        )

        levels = others["levels"]
        assert levels["id"] is None
        levels_payload = levels["data"]
        assert set(levels_payload) == {"revision", "frames"}
        assert set(levels_payload["frames"]) == {"mic", "loopback"}
        for item in levels_payload["frames"].values():
            assert set(item) == {"ts", "dbfs", "peak", "bar", "envelope"}
            assert item["ts"] is None  # levels 帧的 ts 恒为 null
            assert len(item["envelope"]) == ENVELOPE_BAR_COUNT
            assert item["bar"] == item["envelope"][-1]

        transcript = others["transcript"]
        assert transcript["id"] is None
        transcript_payload = transcript["data"]
        assert set(transcript_payload) == {"revision", "line"}
        assert set(transcript_payload["line"]) == TRANSCRIPT_LINE_KEYS
        assert transcript_payload["line"]["source"] == "mic"
        assert transcript_payload["line"]["kind"] == "line"
        assert transcript_payload["line"]["text"]

        # 两个增量通道不带 `id:`，`data.revision` 就是它们自报的落点：与首帧同一版
        # （start 之后没再提交过），这条判据与到达次序无关。
        assert levels_payload["revision"] == first["data"]["revision"]
        assert transcript_payload["revision"] == first["data"]["revision"]

        assert post_action(proc.base_url, "mute_session").status_code == 200
        state = client.read_until("state", timeout=SSE_READ_TIMEOUT_SECONDS)
        assert state["event"] == "state"
        assert state["id"] is not None
        assert int(state["id"]) == state["data"]["revision"]
        # mute 提交一次：state 帧落在首帧之后的那一版上。
        assert state["data"]["revision"] == first["data"]["revision"] + 1
        assert set(state["data"]) == TOP_LEVEL_KEYS

    emit(
        {
            "sse/first-snapshot.json": normalize_frame(first),
            "sse/levels.json": normalize_frame(levels),
            "sse/transcript.json": normalize_frame(transcript),
            "sse/state.json": normalize_frame(state),
        },
        regen=contract_regen,
    )
