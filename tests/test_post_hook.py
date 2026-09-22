"""`meeting/backends/post.py` 的契约用例：会后钩子的解析、立即返回、后台收敛。

四条要盯的（对应任务书的四条完成判定）：

  1. `resolve_post_hook()` 解析到本实现，`has_implementation is True`，
     `capabilities_of()` 不把它当替身；
  2. `launch(session_dir)` **立即返回**：注入一个卡在闸门上的引擎，断言调用耗时
     远小于引擎耗时（这条用例里引擎一直卡着不动，调用却当场回来）；
  3. `launch` 之后状态文件先出现 `running`，后台跑完收敛到终态
     （**事件驱动地等**：轮询状态文件，不用固定 `sleep` 凑时序）；
  4. 惰性导入：全新解释器里 `import meeting.backends.post` 与
     `import meeting.offline.runner`，`torch` / `transformers` / `av` 都不在 `sys.modules`。

全部用例注入假引擎或假 `run_session`，一条都不加载真模型。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from meeting import config as config_module
from meeting.backends import post as post_module
from meeting.backends.registry import capabilities_of, resolve_post_hook
from meeting.offline import artifacts

from tests import _helpers as helpers
from tests.test_offline_runner import FakeConfig, FakeEngine, synth_session

#: 后台线程收敛的轮询上限（事件驱动地等，等到了就立刻回）
CONVERGE_TIMEOUT_SECONDS = 10.0


def wait_until(predicate, *, what: str, timeout: float = CONVERGE_TIMEOUT_SECONDS):
    """轮询一个谓词直到成立（等的是状态本身）；超时即抛并说清在等什么。"""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        threading.Event().wait(0.02)
    raise AssertionError(f"等不到：{what}（最后一次读数 {last!r}）")


def read_status(session: Path) -> dict:
    """读状态文件（**文件还没出现**也算「还没到位」这一档，交给调用方的轮询等它）。"""
    path = session / artifacts.STATUS_NAME
    if not path.exists():
        return {}
    return artifacts.read_status(path)


def patch_engine(monkeypatch, engine) -> None:
    """把真引擎换成假的那一台：只替换 `runner.build_engine` 这一个接缝。

    `runner.run_session()` 按配置现造引擎，这个接缝就是「这一场用哪台引擎」的落点。
    """
    from meeting.offline import runner

    monkeypatch.setattr(runner, "build_engine", lambda config=None: engine)


# ---------------------------------------------------------------------------
# 一、解析：拿到本实现，不被当成替身
# ---------------------------------------------------------------------------


def test_resolve_post_hook_lands_on_this_implementation() -> None:
    """`post.hook = auto` → `meeting.backends.post:PostProcessHook`，`has_implementation` 为真。"""
    resolution = resolve_post_hook()

    assert resolution.key == "post.hook"
    assert resolution.selector == "auto"
    assert resolution.target == "meeting.backends.post:PostProcessHook"
    assert resolution.has_implementation is True
    assert resolution.skipped() is False
    assert isinstance(resolution.implementation, post_module.PostProcessHook)
    assert type(resolution.implementation).__module__ == "meeting.backends.post"


def test_capabilities_of_does_not_treat_it_as_a_stand_in() -> None:
    """`capabilities_of()` 靠 `__module__` 区分替身与本实现：本实现两项采集能力都不声明。"""
    resolution = resolve_post_hook()

    assert capabilities_of(resolution.implementation) == {
        "finalize": False,
        "transcode": False,
        "dev_inject": False,
    }


def test_explicit_null_still_means_absent() -> None:
    """显式取 `null` 仍是缺席档（`has_implementation is False` → 收尾第 4 步记 `skipped`）。"""
    resolution = resolve_post_hook("null")

    assert resolution.has_implementation is False
    assert resolution.skipped() is True


def test_hook_declares_no_capture_capabilities() -> None:
    """本类**不实现** `finalize` / `transcode`（那两项是采集后端的能力）。"""
    assert not hasattr(post_module.PostProcessHook, "finalize")
    assert not hasattr(post_module.PostProcessHook, "transcode")
    assert callable(post_module.PostProcessHook.launch)


def test_launch_needs_a_session_dir() -> None:
    """场次目录是空值时当场抛 `ValueError`（没有落点就装作启动过，盘上会留一版永远 running）。"""
    hook = post_module.PostProcessHook()

    with pytest.raises(ValueError):
        hook.launch("")


# ---------------------------------------------------------------------------
# 二、`launch()` 立即返回
# ---------------------------------------------------------------------------


def test_launch_returns_immediately_while_the_engine_is_still_blocked(
    tmp_path, monkeypatch
) -> None:
    """注入一台卡在闸门上的引擎：`launch()` 当场回来，引擎那一刻还没跑完。"""
    session = synth_session(tmp_path / "250919")
    gate = threading.Event()
    engine = FakeEngine(gate=gate)
    patch_engine(monkeypatch, engine)
    monkeypatch.setattr(config_module, "get", lambda key, default=None: FakeConfig().get(key, default))

    hook = post_module.PostProcessHook()
    started = time.monotonic()
    hook.launch(str(session))
    elapsed = time.monotonic() - started

    # 引擎此刻还卡在闸门上：调用却已经回来了
    assert elapsed < 0.5, f"launch() 该立刻返回，实测 {elapsed:.3f}s"
    assert engine.calls == [], "引擎还没被调用到"

    gate.set()
    wait_until(
        lambda: read_status(session).get("state") == "done",
        what="后台线程跑完并落 done",
    )
    assert [Path(call).name for call in engine.calls] == [
        "meeting_audio_mic.pcm",
        "meeting_audio_loopback.pcm",
    ]
    assert (session / "meeting_offline_transcript_mic.md").exists()


def test_launch_does_not_wait_for_a_slow_run_session(tmp_path, monkeypatch) -> None:
    """`run_session` 整体被换成一个「卡住不返回」的替身：`launch()` 照样当场返回。"""
    session = synth_session(tmp_path / "250919")
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_run_session(session_dir, **_kwargs):
        entered.set()
        release.wait(timeout=CONVERGE_TIMEOUT_SECONDS)
        finished.set()
        return {"session_dir": str(session_dir)}

    from meeting.offline import runner

    monkeypatch.setattr(runner, "run_session", blocking_run_session)

    hook = post_module.PostProcessHook()
    started = time.monotonic()
    hook.launch(str(session))
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, f"launch() 该立刻返回，实测 {elapsed:.3f}s"
    assert entered.wait(timeout=CONVERGE_TIMEOUT_SECONDS), "后台线程该已经跑起来"
    release.set()
    assert finished.wait(timeout=CONVERGE_TIMEOUT_SECONDS), "替身该跑完"


def test_the_background_thread_is_a_named_daemon(monkeypatch) -> None:
    """线程交给 `threading.Thread(name=..., daemon=True)`：服务退出不被它拖住。

    `Thread` 换成一条只记账的替身：这一条量的是「线程是怎么起的」，
    起线程那两行与真实现同一份代码，线程体换成一条哨兵、不跑整条会后链。
    """
    created = {}

    class FakeThread:
        def __init__(self, **kwargs) -> None:
            created.update(kwargs)

        def start(self) -> None:
            created["started"] = True

    def sentinel(self, session_dir):
        return None

    monkeypatch.setattr(post_module.threading, "Thread", FakeThread)
    monkeypatch.setattr(post_module.PostProcessHook, "_run", sentinel)

    post_module.PostProcessHook().launch("C:/tmp/place")

    assert created["daemon"] is True
    assert created["name"] == post_module.THREAD_NAME
    # 目标是本类的 `_run`（哨兵本人）：只比名字，不比函数对象身份
    assert getattr(created["target"], "__name__", "") == sentinel.__name__
    # 线程体收到的是**场次目录**这一个参数（`self` 由绑定方法带走）
    assert created["args"] == ("C:/tmp/place",)
    assert created["started"] is True


# ---------------------------------------------------------------------------
# 三、状态文件：先 running，后台收敛到终态
# ---------------------------------------------------------------------------


def test_status_first_says_running_then_settles(tmp_path, monkeypatch) -> None:
    """`launch` 之后状态文件先出现 `running`，后台跑完收敛到 `done`。"""
    session = synth_session(tmp_path / "250919")
    gate = threading.Event()

    class Gated(FakeEngine):
        def transcribe(self, audio_path):
            gate.wait(timeout=CONVERGE_TIMEOUT_SECONDS)
            return super().transcribe(audio_path)

    patch_engine(monkeypatch, Gated())
    monkeypatch.setattr(
        config_module, "get", lambda key, default=None: FakeConfig().get(key, default)
    )

    post_module.PostProcessHook().launch(str(session))

    running = wait_until(
        lambda: (lambda doc: doc if doc.get("state") == "running" else None)(read_status(session)),
        what="状态文件先落 running",
    )
    assert running["sources"] == ["mic", "loopback"]
    assert running["finished_at"] is None
    assert running["artifacts"] == {}

    gate.set()
    settled = wait_until(
        lambda: (lambda doc: doc if doc.get("state") not in (None, "running") else None)(
            read_status(session)
        ),
        what="后台线程收敛到终态",
    )
    assert settled["state"] == "done"
    assert settled["finished_at"]
    assert settled["artifacts"]["mic"]["transcript"] == "meeting_offline_transcript_mic.md"


def test_a_failing_background_run_lands_in_the_status_file(tmp_path, monkeypatch) -> None:
    """后台线程里的异常落进状态文件的 `error`，**不外传**给调用方。"""
    session = synth_session(tmp_path / "250919")
    entered = threading.Event()

    def exploding_run_session(session_dir, **_kwargs):
        entered.set()
        raise RuntimeError("会后那台机器上的显存不够")

    from meeting.offline import runner

    monkeypatch.setattr(runner, "run_session", exploding_run_session)

    hook = post_module.PostProcessHook()
    hook.launch(str(session))  # 不抛
    assert entered.wait(timeout=CONVERGE_TIMEOUT_SECONDS)

    status = wait_until(
        lambda: (lambda doc: doc if doc.get("state") == "failed" else None)(read_status(session)),
        what="状态文件落 failed",
    )
    assert "RuntimeError" in (status["error"] or "")
    assert "显存不够" in (status["error"] or "")
    assert status["session_id"] == "250919"
    assert status["finished_at"]


def test_a_failing_background_run_keeps_the_earlier_status_fields(
    tmp_path, monkeypatch
) -> None:
    """补写 `failed` 时保留先那一版的场次编号、开始时刻与路清单。"""
    session = synth_session(tmp_path / "250919")
    written = {}

    def exploding_run_session(session_dir, **_kwargs):
        # 先按真路径落一版 `running`，再抛：这样「补写」这一步有东西可保留
        artifacts.write_status(
            Path(session_dir) / artifacts.STATUS_NAME,
            session_id="250919",
            state="running",
            started_at="2025-09-19T20:00:00",
            sources=["mic", "loopback"],
            artifacts={"mic": {"transcript": "t.md", "speakers": "s.json", "report": "r.md"}},
        )
        written.update(read_status(Path(session_dir)))
        raise RuntimeError("写盘路径不可用")

    from meeting.offline import runner

    monkeypatch.setattr(runner, "run_session", exploding_run_session)

    post_module.PostProcessHook().launch(str(session))
    status = wait_until(
        lambda: (lambda doc: doc if doc.get("state") == "failed" else None)(read_status(session)),
        what="状态文件落 failed",
    )

    assert status["started_at"] == written["started_at"] == "2025-09-19T20:00:00"
    assert status["sources"] == written["sources"] == ["mic", "loopback"]
    assert status["session_id"] == written["session_id"] == "250919"
    assert status["artifacts"] == written["artifacts"]
    assert status["schema_version"] == artifacts.STATUS_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 五、收尾第 4 步的接线证明
# ---------------------------------------------------------------------------


def test_the_drain_post_step_turns_done_with_this_implementation(
    tmp_path: Path, monkeypatch
) -> None:
    """真服务里收尾第 4 步从 `skipped` 翻成 `done`（本实现到位之后）。

    走的是真路径：开一场（dev 采集替身）→ 真 `_resolve_backends()` 按 `post.hook = auto`
    解析到本实现 → 真正的 `_drain_post()` 跑那一步。**`launch` 换成一条只落状态文件的替身**
    —— 这一步要量的是「收尾那一步有没有被拉起、记成什么」，会后链自己的账在
    `tests/test_offline_runner.py` 里验（同 `tests/test_drain.py` 的既有做法）。
    """
    import asyncio
    import json

    from meeting import config as config_module
    from meeting.service.app import build_runtime
    from meeting.state import begin_drain

    home = tmp_path / "home-post-wiring"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MEETING_CAPTURE", raising=False)
    previous = config_module.current()

    applied = {
        **config_module.DEFAULT_VALUES,
        "output.root": str(tmp_path / "out-post-wiring"),
        "log.dir": str(home / "logs"),
        "capture.backend": "dev",
        "post.hook": "auto",
    }
    launched: list[str] = []

    def fake_launch(self, target: str) -> None:
        """替身 `launch()`：在它自己收到的那个场次目录下落一版 `running` 状态文件。"""
        launched.append(str(target))
        directory = Path(target)
        directory.mkdir(parents=True, exist_ok=True)
        artifacts.write_status(
            directory / artifacts.STATUS_NAME,
            session_id=directory.name,
            state="running",
            started_at="2025-09-19T20:00:00",
            sources=["mic", "loopback"],
            artifacts={},
        )

    monkeypatch.setattr(post_module.PostProcessHook, "launch", fake_launch)
    try:
        runtime = build_runtime(
            config_module.Config(
                applied=applied, sources={key: "默认" for key in applied}, path=""
            )
        )
        runtime._resolve_backends()

        # 接线第一段：解析结论
        assert runtime.post_resolution.target == "meeting.backends.post:PostProcessHook"
        assert runtime.post_resolution.has_implementation is True
        assert runtime.post_resolution.skipped() is False
        assert isinstance(runtime.post_hook, post_module.PostProcessHook)

        # 开一场（拿真实的场次目录），再把收尾推到「第 4 步待跑」那一档
        runtime.start_session()
        session_dir = Path(runtime.snapshot.session.dir)
        begin_drain(runtime.snapshot, reason="user")

        # 接线第二段：那一步真的跑、落 `done`
        asyncio.run(runtime._drain_post(deadline=time.monotonic() + 10.0))
    finally:
        config_module.set_current(previous)

    step = next(item for item in runtime.snapshot.ended.steps if item.id == "post")
    assert step.status == "done", (step.status, step.detail, step.error)
    assert runtime.post_resolution.run_ok is True
    # 收尾那一步交给钩子的正是这一场的场次目录（缺席档给的是 `None`）
    assert launched == [str(session_dir)], launched
    written = json.loads((session_dir / artifacts.STATUS_NAME).read_text(encoding="utf-8"))
    assert written["session_id"] == session_dir.name
    assert written["state"] == "running"
    assert written["sources"] == ["mic", "loopback"]


def test_the_absent_lane_still_records_skipped(tmp_path: Path, monkeypatch) -> None:
    """替身缺席那条路径没被本单元改坏：`post.hook = null` → 那一步照旧记 `skipped`。"""
    import asyncio

    from meeting import config as config_module
    from meeting.service.app import build_runtime
    from meeting.state import begin_drain

    home = tmp_path / "home-post-absent"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MEETING_CAPTURE", raising=False)
    previous = config_module.current()

    applied = {
        **config_module.DEFAULT_VALUES,
        "output.root": str(tmp_path / "out-post-absent"),
        "log.dir": str(home / "logs"),
        "capture.backend": "dev",
        "post.hook": "null",
    }
    try:
        runtime = build_runtime(
            config_module.Config(
                applied=applied, sources={key: "默认" for key in applied}, path=""
            )
        )
        runtime._resolve_backends()
        assert runtime.post_resolution.has_implementation is False
        runtime.start_session()
        begin_drain(runtime.snapshot, reason="user")
        asyncio.run(runtime._drain_post(deadline=time.monotonic() + 10.0))
    finally:
        config_module.set_current(previous)

    step = next(item for item in runtime.snapshot.ended.steps if item.id == "post")
    assert step.status == "skipped", (step.status, step.detail, step.error)
    # 缺席那一档的说明留着一句「还没接入」（本实现到位之后，这一档只在显式取 null 时出现）
    assert runtime.post_resolution.skipped() is True
    assert runtime.post_resolution.target is None


# ---------------------------------------------------------------------------
# 四、全新解释器里的 import 链
# ---------------------------------------------------------------------------

#: 在子进程里跑的一段：import 之后报出**重依赖**与钩子的解析结论
_IMPORT_CHAIN_CODE = (
    "import sys; "
    "import meeting.backends.post as p; "
    "import meeting.offline.runner as r; "
    "from meeting.backends.registry import resolve_post_hook, capabilities_of; "
    "res = resolve_post_hook(); "
    "heavy = [m for m in ('torch', 'transformers', 'av', 'webrtcvad', 'librosa') "
    "if m in sys.modules]; "
    "print('heavy=' + ','.join(heavy)); "
    "print('hook=' + p.PostProcessHook.__module__ + '/' + p.PostProcessHook.__name__); "
    "print('runner=' + r.run_session.__name__); "
    "print('resolved=' + str(res.has_implementation) + '/' + str(capabilities_of(res.implementation)['finalize']))"
)


def test_import_chain_pulls_no_heavy_dependency() -> None:
    """`import meeting.backends.post` 与 `import meeting.offline.runner` 都保持轻量。

    **必须换一个全新解释器**：本进程里 `torch` / `av` / `numpy` 早被别的用例与
    `tests/conftest.py` 的兜底夹具拉进 `sys.modules` 了，同进程断言等于没验。
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(helpers.REPO_ROOT)
    env["HF_HUB_OFFLINE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_CHAIN_CODE],
        cwd=str(helpers.REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    output = (result.stdout or "") + (result.stderr or "")
    assert result.returncode == 0, f"全新解释器里 import 失败：\n{output}"
    lines = (result.stdout or "").strip().splitlines()

    assert lines[0] == "heavy=", (
        f"`import meeting.backends.post` 把重依赖拉进了 sys.modules：{lines[0]!r}\n{output}"
    )
    assert lines[1] == "hook=meeting.backends.post/PostProcessHook", output
    assert lines[2] == "runner=run_session", output
    assert lines[3] == "resolved=True/False", output
