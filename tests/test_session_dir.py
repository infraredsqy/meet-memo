"""`tests/test_session_dir.py` —— 工作区寻址与 `session.json`（「没有默认落点」定稿）。

覆盖：

  - `start` **必须显式给目标工作区目录**：缺失 / 不是已存在目录都被 `invalid_setting` 拦下；
    服务侧不建目录、不按日期命名；目录里已有 `session.json` 的那一档**放行**——
    那是续接（同一场会再录一段，`docs/design-intent.md` 第 9 条）；
  - **续接**：对着录过的目录再 start，旧记录的身份三格与段账原样保留在合并后的
    `session.json` 里，收尾失败那几格从 waiting 重新起算；
  - **段号分配**：`next_segment_number()` 从盘上实有的段文件认号（最大值 + 1），
    认名规则与 `archive_name()` 同一条；
  - `session.id` **就是目录名**，`dir` 是工作区目录的绝对路径；
  - **日期**是 `session.json` 的元信息字段（`date`），从开场的时刻取，不来自目录名；
  - `session.json` 字段全貌、**原子写**（**无 `.tmp` 残骸**、读到的永远是完整 JSON）、
    **每个阶段变更都落盘一次**（四步的 `status` / `detail` 各写一次）；
  - `sources[*].archives` = 段文件名表（`meeting_audio_<source>_<段号:03d>.pcm`），
    `segments` + `audio_duration_seconds` = 段账（一段 = 一次录制；权威时长 = 各段在录
    时长之和，写端唯一的自洽式）；
  - `start` 写文件失败 → **不进 `recording`**、只收走本场写入的文件、主 log 有留痕。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from tests import _helpers as helpers
from meeting.session import (
    SESSION_FILE_NAME,
    SessionStore,
    archive_name,
    archive_segment_number,
    build_session_record,
    next_segment_number,
    resolve_workspace_dir,
    segment_archive_paths,
    segment_numbers,
    state_for,
)
from meeting.state import DRAIN_STEP_IDS


# ---------------------------------------------------------------------------
# 工作区寻址（没有默认落点）
# ---------------------------------------------------------------------------


def test_resolve_workspace_dir_requires_an_existing_directory(tmp_path):
    """必给、已存在、里面没有另一场的记录；返回**绝对路径**，什么都没建。"""
    workspace = tmp_path / "跟老王聊周会"
    workspace.mkdir()
    resolved = resolve_workspace_dir(str(workspace))
    assert Path(resolved).is_absolute()
    assert resolved == str(workspace)
    # 没设默认落点：除了用户自己建的那个什么都没有
    assert [item.name for item in tmp_path.iterdir()] == ["跟老王聊周会"]


def test_resolve_workspace_dir_refuses_missing_targets(tmp_path):
    """缺失值 / 不存在的目录在这里当场拦下；已有记录的目录放行（那是续接）。"""
    with pytest.raises(ValueError):
        resolve_workspace_dir(None)
    with pytest.raises(ValueError):
        resolve_workspace_dir("")
    with pytest.raises(ValueError):
        resolve_workspace_dir(str(tmp_path / "not-there"))  # 不自动建

    taken = tmp_path / "taken"
    taken.mkdir()
    (taken / SESSION_FILE_NAME).write_text("{}", encoding="utf-8")
    # 录过的目录照样解析得回（对着它按 start = 续接同一场会，第 9 条翻案）
    assert resolve_workspace_dir(str(taken)) == str(taken)


def test_next_segment_number_counts_the_segments_on_disk(tmp_path):
    """下一个段号 = 盘上实有段号的最大值 + 1；两路共一个计器，认名只认三位数字。"""
    assert next_segment_number(tmp_path) == 1  # 空目录从 1 起
    (tmp_path / "meeting_audio_mic_001.pcm").write_bytes(b"a")
    (tmp_path / "meeting_audio_loopback_001.pcm").write_bytes(b"b")
    assert next_segment_number(tmp_path) == 2  # 续接一场（每路恰好一段）之后得 2
    (tmp_path / "meeting_audio_mic_002.pcm").write_bytes(b"c")
    (tmp_path / "meeting_audio_mic_002.mp3").write_bytes(b"m")
    assert next_segment_number(tmp_path) == 3
    # 别路的段、认不出的名字都不进计器
    (tmp_path / "meeting_audio_loopback_010.pcm").write_bytes(b"d")
    (tmp_path / "meeting_audio_mic_00xx.pcm").write_bytes(b"bad")
    assert next_segment_number(tmp_path) == 11
    assert segment_numbers(tmp_path, "mic") == [1, 2]
    assert segment_numbers(tmp_path, "loopback") == [1, 10]


def test_start_action_requires_the_workspace_dir(tmp_path):
    """走真入口：不带 `args.dir` 的 start 回 `invalid_setting`(400)，话里说明没有默认落点。"""
    stack, client, runtime, out_root = build_client(tmp_path)
    try:
        response = _post(client, "start", {})
        assert response.status_code == 400, response.text
        body = response.json()
        assert body["code"] == "invalid_setting"
        assert "默认落点" in body["message"]
        # 放置根下什么都没出现（没建目录、没有场次目录）
        assert list(out_root.iterdir()) == []
        assert runtime.snapshot.top_state == "idle"
    finally:
        stack.close()


def test_start_action_refuses_a_nonexistent_dir_and_does_not_create_it(tmp_path):
    """`args.dir` 指到不存在的目录：400、目录不被建出来（软件不建目录）。"""
    stack, client, runtime, _out = build_client(tmp_path)
    try:
        target = tmp_path / "out" / "陌生人给的目录"
        response = _post(client, "start", {"dir": str(target)})
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_setting"
        assert not target.exists(), "服务不该替用户建目录"
        assert runtime.snapshot.top_state == "idle"
    finally:
        stack.close()


def test_start_action_resumes_a_workspace_that_already_has_a_session(tmp_path):
    """目录里已有 `session.json`：放行 = 续接。旧记录的身份三格与段账原样保留。"""
    stack, client, runtime, out_root = build_client(tmp_path)
    try:
        taken = Path(helpers.allocate_start_dir(out_root))
        assert _post(client, "start", {"dir": str(taken)}).status_code == 200
        first = read_json(taken / SESSION_FILE_NAME)
        assert _post(client, "stop").status_code == 200
        assert _wait_for(
            lambda: (lambda r: r if r and r["state"] == "done" else None)(
                read_json(taken / SESSION_FILE_NAME)
            ),
            what="第一场收尾落成 done",
        )

        # 对着同一个目录再 start：不回 400，进场录音
        assert _post(client, "start", {"dir": str(taken)}).status_code == 200
        assert runtime.snapshot.session.dir == str(taken)
        resumed = read_json(taken / SESSION_FILE_NAME)
        # 身份三格沿用开场那一次（一场会一个身份）；收尾四步重新起算
        assert resumed["session_id"] == first["session_id"]
        assert resumed["created_at"] == first["created_at"]
        assert resumed["started_at"] == first["started_at"]
        assert resumed["mode"] == first["mode"]
        assert resumed["date"] == first["date"]
        assert [step["status"] for step in resumed["drain"]["steps"]] == ["waiting"] * 4
        assert resumed["interrupted"] is False
    finally:
        stack.close()


def test_start_uses_the_given_workspace_verbatim(tmp_path):
    """显式给的目录原样成为这一场的工作区：`session.id` = 目录名、`dir` = 绝对路径、
    日期进 `session.json` 的 `date` 字段。"""
    stack, client, runtime, out_root = build_client(tmp_path)
    try:
        workspace = Path(helpers.allocate_start_dir(out_root)) / "250906-不按日期命名的名字"
        workspace.mkdir()
        response = _post(client, "start", {"dir": str(workspace)})
        assert response.status_code == 200, response.text
        record = read_json(workspace / SESSION_FILE_NAME)
        assert runtime.snapshot.session.id == workspace.name
        assert runtime.snapshot.session.dir == str(workspace)
        assert record["session_id"] == workspace.name == "250906-不按日期命名的名字"
        assert record["dir"] == str(workspace)
        # 日期是元信息：从开场的时刻取（今天），目录名的语义不算数
        assert len(record["date"]) == 10 and record["date"][4] == "-"
        assert set(record) == {
            "session_id", "dir", "date", "mode", "state", "created_at", "started_at",
            "ended_at", "sources", "segments", "audio_duration_seconds", "drain",
            "interrupted", "service",
        }
    finally:
        stack.close()


# ---------------------------------------------------------------------------
# session.json 的 schema 与原子写
# ---------------------------------------------------------------------------


def test_session_json_has_the_frozen_field_shape(tmp_path):
    """字段全貌：一份都不省（四步各一条、每条四键齐；两路各一条、每条三键齐）。"""
    record = build_session_record(
        session_id="跟老王聊周会",
        session_dir=str(tmp_path / "跟老王聊周会"),
        mode="both",
        state="recording",
        created_at="2025-09-06T10:00:00+08:00",
        started_at="2025-09-06T10:00:00+08:00",
        devices={"mic": "麦克风", "loopback": None},
        pid=1234,
    )
    assert set(record) == {
        "session_id", "dir", "date", "mode", "state", "created_at", "started_at", "ended_at",
        "sources", "segments", "audio_duration_seconds", "drain", "interrupted", "service",
    }
    assert record["date"] == "2025-09-06"
    assert record["state"] in ("recording", "draining", "done", "failed")
    assert [step["id"] for step in record["drain"]["steps"]] == list(DRAIN_STEP_IDS)
    for step in record["drain"]["steps"]:
        assert set(step) == {"id", "status", "detail", "error"}
        assert step["status"] == "waiting"
    assert [item["id"] for item in record["sources"]] == ["mic", "loopback"]
    for item in record["sources"]:
        assert set(item) == {"id", "device", "archives"}
        # 段文件名形状 = meeting_audio_<source>_<段号:03d>.pcm（一段 = 一次录制，序号从 1 起）
        assert item["archives"] == [f"meeting_audio_{item['id']}_001.pcm"]
    # 段账：开场第一版还没有段（引擎没起），权威时长为 0
    assert record["segments"] == []
    assert record["audio_duration_seconds"] == 0.0
    assert set(record["service"]) == {"pid", "started_at"}
    assert record["service"]["pid"] == 1234
    assert record["interrupted"] is False
    assert record["ended_at"] is None


def test_archive_names_carry_the_segment_ordinal():
    """段命名：一段 = 一次录制，序号从 1 起进文件名；认号只认三位数字。"""
    assert archive_name("mic") == "meeting_audio_mic_001.pcm"
    assert archive_name("loopback", 2) == "meeting_audio_loopback_002.pcm"
    assert archive_name("mic", 12) == "meeting_audio_mic_012.pcm"
    # 认号：三位数字才算段号，别的形状一概不是段文件
    assert archive_segment_number("meeting_audio_mic_007", "mic") == 7
    assert archive_segment_number("meeting_audio_mic_0007", "mic") is None
    assert archive_segment_number("meeting_audio_mic_abc", "mic") is None
    assert archive_segment_number("meeting_audio_mic", "mic") is None
    assert archive_segment_number("meeting_audio_loopback_001", "mic") is None


def test_segment_archive_paths_enumerate_in_ordinal_order(tmp_path):
    """段列表来自盘上实有的段文件：按序号升序，别路的不混进来，`.mp3` 同一套序号。"""
    (tmp_path / "meeting_audio_mic_002.pcm").write_bytes(b"b")
    (tmp_path / "meeting_audio_mic_001.pcm").write_bytes(b"a")
    (tmp_path / "meeting_audio_mic_010.pcm").write_bytes(b"c")
    (tmp_path / "meeting_audio_mic_00x.pcm").write_bytes(b"bad")
    (tmp_path / "meeting_audio_loopback_001.pcm").write_bytes(b"other")
    (tmp_path / "meeting_audio_mic_002.mp3").write_bytes(b"m")

    assert [path.name for path in segment_archive_paths(tmp_path, "mic")] == [
        "meeting_audio_mic_001.pcm",
        "meeting_audio_mic_002.pcm",
        "meeting_audio_mic_010.pcm",
    ]
    assert [path.name for path in segment_archive_paths(tmp_path, "mic", suffix=".mp3")] == [
        "meeting_audio_mic_002.mp3"
    ]
    assert segment_archive_paths(tmp_path, "loopback") == [
        tmp_path / "meeting_audio_loopback_001.pcm"
    ]
    # 空目录 / 不存在的目录：空表（消费端按「没有段」处理）
    assert segment_archive_paths(tmp_path / "nowhere", "mic") == []


def test_build_session_record_carries_the_segment_ledger():
    """段账进 `session.json`：段列表 + 权威时长 + 每路的段文件名表。"""
    segments = [
        {
            "index": 1,
            "started_at": "2025-09-06T10:00:00+08:00",
            "ended_at": "2025-09-06T10:01:00+08:00",
            "sources": ["mic", "loopback"],
            "pauses": [{"start": "2025-09-06T10:00:30+08:00", "end": "2025-09-06T10:00:40+08:00"}],
            "duration_seconds": 50.0,
        }
    ]
    record = build_session_record(
        session_id="跟老王聊周会",
        session_dir=" somewhere",
        mode="both",
        state="done",
        created_at="2025-09-06T10:00:00+08:00",
        started_at="2025-09-06T10:00:00+08:00",
        segments=segments,
        audio_duration_seconds=50.0,
        archives={
            "mic": ["meeting_audio_mic_001.pcm"],
            "loopback": ["meeting_audio_loopback_001.pcm"],
        },
    )
    assert record["segments"] == segments
    assert record["audio_duration_seconds"] == 50.0
    by_id = {item["id"]: item for item in record["sources"]}
    assert by_id["mic"]["archives"] == ["meeting_audio_mic_001.pcm"]
    assert by_id["loopback"]["archives"] == ["meeting_audio_loopback_001.pcm"]


def test_write_session_json_is_atomic_and_leaves_no_tmp(tmp_path):
    """原子写：**无 `.tmp` 残骸**，盘上那一版永远是完整可解析的 JSON。"""
    workspace = tmp_path / "out" / "ws"
    workspace.mkdir(parents=True)
    store = SessionStore()
    record = build_session_record(
        session_id="ws", session_dir=str(workspace), mode="both", state="recording",
        created_at="t0", started_at="t0",
    )
    target = store.write_session_json(str(workspace), record)

    assert Path(target).name == SESSION_FILE_NAME
    leftovers = [item.name for item in workspace.iterdir() if item.name.endswith(".tmp")]
    assert leftovers == []
    assert json.loads(Path(target).read_text(encoding="utf-8"))["state"] == "recording"

    # 写不进去的情形也要清掉临时文件：让目录里同名目录顶替目标文件
    blocked = workspace / "blocked"
    blocked.mkdir()
    (blocked / SESSION_FILE_NAME).mkdir()
    with pytest.raises(OSError):
        store.write_session_json(str(blocked), record)
    assert [item.name for item in blocked.iterdir() if item.name.endswith(".tmp")] == []


def test_state_for_maps_top_state_to_the_disk_state():
    """顶层四态 → 盘上 `state` 的映射只有这一处（phase2 §7.1 高危点 1）。"""
    assert state_for(top_state="recording") == "recording"
    assert state_for(top_state="alert") == "recording"
    assert state_for(top_state="ended", ended_phase="draining") == "draining"
    assert state_for(top_state="ended", ended_phase="done") == "done"
    assert state_for(top_state="ended", ended_phase="done", has_failed_step=True) == "failed"
    assert state_for(top_state="ended", ended_phase="draining", has_failed_step=True) == "failed"


def test_state_for_refuses_idle_and_unknown_top_states():
    """`idle`（以及任何认不出的取值）按**显式错误**处理：这一档手上没有场次可写盘。

    盘上的 `state` 描述的是真的开过的那一场会；把它兜底成 `recording` 会在盘上留下一版
    「写着在录、实际没录」的记录（R2 最不能犯的错），所以这里当场报错。
    """
    for top_state in ("idle", "unknown", ""):
        with pytest.raises(ValueError):
            state_for(top_state=top_state)


# ---------------------------------------------------------------------------
# 一场会：每个阶段变更都落盘一次
# ---------------------------------------------------------------------------


def build_client(tmp_path: Path, values: dict | None = None):
    import contextlib

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    out_root = tmp_path / "out"
    out_root.mkdir(parents=True, exist_ok=True)
    config = {"capture.backend": "dev", "web.dist_dir": str(helpers.missing_dist_dir(home))}
    config.update(values or {})
    stack = contextlib.ExitStack()
    client = stack.enter_context(helpers.app_client(home, values=config))
    return stack, client, client.app.state.runtime, out_root


def _post(client, action: str, args: dict | None = None, *, explicit_dir: Path | None = None):
    """打一个动作；`explicit_dir is None` 时**不自动补** `dir`（要验「不给 dir」那一档）。"""
    body = {"action": action}
    if args is not None:
        body["args"] = args
    return client.post("/api/actions", json=body, headers={"X-Meeting-Service": "1"})


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_every_phase_change_writes_the_session_file(tmp_path, monkeypatch):
    """`session.id` = 目录名；`start` / 收尾每一步 / 终态各落盘一次。"""
    stack, client, runtime, out_root = build_client(tmp_path)
    try:
        workspace = Path(helpers.allocate_start_dir(out_root))
        assert _post(client, "start", {"dir": str(workspace)}).status_code == 200
        session_dir = Path(runtime.snapshot.session.dir)
        assert session_dir == workspace

        first = read_json(session_dir / SESSION_FILE_NAME)
        assert first["state"] == "recording"
        assert first["session_id"] == session_dir.name
        assert first["dir"] == str(session_dir)
        assert [step["status"] for step in first["drain"]["steps"]] == ["waiting"] * 4
        assert first["interrupted"] is False
        assert first["created_at"] and first["started_at"]
        assert first["ended_at"] is None
        assert [item["archives"] for item in first["sources"]] == [
            ["meeting_audio_mic_001.pcm"], ["meeting_audio_loopback_001.pcm"],
        ]
        # 开场第一版：还没有段账（引擎没起），权威时长 0
        assert first["segments"] == []
        assert first["audio_duration_seconds"] == 0.0

        # 收尾过程中盘上写「收尾中」，终态写 `done`（四步的 status / detail 各写一次）。
        # 为了让 `draining` 那一版可确定性观察到，把第 4 步里那一次同步调用（让会中模型退出
        # 显存）换成慢替身 —— 会后那两趟活现在归用户手上的两个按钮，第 4 步只剩这一件事。
        class SlowRelease:
            def __init__(self) -> None:
                self.started = threading.Event()
                self.release = threading.Event()

            def release_gpu(self) -> bool:
                self.started.set()
                self.release.wait(timeout=SLOW_HOOK_SECONDS)
                return True

        hook = SlowRelease()
        runtime.transcriber_backend.release_gpu = hook.release_gpu

        # 收尾三步的调用都经 `_drain_wait()` 这一层包装（phase3 的自适应预算就把这里包住）。
        # 本用例把这一层换成「照原样调用、如实回报耗时」的替身：**同步实现放进工作线程**
        # （与真实现里的 `asyncio.to_thread` 同形），慢替身因此不会把事件循环钉住，
        # `/api/state` 在整个等待里照常应答。
        async def transparent_drain_wait(step_id, call, **kwargs):
            del step_id, kwargs
            began = time.monotonic()
            result = call()
            if asyncio.iscoroutine(result):
                await result
            else:
                await asyncio.to_thread(lambda: result)
            return None, time.monotonic() - began

        monkeypatch.setattr(runtime, "_drain_wait", transparent_drain_wait)

        assert _post(client, "stop").status_code == 200
        assert hook.started.wait(timeout=SLOW_HOOK_SECONDS), "收尾该走到 post 那一步"
        during = read_json(session_dir / SESSION_FILE_NAME)
        assert during["state"] == "draining"
        assert during["ended_at"] is None
        steps_during = {step["id"]: step["status"] for step in during["drain"]["steps"]}
        assert steps_during["flush"] == "skipped"
        assert steps_during["transcode"] == "skipped"
        assert steps_during["log"] == "done"
        assert steps_during["post"] == "running"
        hook.release.set()

        # 收尾前三步落定、第 4 步停在「等你动手」之后，盘上落 `done`
        # （每一版都是一份完整可解析的 JSON）
        done = _wait_for(
            lambda: (lambda r: r if r and r["state"] == "done" else None)(
                read_json(session_dir / SESSION_FILE_NAME)
            ),
            what="盘上落成 done",
        )
        steps = {step["id"]: step["status"] for step in done["drain"]["steps"]}
        assert steps == {
            "flush": "skipped", "transcode": "skipped", "log": "done", "post": "waiting",
        }
        assert done["ended_at"]
        assert done["interrupted"] is False
        # 工作区里没有 `.tmp` 残骸
        assert [item.name for item in session_dir.rglob("*.tmp")] == []
    finally:
        stack.close()


def _wait_for(predicate, *, what: str, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError(f"等待超时（{timeout}s）：{what}（最后观察值 {last!r}）")


SLOW_HOOK_SECONDS = 10.0


def test_start_writes_sources_archive_names_and_mode(tmp_path):
    """`mode` 与两路 `device` / `archive` 都进第一版 `session.json`。"""
    stack, client, runtime, out_root = build_client(tmp_path)
    try:
        workspace = Path(helpers.allocate_start_dir(out_root))
        assert _post(client, "start", {"dir": str(workspace), "mode": "mic"}).status_code == 200
        session_dir = Path(runtime.snapshot.session.dir)
        record = read_json(session_dir / SESSION_FILE_NAME)
        assert record["mode"] == "mic"
        by_id = {item["id"]: item for item in record["sources"]}
        assert by_id["mic"]["device"] == "dev-mic"
        assert by_id["loopback"]["device"] == "dev-loopback"
    finally:
        stack.close()


def test_start_failure_cleans_its_own_file_and_leaves_a_trace(tmp_path, monkeypatch):
    """`start` 写 `session.json` 失败 → 不进 `recording`、收走本场写入的文件、主 log 有留痕。"""
    stack, client, runtime, out_root = build_client(tmp_path)
    try:
        workspace = Path(helpers.allocate_start_dir(out_root))
        (workspace / "用户自己的文件.txt").write_text("别动我", encoding="utf-8")
        boom = RuntimeError("写盘按测试要求失败")
        original = runtime.store.write_session_json

        def failing(session_dir, record):
            raise boom

        monkeypatch.setattr(runtime.store, "write_session_json", failing)

        response = _post(client, "start", {"dir": str(workspace)})
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "capture_unavailable"
        assert runtime.snapshot.top_state == "idle"
        assert runtime.snapshot.session is None
        # 工作区目录与用户自己的文件**都在**（软件不删用户目录），同一目录可再开一场
        assert workspace.is_dir()
        assert (workspace / "用户自己的文件.txt").read_text(encoding="utf-8") == "别动我"
        assert not (workspace / ".session.json").exists()
        monkeypatch.setattr(runtime.store, "write_session_json", original)
        assert _post(client, "start", {"dir": str(workspace)}).status_code == 200
        assert Path(runtime.snapshot.session.dir) == workspace

        # 主 log 里能读到那条留痕
        log_text = helpers.read_text(helpers.service_log_path_of(tmp_path / "home"))
        assert "写不进去" in log_text
    finally:
        stack.close()
