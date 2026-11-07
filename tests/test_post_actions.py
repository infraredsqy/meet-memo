"""会后两个按钮（`post_offline` / `post_minutes`）与「有没有会后精细稿」那一格的用例。

本节点（`05a48b6a`）的完成判定落在这几条上：

  - 只按「生成会后精细稿」：只有离线段跑起来、纪要一动不动（`minutes=False` 那一趟）；
  - 只按「直接出纪要」：把这一场交给纪要那一层（离线段一个引擎都不调）；
  - 两个按钮都不按：结束录音之后**没有任何会后动作被启动**（第 4 步停在 `waiting`）；
  - 「有没有会后精细稿」那一格走 `GET /api/sessions` 的 `has_offline`（R29）。

**全部用假件**：离线段那一趟换成只登记的替身（`tests/_drain_fakes.py` 的手法），
纪要那一层换成登记替身 —— 不起真模型、不占显卡、不出声、不拉起真 DSH。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from meeting import config as config_module
from meeting.service.app import build_runtime
from meeting.state import begin_drain

#: 等后台那一趟收工的轮询上限（事件驱动地等，等到了就立刻回）
JOB_TIMEOUT_SECONDS = 5.0


def _runtime(tmp_path: Path, monkeypatch, **values):
    """起一台真运行时（dev 采集替身），配置里覆盖 `**values`。"""
    home = tmp_path / "home-post-actions"
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
    return runtime, previous


def _ended_session(tmp_path: Path, monkeypatch, **values):
    """开一场、把收尾**整趟跑完**（第 4 步落在 `waiting`）：返回 `(runtime, session_dir, previous)`。

    走真的 `_run_drain_steps()`（不是只调 `_drain_post()`）：这一步之后的现场就是用户
    按两个按钮时的那一副 —— 音频与日志落好了，会后那一格空着等人。
    """
    import asyncio

    runtime, previous = _runtime(tmp_path, monkeypatch, **values)
    workspace = tmp_path / "ws-post-actions"
    workspace.mkdir()
    runtime.start_session(dir=str(workspace))
    session_dir = Path(runtime.snapshot.session.dir)
    begin_drain(runtime.snapshot, reason="user")
    asyncio.run(runtime._run_drain_steps())
    assert runtime.snapshot.ended.phase == "done", runtime.snapshot.ended.phase
    return runtime, session_dir, previous


def _wait_job_done(runtime) -> None:
    """等后台那一趟收工。

    等的是运行时那一格现场读数（线程活没活着），它没有可挂的事件 —— 这一次短间隔轮询
    是这一格唯一的观测量，因此它带显式上限，等到就立刻回，不赌固定时长。
    """
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not runtime.post_jobs_state():
            return
        time.sleep(0.005)
    raise AssertionError("等不到会后的活儿收工")


@pytest.fixture()
def restore_config():
    """用例结束后把配置单例放回去（与别的用例同一口径）。"""
    previous = config_module.current()
    yield
    config_module.set_current(previous)


# ---------------------------------------------------------------------------
# 一、结束录音之后什么都不发生
# ---------------------------------------------------------------------------


def test_the_drain_post_step_settles_waiting_and_no_job_is_started(tmp_path, monkeypatch) -> None:
    """收尾第 4 步落 `waiting`，运行时的会后现场是空的（没有任何一趟被起）。"""
    import asyncio

    runtime, _session_dir, previous = _ended_session(tmp_path, monkeypatch)
    try:
        step = next(item for item in runtime.snapshot.ended.steps if item.id == "post")
        assert step.status == "waiting", (step.status, step.detail, step.error)
        assert not runtime.post_jobs_state()
        # 再跑一次那一步也还是同一个读数（「不按就什么都不跑」不是一次性的巧合）
        asyncio.run(runtime._drain_post(deadline=time.monotonic() + 10.0))
        assert not runtime.post_jobs_state()
    finally:
        config_module.set_current(previous)


# ---------------------------------------------------------------------------
# 二、「生成会后精细稿」：只有离线段跑起来
# ---------------------------------------------------------------------------


def test_post_offline_runs_the_offline_lane_only(tmp_path, monkeypatch) -> None:
    """这一趟只跑离线那一条链（`minutes=False`），纪要那一层一次都不被通知。"""
    runtime, session_dir, previous = _ended_session(tmp_path, monkeypatch)
    calls: list[dict] = []
    announced: list[str] = []

    from meeting.offline import runner as offline_runner

    def fake_run_session(target, **kwargs):
        calls.append({"target": str(target), **kwargs})
        return {"state": "done"}

    monkeypatch.setattr(offline_runner, "run_session", fake_run_session)
    monkeypatch.setattr(
        runtime, "_run_post_minutes", lambda target, **kwargs: announced.append(str(target))
    )
    try:
        started = runtime.post_offline({})
        _wait_job_done(runtime)
    finally:
        config_module.set_current(previous)

    assert started == {"kind": "offline", "dir": str(session_dir)}
    assert len(calls) == 1, calls
    assert calls[0]["target"] == str(session_dir)
    assert calls[0]["minutes"] is False, "这一趟不许通知纪要那一层（那是另一个按钮的事）"
    assert announced == [], "「生成会后精细稿」一个纪要都不该起"


def test_post_minutes_is_accepted_while_the_offline_job_is_running(
    tmp_path, monkeypatch
) -> None:
    """纪要免闸：同一场的会后精细稿还在跑，「直接出纪要」照常受理 —— 但回执**说破**输入档位。"""
    import threading

    runtime, session_dir, previous = _ended_session(tmp_path, monkeypatch)
    gate = threading.Event()
    started = threading.Event()

    from meeting.offline import runner as offline_runner

    def blocking_run_session(target, **kwargs):
        started.set()
        gate.wait(timeout=JOB_TIMEOUT_SECONDS)
        return {"state": "done"}

    monkeypatch.setattr(offline_runner, "run_session", blocking_run_session)

    from meeting.backends import registry as registry_module

    class FakeMinutesImplementation:
        def launch(self, target: str, **kwargs) -> None:
            pass

    class FakeResolution:
        selector = "fake"
        has_implementation = True
        implementation = FakeMinutesImplementation()

    monkeypatch.setattr(registry_module, "resolve_minutes_hook", lambda *a, **k: FakeResolution())
    try:
        runtime.post_offline({})
        assert started.wait(timeout=JOB_TIMEOUT_SECONDS)
        receipt = runtime.post_minutes({})
        assert receipt["kind"] == "minutes" and receipt["dir"] == str(session_dir)
        # 会后精细稿还在跑：这一趟注定只拿得到会中稿 —— 回执必须说破，不许静默降级
        assert receipt["inputs"] == "none", receipt
        assert "会中稿" in receipt["note"], receipt
    finally:
        gate.set()
        _wait_job_done(runtime)
        config_module.set_current(previous)


def test_post_offline_twice_on_the_same_session_is_refused(tmp_path, monkeypatch) -> None:
    """同一场已经有一趟「生成会后精细稿」在跑 → 第二趟 409 `draining`（(种类,场次) 槽闸）。"""
    import threading

    from meeting.service.runtime import ActionError

    runtime, _session_dir, previous = _ended_session(tmp_path, monkeypatch)
    gate = threading.Event()
    started = threading.Event()

    from meeting.offline import runner as offline_runner

    def blocking_run_session(target, **kwargs):
        started.set()
        gate.wait(timeout=JOB_TIMEOUT_SECONDS)
        return {"state": "done"}

    monkeypatch.setattr(offline_runner, "run_session", blocking_run_session)
    try:
        runtime.post_offline({})
        assert started.wait(timeout=JOB_TIMEOUT_SECONDS)
        with pytest.raises(ActionError) as info:
            runtime.post_offline({})
        assert info.value.code == "draining"
        assert info.value.status == 409
        assert info.value.detail["kind"] == "offline"
    finally:
        gate.set()
        _wait_job_done(runtime)
        config_module.set_current(previous)


def test_cross_session_minutes_run_in_parallel(tmp_path, monkeypatch) -> None:
    """跨场次的纪要能并行：第二场的 minutes 不被第一场的 offline / minutes 拦下。"""
    import threading

    runtime, session_dir, previous = _ended_session(tmp_path, monkeypatch)
    gate = threading.Event()
    started = threading.Event()

    from meeting.offline import runner as offline_runner

    def blocking_run_session(target, **kwargs):
        started.set()
        gate.wait(timeout=JOB_TIMEOUT_SECONDS)
        return {"state": "done"}

    monkeypatch.setattr(offline_runner, "run_session", blocking_run_session)

    other = tmp_path / "ws-post-actions-2"
    other.mkdir()
    runtime.workspace_open(str(other))

    from meeting.backends import registry as registry_module

    class FakeMinutesImplementation:
        def launch(self, target: str, **kwargs) -> None:
            pass

    class FakeResolution:
        selector = "fake"
        has_implementation = True
        implementation = FakeMinutesImplementation()

    monkeypatch.setattr(registry_module, "resolve_minutes_hook", lambda *a, **k: FakeResolution())
    try:
        runtime.post_offline({})
        assert started.wait(timeout=JOB_TIMEOUT_SECONDS)
        receipt = runtime.post_minutes({"dir": str(other)})
        assert receipt["dir"] == str(other)
        flying = {job["kind"] for job in runtime.post_jobs_state()}
        assert flying == {"offline", "minutes"}, flying
    finally:
        gate.set()
        _wait_job_done(runtime)
        config_module.set_current(previous)


def test_post_offline_is_refused_without_the_hook(tmp_path, monkeypatch) -> None:
    """`post.hook = null`（这台机器上没接这一层）→ 404 `action_unavailable`，不起线程。"""
    from meeting.service.runtime import ActionError

    runtime, _session_dir, previous = _ended_session(tmp_path, monkeypatch, **{"post.hook": "null"})
    try:
        assert runtime.post_resolution.skipped() is True
        with pytest.raises(ActionError) as info:
            runtime.post_offline({})
        assert info.value.code == "action_unavailable"
        assert info.value.status == 404
        assert not runtime.post_jobs_state()
    finally:
        config_module.set_current(previous)


# ---------------------------------------------------------------------------
# 三、「直接出纪要」：把手上有的稿子交给 DSH
# ---------------------------------------------------------------------------


def test_post_minutes_hands_the_session_over_without_running_the_offline_lane(
    tmp_path, monkeypatch
) -> None:
    """这一趟拉起纪要那一层（`launch(场次目录)`），离线段一个引擎都不调。"""
    runtime, session_dir, previous = _ended_session(tmp_path, monkeypatch)
    launched: list[str] = []
    offline_calls: list[str] = []

    from meeting.backends import registry as registry_module

    class FakeMinutesImplementation:
        def launch(self, target: str, **kwargs) -> None:
            launched.append(str(target))

    class FakeResolution:
        selector = "fake"
        has_implementation = True
        implementation = FakeMinutesImplementation()

    monkeypatch.setattr(registry_module, "resolve_minutes_hook", lambda *a, **k: FakeResolution())

    from meeting.offline import runner as offline_runner

    monkeypatch.setattr(
        offline_runner, "run_session", lambda target, **kwargs: offline_calls.append(str(target))
    )
    try:
        started = runtime.post_minutes({})
        _wait_job_done(runtime)
    finally:
        config_module.set_current(previous)

    assert started["kind"] == "minutes"
    assert started["dir"] == str(session_dir)
    assert started["inputs"] == "none", started  # 现场没有稿子：如实说破输入档位
    assert launched == [str(session_dir)], launched
    assert offline_calls == [], "「直接出纪要」不补跑离线那一层（R9：那一条是可选的）"


def test_post_minutes_launches_with_the_human_gate_open(tmp_path, monkeypatch) -> None:
    """**接线钉死（按钮路径）**：`post_minutes` 动作走到 `launch(..., deliver=True)`。

    隔离闸的放行标记只认两条人工入口，按钮是其中一条：替身实现把 `launch()` 收到的
    关键字原样记下来，动作本体那一趟必须带着 `deliver=True`（`claimed` 那一格的形状
    照旧）—— 少了它，纪要那一层就会当场安静跳过，按钮按下去什么都不会发生。
    """
    runtime, session_dir, previous = _ended_session(tmp_path, monkeypatch)
    launches: list[dict] = []

    from meeting.backends import registry as registry_module

    class RecordingMinutesImplementation:
        def claim(self, target: str) -> None:  # m2：动作本体先同步认下在飞的那一笔
            launches.append({"claim": str(target)})

        def launch(self, target: str, **kwargs) -> None:
            launches.append({"target": str(target), **kwargs})

    class FakeResolution:
        selector = "fake"
        has_implementation = True
        implementation = RecordingMinutesImplementation()

    monkeypatch.setattr(registry_module, "resolve_minutes_hook", lambda *a, **k: FakeResolution())
    try:
        started = runtime.post_minutes({})
        _wait_job_done(runtime)
    finally:
        config_module.set_current(previous)

    assert started["kind"] == "minutes" and started["dir"] == str(session_dir)
    launch_calls = [item for item in launches if "target" in item]
    assert launch_calls == [
        {"target": str(session_dir), "claimed": True, "deliver": True}
    ], launches


def test_post_minutes_is_refused_while_the_session_is_still_recording(tmp_path, monkeypatch) -> None:
    """还在录（收尾都没开始）时按「出纪要」→ 409 `draining`。"""
    from meeting.service.runtime import ActionError

    runtime, previous = _runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "ws-still-recording"
    workspace.mkdir()
    try:
        runtime.start_session(dir=str(workspace))
        with pytest.raises(ActionError) as info:
            runtime.post_minutes({})
        assert info.value.code == "draining"
        assert not runtime.post_jobs_state()
    finally:
        config_module.set_current(previous)


# ---------------------------------------------------------------------------
# 四、R29：`GET /api/sessions` 那一格
# ---------------------------------------------------------------------------


def test_sessions_dict_carries_has_offline(tmp_path, monkeypatch) -> None:
    """`has_offline` 按盘上有没有 `meeting_offline_transcript_<source>.md` 现场判。"""
    runtime, previous = _runtime(tmp_path, monkeypatch)
    try:
        plain = tmp_path / "plain-meeting"
        plain.mkdir()
        (plain / "session.json").write_text(
            '{"session_id": "plain", "state": "done", "date": "2026-09-21", "mode": "both"}',
            encoding="utf-8",
        )
        with_offline = tmp_path / "offline-meeting"
        with_offline.mkdir()
        (with_offline / "session.json").write_text(
            '{"session_id": "offline", "state": "done", "date": "2026-09-21", "mode": "both"}',
            encoding="utf-8",
        )
        (with_offline / "meeting_offline_transcript_mic.md").write_text(
            "[21:32] **S01**: 一句\n", encoding="utf-8"
        )
        runtime.workspace_open(str(plain))
        runtime.workspace_open(str(with_offline))

        rows = {row["session_id"]: row for row in runtime.sessions_dict()["sessions"]}

        assert rows["plain"]["has_offline"] is False
        assert rows["offline"]["has_offline"] is True
    finally:
        config_module.set_current(previous)


def test_has_offline_transcript_reads_the_directory(tmp_path) -> None:
    """判据只看名目：会后稿在就算有（哪怕还没切块），别的文件不算。"""
    from meeting.workspace import has_offline_transcript

    session = tmp_path / "260921"
    session.mkdir()
    assert has_offline_transcript(session) is False

    (session / "meeting_transcript_mic.md").write_text("会中稿\n", encoding="utf-8")
    assert has_offline_transcript(session) is False, "会中稿不算会后精细稿"

    (session / "meeting_offline_transcript_loopback.md").write_text("会后稿\n", encoding="utf-8")
    assert has_offline_transcript(session) is True
    # 目录不在时不抛错（这一格只服务界面上的一句话）
    assert has_offline_transcript(tmp_path / "not-here") is False


# ---------------------------------------------------------------------------
# 五、C3：`args["dir"]` 指定跑哪一场（界面选中那一行与动作跑的那一场同源）
# ---------------------------------------------------------------------------


def _register_other_session(runtime, tmp_path: Path, monkeypatch, name: str):
    """另建并登记一场（盘上 `session.json` 写 `state` 可给）：返回它的目录。

    `monkeypatch` 只用来保证用例结束时配置单例复原（与别处同一口径）。
    """
    other = tmp_path / name
    other.mkdir(parents=True, exist_ok=True)
    (other / "session.json").write_text(
        '{"session_id": "%s", "state": "done", "date": "2026-09-21", "mode": "both"}' % name,
        encoding="utf-8",
    )
    runtime.workspace_open(str(other))
    return other


def test_post_offline_runs_the_session_named_by_args_dir(tmp_path, monkeypatch) -> None:
    """`args["dir"]` 指到清单里另一场时，引擎跑**那一场**（C3）。

    这一条同时是「改完行为确实变了」的自证：`_ended_session()` 的现场里内存那一场与
    `args.dir` 指的那一场**是两个不同的目录**，旧行为（恒取 `snapshot.session.dir`）
    跑的一定是前者。
    """
    runtime, session_dir, previous = _ended_session(tmp_path, monkeypatch)
    calls: list[str] = []

    from meeting.offline import runner as offline_runner

    monkeypatch.setattr(
        offline_runner, "run_session", lambda target, **kwargs: calls.append(str(target))
    )
    try:
        other = _register_other_session(runtime, tmp_path, monkeypatch, "another-meeting")
        assert other != session_dir
        started = runtime.post_offline({"dir": str(other)})
        _wait_job_done(runtime)
    finally:
        config_module.set_current(previous)

    assert started == {"kind": "offline", "dir": str(other)}, started
    assert calls == [str(other)], calls
    assert str(session_dir) not in calls, "不许跑到内存里那一场上去"


def test_post_offline_without_args_dir_keeps_running_the_just_ended_session(
    tmp_path, monkeypatch
) -> None:
    """没给 `dir` 时退回原来那条路（命令行与既有用例走的就是这一路）。"""
    runtime, session_dir, previous = _ended_session(tmp_path, monkeypatch)
    calls: list[str] = []

    from meeting.offline import runner as offline_runner

    monkeypatch.setattr(
        offline_runner, "run_session", lambda target, **kwargs: calls.append(str(target))
    )
    try:
        _register_other_session(runtime, tmp_path, monkeypatch, "not-the-target")
        started = runtime.post_offline({})
        _wait_job_done(runtime)
    finally:
        config_module.set_current(previous)

    assert started == {"kind": "offline", "dir": str(session_dir)}
    assert calls == [str(session_dir)], calls


@pytest.mark.parametrize(
    "build, expected_kind",
    [
        ("unregistered", "missing-from-registry"),
        ("not-on-disk", "missing"),
    ],
)
def test_post_offline_refuses_an_args_dir_that_is_not_a_runnable_session(
    tmp_path, monkeypatch, build, expected_kind
) -> None:
    """`args["dir"]` 不合法时按 400 `invalid_setting` 如实回错档，不起任何一趟（C3）。

    两档判据与 `GET /api/sessions` 同一套注册表 / 目录判读：没登记过、目录不在原地。
    「盘上 `state=recording` 就拒」那一档已按三档口径收走：盘上停在 `recording` 的强杀
    残档放行（见 `test_post_offline_accepts_a_session_killed_mid_recording`）；本进程
    此刻在录的那一场归运行时闸的 409（见
    `test_post_offline_is_refused_while_the_session_is_still_recording`）。
    """
    from meeting.service.runtime import ActionError

    runtime, _session_dir, previous = _ended_session(tmp_path, monkeypatch)
    calls: list[str] = []

    from meeting.offline import runner as offline_runner

    monkeypatch.setattr(
        offline_runner, "run_session", lambda target, **kwargs: calls.append(str(target))
    )
    if build == "unregistered":
        target = tmp_path / "never-opened"
        target.mkdir()
        (target / "session.json").write_text(
            '{"session_id": "never-opened", "state": "done", "mode": "both"}', encoding="utf-8"
        )
    else:
        target = _register_other_session(runtime, tmp_path, monkeypatch, build)
        import shutil

        shutil.rmtree(target)
    try:
        with pytest.raises(ActionError) as info:
            runtime.post_offline({"dir": str(target)})
        assert info.value.code == "invalid_setting", (expected_kind, info.value.code)
        assert info.value.status == 400, (expected_kind, info.value.status)
        assert info.value.detail.get("key") == "dir", info.value.detail
        assert not runtime.post_jobs_state()
    finally:
        config_module.set_current(previous)
    assert calls == [], f"{expected_kind}：这一档一个引擎都不该被拉起来"


def test_post_offline_is_refused_while_the_session_is_still_recording(
    tmp_path, monkeypatch
) -> None:
    """还在录（收尾都没开始）时按「生成会后精细稿」→ 409 `draining`。

    「录音中」的拒只剩运行时闸这一道（`_reject_recording_post`）：与「直接出纪要」那一条
    （`test_post_minutes_is_refused_while_the_session_is_still_recording`）同一道闸、
    同一个档；盘上残档放行见 `test_post_offline_accepts_a_session_killed_mid_recording`。
    """
    from meeting.service.runtime import ActionError

    runtime, previous = _runtime(tmp_path, monkeypatch)
    workspace = tmp_path / "ws-still-recording-offline"
    workspace.mkdir()
    try:
        runtime.start_session(dir=str(workspace))
        with pytest.raises(ActionError) as info:
            runtime.post_offline({"dir": str(workspace)})
        assert info.value.code == "draining"
        assert info.value.status == 409
        assert not runtime.post_jobs_state()
    finally:
        config_module.set_current(previous)


def test_post_offline_accepts_a_session_killed_mid_recording(tmp_path, monkeypatch) -> None:
    """`args["dir"]` 指到盘上停在 `recording`（被强杀在录音半路）的残档 → 照常受理。

    「盘上 `state=recording` 就拒」这条旧判据作废：它只会拦住强杀在录音半路留下的残档
    （本进程活跃在录的那一场由运行时闸先按 409 拦下，轮不到这一档）—— 永久卡死类就此
    收口；残档的清单读数是 `ended`（三档口径，`tests/test_workspace_registry.py` 同场判据）。
    """
    runtime, _session_dir, previous = _ended_session(tmp_path, monkeypatch)
    calls: list[str] = []

    from meeting.offline import runner as offline_runner

    monkeypatch.setattr(
        offline_runner, "run_session", lambda target, **kwargs: calls.append(str(target))
    )
    try:
        target = _register_other_session(runtime, tmp_path, monkeypatch, "killed-mid-recording")
        (target / "session.json").write_text(
            json.dumps(
                {"session_id": "killed-mid-recording", "state": "recording",
                 "interrupted": True, "mode": "both"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        started = runtime.post_offline({"dir": str(target)})
        _wait_job_done(runtime)
    finally:
        config_module.set_current(previous)

    assert started == {"kind": "offline", "dir": str(target)}, started
    assert calls == [str(target)], calls


def test_post_offline_accepts_a_session_killed_mid_drain(tmp_path, monkeypatch) -> None:
    """`args["dir"]` 指到盘上停在 `draining`（被强杀在收尾半路）的那一场 → 照常受理。

    「落盘没落定就拒」这条判据作废：会后能不能按只由「已经有一趟在飞」与 minutes 层的
    claim 闸决定；盘上 `recording` 的残档同档放行（下一条）。
    """
    runtime, _session_dir, previous = _ended_session(tmp_path, monkeypatch)
    calls: list[str] = []

    from meeting.offline import runner as offline_runner

    monkeypatch.setattr(
        offline_runner, "run_session", lambda target, **kwargs: calls.append(str(target))
    )
    try:
        target = _register_other_session(runtime, tmp_path, monkeypatch, "killed-mid-drain")
        (target / "session.json").write_text(
            json.dumps(
                {"session_id": "killed-mid-drain", "state": "draining",
                 "interrupted": True, "mode": "both"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        started = runtime.post_offline({"dir": str(target)})
        _wait_job_done(runtime)
    finally:
        config_module.set_current(previous)

    assert started == {"kind": "offline", "dir": str(target)}, started
    assert calls == [str(target)], calls


# ---------------------------------------------------------------------------
# 六、m2：`minutes.hook = null` 那一档（404）与「纪要这一层已经在飞」那一档（409）
# ---------------------------------------------------------------------------


def test_post_minutes_is_refused_without_the_minutes_hook(tmp_path, monkeypatch) -> None:
    """`minutes.hook = null` → 404 `action_unavailable`，不起线程（对称于 `post.hook` 那一侧）。

    坑（复审 m3 点名的那一个）：`_reject_recording_post()` 的两道闸排在钩子闸**前面**，
    一个 runtime 里连按两个按钮会把 404 盖成 409 —— 本条只按纪要那一个按钮。
    """
    from meeting.service.runtime import ActionError

    runtime, _session_dir, previous = _ended_session(
        tmp_path, monkeypatch, **{"minutes.hook": "null"}
    )
    try:
        with pytest.raises(ActionError) as info:
            runtime.post_minutes({})
        assert info.value.code == "action_unavailable"
        assert info.value.status == 404
        assert info.value.detail.get("key") == "minutes.hook", info.value.detail
        assert not runtime.post_jobs_state()
    finally:
        config_module.set_current(previous)


def test_post_minutes_is_refused_when_the_minutes_layer_is_still_in_flight(
    tmp_path, monkeypatch
) -> None:
    """第一趟还在飞时再按「直接出纪要」→ 409 `draining`，**不许回 `{kind:'minutes'}` 的假成功**（m2）。

    现场（一个真 DSH 子会话都没有，不占显卡、不出声）：先由 `claim.mark_running()` 在盘上
    落一版停在 `running` 的 `meeting_minutes_status.json`（第一趟还在飞的现场读数，
    它落的正是那一版），`meeting.minutes.runner.run` 换成登记 no-op。判据落在**动作本体**上：
    第二个按钮在起线程之前就按 409 拒，`post_jobs_state()` 因此读不到第二趟。
    """
    from meeting.minutes import claim as claim_module
    from meeting.minutes import status as minutes_status
    from meeting.service.runtime import ActionError

    runtime, session_dir, previous = _ended_session(tmp_path, monkeypatch)
    armed: list[str] = []

    from meeting.minutes import runner as minutes_runner

    monkeypatch.setattr(
        minutes_runner, "run", lambda target, **kwargs: armed.append(str(target))
    )
    try:
        # 第一趟的现场：盘上那一版停在 running（改之前，动作本体对这一版一个字都不读）
        claim_module.mark_running(str(session_dir))
        assert claim_module._peek_state(session_dir) == minutes_status.STATE_RUNNING

        # 头一道闸此刻**没有**东西可拒（没有线程在飞、收尾已经落定）—— 这一趟要是被拒，
        # 只可能落在 claim 闸上；`status_path` 也只在 claim 闸那支的 detail 里（review-2 m7）
        assert not runtime.post_jobs_state(), "这一刻没有会后的活儿在飞"
        assert runtime.drain_has_settled() is True

        with pytest.raises(ActionError) as info:
            runtime.post_minutes({})
        assert info.value.code == "draining", info.value.detail
        assert info.value.status == 409
        assert info.value.detail.get("status_path", "").endswith(
            "meeting_minutes_status.json"
        ), info.value.detail
        assert not runtime.post_jobs_state(), "这一趟压根没有起线程"
        assert armed == [], "第二趟不许再进纪要那一层"
    finally:
        config_module.set_current(previous)


# ---------------------------------------------------------------------------
# 六之二、m4：线没起来时，刚认下的那一版 claim 要如实退回（盘上不留永远在飞的记录）
# ---------------------------------------------------------------------------


def test_post_minutes_returns_the_claim_when_the_thread_cannot_start(
    tmp_path, monkeypatch
) -> None:
    """起线程那一步抛出来时：那一版 `running` 退回成 `failed` + 原因，且这一场还有出路（m4）。

    改之前的现场：`claim()` 已经把盘上写成 `running`，`_start_post_job()` 一抛异常，
    动作把异常原样上抛，而**盘上那一版停在 `running`** —— 之后每一次按「直接出纪要」都被
    claim 闸按 409 拒（`--force` 只在命令行那一趟上），界面上没有出路。
    这里先造那一次失败（真 `MinutesHook` + 真 claim 闸，`meeting.minutes.runner.run` 换成
    登记 no-op、不起真 DSH、不占显卡），再按第二趟证明出路回来了。
    """
    import threading

    from meeting.minutes import runner as minutes_runner
    from meeting.minutes import status as minutes_status

    runtime, session_dir, previous = _ended_session(tmp_path, monkeypatch)
    armed: list[str] = []
    handed = threading.Event()
    boom = RuntimeError("can't start new thread")
    attempts: list[str] = []
    real_start = runtime._start_post_job

    def flaky_start(kind: str, target: str, **kwargs) -> None:
        attempts.append(kind)
        if len(attempts) == 1:
            raise boom
        real_start(kind, target, **kwargs)

    monkeypatch.setattr(runtime, "_start_post_job", flaky_start)
    monkeypatch.setattr(
        minutes_runner,
        "run",
        lambda target, **kwargs: (armed.append(str(target)), handed.set()),
    )
    try:
        with pytest.raises(RuntimeError) as info:
            runtime.post_minutes({})
        assert info.value is boom, info.value
        # 盘上那一版**不再**停在 running：退回成 failed + 原因
        record = minutes_status.read_status(minutes_status.status_path(session_dir))
        assert record["state"] == "failed", record
        assert "can't start new thread" in str(record.get("error")), record
        assert not runtime.post_jobs_state()

        # 出路：同一现场再按一次，claim 闸不再拒（改之前这里是 409 draining）
        started = runtime.post_minutes({})
        assert started["kind"] == "minutes" and started["dir"] == str(session_dir), started
        assert handed.wait(timeout=JOB_TIMEOUT_SECONDS), "这一趟该真的把这一场交给纪要那一层"
        assert armed == [str(session_dir)], armed
        _wait_job_done(runtime)
    finally:
        config_module.set_current(previous)


def test_post_minutes_returns_the_claim_without_a_mark_failed_writer(
    tmp_path, monkeypatch
) -> None:
    """`claim()` 在、`_mark_failed()` 不在的实现：退回那一版 claim 走同一份状态文件的既有写法（m4）。"""
    from meeting.backends import registry as registry_module
    from meeting.minutes import status as minutes_status

    runtime, session_dir, previous = _ended_session(tmp_path, monkeypatch)
    claimed_dirs: list[str] = []

    class ClaimOnlyImplementation:
        """只实现 `claim()` 的替身（`claim()` 是可选能力，实现可以不带上 `_mark_failed()`）。"""

        def claim(self, target: str) -> None:
            claimed_dirs.append(str(target))

        def launch(self, target: str, **kwargs) -> None:  # pragma: no cover - 这一趟不该走到它
            raise AssertionError("线都没起来，不该拉起纪要那一层")

    class FakeResolution:
        selector = "claim-only"
        has_implementation = True
        implementation = ClaimOnlyImplementation()

    monkeypatch.setattr(registry_module, "resolve_minutes_hook", lambda *a, **k: FakeResolution())
    monkeypatch.setattr(
        runtime,
        "_start_post_job",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("can't start new thread")),
    )
    try:
        with pytest.raises(RuntimeError):
            runtime.post_minutes({})
        assert claimed_dirs == [str(session_dir)], claimed_dirs
        record = minutes_status.read_status(minutes_status.status_path(session_dir))
        assert record["state"] == "failed", record
        assert "can't start new thread" in str(record.get("error")), record
    finally:
        config_module.set_current(previous)


# ---------------------------------------------------------------------------
# 七、「落盘没落定就拒会后动作」判据作废：收尾没走完照样受理
# ---------------------------------------------------------------------------


def test_post_minutes_is_accepted_while_the_drain_has_not_settled(
    tmp_path, monkeypatch
) -> None:
    """收尾没落定不再拒会后动作：第 4 步还停在 `running` 时按「出纪要」照常受理。

    旧判据（`drain_has_settled()` 不过 → 409 `draining`）作废：会后能不能按只由两道闸
    决定（「已经有一趟在飞」与 minutes 层的 claim 闸），收尾走到哪一步不拦按钮。
    第 4 步落 `waiting` 之后同一调用照常受理 ——「受理」不因收尾进度分档。
    """
    import asyncio

    from meeting.state import begin_drain, finish_drain_step

    runtime, previous = _runtime(tmp_path, monkeypatch)
    launched: list[str] = []

    from meeting.backends import registry as registry_module

    class FakeMinutesImplementation:
        def launch(self, target: str, **kwargs) -> None:
            launched.append(str(target))

    class FakeResolution:
        selector = "fake"
        has_implementation = True
        implementation = FakeMinutesImplementation()

    monkeypatch.setattr(registry_module, "resolve_minutes_hook", lambda *a, **k: FakeResolution())
    workspace = tmp_path / "ws-drain-not-settled"
    workspace.mkdir()
    try:
        runtime.start_session(dir=str(workspace))
        begin_drain(runtime.snapshot, reason="user")
        # 前两步落定、`post` 步停在 `running`：收尾还没收完 —— 会后动作照常受理
        for step_id in ("flush", "log"):
            finish_drain_step(step_id, "done", snapshot=runtime.snapshot)
        finish_drain_step("post", "running", snapshot=runtime.snapshot)
        assert runtime.drain_has_settled() is False

        asyncio.run(runtime._drain_post(deadline=time.monotonic() + 10.0))
        started = runtime.post_minutes({})
        _wait_job_done(runtime)
        assert started["kind"] == "minutes" and started["dir"] == str(workspace)
        assert launched == [str(workspace)], launched
        assert not runtime.post_jobs_state()
    finally:
        config_module.set_current(previous)


# ---------------------------------------------------------------------------
# 八、停掉正在跑的「生成会后精细稿」（b1c6a58e，R28）
# ---------------------------------------------------------------------------


def test_post_stop_sets_the_cancel_flag_and_answers_the_receipt(
    tmp_path, monkeypatch
) -> None:
    """offline 在飞时 `post_stop` → 受理回执 `{kind, dir, stopping}` + 旗标立起 + 记录清点。

    受理即回（不等块边界）：旗标立起就回；线程体那一路拿到的是**同一条 Event**
    （`is_set` 形状递进 `run_session(cancel=…)`），盘上终态归 `run_session` 自己写。
    在飞记录（`post_jobs.json`）起跑时落、收工时清。
    """
    import threading

    from meeting.service import postjobs

    gate = threading.Event()
    started = threading.Event()
    seen: dict[str, object] = {}

    runtime, session_dir, previous = _ended_session(tmp_path, monkeypatch)

    def blocking_run_session(target, **kwargs):
        seen["cancel"] = kwargs.get("cancel")
        started.set()
        gate.wait(timeout=JOB_TIMEOUT_SECONDS)
        return {"state": "done"}

    from meeting.offline import runner as offline_runner

    monkeypatch.setattr(offline_runner, "run_session", blocking_run_session)
    try:
        runtime.post_offline({})
        assert started.wait(timeout=JOB_TIMEOUT_SECONDS)
        on_disk = json.loads(Path(postjobs.path()).read_text(encoding="utf-8"))
        assert [item["dir"] for item in on_disk] == [str(session_dir)], (
            "起跑那一刻在飞记录该在盘上（治「杀了进程就永远停在在跑」的那一格）"
        )
        assert [item["kind"] for item in on_disk] == ["offline"]

        receipt = runtime.post_stop({})

        assert receipt == {"kind": "offline", "dir": str(session_dir), "stopping": True}
        assert seen["cancel"] is not None and seen["cancel"](), (
            "线程体递进 run_session 的旗标该已立起"
        )
        cancel = runtime.post_jobs[("offline", str(session_dir))]["cancel"]
        assert cancel is not None and cancel.is_set()
    finally:
        gate.set()
        _wait_job_done(runtime)
        config_module.set_current(previous)
    assert json.loads(Path(postjobs.path()).read_text(encoding="utf-8")) == [], (
        "收工之后在飞记录清空（留给下一次起跑）"
    )


def test_post_stop_without_a_flying_job_is_refused(tmp_path, monkeypatch) -> None:
    """没有在飞的会后 → 409 `draining`、`detail = {}`（那一场已经收工，重按按钮即可）。"""
    from meeting.service.runtime import ActionError

    runtime, _session_dir, previous = _ended_session(tmp_path, monkeypatch)
    try:
        with pytest.raises(ActionError) as info:
            runtime.post_stop({})
        assert info.value.code == "draining"
        assert info.value.status == 409
        assert info.value.detail == {}
    finally:
        config_module.set_current(previous)


def test_post_stop_refuses_the_minutes_lane_truthfully(tmp_path, monkeypatch) -> None:
    """在飞的是纪要那一趟 → 409 `draining`：停的动作只接 offline（机制两样，如实拒）。

    `detail` 与「有一趟在飞」同一形状（`{kind, dir, started_at}`），界面按 `kind`
    读出「这趟停不了」。
    """
    import threading

    from meeting.service.runtime import ActionError

    runtime, session_dir, previous = _ended_session(tmp_path, monkeypatch)
    gate = threading.Event()
    started = threading.Event()

    from meeting.backends import registry as registry_module

    class BlockingMinutesImplementation:
        def launch(self, target: str, **kwargs) -> None:
            started.set()
            gate.wait(timeout=JOB_TIMEOUT_SECONDS)

    class FakeResolution:
        selector = "fake"
        has_implementation = True
        implementation = BlockingMinutesImplementation()

    monkeypatch.setattr(registry_module, "resolve_minutes_hook", lambda *a, **k: FakeResolution())
    try:
        runtime.post_minutes({})
        assert started.wait(timeout=JOB_TIMEOUT_SECONDS)
        with pytest.raises(ActionError) as info:
            runtime.post_stop({})
        assert info.value.code == "draining"
        assert info.value.status == 409
        assert info.value.detail["kind"] == "minutes"
        assert info.value.detail["dir"] == str(session_dir)
    finally:
        gate.set()
        _wait_job_done(runtime)
        config_module.set_current(previous)


def test_post_stop_with_another_dir_answers_truthfully(tmp_path, monkeypatch) -> None:
    """多趟在飞后的指向：`dir` 点名停哪一场；指到没有在飞的一场 → 409（那里没东西可停），
    在飞的那一场不受影响。"""
    import threading

    from meeting.service.runtime import ActionError

    runtime, session_dir, previous = _ended_session(tmp_path, monkeypatch)
    gate = threading.Event()
    started = threading.Event()

    from meeting.offline import runner as offline_runner

    def blocking_run_session(target, **kwargs):
        started.set()
        gate.wait(timeout=JOB_TIMEOUT_SECONDS)
        return {"state": "done"}

    monkeypatch.setattr(offline_runner, "run_session", blocking_run_session)
    try:
        runtime.post_offline({})
        assert started.wait(timeout=JOB_TIMEOUT_SECONDS)
        with pytest.raises(ActionError) as info:
            runtime.post_stop({"dir": str(tmp_path / "另一场")})
        assert info.value.code == "draining"
        assert info.value.status == 409
        # 在飞的那一场照旧活着：省略 dir 再停，停的就是它
        receipt = runtime.post_stop({})
        assert receipt == {"kind": "offline", "dir": str(session_dir), "stopping": True}, receipt
    finally:
        gate.set()
        _wait_job_done(runtime)
        config_module.set_current(previous)


def test_post_stop_with_two_offline_flights_requires_dir(tmp_path, monkeypatch) -> None:
    """两场 offline 都在飞：省略 `dir` → 400 让 `dir` 点名（不猜）；点名停哪场停哪场。"""
    import threading

    from meeting.service.runtime import ActionError

    runtime, session_dir, previous = _ended_session(tmp_path, monkeypatch)
    gates: list[threading.Event] = []

    from meeting.offline import runner as offline_runner

    def blocking_run_session(target, **kwargs):
        marker = threading.Event()
        gates.append(marker)
        marker.wait(timeout=JOB_TIMEOUT_SECONDS)
        return {"state": "done"}

    monkeypatch.setattr(offline_runner, "run_session", blocking_run_session)
    other = tmp_path / "ws-post-actions-2"
    other.mkdir()
    runtime.workspace_open(str(other))
    try:
        runtime.post_offline({})
        runtime.post_offline({"dir": str(other)})
        # 等**两条线程都跑进假件**（marker 登记满两枚）：在飞登记在线程 start 那一刻就
        # 有两格，光等它不算数 —— 两问都过才动手
        tick = threading.Event()
        for _ in range(500):
            if len(gates) == 2:
                break
            tick.wait(timeout=0.01)
        assert len(gates) == 2, gates
        with pytest.raises(ActionError) as info:
            runtime.post_stop({})
        assert info.value.code == "invalid_setting"
        assert sorted(info.value.detail["legal"]) == sorted([str(session_dir), str(other)])
        receipt = runtime.post_stop({"dir": str(other)})
        assert receipt["dir"] == str(other) and receipt["stopping"] is True
        # 受理即回：那一场的取消旗标立起、另一场的旗标不动
        assert runtime.post_jobs[("offline", str(other))]["cancel"].is_set()
        assert not runtime.post_jobs[("offline", str(session_dir))]["cancel"].is_set()
    finally:
        for marker in gates:
            marker.set()
        _wait_job_done(runtime)
        config_module.set_current(previous)
