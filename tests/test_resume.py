"""`tests/test_resume.py` —— 续接的端到端验收。

**全程假采集**：`capture.backend = tests._capture_faulty:FaultyCaptureBackend`
（真后端 + 假端点表 + 带剧本的假源，段记录/归档/字节数全真），进程内真
`ServiceRuntime`，不起子进程、不碰真声卡、不放音、不加载任何模型
（转写与会后钩子显式取 null 替身；断口的产消两半分别在
`tests/test_asr_pipeline.py` 与既有 SSE/界面链路上验）。

一场戏：**录一段、结束，再对同一个工作区接着录一段**——

  - 旧段的 PCM 逐字节原样（续接要是截断了旧文件，这里当场红）；
  - `session.json` 里两段齐：段记录按序号升序、权威时长 = 两段之和；
  - 各路的段文件名表两段都在，收尾转码给两段各产一份 MP3；
  - 界面的取数端点（`GET /api/sessions` 与 `GET /api/sessions/{id}`）看得到两段
    的时长与时间。
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest

from meeting.session import SESSION_FILE_NAME, archive_name
from tests._capture_faulty import (
    BACKEND_TARGET,
    DELIVER_LOOPBACK_ENV,
    DELIVER_MIC_ENV,
    PACE_SECONDS_ENV,
    expected_archive_bytes,
)
from tests._app_drivers import live, post, snapshot_of
from tests._helpers import wait_until

pytestmark = pytest.mark.slow

#: 进程内用例的轮询超时（收尾三步跑在收尾线程池里）
POLL_TIMEOUT_SECONDS = 20.0

#: 两场各喂多少块（0.1 秒一块）：第一场 10 块、续接那一场 12 块，两段的时长拉开可辨
FIRST_CHUNKS = 10
SECOND_CHUNKS = 12

#: 块节奏（秒/块）：0.02 秒一块
PACE_SECONDS = "0.02"


def _wait_for_done(client) -> dict:
    return wait_until(
        lambda: (
            lambda snap: snap
            if snap["ended"] and snap["ended"]["phase"] == "done"
            else None
        )(snapshot_of(client)),
        timeout=POLL_TIMEOUT_SECONDS,
        what="收尾停在 done",
    )


def _wait_recording(client) -> None:
    wait_until(
        lambda: snapshot_of(client)["top_state"] == "recording",
        timeout=POLL_TIMEOUT_SECONDS,
        what="会话进入 recording",
    )


def _read_record(session_dir: Path) -> dict:
    return json.loads((session_dir / SESSION_FILE_NAME).read_text(encoding="utf-8"))


def test_a_resumed_session_keeps_the_old_segment_and_adds_a_new_one(tmp_path, monkeypatch):
    """录一段、结束，对同一个工作区再录一段：旧段原样、段记录两段、时长是和。"""
    monkeypatch.setenv(PACE_SECONDS_ENV, PACE_SECONDS)
    monkeypatch.setenv(DELIVER_MIC_ENV, str(FIRST_CHUNKS))
    monkeypatch.setenv(DELIVER_LOOPBACK_ENV, str(FIRST_CHUNKS))
    with live(
        tmp_path,
        {
            "capture.backend": BACKEND_TARGET,
            "transcriber.backend": "null",
            "post.hook": "null",
        },
    ) as (client, runtime, _home, out_root):
        workspace = Path(out_root) / "跟老王聊周会"
        workspace.mkdir()

        assert post(client, "start", {"dir": str(workspace)}).status_code == 200
        _wait_recording(client)
        backend = runtime.capture_backend
        wait_until(
            lambda: backend.fed_chunks("mic") >= FIRST_CHUNKS
            and backend.fed_chunks("loopback") >= FIRST_CHUNKS,
            timeout=POLL_TIMEOUT_SECONDS,
            what="第一场两路各喂满",
        )
        assert post(client, "stop").status_code == 200
        _wait_for_done(client)
        first = _read_record(workspace)
        assert [item["index"] for item in first["segments"]] == [1]
        old_bytes = {
            source: (workspace / archive_name(source, 1)).read_bytes()
            for source in ("mic", "loopback")
        }
        for source in ("mic", "loopback"):
            assert old_bytes[source] == expected_archive_bytes(source, FIRST_CHUNKS)

        monkeypatch.setenv(DELIVER_MIC_ENV, str(SECOND_CHUNKS))
        monkeypatch.setenv(DELIVER_LOOPBACK_ENV, str(SECOND_CHUNKS))
        assert post(client, "start", {"dir": str(workspace)}).status_code == 200
        _wait_recording(client)
        engine = getattr(backend, "session", None)
        assert engine is not None and engine.segment_number == 2, (
            "续接这一段该开出段 2（段 1 一个字节不碰）"
        )
        wait_until(
            lambda: backend.fed_chunks("mic") >= SECOND_CHUNKS
            and backend.fed_chunks("loopback") >= SECOND_CHUNKS,
            timeout=POLL_TIMEOUT_SECONDS,
            what="续接一场两路各喂满",
        )
        assert post(client, "stop").status_code == 200
        _wait_for_done(client)

        # 旧段逐字节原样（最大的坑就是这里被截断清零）
        for source in ("mic", "loopback"):
            assert (workspace / archive_name(source, 1)).read_bytes() == old_bytes[source]
            new_path = workspace / archive_name(source, 2)
            assert new_path.read_bytes() == expected_archive_bytes(source, SECOND_CHUNKS)

        record = _read_record(workspace)
        assert record["state"] == "done"
        assert record["session_id"] == first["session_id"]
        assert record["created_at"] == first["created_at"]
        assert record["started_at"] == first["started_at"]
        assert record["mode"] == first["mode"]
        assert [item["index"] for item in record["segments"]] == [1, 2]
        duration_one, duration_two = (
            item["duration_seconds"] for item in record["segments"]
        )
        assert duration_one == pytest.approx(FIRST_CHUNKS * 0.1, abs=0.05)
        assert duration_two == pytest.approx(SECOND_CHUNKS * 0.1, abs=0.05)
        assert record["audio_duration_seconds"] == pytest.approx(
            duration_one + duration_two, abs=0.001
        )
        for item in record["sources"]:
            assert item["archives"] == [
                archive_name(item["id"], 1),
                archive_name(item["id"], 2),
            ]

        from meeting.capture import transcode as transcode_module

        wait_until(
            lambda: (
                doc
                if (doc := transcode_module.read_status(workspace)) is not None
                and doc.get("state") == "done"
                else None
            ),
            timeout=POLL_TIMEOUT_SECONDS,
        )
        for source in ("mic", "loopback"):
            for index in (1, 2):
                assert (workspace / archive_name(source, index)).with_suffix(".mp3").is_file()

        listing = client.get("/api/sessions").json()
        row = next(item for item in listing["sessions"] if item["dir"] == str(workspace))
        assert [item["index"] for item in row["segments"]] == [1, 2]
        status = client.get(
            f"/api/sessions/{urllib.parse.quote(str(workspace), safe='')}"
        ).json()
        assert [item["index"] for item in status["segments"]] == [1, 2]
        assert status["segments"][0]["duration_seconds"] == pytest.approx(duration_one)
