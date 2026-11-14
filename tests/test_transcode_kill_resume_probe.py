"""造场探针：转码跑到一半强杀进程 → 重启 → 补跑收口。

**造场**（真服务子进程 + 真 ffmpeg 转码； autopilot 红线之内 —— 不碰任何模型端点）：

  1. 工作区里躺着一场会：两路各 10 段真 PCM（每段 1.5 秒，字节是确定的正弦形状），
     `session.json` 照真实形状声明 sources 与段归档；
  2. 经 `workspace_open` 登记进工作台，`transcode_run` 起真转码工序；
  3. 盘上落出数段认账凭据（短间隔探测循环等这个事件，不数钟点）**之后立刻强杀进程**
     （`kill()`，不是优雅退出）——盘上留下「半路的凭据 + 停在 `running` 的状态文件 + 可能的半截产物」；
  4. 把其中一段已转完的 MP3 改成半截垃圾（凭据记的字节数对不上 → 不认账）；
  5. 重启服务，再按一次 `transcode_run`（界面补跑与它同一条路）。

**断言**（读数全部落在盘上，判据与服务共用 `meeting/capture/transcode.py` 那一份）：

  - 工序收工落 `done`；
  - 每一段都有认账的凭据、每一段 MP3 都能被 `pydub` 解回，时长与对应 PCM 对得上
    （字节数 ÷ 32000，容差 0.15 秒）；
  - 被改成半截垃圾的那一段**没有被跳过**：补跑把它重做回可解的成品。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from meeting.capture import transcode as transcode_module
from tests import _helpers as helpers
from tests._helpers import wait_until
from tests._service import post_action

pytestmark = pytest.mark.slow

#: 段要够多，强杀才来得及落在工序半路。
SEGMENTS_PER_SOURCE = 10
SEGMENT_SECONDS = 1.5
#: PCM 的形状（16k / 单声道 / int16）。
BYTES_PER_SECOND = 32000

#: 杀前事件：盘上认账凭据落满 2 段就杀，等的是「凭据落盘」这个事实、不数钟点。
#: 取小是为了让强杀尽量早地落进工序半路，与「20 段全部转完」拉开间隔；判据与服务
#: 共用 `segment_done` 那一份（凭据在且认账），补跑认的正是同一批凭据。
KILL_AFTER_SEGMENTS = 2
#: 短间隔探测循环，不拿固定长 sleep 等杀点。
KILL_PROBE_INTERVAL_SECONDS = 0.02
KILL_WAIT_SECONDS = 20.0

MP3_DURATION_TOLERANCE_SECONDS = 0.15

RESUME_TIMEOUT_SECONDS = 60.0

SOURCES = ("mic", "loopback")


def _pcm_bytes(segment: int, source: str) -> bytes:
    """一段确定的 PCM（1.5 秒、440Hz 正弦、int16 小端）：内容不重要，时长对得上就行。"""
    import array

    frames = int(SEGMENT_SECONDS * 16000)
    payload = array.array("h")
    for index in range(frames):
        # 段与路都进相位，两段的内容不重样
        value = int(12000.0 * math.sin(2.0 * math.pi * 440.0 * index / 16000.0 + segment + (source == "loopback")))
        payload.append(value)
    return payload.tobytes()


def _craft_session(place: Path) -> Path:
    """造一场录完的会：两路各 10 段真 PCM + 照真实形状的 `session.json`。"""
    workspace = place / "261016-转码强杀现场"
    workspace.mkdir(parents=True, exist_ok=True)
    archives: dict[str, list[str]] = {}
    for source in SOURCES:
        names = []
        for segment in range(1, SEGMENTS_PER_SOURCE + 1):
            name = f"meeting_audio_{source}_{segment:03d}.pcm"
            (workspace / name).write_bytes(_pcm_bytes(segment, source))
            names.append(name)
        archives[source] = names
    record = {
        "session_id": workspace.name,
        "dir": str(workspace),
        "date": "2026-10-16",
        "mode": "both",
        "state": "done",
        "created_at": "2026-10-16T09:00:00+08:00",
        "started_at": "2026-10-16T09:00:00+08:00",
        "ended_at": "2026-10-16T09:00:20+08:00",
        "interrupted": False,
        "sources": [
            {"id": source, "device": None, "archives": archives[source]}
            for source in SOURCES
        ],
        "segments": [],
        "audio_duration_seconds": SEGMENTS_PER_SOURCE * SEGMENT_SECONDS,
        "drain": {
            "steps": [
                {"id": "flush", "status": "done", "detail": "", "error": None},
                {"id": "log", "status": "done", "detail": "", "error": None},
                {"id": "post", "status": "waiting", "detail": "", "error": None},
            ]
        },
        "service": {"pid": 0, "started_at": "2026-10-16T09:00:00+08:00"},
    }
    (workspace / "session.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return workspace


def _progress_rows(session_dir: Path) -> list[dict[str, Any]]:
    _header, rows = transcode_module.read_progress(session_dir)
    return rows


def _done_segments(
    plan: list[transcode_module.TranscodeSegment],
) -> list[transcode_module.TranscodeSegment]:
    """计划里凭据已认账的段（按计划顺序；判据与服务共用 `segment_done` 那一份）。"""
    return [entry for entry in plan if transcode_module.segment_done(entry)]


def test_a_transcode_killed_mid_run_resumes_and_every_mp3_decodes(
    tmp_path: Path, meeting_env: Any, service_process: Any
) -> None:
    """强杀在工序半路 → 重启 → 补跑：每段凭据认账、每段 MP3 可解且时长对得上。"""
    env = meeting_env
    workspace = _craft_session(env.out_root)
    env.write_config({"capture.backend": "dev"})

    proc = service_process(home=env.home, out_root=env.out_root, extra_env=env.env)
    base_url = proc.wait_ready(timeout=20.0)
    opened = post_action(base_url, "workspace_open", {"dir": str(workspace)})
    assert opened.status_code == 200, opened.text
    receipt = post_action(base_url, "transcode_run", {"dir": str(workspace)})
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["transcode"] == {
        "kind": "transcode",
        "dir": str(workspace),
        "started": True,
    }, receipt.text

    plan = transcode_module.plan_segments(workspace, SOURCES)
    done_segments = wait_until(
        lambda: (done if len(done := _done_segments(plan)) >= KILL_AFTER_SEGMENTS else None),
        timeout=KILL_WAIT_SECONDS,
        interval=KILL_PROBE_INTERVAL_SECONDS,
        what=f"盘上落出 ≥{KILL_AFTER_SEGMENTS} 段认账凭据",
    )
    proc.kill()
    killed_rows = _progress_rows(workspace)
    assert len(killed_rows) < SEGMENTS_PER_SOURCE * len(SOURCES), (
        f"强杀该落在工序半路（实测进度 {len(killed_rows)} 行）"
    )
    on_disk_status = transcode_module.read_status(workspace)
    assert on_disk_status is not None, "强杀后盘上该停着一版状态文件"
    assert on_disk_status["state"] in ("running", "paused"), on_disk_status

    victim = done_segments[0]
    victim_mp3 = victim.mp3
    assert victim_mp3.is_file(), "被拿来造半截的那一段该已有产物"
    victim_mp3.write_bytes(b"half-written-garbage")

    proc2 = service_process(home=env.home, out_root=env.out_root, extra_env=env.env)
    base_url2 = proc2.wait_ready(timeout=20.0)
    # 重启后的读数：盘上停着的那版状态照它答，in_process 如实为假（本进程没人跑）
    single = helpers.local_get(f"{base_url2}/api/sessions/{quote(str(workspace))}")
    assert single.status_code == 200, single.text
    transcode_cell = single.json()["transcode"]
    assert transcode_cell["state"] in ("running", "paused"), transcode_cell
    assert transcode_cell["in_process"] is False, transcode_cell

    rerun = post_action(base_url2, "transcode_run", {"dir": str(workspace)})
    assert rerun.status_code == 200, rerun.text

    document = wait_until(
        lambda: (
            doc
            if (doc := transcode_module.read_status(workspace)) is not None
            and doc.get("state") in ("done", "partial", "failed")
            else None
        ),
        timeout=RESUME_TIMEOUT_SECONDS,
    )
    assert document["state"] == "done", document

    from pydub import AudioSegment

    plan = transcode_module.plan_segments(workspace, SOURCES)
    assert len(plan) == SEGMENTS_PER_SOURCE * len(SOURCES), len(plan)
    for entry in plan:
        assert transcode_module.segment_done(entry), (
            f"{entry.source} 第 {entry.segment} 段没有认账的凭据"
        )
        mp3_path = entry.mp3
        assert mp3_path.is_file(), f"{mp3_path.name} 缺席"
        pcm_seconds = entry.pcm.stat().st_size / BYTES_PER_SECOND
        duration = len(AudioSegment.from_file(mp3_path)) / 1000.0
        assert abs(duration - pcm_seconds) <= MP3_DURATION_TOLERANCE_SECONDS, (
            f"{mp3_path.name} 时长 {duration:.3f}s 与 PCM {pcm_seconds:.3f}s 对不上"
        )
    assert victim_mp3.read_bytes() != b"half-written-garbage", (
        "半截产物该被重做，而不是被当成品跳过"
    )
