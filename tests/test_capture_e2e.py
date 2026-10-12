"""`tests/test_capture_e2e.py` —— 端到端（假源）：录下的东西能不能信（`dca4539b` phase3 §1）。

形态照 `tests/test_capture_backend.py` 那两条端到端：**进程内真 `ServiceRuntime`** +
真采集路径，唯一换掉的是最外面那层声卡库——配置键
`capture.backend = "tests._capture_faulty:FaultyCaptureBackend"`（`module:Class` 是
registry 的既有口径）把假端点表与带剧本的假源挂进真后端，读线程 / 写线程 / 电平刻度 /
收尾四步 / `session.json` 全是生产代码。不起子进程（时间预算），不碰真声卡
（`tests/conftest.py` 的看门原样在用）。

三场戏：

  1. **一场正常会，逐帧对账**：收尾 phase 走到 `done`（第 4 步 `post` 按 R26 停
     `waiting`，会后等你按按钮）；场次目录里两路 `.pcm` + 两个
     `.mp3` 都在；两路样本数与「喂进去的块数 × 每块样本数」相符、与「start 到 stop
     之间的墙钟秒数 × 16000」偏差在一个采集块以内、字节数为偶；归档字节与假源的
     喂块账**逐字节一致**（丢 0 秒）；按 0.5 秒重跑电平换算，帧序号连续无空洞，
     「有声帧 / 静音帧」两个计数落进证据（`-s` 可见）；两个 MP3 用 `ffprobe` 读出的
     时长与对应 PCM 时长相符。
  2. **半路掉一路**：loopback 真跑起来、喂满 10 块之后才抛错 → 该路 `lost`、
     `top_state` 进 `alert`；mic 的归档字节数在掉线前后**都在增长**；收尾照常
     `done`、mic 的 MP3 照样出得来；loopback 留下恰好那 10 块、逐字节可对。
  3. **转码吞吐**：10 分钟合成 PCM 量一次真 `transcode()` 的秒数，线性外推两小时，
     对 30 秒收尾预算说话（任务书 §3 给了开工前基线 1.69 秒，两个数摆在一起比）。

任务书 §3 写「loopback 在 3 秒后抛错」；为守住「单条用例 ≤ 5 秒」的看门红线
（`tests/conftest.py`），第 2 场改成「喂满 10 块（1.0 秒）后抛错」——语义同一档：
那一路真的起来、真的在采之后才死（`fed == 10` 当场断言），而非启动失败。
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
from tests.test_api_actions import live, post, snapshot_of, wait_until
from tests.test_capture_accounting import (
    audit_pcm,
    replay_level_frames,
    samples_within_one_block,
)
from tests.test_capture_engine import FakeCaptureSource

#: 慢档标记：`pytest.mark.slow` 的归位判据见 `docs/service.md` 第 7 章。
pytestmark = pytest.mark.slow

#: 进程内用例的轮询超时（收尾四步跑在收尾线程池里；与既有端到端同档）
E2E_POLL_TIMEOUT_SECONDS = 15.0

#: 「两路先攒出这么多块再收尾」（1.6 秒：3 个整 0.5 秒帧 + 尾数，MP3 时长也有得比）
RECORD_CHUNKS = 16

#: 两路的固定次序（对账循环与段账断言共用）
SOURCE_ORDER = ("mic", "loopback")

#: MP3 与 PCM 的时长容差（秒）。MP3 按帧编解码（16k 下一帧 576 样本 ≈ 36ms），编码器
#: 首尾各垫小半帧 —— 实测差距见 `phase3/evidence/`（本机 8.1 的 lame 对 1.6 秒素材差 0）。
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


# ---------------------------------------------------------------------------
# ① 一场正常会：逐帧对账 + MP3 能解 + 帧序号连续
# ---------------------------------------------------------------------------


def test_a_full_session_reconciles_frame_by_frame(tmp_path: Path, monkeypatch):
    """假源的一场正常会：收尾四步 `done`，两路归档逐字节、逐帧、逐样本全部对上。

    墙钟对账的确定性手段：两路的交付上限都钉在 `RECORD_CHUNKS`（喂满之后活着但不再
    出块）。于是「取墙钟读数」的那一刻，最后一块已经落定、之后不可能再有块进来 ——
    偏差不会随「stop 赶没赶上下一块」翻转（旋钮的说明见 `tests/_capture_faulty.py`）。
    """
    monkeypatch.setenv(PACE_SECONDS_ENV, "0.1")
    monkeypatch.setenv(DELIVER_MIC_ENV, str(RECORD_CHUNKS))
    monkeypatch.setenv(DELIVER_LOOPBACK_ENV, str(RECORD_CHUNKS))
    # 这一场只验采集侧的账：`transcriber.backend` 显式取 `null` 替身。
    # 不写这一项的话产品缺省 `auto` 会解析到 `meeting.backends.transcribe:TranscriberBackend`
    # （`9e5db89c` phase2 落地之后它就在场了），而这一场喂进去的是**真音频**——
    # 于是那条流水线会真的去加载 GLM 权重（实测把本用例拖到 7 秒，越过单条用例的 5 秒看门，
    # 也违背「任何用例都不许加载真模型」这条硬要求）。
    #
    # `post.hook` 同样显式取 `null`：会后钩子（`7c6952e5` phase2 的
    # `meeting.backends.post:PostProcessHook`）落地之后，产品缺省 `auto` 会让收尾第 4 步
    # 真的去起一条后台线程跑整条会后链、往这个场次目录里多落四五份产物 —— 那既不是本用例
    # 要验的账，也会让下面「场次目录里恰好这几份文件」那条断言变成一条与后台线程赛跑的断言。
    # 会后链自己的账在 `tests/test_post_hook.py` 与 `tests/test_offline_runner.py` 里验。
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
        # 第 4 步按 R26 落 `waiting`（会后不自动跑，等你按按钮），`done` 是收尾 phase 的终态
        assert steps == {
            "flush": "done", "transcode": "done", "log": "done", "post": "waiting",
        }

        session_dir = _session_dir_of(client)
        assert str(session_dir).startswith(str(out_root))
        assert sorted(item.name for item in session_dir.iterdir()) == sorted(
            [
                SESSION_FILE_NAME,
                archive_name("mic"), archive_name("loopback"),
                "meeting_audio_mic_001.mp3", "meeting_audio_loopback_001.mp3",
                "meeting.log",
            ]
        )

        wall_seconds = stopped_at - started_at
        mic_pcm = (session_dir / archive_name("mic")).read_bytes()
        loop_pcm = (session_dir / archive_name("loopback")).read_bytes()
        evidence: dict = {"wall_seconds": round(wall_seconds, 3)}
        audits: dict = {}

        for source, data in (("mic", mic_pcm), ("loopback", loop_pcm)):
            fed = backend.fed_chunks(source)
            assert fed == RECORD_CHUNKS  # 交付上限钉死了终点：收尾时不多不少就是这些块
            audit = audit_pcm(
                data,
                expected_bytes=expected_archive_bytes(source, fed),
                expected_samples=fed * CHUNK_FRAMES,
            )
            audits[source] = audit
            # 样本数 = 喂进去的块数 × 每块样本数；与墙钟的偏差在一个采集块以内
            assert audit.sample_gap == 0
            assert samples_within_one_block(audit.samples, wall_seconds)
            # 字节完整：与假源的喂块账逐字节一致（丢 0 秒），且为偶数字节
            assert audit.even_bytes and audit.trailing_bytes == 0
            assert audit.byte_match is True
            # 逐帧：0.5 秒一帧重跑电平，帧序号连续、没有空洞
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
            # MP3 能解且时长对得上
            duration = _mp3_duration(session_dir / f"meeting_audio_{source}_001.mp3")
            assert abs(duration - audit.seconds) <= MP3_DURATION_TOLERANCE_SECONDS
            evidence[source]["mp3_duration"] = round(duration, 3)

        # `session.json` 的终态与四步记录，与上面从快照看到的一致
        record = json.loads((session_dir / SESSION_FILE_NAME).read_text(encoding="utf-8"))
        assert record["state"] == "done"
        assert record["interrupted"] is False
        assert {step["id"]: step["status"] for step in record["drain"]["steps"]} == steps
        assert record["ended_at"] is not None
        assert [item["archives"] for item in record["sources"]] == [
            [archive_name("mic")], [archive_name("loopback")],
        ]
        # 段账：一场恰好一段，段起止、在录时长都落了；权威时长 = 在录时长之和
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
    # 会后钩子也取 `null`（同 ① 的理由：这一场只验采集侧的账）。
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
            "flush": "done", "transcode": "done", "log": "done", "post": "waiting",
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
        assert (session_dir / "meeting_audio_mic_001.mp3").exists()
        assert (session_dir / "meeting_audio_loopback_001.mp3").exists()


# ---------------------------------------------------------------------------
# ③ 转码吞吐：10 分钟合成 PCM 实测一次，线性外推两小时
# ---------------------------------------------------------------------------

#: 吞吐素材的长度（分钟）。任务书 §3 的基线量的是 10 分钟 / 19.2MB，同规格复量才可比
THROUGHPUT_MINUTES = 10

#: 一分钟多少块（一块 0.1 秒）
_CHUNKS_PER_MINUTE = 600

#: 收尾预算的 30 秒是给**整趟收尾**的（`docs/state.md` 第 1 章）；两小时外推不得越过它
TWO_HOUR_BUDGET_SECONDS = 30.0


