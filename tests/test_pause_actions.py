"""`tests/test_pause_actions.py` —— 暂停 / 继续 / 会中开关某一路（本节点 spec 的完成判定）。

**全程假采集**：`capture.backend = tests._capture_faulty:FaultyCaptureBackend`（真后端 +
假端点表 + 带剧本的假源；归档写线程、电平刻度、收尾全是生产代码），进程内真
`ServiceRuntime`。**三条读数全部经 HTTP 动作驱动**（`pause` / `resume` / `set_source`
是本节点新加的那一层；引擎里的能力是上游落地、动作面此前没有入口）。

四场戏：

  1. **暂停 → 继续**（动作 `pause` / `resume`）：暂停窗那一截逐样本为零、继续之后内容
     逐字节照旧；暂停期间时长不走、继续之后接着走；段账恒为一段（暂停与切源都不切段，
     `docs/design-intent.md` 第 9 条），权威时长不含暂停（第 7 条）。
  2. **关一路 → 重新打开**（动作 `set_source`）：关掉的那一路改写静音、**不报故障**
     （`alerts.critical` 为空、顶层停在 `recording`）、另一路照常；重新打开之后那一路
     又出内容；两路时间轴仍等长。
  3. **重新打开时设备打不开**：如实回 `capture_unavailable`(503) 带 `{source, error}`，
     那一路照旧停在「已关掉」（不许悄悄不打开，第 1 条）。
  4. **暂停期间不累计静默判定**：安静场景里暂停跨过 `alert.silence_seconds` 也不冒致命级；
     同一场继续之后那条致命级照常到点出现（阈值真的够得着，不是把判定关掉了）。
     同时读一条：暂停期间电平帧照发（`recording_alive` 仍为真），
     因此界面不会把「暂停」误报成「采集已停」。
"""

from __future__ import annotations

import json
import struct
import time
from pathlib import Path

import pytest

from meeting.asr.pipeline import (
    FRAME_BYTES,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    VoiceActivitySegmenter,
)
from meeting.capture.engine import BYTES_PER_SECOND
from meeting.session import SESSION_FILE_NAME, archive_name, segment_archive_paths
from tests._capture_faulty import (
    BACKEND_TARGET,
    CHUNK_BYTES,
    DELIVER_LOOPBACK_ENV,
    DELIVER_MIC_ENV,
    PACE_SECONDS_ENV,
    REOPEN_ERROR_ENV,
    FaultyCaptureBackend,
    chunk_bytes,
    chunk_value,
)
from tests.test_api_actions import live, post, snapshot_of, wait_until

#: 慢档标记：`pytest.mark.slow` 的归位判据与实测数据见 `docs/service.md` 第 7 章。
pytestmark = pytest.mark.slow

#: 进程内用例的轮询超时（收尾四步跑在收尾线程池里；与既有端到端同档）
POLL_TIMEOUT_SECONDS = 15.0

#: 暂停前后各采这么多块（0.1 秒一块）
EDGE_CHUNKS = 8

#: 暂停窗最少写到的静音量（12 块 = 1.2 秒）：留出「首尾各一块在途抖动」的余地之后，
#: 中间那几块仍必须个个为零，而且这一截的连续零要明显长过下面那条「识别器静音上界」
PAUSE_SILENCE_CHUNKS = 12

#: 假 VAD 的判定阈值：一帧里最大样本绝对值 ≥ 它就当语音。
#: 假源的 mic 内容常值是 2000+块号（loopback 是 12000+块号），静音区与暂停窗是 0 ——
#: 阈值取 1000，两档因此分得开（确定、与真 `webrtcvad` 无关）。
SPEECH_AMPLITUDE = 1000

#: 切段器用的静默出段时长（秒）：生产值是 2.0 —— 这里压到 0.3，让「送进识别器的音频里
#: 最长只有 VAD 自己那一截收尾静音」这件事在 1.2 秒的暂停窗上验得出来，且不必等 2 秒
SEGMENTER_SILENCE_SECONDS = 0.3

#: 交到识别器手上的音频里，最长的一截静音允许有多长（秒）：VAD 自己的收尾静音
#: （`SEGMENTER_SILENCE_SECONDS`）+ 喂进去那一拍的时间量化（一块 0.1 秒）+ 一帧（30ms）。
#: 暂停窗在盘上是 1.2 秒的连续零，明显长过这条上界 —— 于是「整截暂停进了识别器」当场红。
RECOGNIZER_SILENCE_BOUND_SECONDS = SEGMENTER_SILENCE_SECONDS + 0.1 + 0.05

#: 戏 ④ 里用的静默阈值（秒，配置层收整数）：压到 1 秒，好让「暂停那一段时间算不算数」
#: 在几秒内验得出来（生产默认 180 秒留给真实会议）
TEST_SILENCE_SECONDS = 1

#: 戏 ① 里用来证明「暂停期间秒数不往前走」的那一小段墙钟（秒）：等的是时间本身。
#: 0.2 秒对下面那条断言的容差（0.05 秒）仍有 4 倍余量 —— 读数要是真在走，
#: 这一截墙钟就会以 ~0.2 秒的位移当场红（review-1 m4：整条命令压在 10 秒内）
CLOCK_FROZEN_PROBE_SECONDS = 0.2

#: 两路的时间轴对齐容差（块以内的抖动）
ALIGN_TOLERANCE_BLOCKS = 2


class AmplitudeVad:
    """确定性的 VAD 替身：一帧里最大样本绝对值过阈值就当语音（`webrtcvad.Vad` 同形接口）。"""

    def __init__(self, threshold: int = SPEECH_AMPLITUDE) -> None:
        self.threshold = int(threshold)

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        del sample_rate
        samples = struct.unpack(f"<{len(frame) // 2}h", frame)
        return max(abs(value) for value in samples) >= self.threshold


def _blocks(data: bytes) -> list[bytes]:
    """把归档按采集块切开（一块 0.1 秒 = 3200 字节）。"""
    return [data[i * CHUNK_BYTES : (i + 1) * CHUNK_BYTES] for i in range(len(data) // CHUNK_BYTES)]


def _zeros() -> bytes:
    return bytes(CHUNK_BYTES)


def _longest_zero_run(pcm: bytes) -> float:
    """一段 PCM 里最长的那一截数字静音有多长（秒）。"""
    best = run = 0
    for index in range(len(pcm) // 2):
        sample = int.from_bytes(pcm[index * 2 : index * 2 + 2], "little", signed=True)
        run = run + 1 if sample == 0 else 0
        best = max(best, run)
    return best / SAMPLE_RATE


def _segments_handed_to_the_recognizer(data: bytes) -> list[tuple[int, bytes]]:
    """把一段归档音频喂进**生产的切段器**，交回「识别器会收到什么」。

    这一层就是「转写有没有内容」的判据所在：转写侧尾读的就是这份归档（`tail_archive()`），
    切段器只把 VAD 判成语音的那些帧攒成一段交给识别器；一段都没切出来，那一截就没有转写行。
    VAD 换成确定性的振幅替身（真 `webrtcvad` 对常值音会做高通、读不出我们这份假素材），
    时钟按**音频时长**往前走（判据是「离上一块语音多久了」）。交回 `(段尾的字节位置, PCM)`。
    """
    segmenter = VoiceActivitySegmenter(
        silence_timeout=SEGMENTER_SILENCE_SECONDS,
        vad_backend=AmplitudeVad(),
    )
    handed: list[tuple[int, bytes]] = []
    position = 0
    now = 0.0
    for block in _blocks(data):
        position += len(block)
        segmenter.feed(
            block,
            lambda pcm, at=position: handed.append((at, pcm)),
            now,
        )
        now += len(block) / BYTES_PER_SECOND
    segmenter.flush(lambda pcm: handed.append((position, pcm)))
    return handed


def _wait_for_done(client) -> dict:
    return wait_until(
        lambda: (lambda snap: snap if snap["ended"] and snap["ended"]["phase"] == "done" else None)(
            snapshot_of(client)
        ),
        timeout=POLL_TIMEOUT_SECONDS,
        what="收尾停在 done",
    )


def _record_of(session_dir: Path) -> dict:
    return json.loads((session_dir / SESSION_FILE_NAME).read_text(encoding="utf-8"))


def _start(client, backend: FaultyCaptureBackend) -> None:
    """经 HTTP 起一场、等到 `recording` 与两路各喂满 `EDGE_CHUNKS` 块。"""
    assert post(client, "start", {"mode": "both"}).status_code == 200
    wait_until(
        lambda: snapshot_of(client)["top_state"] == "recording",
        timeout=POLL_TIMEOUT_SECONDS,
        what="会话进入 recording",
    )
    assert isinstance(backend, FaultyCaptureBackend)
    wait_until(
        lambda: backend.fed_chunks("mic") >= EDGE_CHUNKS
        and backend.fed_chunks("loopback") >= EDGE_CHUNKS,
        timeout=POLL_TIMEOUT_SECONDS,
        what="两路先各喂满一段",
    )


def _sources(snapshot: dict) -> dict:
    return {item["id"]: item for item in snapshot["sources"]}


# ---------------------------------------------------------------------------
# ① 暂停 → 继续
# ---------------------------------------------------------------------------


def test_a_pause_writes_digital_silence_and_keeps_one_segment(tmp_path: Path, monkeypatch):
    """暂停那一截逐样本为零、继续之后逐字节照旧；时长扣掉暂停；段账恒为一段。"""
    monkeypatch.setenv(PACE_SECONDS_ENV, "0.02")
    with live(
        tmp_path,
        {
            "capture.backend": BACKEND_TARGET,
            "transcriber.backend": "null",
            "post.hook": "null",
        },
    ) as (client, runtime, _home, _out_root):
        backend = runtime.capture_backend
        _start(client, backend)
        session = backend.session

        before_pause = {source: session.bytes_written(source) for source in ("mic", "loopback")}
        content_at_pause = {source: session.content_bytes(source) for source in ("mic", "loopback")}

        # —— 暂停（**经 HTTP 动作**驱动）——
        assert post(client, "pause").status_code == 200
        paused = snapshot_of(client)
        assert paused["session"]["paused"] is True, "暂停要落在快照的 session.paused 这一格上"
        assert paused["top_state"] == "recording", "暂停不许做成第五个顶层状态"
        # 幂等：再按一次，状态照旧
        assert post(client, "pause").status_code == 200
        assert snapshot_of(client)["session"]["paused"] is True

        wait_until(
            lambda: session.silence_bytes("mic") >= PAUSE_SILENCE_CHUNKS * CHUNK_BYTES
            and session.silence_bytes("loopback") >= PAUSE_SILENCE_CHUNKS * CHUNK_BYTES,
            timeout=POLL_TIMEOUT_SECONDS,
            what="两路的暂停静音都写到量",
        )
        # 暂停期间时长不走（第 7 条：权威长度是实际录到的音频时长，不含暂停）：
        # 这里等的就是时间本身 —— 墙钟走过 0.4 秒，读数应当原地不动
        frozen_a = snapshot_of(client)["session"]["elapsed_seconds"]
        time.sleep(CLOCK_FROZEN_PROBE_SECONDS)
        frozen_b = snapshot_of(client)["session"]["elapsed_seconds"]
        assert frozen_b == pytest.approx(frozen_a, abs=0.05), "暂停期间已录秒数不该往前走"

        at_resume = {source: session.bytes_written(source) for source in ("mic", "loopback")}
        # —— 继续 ——
        assert post(client, "resume").status_code == 200
        assert snapshot_of(client)["session"]["paused"] is False
        wait_until(
            lambda: backend.fed_chunks("mic") >= 2 * EDGE_CHUNKS,
            timeout=POLL_TIMEOUT_SECONDS,
            what="恢复后素材继续长",
        )
        running_a = snapshot_of(client)["session"]["elapsed_seconds"]
        wait_until(
            lambda: snapshot_of(client)["session"]["elapsed_seconds"] > running_a + 0.2,
            timeout=POLL_TIMEOUT_SECONDS,
            what="继续之后时长接着走",
        )

        assert post(client, "stop").status_code == 200
        _wait_for_done(client)

        session_dir = Path(snapshot_of(client)["session"]["dir"])
        for source in ("mic", "loopback"):
            fed_total = backend.fed_chunks(source)
            data = (session_dir / archive_name(source)).read_bytes()
            assert len(data) == fed_total * CHUNK_BYTES
            blocks = _blocks(data)
            start = before_pause[source] // CHUNK_BYTES
            end = at_resume[source] // CHUNK_BYTES
            assert end - start >= PAUSE_SILENCE_CHUNKS - 2, (
                f"{source} 的暂停窗只有 {end - start} 块（账本写了 "
                f"{(at_resume[source] - before_pause[source]) // CHUNK_BYTES} 块）"
            )
            # ① 暂停窗中间那几块个个是数字静音（首尾各留一块在途抖动的余地）
            for index in range(start + 2, max(start + 2, end - 2)):
                assert blocks[index] == _zeros(), (
                    f"{source} 第 {index} 块落在暂停窗里却不是数字零（暂停期间还在录内容）"
                )
            # ② 暂停窗之外：源给内容的那几块逐字节照旧（继续之后内容照常）
            for index, block in enumerate(blocks):
                if start - 1 <= index <= end + 1:
                    continue
                if chunk_value(source, index) == 0:
                    assert block == _zeros()
                else:
                    assert block == chunk_bytes(source, index), (
                        f"{source} 第 {index} 块本该是源给的内容（暂停窗之外），盘上却是别的字节"
                    )
        mic_size = (session_dir / archive_name("mic")).stat().st_size
        loop_size = (session_dir / archive_name("loopback")).stat().st_size
        assert abs(mic_size - loop_size) <= ALIGN_TOLERANCE_BLOCKS * CHUNK_BYTES

        # ③ 转写那一半：把盘上这两份音频喂进**生产的切段器**，看识别器手上会有什么。
        #    转写侧尾读的就是这份归档；切段器只把 VAD 判成语音的帧攒成段交给识别器 ——
        #    暂停窗那 1.2 秒连续数字零整截都没进任何一段，因此那一截没有转写行；
        #    送进去的最长一截静音只有 VAD 自己的收尾窗口那么大。
        for source in ("mic", "loopback"):
            data = (session_dir / archive_name(source)).read_bytes()
            handed = _segments_handed_to_the_recognizer(data)
            assert handed, f"{source} 这一路该有内容送进识别器（转写照常）"
            start = before_pause[source] // CHUNK_BYTES
            end = at_resume[source] // CHUNK_BYTES
            for tail, pcm in handed:
                assert _longest_zero_run(pcm) <= RECOGNIZER_SILENCE_BOUND_SECONDS, (
                    f"{source} 这一段交到识别器手上的音频里带着 "
                    f"{_longest_zero_run(pcm):.2f} 秒连续静音"
                    "（暂停窗那 1.2 秒连续零本不该整截进识别器）"
                )
            # 继续之后的素材照常切出了段（段尾落在暂停窗之后）
            assert [item for item in handed if item[0] - len(item[1]) > end * CHUNK_BYTES], (
                f"{source} 继续之后没有内容送进识别器"
            )

        # ④ 段账：一段、两路、一条暂停区间、权威时长不含暂停
        record = _record_of(session_dir)
        assert record["state"] == "done"
        assert len(record["segments"]) == 1, "暂停与切源都不切段（第 9 条）"
        segment = record["segments"][0]
        assert segment["sources"] == ["mic", "loopback"]
        assert len(segment["pauses"]) == 1
        assert segment["pauses"][0]["start"] and segment["pauses"][0]["end"]
        pause_window_bytes = at_resume["mic"] - content_at_pause["mic"]
        expected_duration = (
            backend.fed_chunks("mic") * CHUNK_BYTES - pause_window_bytes
        ) / BYTES_PER_SECOND
        assert segment["duration_seconds"] == pytest.approx(expected_duration, abs=0.3)
        assert record["audio_duration_seconds"] == pytest.approx(
            segment["duration_seconds"], abs=0.001
        )
        # 只有段 1，没有任何「段 2」冒出来
        for source in ("mic", "loopback"):
            assert segment_archive_paths(session_dir, source) == [
                session_dir / archive_name(source)
            ]


# ---------------------------------------------------------------------------
# ② 关一路 → 重新打开
# ---------------------------------------------------------------------------


def test_b_closing_one_source_writes_silence_there_and_reopening_restores_it(
    tmp_path: Path, monkeypatch
):
    """关掉 mic：那一路改写静音、不进告警，loopback 照常；重新打开之后 mic 又出内容。"""
    monkeypatch.setenv(PACE_SECONDS_ENV, "0.02")
    with live(
        tmp_path,
        {
            "capture.backend": BACKEND_TARGET,
            "transcriber.backend": "null",
            "post.hook": "null",
        },
    ) as (client, runtime, _home, _out_root):
        backend = runtime.capture_backend
        _start(client, backend)
        session = backend.session

        loopback_content_before = session.content_bytes("loopback")
        assert post(client, "set_source", {"source": "mic", "active": False}).status_code == 200
        snap = snapshot_of(client)
        assert _sources(snap)["mic"]["health"] == "stopped", "关掉的那一路如实落进现有档位"
        assert _sources(snap)["loopback"]["health"] == "capturing"
        assert snap["alerts"]["critical"] is None, "用户主动关一路不许报故障"
        assert snap["top_state"] == "recording"
        # 幂等
        assert post(client, "set_source", {"source": "mic", "active": False}).status_code == 200

        wait_until(
            lambda: session.silence_bytes("mic") >= PAUSE_SILENCE_CHUNKS * CHUNK_BYTES,
            timeout=POLL_TIMEOUT_SECONDS,
            what="关掉的那一路写够静音",
        )
        # 那一截没在录内容：等到又写了几块静音之后再核内容账，它一动不动；
        # 另一路的素材照常往上长（一路出事另一路照跑）
        closed_content = session.content_bytes("mic")
        quiet_mark = session.silence_bytes("mic")
        wait_until(
            lambda: session.silence_bytes("mic") >= quiet_mark + 3 * CHUNK_BYTES,
            timeout=POLL_TIMEOUT_SECONDS,
            what="关掉之后静音继续写",
        )
        assert session.content_bytes("mic") == closed_content, "关掉之后这一路还在录内容"
        assert session.content_bytes("loopback") > loopback_content_before

        # —— 重新打开 ——
        assert post(client, "set_source", {"source": "mic", "active": True}).status_code == 200
        assert _sources(snapshot_of(client))["mic"]["health"] == "capturing"
        mic_fed_at_reopen = backend.fed_chunks("mic")
        wait_until(
            lambda: backend.fed_chunks("mic") > mic_fed_at_reopen + 8,
            timeout=POLL_TIMEOUT_SECONDS,
            what="重新打开之后 mic 又出块",
        )

        assert post(client, "stop").status_code == 200
        _wait_for_done(client)

        session_dir = Path(snapshot_of(client)["session"]["dir"])
        data = (session_dir / archive_name("mic")).read_bytes()
        assert len(data) == session.bytes_written("mic")
        blocks = _blocks(data)
        # 盘上从前往后就是：关掉之前的内容 → 关掉那一截的静音 → 重新打开之后的内容。
        # 「重新打开」那一次造的是**新的一份源**（它自己的块号从头数），段尾的长度照它算
        resumed_blocks = backend.created["mic"].fed
        closed_index = closed_content // CHUNK_BYTES
        reopened_index = len(blocks) - resumed_blocks
        assert resumed_blocks >= 2, "重新打开之后该真的采到内容"
        assert reopened_index >= closed_index
        for index in range(closed_index, reopened_index):
            assert blocks[index] == _zeros(), f"mic 第 {index} 块落在关掉的那一段里却不是数字零"
        for offset, block in enumerate(blocks[reopened_index:]):
            if chunk_value("mic", offset) == 0:
                assert block == _zeros()
            else:
                assert block == chunk_bytes("mic", offset), (
                    f"mic 重新打开之后的第 {offset} 块本该是它采到的内容"
                )
        # 转写那一半：关掉的那一段是 1.2 秒以上的连续零，整截都进不了识别器
        # （因此那一路不再产生转写行）；重新打开之后的素材照常出段
        handed = _segments_handed_to_the_recognizer(data)
        for tail, pcm in handed:
            assert _longest_zero_run(pcm) <= RECOGNIZER_SILENCE_BOUND_SECONDS, (
                f"mic 这一段交到识别器手上的音频里带着 {_longest_zero_run(pcm):.2f} 秒连续静音"
                "（关掉的那一段本不该整截进识别器）"
            )
        assert [item for item in handed if item[0] - len(item[1]) >= reopened_index * CHUNK_BYTES], (
            "重新打开之后没有内容送进识别器"
        )
        # 另一路照常：它那边一直有内容送进识别器
        loop_data = (session_dir / archive_name("loopback")).read_bytes()
        assert _segments_handed_to_the_recognizer(loop_data)

        # 关一路这一场不核「两路字节等长」：本用例把假源的块节奏压到 0.02 秒一块（真机上是
        # 0.1 秒一块），活着的这一路因此比写静音的那一路快 5 倍 —— 等长这件事要真机那种
        # 「一块音频就是 0.1 秒」的节奏才成立（既有判据在 `tests/test_capture_segments.py`）
        record = _record_of(session_dir)
        assert len(record["segments"]) == 1, "关一路与重新打开都不产生新的段"
        assert record["segments"][0]["sources"] == ["mic", "loopback"]
        assert record["segments"][0]["pauses"] == []


# ---------------------------------------------------------------------------
# ③ 重新打开时设备打不开
# ---------------------------------------------------------------------------


def test_c_reopening_a_busy_source_reports_capture_unavailable(tmp_path: Path, monkeypatch):
    """重开失败（设备被占着）如实回 503 + `{source, error}`，那一路照旧停在已关掉。"""
    monkeypatch.setenv(PACE_SECONDS_ENV, "0.02")
    with live(
        tmp_path,
        {
            "capture.backend": BACKEND_TARGET,
            "transcriber.backend": "null",
            "post.hook": "null",
        },
    ) as (client, runtime, _home, _out_root):
        backend = runtime.capture_backend
        _start(client, backend)
        session = backend.session

        assert post(client, "set_source", {"source": "mic", "active": False}).status_code == 200
        wait_until(
            lambda: session.silence_bytes("mic") >= CHUNK_BYTES,
            timeout=POLL_TIMEOUT_SECONDS,
            what="关掉的那一路先写上一块静音",
        )
        # 这一段里为 mic 造的源（第 2 次及以后）一 start() 就抛错 = 设备被别的程序占着
        monkeypatch.setenv(REOPEN_ERROR_ENV, "mic")
        failed = post(client, "set_source", {"source": "mic", "active": True})
        assert failed.status_code == 503
        body = failed.json()
        assert body["ok"] is False
        assert body["code"] == "capture_unavailable"
        assert set(body["detail"]) == {"source", "error"}
        assert body["detail"]["source"] == "mic"
        assert "打不开" in body["detail"]["error"] or "RuntimeError" in body["detail"]["error"]

        after = snapshot_of(client)
        assert _sources(after)["mic"]["health"] == "stopped", "打不开就照旧停在已关掉"
        assert _sources(after)["loopback"]["health"] == "capturing"
        assert after["alerts"]["critical"] is None, "打不开这一路也不许报成「这一路丢了」"

        # 设备腾出来之后再按一次：这次真的接回去
        monkeypatch.delenv(REOPEN_ERROR_ENV)
        reopened = post(client, "set_source", {"source": "mic", "active": True})
        assert reopened.status_code == 200
        assert _sources(snapshot_of(client))["mic"]["health"] == "capturing"
        mic_after = backend.fed_chunks("mic")
        wait_until(
            lambda: backend.fed_chunks("mic") > mic_after + 2,
            timeout=POLL_TIMEOUT_SECONDS,
            what="设备腾出来之后 mic 真的回到读源",
        )
        assert post(client, "stop").status_code == 200
        _wait_for_done(client)


# ---------------------------------------------------------------------------
# ⑤ 三个动作的取值校验（都在动手之前拒）
# ---------------------------------------------------------------------------


def test_e_set_source_rejects_bad_arguments_before_touching_anything(tmp_path: Path, monkeypatch):
    """`source` / `active` 非法、或本场没采这一路 → 400 `invalid_setting`，那一路不受影响。"""
    monkeypatch.setenv(PACE_SECONDS_ENV, "0.02")
    with live(
        tmp_path,
        {
            "capture.backend": BACKEND_TARGET,
            "transcriber.backend": "null",
            "post.hook": "null",
        },
    ) as (client, runtime, _home, _out_root):
        backend = runtime.capture_backend
        # 这一场只采 mic（`mode` 开场定死）：loopback 这一路本场没有采，关它 / 开它都没有对象
        assert post(client, "start", {"mode": "mic"}).status_code == 200
        wait_until(
            lambda: snapshot_of(client)["top_state"] == "recording",
            timeout=POLL_TIMEOUT_SECONDS,
            what="会话进入 recording",
        )
        wait_until(
            lambda: backend.fed_chunks("mic") >= EDGE_CHUNKS,
            timeout=POLL_TIMEOUT_SECONDS,
            what="mic 这一路先喂满一段",
        )

        out_of_mode = post(client, "set_source", {"source": "loopback", "active": False})
        assert out_of_mode.status_code == 400
        body = out_of_mode.json()
        assert body["code"] == "invalid_setting"
        assert body["detail"]["key"] == "source"
        assert body["detail"]["legal"] == ["mic"], "本场在采的那几路就是这一档的合法取值"

        bad_source = post(client, "set_source", {"source": "speaker", "active": False})
        assert bad_source.status_code == 400
        assert bad_source.json()["detail"]["legal"] == ["mic", "loopback"]

        bad_active = post(client, "set_source", {"source": "mic", "active": "yes"})
        assert bad_active.status_code == 400
        assert bad_active.json()["detail"]["key"] == "active"

        # 三档都在动手之前拒：两路的 health 一个都没动，采集照常
        assert {item["id"]: item["health"] for item in snapshot_of(client)["sources"]} == {
            "mic": "capturing",
            "loopback": "capturing",
        }
        mic_fed = backend.fed_chunks("mic")
        wait_until(
            lambda: backend.fed_chunks("mic") > mic_fed + 2,
            timeout=POLL_TIMEOUT_SECONDS,
            what="拒掉那三次之后 mic 照常在采",
        )
        assert post(client, "stop").status_code == 200
        _wait_for_done(client)


# ---------------------------------------------------------------------------
# ④ 暂停期间不累计静默判定（也不把「暂停」误报成「采集已停」）
# ---------------------------------------------------------------------------


def test_d_pausing_does_not_accumulate_the_silence_judgement(tmp_path: Path, monkeypatch):
    """安静场景里暂停跨过 `alert.silence_seconds` 也不冒致命级；继续之后那条照常到点出现。"""
    monkeypatch.setenv(PACE_SECONDS_ENV, "0.02")
    # 喂满 6 块就活着不再出块：第 6 块（块号 5）正好落在假源自带的静音区里，
    # 于是从那以后每一帧电平都是 -120 dBFS —— 判定器眼里的「安静房间」
    monkeypatch.setenv(DELIVER_MIC_ENV, "6")
    monkeypatch.setenv(DELIVER_LOOPBACK_ENV, "6")
    with live(
        tmp_path,
        {
            "capture.backend": BACKEND_TARGET,
            "transcriber.backend": "null",
            "post.hook": "null",
            # 静默阈值压到 1 秒：这条读数只关心「暂停那一段时间算不算数」，
            # 180 秒那个生产默认值留给真实会议
            "alert.silence_seconds": TEST_SILENCE_SECONDS,
        },
    ) as (client, runtime, _home, _out_root):
        backend = runtime.capture_backend
        assert post(client, "start", {"mode": "both"}).status_code == 200
        wait_until(
            lambda: snapshot_of(client)["top_state"] == "recording",
            timeout=POLL_TIMEOUT_SECONDS,
            what="会话进入 recording",
        )
        wait_until(
            lambda: backend.fed_chunks("mic") >= 6 and backend.fed_chunks("loopback") >= 6,
            timeout=POLL_TIMEOUT_SECONDS,
            what="源喂到静音区就封顶",
        )
        # 先等第一帧电平落到服务侧（帧每 0.5 秒一帧），这样下面那两条心跳读数才有意义
        wait_until(
            lambda: client.get("/api/heartbeat").json()["recording_alive"] is True,
            timeout=POLL_TIMEOUT_SECONDS,
            what="第一帧电平到达",
        )
        assert post(client, "pause").status_code == 200

        # 暂停期间电平帧照发：心跳这一格仍说「采集活着」（不许用停发帧来实现暂停）
        heart = client.get("/api/heartbeat").json()
        assert heart["recording_alive"] is True
        assert heart["seconds_since_last_tick"] is not None
        assert heart["seconds_since_last_tick"] < 2.0

        # 暂停跨过静默阈值那一段时间：一条致命级都不许冒出来。
        # 这一段等的是时间本身 —— 阈值判据挂在「过了多久」上，墙钟得真的走过它。
        # 走的是**阈值的两倍**：判错的话那条致命级会在 ~1.0–1.5 秒（阈值 + 帧的
        # 0.5 秒量化）内冒出来，两倍照样抓得到（review-1 m4：原来那个 +0.5 秒的尾巴去掉了）
        time.sleep(TEST_SILENCE_SECONDS * 2.0)
        during = snapshot_of(client)
        assert during["alerts"]["critical"] is None, (
            "暂停区间不该累计静默秒数（安静房间里暂停三分钟会弹一条致命级）"
        )
        assert during["top_state"] == "recording"
        assert during["session"]["paused"] is True
        # 暂停这一整段里帧一帧都没停：采集这一侧始终答得出「我还活着」
        assert client.get("/api/heartbeat").json()["recording_alive"] is True

        # 继续之后同一条判据照常到点：阈值真的够得着
        assert post(client, "resume").status_code == 200
        alerted = wait_until(
            lambda: (
                lambda snap: snap["alerts"]["critical"] if snap["alerts"]["critical"] else None
            )(snapshot_of(client)),
            timeout=POLL_TIMEOUT_SECONDS,
            what="继续之后静默判定照常起致命级",
        )
        assert "静默" in alerted["message"]
        assert snapshot_of(client)["top_state"] == "alert"
        assert post(client, "stop").status_code == 200
        _wait_for_done(client)
