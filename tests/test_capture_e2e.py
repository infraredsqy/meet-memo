"""端到端（假源）：录下的东西能不能信。

进程内真 `ServiceRuntime` + 真采集路径，唯一换掉的是最外面那层声卡库——配置键
`capture.backend = "tests._capture_faulty:FaultyCaptureBackend"` 把假端点表与带剧本的
假源挂进真后端，读线程 / 写线程 / 电平刻度 / 收尾三步与转码独立工序 / `session.json`
全是生产代码。不起子进程（时间预算），不碰真声卡。
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from meeting.capture.engine import CaptureSession
from meeting.session import SESSION_FILE_NAME, archive_name
from tests._capture_faulty import (
    BACKEND_TARGET,
    CHUNK_BYTES,
    CHUNK_FRAMES,
    DELIVER_LOOPBACK_ENV,
    DELIVER_MIC_ENV,
    ERROR_AT_LOOPBACK_ENV,
    FaultyCaptureBackend,
    expected_archive_bytes,
    PACE_SECONDS_ENV,
)
from tests._app_drivers import live, post, snapshot_of
from tests._helpers import wait_until
from tests._capture_fakes import FakeCaptureSource, audit_pcm, replay_level_frames, samples_within_one_block

pytestmark = pytest.mark.slow

E2E_POLL_TIMEOUT_SECONDS = 15.0

#: 「两路先攒出这么多块再收尾」（量级得够 MP3 时长有得比）
RECORD_CHUNKS = 16

SOURCE_ORDER = ("mic", "loopback")

#: MP3 与 PCM 的时长容差（秒）。MP3 按帧编解码（16k 下一帧 576 样本 ≈ 36ms），
#: 编码器首尾各垫小半帧。
MP3_DURATION_TOLERANCE_SECONDS = 0.1


def _mp3_duration(mp3: Path) -> float:
    """`ffprobe` 读一份 MP3 的时长（秒）—— 解得出来本身就是「能解」的那一判。"""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            str(mp3),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return float(proc.stdout.strip())


def _session_dir_of(client) -> Path:
    return Path(snapshot_of(client)["session"]["dir"])


def _drain_steps(snapshot: dict) -> dict:
    return {step["id"]: step["status"] for step in snapshot["ended"]["steps"]}


def _wait_for_done(client) -> dict:
    return wait_until(
        lambda: (
            lambda snap: snap
            if snap["ended"] and snap["ended"]["phase"] == "done"
            else None
        )(snapshot_of(client)),
        timeout=E2E_POLL_TIMEOUT_SECONDS,
        what="收尾停在 done",
    )


def _wait_transcode_done(session_dir: Path) -> dict:
    """等转码独立工序收工（flush 落定自动起的那一道）。"""
    from meeting.capture import transcode as transcode_module

    def _settled():
        document = transcode_module.read_status(session_dir)
        if document is not None and document.get("state") in ("done", "partial", "failed"):
            return document
        return None

    return wait_until(
        _settled,
        timeout=E2E_POLL_TIMEOUT_SECONDS,
        what="转码独立工序收工（状态文件落终档）",
    )


def test_a_full_session_reconciles_frame_by_frame(tmp_path: Path, monkeypatch):
    """假源的一场正常会：收尾三步落定、转码独立工序收工，两路归档逐字节、逐帧、逐样本全部对上。

    墙钟核对的确定性手段：两路的交付上限都钉在 `RECORD_CHUNKS`（喂满之后活着但不再
    出块）。于是「取墙钟读数」的那一刻，最后一块已经落定、之后不可能再有块进来 ——
    偏差不会随「stop 赶没赶上下一块」翻转。
    """
    monkeypatch.setenv(PACE_SECONDS_ENV, "0.1")
    monkeypatch.setenv(DELIVER_MIC_ENV, str(RECORD_CHUNKS))
    monkeypatch.setenv(DELIVER_LOOPBACK_ENV, str(RECORD_CHUNKS))
    # 这一场只验采集侧的记录：`transcriber.backend` 显式取 `null` 替身。
    # 不写这一项的话产品缺省 `auto` 会解析到 `meeting.backends.transcribe:TranscriberBackend`，
    # 而这一场喂进去的是**真音频**——那条流水线会真的去加载 GLM 权重，违背
    # 「任何用例都不许加载真模型」这条硬要求。
    with live(
        tmp_path,
        {
            "capture.backend": BACKEND_TARGET,
            "transcriber.backend": "null",
            "post.hook": "null",
        },
    ) as (client, runtime, _home, out_root):
        backend = runtime.capture_backend
        # 看门：配置键真的解析到本单元的挂件（解析失败会静默回退替身，这里当场说话）
        assert isinstance(backend, FaultyCaptureBackend)

        started_at = time.monotonic()
        assert post(client, "start", {"mode": "both"}).status_code == 200
        wait_until(
            lambda: snapshot_of(client)["top_state"] == "recording",
            timeout=E2E_POLL_TIMEOUT_SECONDS,
            what="会话进入 recording",
        )
        wait_until(
            lambda: backend.fed_chunks("mic") >= RECORD_CHUNKS
            and backend.fed_chunks("loopback") >= RECORD_CHUNKS,
            timeout=E2E_POLL_TIMEOUT_SECONDS,
            what=f"两路各喂满 {RECORD_CHUNKS} 块",
        )
        stopped_at = time.monotonic()
        assert post(client, "stop").status_code == 200
        done = _wait_for_done(client)

        steps = _drain_steps(done)
        # `post` 那一格落 `waiting`（会后不自动跑，等你按按钮），`done` 是收尾 phase 的终态；
        # 转码已摘出收尾序列：flush 落定那一刻它在后台自动起（真后端带 transcode 能力），
        # 这里等独立工序自己收工，再核对它的产物。
        assert steps == {
            "flush": "done", "log": "done", "post": "waiting",
        }

        session_dir = _session_dir_of(client)
        assert str(session_dir).startswith(str(out_root))
        _wait_transcode_done(session_dir)
        # 落定后的清单；原子写留在盘上的 `*.tmp` 是在途件，不算这一档的东西
        landed = sorted(
            item.name for item in session_dir.iterdir() if not item.name.endswith(".tmp")
        )
        assert landed == sorted(
            [
                SESSION_FILE_NAME,
                archive_name("mic"), archive_name("loopback"),
                "meeting_audio_mic_001.mp3", "meeting_audio_loopback_001.mp3",
                "meeting.log",
                "meeting_transcode_status.json", "meeting_transcode_progress.jsonl",
                "meeting_transcode_mic_001.json", "meeting_transcode_loopback_001.json",
            ]
        )

        wall_seconds = stopped_at - started_at
        mic_pcm = (session_dir / archive_name("mic")).read_bytes()
        loop_pcm = (session_dir / archive_name("loopback")).read_bytes()
        evidence: dict = {"wall_seconds": round(wall_seconds, 3)}
        audits: dict = {}

        for source, data in (("mic", mic_pcm), ("loopback", loop_pcm)):
            fed = backend.fed_chunks(source)
            assert fed == RECORD_CHUNKS
            audit = audit_pcm(
                data,
                expected_bytes=expected_archive_bytes(source, fed),
                expected_samples=fed * CHUNK_FRAMES,
            )
            audits[source] = audit
            assert audit.sample_gap == 0
            assert samples_within_one_block(audit.samples, wall_seconds)
            assert audit.even_bytes and audit.trailing_bytes == 0
            assert audit.byte_match is True
            assert audit.frame_indices == list(range(audit.frames))
            assert audit.missing_frame_indices == []
            readings = replay_level_frames(data)
            assert [reading.index for reading in readings] == audit.frame_indices
            evidence[source] = {
                "fed_chunks": fed,
                "bytes": audit.total_bytes,
                "samples": audit.samples,
                "seconds": round(audit.seconds, 3),
                "wall_deviation_samples": audit.samples - round(wall_seconds * 16000),
                "frames": audit.frames,
                "voiced_frames": audit.voiced_frames,
                "silent_frames": audit.silent_frames,
            }
            duration = _mp3_duration(session_dir / f"meeting_audio_{source}_001.mp3")
            assert abs(duration - audit.seconds) <= MP3_DURATION_TOLERANCE_SECONDS
            evidence[source]["mp3_duration"] = round(duration, 3)

        record = json.loads((session_dir / SESSION_FILE_NAME).read_text(encoding="utf-8"))
        assert record["state"] == "done"
        assert record["interrupted"] is False
        assert {step["id"]: step["status"] for step in record["drain"]["steps"]} == steps
        assert record["ended_at"] is not None
        assert [item["archives"] for item in record["sources"]] == [
            [archive_name("mic")], [archive_name("loopback")],
        ]
        # 段记录：一场恰好一段，权威时长 = 在录时长之和
        assert len(record["segments"]) == 1
        segment = record["segments"][0]
        assert segment["index"] == 1
        assert segment["started_at"] and segment["ended_at"]
        assert segment["sources"] == ["mic", "loopback"]
        assert segment["pauses"] == []
        assert segment["duration_seconds"] == pytest.approx(
            max(audits[source].seconds for source in SOURCE_ORDER), abs=0.15
        )
        assert record["audio_duration_seconds"] == pytest.approx(
            segment["duration_seconds"], abs=0.001
        )

        print("E2E-EVIDENCE " + json.dumps(evidence, ensure_ascii=False))


# ---------------------------------------------------------------------------
# ② 半路掉一路：另一路归档完好无损
# ---------------------------------------------------------------------------


def test_a_route_dying_midrun_leaves_the_other_route_intact(tmp_path: Path, monkeypatch):
    """loopback 喂满 10 块后抛错：该路 `lost`、顶层 `alert`，mic 照常采、MP3 照出。"""
    monkeypatch.setenv(PACE_SECONDS_ENV, "0.1")
    monkeypatch.setenv(ERROR_AT_LOOPBACK_ENV, "10")
    # 同 ①：这一场喂的是**真音频**，转写显式取 `null` 替身，别让那条流水线去加载真模型；
    # 会后钩子也取 `null`（同 ① 的理由：这一场只验采集侧的记录）。
    with live(
        tmp_path,
        {
            "capture.backend": BACKEND_TARGET,
            "transcriber.backend": "null",
            "post.hook": "null",
        },
    ) as (client, runtime, _home, _out):
        backend = runtime.capture_backend
        assert isinstance(backend, FaultyCaptureBackend)
        assert post(client, "start", {"mode": "both"}).status_code == 200
        session_dir = _session_dir_of(client)
        mic_file = session_dir / archive_name("mic")

        # 掉线**前**：两路都在采，mic 的字节数在涨
        wait_until(lambda: backend.fed_chunks("mic") >= 4, what="mic 先采起来")
        size_before_death = mic_file.stat().st_size
        wait_until(
            lambda: mic_file.stat().st_size >= size_before_death + 2 * CHUNK_BYTES,
            what="掉线之前 mic 的归档就在增长",
        )

        # 掉线：loopback 第 10 块交付后的下一次 read 抛错 → SourceLost → alert
        wait_until(
            lambda: snapshot_of(client)["top_state"] == "alert",
            timeout=E2E_POLL_TIMEOUT_SECONDS,
            what="loopback 掉线把顶层带进 alert",
        )
        snapshot = snapshot_of(client)
        sources = {item["id"]: item for item in snapshot["sources"]}
        assert sources["loopback"]["health"] == "lost"
        assert sources["mic"]["health"] == "capturing"
        assert snapshot["alerts"]["critical"] is not None
        assert backend.created["loopback"].started is True
        assert backend.created["loopback"].fed == 10  # 真采到第 10 块才死的
        size_at_death = mic_file.stat().st_size

        # 掉线**后**：mic 的字节数继续按块长（3200 字节/块 = 32000 字节/秒）增长
        wait_until(
            lambda: mic_file.stat().st_size >= size_at_death + 4 * CHUNK_BYTES,
            timeout=E2E_POLL_TIMEOUT_SECONDS,
            what="掉线之后 mic 的归档继续增长",
        )

        assert post(client, "stop").status_code == 200
        done = _wait_for_done(client)
        # 第 4 步按 R26 落 `waiting`（会后不自动跑，等你按按钮），`done` 是收尾 phase 的终态
        assert _drain_steps(done) == {
            "flush": "done", "log": "done", "post": "waiting",
        }

        # 死掉那一路留下恰好那 10 块，逐字节可对（半份归档照样可信）
        loop_data = (session_dir / archive_name("loopback")).read_bytes()
        audit = audit_pcm(
            loop_data,
            expected_bytes=expected_archive_bytes("loopback", 10),
            expected_samples=10 * CHUNK_FRAMES,
        )
        assert audit.samples == 10 * CHUNK_FRAMES
        assert audit.byte_match is True
        assert audit.frame_indices == list(range(audit.frames))

        # mic 那一路：字节数 = 喂进去的块数 × 块长，且比掉线时刻只多不少
        mic_data = (session_dir / archive_name("mic")).read_bytes()
        mic_fed = backend.fed_chunks("mic")
        assert len(mic_data) == mic_fed * CHUNK_BYTES
        assert len(mic_data) > size_at_death
        # 收尾的 MP3：mic 照样出得来（loopback 的 PCM 非空，也有它的一份）
        # 转码独立工序收工（flush 落定自动起的那一道）之后 MP3 才在场
        _wait_transcode_done(session_dir)
        assert (session_dir / "meeting_audio_mic_001.mp3").exists()
        assert (session_dir / "meeting_audio_loopback_001.mp3").exists()


