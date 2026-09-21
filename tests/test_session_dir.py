"""`tests/test_session_dir.py` —— 场次目录、`session.json` 与原子写（phase2 §5.1 第四行）。

覆盖：

  - 输出根不存在时**只在 `start` 那一刻建**（服务启动与扫描都不建）；
  - `preview_next_dir()` **只算不建**（调用后目录不存在）且与随后 `start` 选中的名字一致；
  - `YYMMDD` 命名与 `-2` / `-3` 递增；`session.id` **就是目录名**；
  - `session.json` 字段全貌、**原子写**（输出根里**无 `.tmp` 残骸**、读到的永远是完整 JSON）、
    **每个阶段变更都落盘一次**（四步的 `status` / `detail` 各写一次）；
  - `sources[*].archive` = `meeting_audio_<source>.pcm`；
  - `start` 写文件失败 → **不进 `recording`**、刚建的场次目录被清掉、主 log 有那条清理留痕。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from tests import _helpers as helpers
from meeting.session import (
    SESSION_FILE_NAME,
    SessionStore,
    build_session_record,
    state_for,
)
from meeting.state import DRAIN_STEP_IDS

#: 慢钩子最长阻塞（秒）：足够用例观察到「收尾中」那一版盘上内容
SLOW_HOOK_SECONDS = 10.0


# ---------------------------------------------------------------------------
# 目录分配
# ---------------------------------------------------------------------------


def test_output_root_is_created_only_at_start(tmp_path):
    """输出根不存在时**只在 `start` 那一刻建**：服务启动不建、扫描不建。"""
    out_root = tmp_path / "not-created-yet"
    assert not out_root.exists()

    home = tmp_path / "home"
    home.mkdir()
    helpers.write_config_json(home, {"web.port": 0, "output.root": str(out_root)})
    proc = helpers.spawn_service(home)
    try:
        proc.wait_ready()
        assert not out_root.exists(), "服务启动（含扫未收尾场次）不该建输出根"
    finally:
        proc.terminate()


def test_preview_next_dir_calculates_without_creating(tmp_path):
    """`preview_next_dir()` **只算不建**，且与随后真正 `start` 时算出的名字一致。"""
    out_root = tmp_path / "out"
    store = SessionStore(str(out_root))
    preview = store.preview_next_dir()
    assert not Path(preview).exists()
    assert not out_root.exists(), "预览不该顺手把输出根建出来"

    session_id, session_dir = store.create_session_dir()
    assert Path(session_dir).exists()
    assert Path(preview).name == session_id
    # 已经用掉那个名字之后，下一次预览让位到 -2
    assert Path(store.preview_next_dir()).name == f"{session_id}-2"


def test_session_dir_names_increment_with_dash_suffix(tmp_path):
    """`YYMMDD` 与 `-2` / `-3` 递增；**已存在就让位、不复用**。"""
    out_root = tmp_path / "out"
    store = SessionStore(str(out_root))
    day = datetime(2026, 9, 6)

    first_id, first_dir = store.create_session_dir(today=day)
    second_id, second_dir = store.create_session_dir(today=day)
    third_id, third_dir = store.create_session_dir(today=day)

    assert first_id == "250906"
    assert second_id == "250906-2"
    assert third_id == "250906-3"
    for directory in (first_dir, second_dir, third_dir):
        assert Path(directory).is_dir()
    assert len({first_dir, second_dir, third_dir}) == 3


def test_preview_matches_the_name_start_will_take(tmp_path):
    """预览与实建同名：留一个 `250906` 在场，预览与实建都落在 -2 上。"""
    out_root = tmp_path / "out"
    store = SessionStore(str(out_root))
    day = datetime(2026, 9, 6)
    (out_root / "250906").mkdir(parents=True)

    assert Path(store.preview_next_dir(today=day)).name == "250906-2"
    session_id, _dir = store.create_session_dir(today=day)
    assert session_id == "250906-2"
    assert Path(store.preview_next_dir(today=day)).name == "250906-3"


# ---------------------------------------------------------------------------
# session.json 的 schema 与原子写
# ---------------------------------------------------------------------------


def test_session_json_has_the_frozen_field_shape(tmp_path):
    """字段全貌：一份都不省（四步各一条、每条四键齐；两路各一条、每条三键齐）。"""
    record = build_session_record(
        session_id="250906",
        session_dir=str(tmp_path / "250906"),
        mode="both",
        state="recording",
        created_at="2025-09-06T10:00:00+08:00",
        started_at="2025-09-06T10:00:00+08:00",
        devices={"mic": "麦克风", "loopback": None},
        pid=1234,
    )
    assert set(record) == {
        "session_id", "dir", "mode", "state", "created_at", "started_at", "ended_at",
        "sources", "drain", "interrupted", "service",
    }
    assert record["state"] in ("recording", "draining", "done", "failed")
    assert [step["id"] for step in record["drain"]["steps"]] == list(DRAIN_STEP_IDS)
    for step in record["drain"]["steps"]:
        assert set(step) == {"id", "status", "detail", "error"}
        assert step["status"] == "waiting"
    assert [item["id"] for item in record["sources"]] == ["mic", "loopback"]
    for item in record["sources"]:
        assert set(item) == {"id", "device", "archive"}
        # 归档名形状 = meeting_audio_<source>.pcm
        assert item["archive"] == f"meeting_audio_{item['id']}.pcm"
    assert set(record["service"]) == {"pid", "started_at"}
    assert record["service"]["pid"] == 1234
    assert record["interrupted"] is False
    assert record["ended_at"] is None


def test_write_session_json_is_atomic_and_leaves_no_tmp(tmp_path):
    """原子写：输出根里**无 `.tmp` 残骸**，盘上那一版永远是完整可解析的 JSON。"""
    out_root = tmp_path / "out"
    store = SessionStore(str(out_root))
    session_id, session_dir = store.create_session_dir()
    record = build_session_record(
        session_id=session_id, session_dir=session_dir, mode="both", state="recording",
        created_at="t0", started_at="t0",
    )
    target = store.write_session_json(session_dir, record)

    assert Path(target).name == SESSION_FILE_NAME
    leftovers = [item.name for item in Path(session_dir).iterdir() if item.name.endswith(".tmp")]
    assert leftovers == []
    assert json.loads(Path(target).read_text(encoding="utf-8"))["state"] == "recording"

    # 写不进去的情形也要清掉临时文件：让场次目录本身不可写（同名目录顶替目标文件）
    blocked = Path(session_dir) / "blocked"
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
    config = {"output.root": str(out_root), "capture.backend": "dev",
              "web.dist_dir": str(helpers.missing_dist_dir(home))}
    config.update(values or {})
    stack = contextlib.ExitStack()
    client = stack.enter_context(helpers.app_client(home, values=config))
    return stack, client, client.app.state.runtime, out_root


def post(client, action: str, args: dict | None = None):
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
        assert post(client, "start").status_code == 200
        session_dir = Path(runtime.snapshot.session.dir)
        assert session_dir.parent == out_root
        assert session_dir.name == runtime.snapshot.session.id

        first = read_json(session_dir / SESSION_FILE_NAME)
        assert first["state"] == "recording"
        assert first["session_id"] == session_dir.name
        assert first["dir"] == str(session_dir)
        assert [step["status"] for step in first["drain"]["steps"]] == ["waiting"] * 4
        assert first["interrupted"] is False
        assert first["created_at"] and first["started_at"]
        assert first["ended_at"] is None
        assert [item["archive"] for item in first["sources"]] == [
            "meeting_audio_mic.pcm", "meeting_audio_loopback.pcm",
        ]

        # 收尾过程中盘上写「收尾中」，终态写 `done`（四步的 status / detail 各写一次）。
        # 为了让 `draining` 那一版可确定性观察到，把末尾两步之一的 `post` 换成慢钩子。
        class SlowHook:
            def __init__(self) -> None:
                self.started = threading.Event()
                self.release = threading.Event()

            def launch(self, target: str) -> None:
                self.started.set()
                self.release.wait(timeout=SLOW_HOOK_SECONDS)

        hook = SlowHook()
        runtime.post_hook = hook
        runtime.post_resolution.implementation = hook
        runtime.post_resolution.has_implementation = True

        # 收尾三步的调用都经 `_drain_wait()` 这一层包装（phase3 的自适应预算就把这里包住）。
        # 本用例把这一层换成「照原样调用、如实回报耗时」的替身：**同步实现放进工作线程**
        # （与真实现里的 `asyncio.to_thread` 同形），慢钩子因此不会把事件循环钉住，
        # `/api/state` 在整个等待里照常应答。
        # 接缝要收得下 `_drain_wait()` 的签名（`deadline` / `min_budget` / `invoke` 都是关键字）。
        #
        # **这一处改动的授权来源（第七轮审查 m4）**：phase3 的 `_drain_wait` 取代
        # `_call_step` —— phase2 时本用例 monkeypatch 的是 `runtime._call_step`，那个接缝在
        # phase3 的等待上限里被删掉了，替换成这一个。本用例要验的仍是同一件事
        # （「每换一个阶段都写一次 session.json」+ 慢钩子期间盘上那一版的样子），
        # 断言一字未改。
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

        assert post(client, "stop").status_code == 200
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

        # 收尾四步各自落定之后盘上落 `done`（每一版都是一份完整可解析的 JSON）
        done = _wait_for(
            lambda: (lambda r: r if r and r["state"] == "done" else None)(
                read_json(session_dir / SESSION_FILE_NAME)
            ),
            what="盘上落成 done",
        )
        steps = {step["id"]: step["status"] for step in done["drain"]["steps"]}
        assert steps == {
            "flush": "skipped", "transcode": "skipped", "log": "done", "post": "done",
        }
        assert done["ended_at"]
        assert done["interrupted"] is False
        # 输出根里没有 `.tmp` 残骸
        assert [item.name for item in out_root.rglob("*.tmp")] == []
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


def test_start_writes_sources_archive_names_and_mode(tmp_path):
    """`mode` 与两路 `device` / `archive` 都进第一版 `session.json`。"""
    stack, client, runtime, _out = build_client(tmp_path)
    try:
        assert post(client, "start", {"mode": "mic"}).status_code == 200
        session_dir = Path(runtime.snapshot.session.dir)
        record = read_json(session_dir / SESSION_FILE_NAME)
        assert record["mode"] == "mic"
        by_id = {item["id"]: item for item in record["sources"]}
        assert by_id["mic"]["device"] == "dev-mic"
        assert by_id["loopback"]["device"] == "dev-loopback"
    finally:
        stack.close()


def test_start_failure_cleans_the_session_dir_and_leaves_a_trace(tmp_path, monkeypatch):
    """`start` 写 `session.json` 失败 → 不进 `recording`、刚建的目录被清掉、主 log 有留痕。"""
    stack, client, runtime, out_root = build_client(tmp_path)
    try:
        boom = RuntimeError("写盘按测试要求失败")
        original = runtime.store.write_session_json

        def failing(session_dir, record):
            raise boom

        monkeypatch.setattr(runtime.store, "write_session_json", failing)

        response = post(client, "start")
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "capture_unavailable"
        assert runtime.snapshot.top_state == "idle"
        assert runtime.snapshot.session is None
        # 刚建的场次目录被清掉、盘上不留任何场次目录
        assert [item for item in out_root.iterdir() if item.is_dir()] == []
        # 清掉之后 `preview_next_dir()` 算出的下一个名字与随后真正 start 时一致
        monkeypatch.setattr(runtime.store, "write_session_json", original)
        today_name = datetime.now().strftime("%y%m%d")
        preview = Path(runtime.store.preview_next_dir())
        assert not preview.exists(), "预览只算不建"
        assert preview.name == today_name, "清掉之后下一个名字回到当天那一档"
        assert post(client, "start").status_code == 200
        session_dir = Path(runtime.snapshot.session.dir)
        assert session_dir.name == preview.name, "预览的名字就是随后真正 start 用掉的那个"
        assert session_dir.name == today_name

        # 主 log 里能读到那条清理留痕
        log_text = helpers.read_text(helpers.service_log_path_of(tmp_path / "home"))
        assert "场次目录已清理" in log_text
        assert "写不进去" in log_text
    finally:
        stack.close()
