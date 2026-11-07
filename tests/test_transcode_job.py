"""转码独立工序的用例：凭据判据、打断续做、半截产物重做、暂停继续、重启存活、三只动作。

本节点（转码搬出收尾）的完成判定落在这几条上：

  - **「这一段写完了」以凭据为准**：产物文件在那儿不算完成 —— 造一份同名半截产物 +
    完整源，验证它**不被跳过**而是重做；
  - **凭据不认账就重做**：源 PCM 尺寸对不上、产物字节对不上，那一段重做；
  - **可暂停、可继续、可补跑**：暂停后逐段进度不再前进，继续后接着前进；
  - **进度 / 暂停状态在重启后存活**：判据读盘上凭据与状态文件，不依赖进程内存；
  - **唯一硬闸 = 本地大模型互斥**：会后精细稿在飞时 `start` 409；**转码在飞不构成
    `start` 的阻挡**。

全部用假件（假转码器只写字节，不起真 ffmpeg / 真模型）：不起真模型、不占显卡、
不拉起真 DSH（autopilot 红线）。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from meeting import config as config_module
from meeting.capture import transcode as transcode_module
from meeting.service.app import build_runtime
from tests._offline_fakes import synth_session

#: 等后台那一趟收工的轮询上限（事件驱动地等，等到了就立刻回）
JOB_TIMEOUT_SECONDS = 5.0


# ---------------------------------------------------------------------------
# 假件
# ---------------------------------------------------------------------------


class RecordingConverter:
    """假转码器：登记 + 写一份可辨的假 MP3（真 ffmpeg 在不在场与用例无关）。"""

    def __init__(self, *, body: bytes = b"fake-mp3-bytes", raises: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.body = body
        self.raises = raises

    def __call__(self, pcm_path, mp3_path) -> bool:
        self.calls.append((str(pcm_path), str(mp3_path)))
        if self.raises:
            raise RuntimeError(self.raises)
        Path(mp3_path).write_bytes(self.body)
        return True


class GatedConverter:
    """按段放行的假转码器：每一段等到自己的放行事件才写产物（暂停 / 继续的读数靠它）。"""

    def __init__(self, *, bodies: dict[str, bytes] | None = None) -> None:
        self.gates: dict[str, threading.Event] = {}
        self.reached: dict[str, threading.Event] = {}
        self.bodies = bodies or {}
        self.calls: list[str] = []

    def gate_for(self, key: str) -> threading.Event:
        self.gates.setdefault(key, threading.Event())
        self.reached.setdefault(key, threading.Event())
        return self.gates[key]

    def __call__(self, pcm_path, mp3_path) -> bool:
        key = f"{Path(pcm_path).stem}"
        self.calls.append(key)
        self.reached.setdefault(key, threading.Event()).set()
        self.gates.setdefault(key, threading.Event()).wait(timeout=JOB_TIMEOUT_SECONDS)
        Path(mp3_path).write_bytes(self.bodies.get(key, b"gated-mp3"))
        return True


# ---------------------------------------------------------------------------
# 一、凭据判据（共享模块；`meeting post` 与服务工序共用这一份）
# ---------------------------------------------------------------------------


def test_run_transcode_writes_credentials_and_skips_them_on_rerun(tmp_path) -> None:
    """转完落凭据；补跑时凭据认账的那一段**不重转**（转码器一次都不被喊）。"""
    session = synth_session(tmp_path / "ws-tc-a", sources=("mic",), seconds=1.0)
    converter = RecordingConverter()

    first = transcode_module.run_transcode(session, converter=converter)

    assert first["state"] == "done", first
    assert len(converter.calls) == 1, converter.calls
    assert (session / "meeting_audio_mic_001.mp3").read_bytes() == b"fake-mp3-bytes"
    credential = transcode_module.read_credential(
        transcode_module.credential_path(session, "mic", 1)
    )
    assert credential is not None and credential["produced"] is True, credential
    assert credential["pcm_bytes"] == 32000, credential
    assert transcode_module.read_status(session)["state"] == "done"

    second = transcode_module.run_transcode(session, converter=converter)

    assert second["state"] == "done"
    assert len(converter.calls) == 1, "凭据认账的段不重转"
    assert second["rows"][0]["reused"] is True


def test_a_half_written_mp3_with_a_full_source_is_redone(tmp_path) -> None:
    """同名半截产物 + 完整源、**没有凭据** → 不被当成品跳过，那一段重做。"""
    session = synth_session(tmp_path / "ws-tc-b", sources=("mic",), seconds=1.0)
    mp3 = session / "meeting_audio_mic_001.mp3"
    mp3.write_bytes(b"half")  # 强杀留下的半截产物（没有凭据替它作证）
    converter = RecordingConverter(body=b"redone-mp3")

    summary = transcode_module.run_transcode(session, converter=converter)

    assert summary["state"] == "done", summary
    assert mp3.read_bytes() == b"redone-mp3", "半截产物被重做替换"
    assert transcode_module.segment_done(
        transcode_module.plan_segments(session, ("mic",))[0]
    )


def test_a_credential_that_disagrees_with_the_source_is_redone(tmp_path) -> None:
    """凭据与源对不上账（PCM 尺寸 / 产物字节变了）→ 那一段重做。"""
    session = synth_session(tmp_path / "ws-tc-c", sources=("mic",), seconds=1.0)
    entry = transcode_module.plan_segments(session, ("mic",))[0]
    transcode_module.write_credential(
        entry,
        {
            "produced": True,
            "pcm_bytes": 12345,  # 与盘上的源对不上
            "pcm_seconds": 0.38,
            "mp3": entry.mp3.name,
            "mp3_bytes": 3,
            "finished_at": transcode_module.now_iso(),
        },
    )
    converter = RecordingConverter(body=b"fresh")

    summary = transcode_module.run_transcode(session, converter=converter)

    assert summary["state"] == "done"
    assert converter.calls, "不认账的凭据要重做"
    assert (session / "meeting_audio_mic_001.mp3").read_bytes() == b"fresh"

    # 产物被人改了字节（凭据记的 mp3_bytes 对不上）→ 同样重做
    (session / "meeting_audio_mic_001.mp3").write_bytes(b"tampered")
    converter2 = RecordingConverter(body=b"fresh-2")
    transcode_module.run_transcode(session, converter=converter2)
    assert converter2.calls, "产物字节对不上凭据的段要重做"


def test_an_empty_pcm_segment_gets_an_empty_credential_and_no_mp3(tmp_path) -> None:
    """空段：不产 MP3、落 `produced: false` 的凭据；补跑不再进转码器。"""
    session = synth_session(tmp_path / "ws-tc-d", sources=("mic",), empty=("mic",))
    converter = RecordingConverter()

    summary = transcode_module.run_transcode(session, converter=converter)

    assert summary["state"] == "done"
    assert summary["rows"][0]["state"] == "skipped"
    assert not (session / "meeting_audio_mic_001.mp3").exists()
    credential = transcode_module.read_credential(
        transcode_module.credential_path(session, "mic", 1)
    )
    assert credential["produced"] is False, credential

    again = transcode_module.run_transcode(session, converter=converter)
    assert again["rows"][0]["reused"] is True
    assert converter.calls == [], "空段凭据认账，转码器一次都不进"


def test_a_failed_segment_does_not_stop_the_others_and_leaves_pcm_alone(tmp_path) -> None:
    """一段转码器抛错：该段 `failed`、别的段照转；PCM 一个字节都不动、临时产物不残留。"""
    session = synth_session(tmp_path / "ws-tc-e", sources=("mic", "loopback"), seconds=0.5)
    pcm_bytes = (session / "meeting_audio_mic_001.pcm").read_bytes()

    class Pickiness:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def __call__(self, pcm_path, mp3_path) -> bool:
            self.calls.append(Path(pcm_path).name)
            if "mic" in Path(pcm_path).name:
                raise RuntimeError("ffmpeg 不在 PATH 上")
            Path(mp3_path).write_bytes(b"ok-loopback")
            return True

    converter = Pickiness()
    summary = transcode_module.run_transcode(session, converter=converter)

    assert summary["state"] == "partial", summary
    assert (session / "meeting_audio_mic_001.pcm").read_bytes() == pcm_bytes
    assert (session / "meeting_audio_loopback_001.mp3").read_bytes() == b"ok-loopback"
    statuses = transcode_module.read_status(session)
    assert statuses["state"] == "partial"
    assert statuses["error"] and "mic" in statuses["error"], statuses


def test_pause_stops_the_progress_and_a_resume_pass_finishes_it(tmp_path) -> None:
    """暂停后逐段进度不再前进（工序在段边界落 `paused` 等旗标）；摘旗后接着前进到收工。"""
    session = synth_session(tmp_path / "ws-tc-f", sources=("mic", "loopback"), seconds=0.5)
    converter = RecordingConverter()
    pause = threading.Event()
    pause.set()  # 开跑前就立着：一段都不该转

    # `pause` 立着时工序在段边界等旗标（盘上状态落 paused），函数不散场 —— 用线程跑它
    summary: list[dict] = []
    worker = threading.Thread(
        target=lambda: summary.append(
            transcode_module.run_transcode(session, converter=converter, pause=pause.is_set)
        ),
        daemon=True,
    )
    worker.start()
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        document = transcode_module.read_status(session)
        if document is not None and document["state"] == "paused":
            break
        time.sleep(0.005)
    document = transcode_module.read_status(session)
    assert document["state"] == "paused", document
    assert converter.calls == [], "暂停期间一段都不开跑"
    # 暂停状态在盘上（重启后照样读得到）：进程内的旗标与本读法无关

    # 继续 = 旗标摘下（服务侧 `transcode_resume` 的接缝）：同一条工序在段边界接着前进
    pause.clear()
    worker.join(timeout=JOB_TIMEOUT_SECONDS)
    assert not worker.is_alive(), "旗标摘下之后工序该接着做完"

    assert summary[0]["state"] == "done", summary
    assert summary[0]["segments_done"] == 2, summary
    assert transcode_module.read_status(session)["state"] == "done"


# ---------------------------------------------------------------------------
# 二、服务侧：三只动作与读数（进程内真运行时）
# ---------------------------------------------------------------------------

#: 本文件起过的运行时（收场统一把转写流水线收干净，见下面那道 autouse 夹具）
_RUNTIMES: list[Any] = []


@pytest.fixture(autouse=True)
def _join_transcriber_pipelines():
    """收场把本用例起过的转写流水线收干净（线程汇合，`asr-` 线程不漏给同进程的下一个文件）。

    生产路径上转写后端的 `stop()` 只有 `shutdown()` 一个调用点（收尾编排不停转写），本文件
    的用例又各自 `start_session()` 起一条真转写流水线（worker + 两路读线程，daemon）：单进程
    连跑时这些线程会一直活着漏给下一个文件，`tests/test_asr_pipeline.py` 开场那条「进程里
    一条 `asr-` 线程都不剩」的线程闸因此每条用例都红（实测 4 条流水线 12 个线程、44 errors）。
    这里在测试层补上汇合点：收场对本文件登记过的每个运行时调一次
    `transcriber_backend.stop()`（幂等；空闲流水线毫秒级收工）。
    """
    yield
    for runtime in _RUNTIMES:
        backend = getattr(runtime, "transcriber_backend", None)
        stop = getattr(backend, "stop", None)
        if callable(stop):
            stop()
    _RUNTIMES.clear()


def _runtime(tmp_path: Path, monkeypatch, **values):
    """起一台真运行时（dev 采集替身），配置里覆盖 `**values`。"""
    home = tmp_path / "home-tc"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("MEETING_CAPTURE", raising=False)
    previous = config_module.current()
    applied = {
        **config_module.DEFAULT_VALUES,
        "log.dir": str(home / "logs"),
        "capture.backend": "dev",
        **values,
    }
    runtime = build_runtime(
        config_module.Config(applied=applied, sources={key: "默认" for key in applied}, path="")
    )
    runtime._resolve_backends()
    _RUNTIMES.append(runtime)
    return runtime, previous


def _wait_transcode_done(runtime, session: Path) -> None:
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if runtime._post_job_slot("transcode", str(session)) is None:
            return
        time.sleep(0.005)
    raise AssertionError("等不到转码工序收工")


def test_transcode_run_action_runs_the_job_and_writes_the_status(tmp_path, monkeypatch) -> None:
    """`transcode_run`：起一道工序、按凭据把段转完；状态文件落 `done`。"""
    runtime, previous = _runtime(tmp_path, monkeypatch)
    session = synth_session(tmp_path / "ws-tc-run", sources=("mic",), seconds=0.5)
    runtime.workspace_open(str(session))
    converter = RecordingConverter()
    monkeypatch.setattr(transcode_module, "default_converter", lambda: converter)
    try:
        receipt = runtime.transcode_run({"dir": str(session)})
        assert receipt == {"kind": "transcode", "dir": str(session), "started": True}, receipt
        assert runtime._post_job_slot("transcode", str(session)) is not None
        _wait_transcode_done(runtime, session)

        assert transcode_module.read_status(session)["state"] == "done"
        assert (session / "meeting_audio_mic_001.mp3").exists()
        # 同一场第二趟在飞时如实拒（(种类, 场次) 槽闸）
        blocker = GatedConverter()
        gate = blocker.gate_for("meeting_audio_mic_001")
        monkeypatch.setattr(transcode_module, "default_converter", lambda: blocker)
        runtime.transcode_run({"dir": str(session)})
        try:
            from meeting.service.runtime_shared import ActionError

            with pytest.raises(ActionError) as info:
                runtime.transcode_run({"dir": str(session)})
            assert info.value.code == "draining" and info.value.status == 409
            assert info.value.detail["kind"] == "transcode"
        finally:
            gate.set()
            _wait_transcode_done(runtime, session)
    finally:
        config_module.set_current(previous)


def test_session_status_post_list_excludes_the_transcode_job(tmp_path, monkeypatch) -> None:
    """契约 4.9：转码那一趟**不进**单场状态的 `post` 清单（它的读数在 `transcode` 格）。

    在飞与收工后（那一格带着 `finished_at` 留在 `post_jobs` 里）都不外泄到 `post`；
    `transcode` 格照答（在飞 `running`、收工 `done`）。没有这道滤时，前端
    `sessionPostViewSchema`（`kind` 只认 offline / minutes）对整只端点 parse 必败。
    """
    runtime, previous = _runtime(tmp_path, monkeypatch)
    session = synth_session(tmp_path / "ws-tc-postlist", sources=("mic",), seconds=0.5)
    runtime.workspace_open(str(session))
    converter = GatedConverter()
    gate = converter.gate_for("meeting_audio_mic_001")
    monkeypatch.setattr(transcode_module, "default_converter", lambda: converter)
    try:
        runtime.transcode_run({"dir": str(session)})
        assert converter.reached["meeting_audio_mic_001"].wait(timeout=JOB_TIMEOUT_SECONDS)

        body = runtime.session_status_dict(str(session))
        assert [item["kind"] for item in body["post"]] == [], body["post"]
        assert body["transcode"]["state"] == "running", body["transcode"]

        gate.set()
        _wait_transcode_done(runtime, session)

        body = runtime.session_status_dict(str(session))
        assert [item["kind"] for item in body["post"]] == [], body["post"]
        assert body["transcode"]["state"] == "done", body["transcode"]
    finally:
        gate.set()
        config_module.set_current(previous)


def test_transcode_pause_and_resume_freeze_and_resume_the_progress(
    tmp_path, monkeypatch
) -> None:
    """暂停后逐段进度不再前进（受理即回、段边界收场）；继续后接着前进。"""
    runtime, previous = _runtime(tmp_path, monkeypatch)
    session = synth_session(tmp_path / "ws-tc-pause", sources=("mic", "loopback"), seconds=0.5)
    runtime.workspace_open(str(session))
    converter = GatedConverter()
    mic_gate = converter.gate_for("meeting_audio_mic_001")
    loop_gate = converter.gate_for("meeting_audio_loopback_001")
    monkeypatch.setattr(transcode_module, "default_converter", lambda: converter)
    try:
        runtime.transcode_run({"dir": str(session)})
        assert converter.reached["meeting_audio_mic_001"].wait(timeout=JOB_TIMEOUT_SECONDS)
        receipt = runtime.transcode_pause({"dir": str(session)})
        assert receipt == {
            "kind": "transcode",
            "dir": str(session),
            "pausing": True,
        }, receipt
        # 当前段（mic）收尾之后工序停在 paused：loopback 那一段不开跑 —— 进度不再前进
        mic_gate.set()
        deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            document = transcode_module.read_status(session)
            if document is not None and document["state"] == "paused":
                break
            time.sleep(0.005)
        document = transcode_module.read_status(session)
        assert document["state"] == "paused", document
        done_before = document["sources"][0]["done"]
        loop_cell = next(
            item for item in document["sources"] if item["source"] == "loopback"
        )
        assert loop_cell["done"] == 0, "暂停之后下一段不开跑"

        runtime.transcode_resume({"dir": str(session)})
        loop_gate.set()
        _wait_transcode_done(runtime, session)
        document = transcode_module.read_status(session)
        assert document["state"] == "done", document
        assert document["sources"][0]["done"] >= done_before, "继续后进度接着前进"
        loop_cell = next(
            item for item in document["sources"] if item["source"] == "loopback"
        )
        assert loop_cell["done"] == 1, "继续后剩下的段接着做完"

        # 没有在跑的工序再暂停 → 409（没有可暂停的）
        from meeting.service.runtime_shared import ActionError

        with pytest.raises(ActionError) as info:
            runtime.transcode_pause({"dir": str(session)})
        assert info.value.code == "draining"
    finally:
        mic_gate.set()
        loop_gate.set()
        config_module.set_current(previous)


def test_the_transcode_readout_survives_a_restart(tmp_path, monkeypatch) -> None:
    """进度 / 暂停读数读盘（凭据 + 状态文件）：进程内的登记摘掉之后照旧读得到。"""
    runtime, previous = _runtime(tmp_path, monkeypatch)
    session = synth_session(tmp_path / "ws-tc-restart", sources=("mic",), seconds=0.5)
    runtime.workspace_open(str(session))
    converter = RecordingConverter()
    monkeypatch.setattr(transcode_module, "default_converter", lambda: converter)
    try:
        runtime.transcode_run({"dir": str(session)})
        _wait_transcode_done(runtime, session)
        flying = runtime.session_status_dict(str(session))["transcode"]
        assert flying["in_process"] is False, "收工那一刻这一格翻回假"
        assert flying["state"] == "done"
        assert flying["sources"][0]["total"] == 1

        # 「重启」 = 进程内的登记没了（内存事实）：读数照旧从盘上回答
        runtime.post_jobs.pop(("transcode", str(session)), None)
        after = runtime.session_status_dict(str(session))["transcode"]
        assert after["state"] == "done" and after["in_process"] is False
        assert after["sources"][0]["done"] == 1

        # 补跑一趟全凭据认账的场次：照常受理、毫秒级收工、不重转
        converter.calls.clear()
        runtime.transcode_run({"dir": str(session)})
        _wait_transcode_done(runtime, session)
        assert converter.calls == [], "凭据认账的补跑不重转"
    finally:
        config_module.set_current(previous)


def test_transcode_run_refuses_the_actively_recording_session(tmp_path, monkeypatch) -> None:
    """本进程在录的那一场：PCM 还在长，补跑如实拒（凭据对不上账的那档现场）。"""
    runtime, previous = _runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "ws-tc-live"
    workspace.mkdir()
    try:
        runtime.start_session(dir=str(workspace))
        from meeting.service.runtime_shared import ActionError

        with pytest.raises(ActionError) as info:
            runtime.transcode_run({"dir": str(workspace)})
        assert info.value.code == "invalid_setting" and info.value.status == 400
    finally:
        config_module.set_current(previous)


# ---------------------------------------------------------------------------
# 三、唯一硬闸：会后精细稿在飞时 start 409；转码在飞不构成 start 的阻挡
# ---------------------------------------------------------------------------


def _ended_runtime(tmp_path: Path, monkeypatch):
    """开一场、把收尾整趟跑完：返回 `(runtime, session_dir, previous)`。"""
    import asyncio

    from meeting.state import begin_drain

    runtime, previous = _runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "ws-tc-ended"
    workspace.mkdir()
    runtime.start_session(dir=str(workspace))
    begin_drain(runtime.snapshot, reason="user")
    asyncio.run(runtime._run_drain_steps())
    assert runtime.snapshot.ended.phase == "done"
    return runtime, Path(runtime.snapshot.session.dir), previous


def _wait_offline_done(runtime) -> None:
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if runtime._post_job_slot("offline", str(runtime.snapshot.session.dir or "")) is None:
            return
        time.sleep(0.005)
    raise AssertionError("等不到会后的活儿收工")


def test_start_is_refused_while_an_offline_job_is_flying(tmp_path, monkeypatch) -> None:
    """会后精细稿在飞：`start` 409（本地大模型互斥）；停了它之后 start 放行。"""
    import threading

    from meeting.offline import runner as offline_runner
    from meeting.service.runtime_shared import ActionError

    runtime, session_dir, previous = _ended_runtime(tmp_path, monkeypatch)
    gate = threading.Event()
    started = threading.Event()

    def blocking_run_session(target, **kwargs):
        started.set()
        gate.wait(timeout=JOB_TIMEOUT_SECONDS)
        return {"state": "done"}

    monkeypatch.setattr(offline_runner, "run_session", blocking_run_session)
    other = tmp_path / "ws-tc-next"
    other.mkdir()
    runtime.workspace_open(str(other))
    try:
        runtime.post_offline({})
        assert started.wait(timeout=JOB_TIMEOUT_SECONDS)
        with pytest.raises(ActionError) as info:
            runtime.start_session(dir=str(other))
        assert info.value.code == "draining" and info.value.status == 409
        assert info.value.detail["kind"] == "offline", info.value.detail
        assert info.value.detail["dir"] == str(session_dir)

        # 停掉它（post_stop 受理即回；假件在场，旗标立起后块边界即收）之后放行
        runtime.post_stop({"dir": str(session_dir)})
        gate.set()
        deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not runtime._post_jobs_of_kind("offline"):
                break
            time.sleep(0.005)
        assert not runtime._post_jobs_of_kind("offline")
        runtime.start_session(dir=str(other))
        assert runtime.snapshot.session.dir == str(other)
    finally:
        gate.set()
        config_module.set_current(previous)


def test_transcode_run_is_accepted_while_an_offline_job_is_flying(
    tmp_path, monkeypatch
) -> None:
    """会后精细稿在飞（另一场）：`transcode_run` 照常受理 —— 转码与 offline 互不设闸。"""
    import threading

    from meeting.offline import runner as offline_runner

    runtime, session_dir, previous = _ended_runtime(tmp_path, monkeypatch)
    gate = threading.Event()
    started = threading.Event()

    def blocking_run_session(target, **kwargs):
        started.set()
        gate.wait(timeout=JOB_TIMEOUT_SECONDS)
        return {"state": "done"}

    monkeypatch.setattr(offline_runner, "run_session", blocking_run_session)
    session = synth_session(tmp_path / "ws-tc-para", sources=("mic",), seconds=0.5)
    runtime.workspace_open(str(session))
    converter = RecordingConverter()
    monkeypatch.setattr(transcode_module, "default_converter", lambda: converter)
    try:
        runtime.post_offline({})
        assert started.wait(timeout=JOB_TIMEOUT_SECONDS)
        receipt = runtime.transcode_run({"dir": str(session)})
        assert receipt == {"kind": "transcode", "dir": str(session), "started": True}, receipt
        _wait_transcode_done(runtime, session)
        flying = {job["kind"] for job in runtime.post_jobs_state()}
        assert flying == {"offline"}, flying
        assert transcode_module.read_status(session)["state"] == "done"
    finally:
        gate.set()
        _wait_offline_done(runtime)
        config_module.set_current(previous)


def test_start_is_not_blocked_by_a_flying_transcode(tmp_path, monkeypatch) -> None:
    """转码在飞：`start` 照常放行（转码不在收尾序列里、也不吃会中那张模型）。"""
    runtime, _session_dir, previous = _ended_runtime(tmp_path, monkeypatch)
    session = synth_session(tmp_path / "ws-tc-block", sources=("mic",), seconds=0.5)
    runtime.workspace_open(str(session))
    converter = GatedConverter()
    gate = converter.gate_for("meeting_audio_mic_001")
    monkeypatch.setattr(transcode_module, "default_converter", lambda: converter)
    try:
        runtime.transcode_run({"dir": str(session)})
        assert converter.reached["meeting_audio_mic_001"].wait(timeout=JOB_TIMEOUT_SECONDS)
        assert runtime._post_job_slot("transcode", str(session)) is not None

        other = tmp_path / "ws-tc-next-2"
        other.mkdir()
        runtime.workspace_open(str(other))
        runtime.start_session(dir=str(other))
        assert runtime.snapshot.session.dir == str(other)
    finally:
        gate.set()
        config_module.set_current(previous)
