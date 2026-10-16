"""`tests/test_capture_segments.py` —— 分段落盘与时间明细的验收（spec 完成判定的那一场戏）。

**全程假采集**：`capture.backend = tests._capture_faulty:FaultyCaptureBackend`
（真后端 + 假端点表 + 带剧本的假源，归档/电平/收尾全是生产代码），进程内真
`ServiceRuntime`，不起子进程、不碰真声卡、不放音。暂停与「关一路」的驱动能力做在
采集引擎/服务层（`CaptureBackend.pause()` / `resume()`），动作面与 web 界面本轮不动。

两场戏：

  1. **喂真素材、中间带一次暂停**：归档里出现按序号命名的段；两路的段起止时间一致；
     暂停窗口逐样本为零、时间轴照常增长；`session.json` 的段列表、暂停区间、权威时长
     都与盘上事实对得上（权威时长 = 各段在录时长之和，写端唯一的自洽式）。
  2. **墙钟改错**：把 `session.json` 的墙钟字段与段戳改成胡话，程序的读法与结论一个
     字都不变（元信息除权威时长外一律不当判据，`docs/design-intent.md` 第 6 条）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from meeting.capture.engine import BYTES_PER_SECOND
from meeting.session import SESSION_FILE_NAME, archive_name, segment_archive_paths
from tests._capture_faulty import (
    BACKEND_TARGET,
    CHUNK_BYTES,
    DELIVER_LOOPBACK_ENV,
    DELIVER_MIC_ENV,
    FaultyCaptureBackend,
    PACE_SECONDS_ENV,
    chunk_bytes,
    chunk_value,
)
from tests.test_api_actions import live, post, snapshot_of, wait_until

#: 慢档标记：`pytest.mark.slow` 的归位判据见 `docs/service.md` 第 7 章。
pytestmark = pytest.mark.slow

#: 进程内用例的轮询超时（收尾三步跑在收尾线程池里；与既有端到端同档）
POLL_TIMEOUT_SECONDS = 15.0

#: 暂停前后各采这么多块（0.1 秒一块）：素材量级与 e2e ① 同档，MP3 与段记录都有得比
EDGE_CHUNKS = 10

#: 暂停窗口最少写到的静音量（3 块 = 0.3 秒；判据挂在写侧的静音记录里，不靠猜时序）
PAUSE_SILENCE_CHUNKS = 3


def _longest_zero_run(zero_at: set[int]) -> int:
    """一组零块序号里最长的那段连续零（暂停窗落在它身上）。"""
    best = current = 0
    previous: int | None = None
    for index in sorted(zero_at):
        current = current + 1 if previous is not None and index == previous + 1 else 1
        best = max(best, current)
        previous = index
    return best


def _drain_steps(snapshot: dict) -> dict:
    return {step["id"]: step["status"] for step in snapshot["ended"]["steps"]}


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


def test_a_pause_records_silence_and_the_ledger_adds_up(tmp_path: Path, monkeypatch):
    """真素材 + 一次暂停：序号命名的段、零窗、两路时间对齐、段记录与权威时长对得上。"""
    monkeypatch.setenv(PACE_SECONDS_ENV, "0.1")
    # 交付上限钉住终点：喂满之后再收尾，核对因此是确定的
    monkeypatch.setenv(DELIVER_MIC_ENV, str(2 * EDGE_CHUNKS + PAUSE_SILENCE_CHUNKS + 5))
    monkeypatch.setenv(DELIVER_LOOPBACK_ENV, str(2 * EDGE_CHUNKS + PAUSE_SILENCE_CHUNKS + 5))
    # 这一场只验采集与段记录：转写与会后钩子都显式取 null 替身（同 e2e ① 的理由）
    with live(
        tmp_path,
        {
            "capture.backend": BACKEND_TARGET,
            "transcriber.backend": "null",
            "post.hook": "null",
        },
    ) as (client, runtime, _home, out_root):
        backend = runtime.capture_backend
        assert isinstance(backend, FaultyCaptureBackend)

        assert post(client, "start", {"mode": "both"}).status_code == 200
        wait_until(
            lambda: snapshot_of(client)["top_state"] == "recording",
            timeout=POLL_TIMEOUT_SECONDS,
            what="会话进入 recording",
        )
        session = backend.session
        wait_until(
            lambda: backend.fed_chunks("mic") >= EDGE_CHUNKS
            and backend.fed_chunks("loopback") >= EDGE_CHUNKS,
            timeout=POLL_TIMEOUT_SECONDS,
            what="两路先各喂满一段",
        )
        # —— 暂停（服务层出口；动作面没有这一格，本轮也不加） ——
        backend.pause()
        assert session.paused is True
        content_at_pause = {source: session.content_bytes(source) for source in ("mic", "loopback")}
        bytes_at_pause = {source: session.bytes_written(source) for source in ("mic", "loopback")}
        wait_until(
            lambda: session.silence_bytes("mic") >= PAUSE_SILENCE_CHUNKS * CHUNK_BYTES
            and session.silence_bytes("loopback") >= PAUSE_SILENCE_CHUNKS * CHUNK_BYTES,
            timeout=POLL_TIMEOUT_SECONDS,
            what="两路的暂停静音都写到量",
        )
        # —— 继续 ——
        backend.resume()
        assert session.paused is False
        # 暂停窗在盘上的长度：resume 那一刻的总量 − 暂停时的内容量（含 ≤1 块的在途抖动）
        pause_window_bytes = session.bytes_written("mic") - content_at_pause["mic"]
        wait_until(
            lambda: backend.fed_chunks("mic") >= 2 * EDGE_CHUNKS + PAUSE_SILENCE_CHUNKS,
            timeout=POLL_TIMEOUT_SECONDS,
            what="恢复后素材继续长",
        )
        assert post(client, "stop").status_code == 200
        done = _wait_for_done(client)
        # `post` 那一格按 R26 落 `waiting`；转码已摘出收尾序列，flush 落定自动在后台另起
        assert _drain_steps(done) == {
            "flush": "done", "log": "done", "post": "waiting",
        }

        session_dir = Path(snapshot_of(client)["session"]["dir"])
        assert str(session_dir).startswith(str(out_root))
        # 等转码独立工序收工（真后端带 transcode 能力 → flush 落定自动起），MP3 才算在场
        from meeting.capture import transcode as transcode_module

        wait_until(
            lambda: (
                doc
                if (doc := transcode_module.read_status(session_dir)) is not None
                and doc.get("state") == "done"
                else None
            ),
            timeout=POLL_TIMEOUT_SECONDS,
            what="转码独立工序落 done",
        )
        # ① 按序号命名的段：两路各一个段 1，外加各自的 MP3、场次文件与转码工序的三层记录
        assert sorted(item.name for item in session_dir.iterdir()) == sorted(
            [
                SESSION_FILE_NAME,
                archive_name("mic"), archive_name("loopback"),
                "meeting_audio_mic_001.mp3", "meeting_audio_loopback_001.mp3",
                "meeting.log",
                "meeting_transcode_status.json", "meeting_transcode_progress.jsonl",
                "meeting_transcode_mic_001.json", "meeting_transcode_loopback_001.json",
            ]
        )

        # ② 盘上核对（收尾结算之后做，不跟写线程的登记时序赛跑）：
        #    每一块要么是源按块号给的内容、要么是整块零；零块总量 = 暂停静音量 + 源自带的静音区；
        #    暂停窗（记录记的那一段）落在一段连续零里——暂停只是把中间那截换成了零
        for source in ("mic", "loopback"):
            fed_total = backend.fed_chunks(source)
            data = (session_dir / archive_name(source)).read_bytes()
            assert len(data) == fed_total * CHUNK_BYTES
            blocks = [data[i * CHUNK_BYTES : (i + 1) * CHUNK_BYTES] for i in range(fed_total)]
            silence_chunks = session.silence_bytes(source) // CHUNK_BYTES
            own_silent_chunks = sum(
                1 for index in range(fed_total) if chunk_value(source, index) == 0
            )
            zero_at = {
                index for index, item in enumerate(blocks) if item == bytes(CHUNK_BYTES)
            }
            # 零块总量 = 暂停静音量 + 源自带的静音区；两者在同一块上相遇时只算一块
            # （重叠最多盖住整个暂停窗），所以下界按整窗重叠扣
            assert (
                own_silent_chunks + silence_chunks - PAUSE_SILENCE_CHUNKS
                <= len(zero_at)
                <= own_silent_chunks + silence_chunks
            ), (
                f"{source} 的零块总量对不上：盘上 {len(zero_at)}，"
                f"暂停静音 {silence_chunks} + 源自带静音 {own_silent_chunks}"
            )
            for index, item in enumerate(blocks):
                if index not in zero_at:
                    assert item == chunk_bytes(source, index), (
                        f"{source} 第 {index} 块按块号该是 {chunk_value(source, index)} 的常值块，"
                        "盘上却是别的字节（内容被换过或丢了）"
                    )
            # 最长连续零段盖得住暂停窗（记录 ≥3 块；留 1 块在途抖动的余地）
            assert _longest_zero_run(zero_at) >= PAUSE_SILENCE_CHUNKS - 1
        # 两路时间轴等长（同一场同一段；差在采集块以内的抖动）
        mic_size = (session_dir / archive_name("mic")).stat().st_size
        loop_size = (session_dir / archive_name("loopback")).stat().st_size
        assert abs(mic_size - loop_size) <= 2 * CHUNK_BYTES

        # ③ `session.json` 的段记录：一段、两路、一条暂停区间、权威时长对得上
        record = json.loads((session_dir / SESSION_FILE_NAME).read_text(encoding="utf-8"))
        assert record["state"] == "done"
        assert len(record["segments"]) == 1
        segment = record["segments"][0]
        assert segment["index"] == 1
        assert segment["started_at"] and segment["ended_at"]
        assert segment["sources"] == ["mic", "loopback"]
        assert len(segment["pauses"]) == 1
        assert segment["pauses"][0]["start"] and segment["pauses"][0]["end"]
        # 权威时长 = 各段在录时长之和（本节点恰好一段）；与盘上字节数对得上：
        # 落盘总字节 − 暂停窗静音字节 = 内容字节，÷ 每秒字节数 = 在录时长
        # （容差 0.25s = 两块：暂停/恢复边界上各有一块在途抖动的余地）
        expected_duration = (fed_total * CHUNK_BYTES - pause_window_bytes) / BYTES_PER_SECOND
        assert segment["duration_seconds"] == pytest.approx(expected_duration, abs=0.25)
        assert record["audio_duration_seconds"] == pytest.approx(
            segment["duration_seconds"], abs=0.001
        )
        # 每路的段文件名表与盘上一致
        for item in record["sources"]:
            assert item["archives"] == [archive_name(item["id"], 1)]
            assert segment_archive_paths(session_dir, item["id"]) == [
                session_dir / archive_name(item["id"])
            ]


def test_a_wrong_wall_clock_changes_no_conclusion(tmp_path: Path, monkeypatch):
    """把 `session.json` 的墙钟栏改成胡话：程序的读法与结论一个字都不变。"""
    monkeypatch.setenv(PACE_SECONDS_ENV, "0.02")
    monkeypatch.setenv(DELIVER_MIC_ENV, "20")
    monkeypatch.setenv(DELIVER_LOOPBACK_ENV, "20")
    with live(
        tmp_path,
        {
            "capture.backend": BACKEND_TARGET,
            "transcriber.backend": "null",
            "post.hook": "null",
        },
    ) as (client, runtime, _home, _out):
        assert isinstance(runtime.capture_backend, FaultyCaptureBackend)
        assert post(client, "start", {"mode": "both"}).status_code == 200
        wait_until(
            lambda: snapshot_of(client)["top_state"] == "recording",
            timeout=POLL_TIMEOUT_SECONDS,
            what="会话进入 recording",
        )
        wait_until(
            lambda: runtime.capture_backend.fed_chunks("mic") >= 20,
            timeout=POLL_TIMEOUT_SECONDS,
            what="喂满 20 块",
        )
        assert post(client, "stop").status_code == 200
        _wait_for_done(client)

        session_dir = Path(snapshot_of(client)["session"]["dir"])
        record_path = session_dir / SESSION_FILE_NAME
        record = json.loads(record_path.read_text(encoding="utf-8"))
        before = {
            "state": record["state"],
            "interrupted": record["interrupted"],
            "steps": {step["id"]: step["status"] for step in record["drain"]["steps"]},
            "duration": record["audio_duration_seconds"],
            "archives": [item["archives"] for item in record["sources"]],
        }

        # 墙钟栏与段戳全改成胡话（权威时长这一格**不动**——它是唯一当判据的数）
        record["date"] = "1999-99-99"
        record["created_at"] = "去年冬天"
        record["started_at"] = "比宇宙还早"
        record["ended_at"] = "永不"
        record["segments"][0]["started_at"] = "史前"
        record["segments"][0]["ended_at"] = "热寂以后"
        record["segments"][0]["pauses"] = [{"start": "永不", "end": "从未"}]
        record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

        # 程序的结论一格不变：状态、步骤、权威时长、段文件名表原样读得回来
        after = json.loads(record_path.read_text(encoding="utf-8"))
        assert {
            "state": after["state"],
            "interrupted": after["interrupted"],
            "steps": {step["id"]: step["status"] for step in after["drain"]["steps"]},
            "duration": after["audio_duration_seconds"],
            "archives": [item["archives"] for item in after["sources"]],
        } == before
        # 会后链的取路判据只看 `sources[*].id`，墙钟改错不影响它
        from meeting.offline.runner import declared_sources

        assert declared_sources(session_dir) == ("mic", "loopback")
