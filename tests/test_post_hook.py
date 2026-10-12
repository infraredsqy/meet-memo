"""`meeting/backends/post.py` 的契约用例：解析落点、收尾第 4 步的接线、惰性导入。

三条要盯的：

  1. `resolve_post_hook()` 解析到本实现，`has_implementation is True`，
     `capabilities_of()` 不把它当替身（本类只是解析落点，不带任何钩子方法）；
  2. 收尾第 4 步**不拉起任何会后动作**：那一格如实停在 `waiting`（＝等你动手），
     释放显存照做，事后盘上也不多出一份离线状态文件；
  3. 惰性导入：全新解释器里 `import meeting.backends.post` 与
     `import meeting.offline.runner`，`torch` / `transformers` / `av` 都不在 `sys.modules`。

全部用例注入假引擎或假替身，一条都不加载真模型。
"""

from __future__ import annotations

import os
import logging
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from meeting import config as config_module
from meeting.backends import post as post_module
from meeting.backends.registry import capabilities_of, resolve_post_hook
from meeting.offline import artifacts

from tests import _helpers as helpers


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
    """本类**不实现** `finalize` / `transcode`（那两项是采集后端的能力），也不带钩子方法。"""
    assert not hasattr(post_module.PostProcessHook, "finalize")
    assert not hasattr(post_module.PostProcessHook, "transcode")
    assert not hasattr(post_module.PostProcessHook, "launch")


# ---------------------------------------------------------------------------
# 二、收尾第 4 步的接线证明
# ---------------------------------------------------------------------------


def test_the_drain_post_step_leaves_the_lane_waiting_for_the_user(
    tmp_path: Path, monkeypatch
) -> None:
    """收尾第 4 步**不拉起任何会后动作**，这一格如实停在 `waiting`（＝等你动手）。

    R26 / `docs/design-intent.md` 第 10 条：按「结束录音」之后什么都不发生。
    走的是真路径：开一场（dev 采集替身）→ 真 `_resolve_backends()` 按 `post.hook = auto`
    解析到本实现 → 真正的 `_drain_post()` 跑那一步。判据是「这一格停 `waiting`、
    盘上也不多出一份离线状态文件」（本实现不带任何钩子方法，一个会后动作都起不了）。
    """
    import asyncio

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
        "log.dir": str(home / "logs"),
        "capture.backend": "dev",
        "post.hook": "auto",
    }
    try:
        runtime = build_runtime(
            config_module.Config(
                applied=applied, sources={key: "默认" for key in applied}, path=""
            )
        )
        runtime._resolve_backends()

        # 接线第一段：解析结论照旧（钩子落点在位，与「要不要自动拉起」是两件事）
        assert runtime.post_resolution.target == "meeting.backends.post:PostProcessHook"
        assert runtime.post_resolution.has_implementation is True
        assert runtime.post_resolution.skipped() is False
        assert isinstance(runtime.post_hook, post_module.PostProcessHook)

        # 开一场（拿真实的场次目录），再把收尾推到「第 4 步待跑」那一档
        workspace = tmp_path / "ws-post-wiring"
        workspace.mkdir()
        runtime.start_session(dir=str(workspace))
        session_dir = Path(runtime.snapshot.session.dir)
        begin_drain(runtime.snapshot, reason="user")

        # 接线第二段：那一步真的跑，但**一个会后动作都不启动**
        asyncio.run(runtime._drain_post(deadline=time.monotonic() + 10.0))
    finally:
        config_module.set_current(previous)

    step = next(item for item in runtime.snapshot.ended.steps if item.id == "post")
    assert step.status == "waiting", (step.status, step.detail, step.error)
    # 「什么都不按」是合法的一档（R26），而这一格已经走完了：盘上不会多出一份离线状态文件
    assert not (session_dir / artifacts.STATUS_NAME).exists()


def test_the_post_step_stays_waiting_even_without_a_hook(tmp_path: Path, monkeypatch) -> None:
    """`post.hook = null` 那一档也停在 `waiting`：**没人按按钮**时两种钩子配置长得一样。

    这一步的读数不再区分「钩子在不在」（它一次都不调），因此缺席档不再是 `skipped` ——
    「会后流程还没接入」这句话在按钮那一层另有回答（`post_offline` 的 404 档）。
    """
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
        workspace = tmp_path / "ws-post-absent"
        workspace.mkdir()
        runtime.start_session(dir=str(workspace))
        begin_drain(runtime.snapshot, reason="user")
        asyncio.run(runtime._drain_post(deadline=time.monotonic() + 10.0))
    finally:
        config_module.set_current(previous)

    step = next(item for item in runtime.snapshot.ended.steps if item.id == "post")
    assert step.status == "waiting", (step.status, step.detail, step.error)
    # 缺席那一档的解析结论照旧（本实现到位之后，这一档只在显式取 null 时出现）
    assert runtime.post_resolution.skipped() is True
    assert runtime.post_resolution.target is None


# ---------------------------------------------------------------------------
# 三、收尾第 4 步**只做一件事**：让会中模型退出显存
#
# 为什么这一步还要跑：会中那个模型用完了就该当场退出显卡（`docs/design-intent.md` 第 21 条），
# 这件事与「要不要跑会后」无关。会后那两趟活各有各的按钮，这一条线一次都不碰它们。
#
# 判据取**事件列表**：替身后端的 `release_gpu()` 往列表里记一笔 —— 列表里只有
# `release` 这一项。不用固定 `sleep` 赌时序（项目纪律），也不读源码行号。
# ---------------------------------------------------------------------------

#: 释放抛异常时写进 log 的那一段（用例按它核实「如实记账、不静默」）
RELEASE_FAILURE_NOTE = "会中模型退出显存失败"

#: 收尾那几步的 logger（record 直接挂在这一层上；见 `recording_log` 的说明）
RUNTIME_LOGGER_NAME = "meeting.service.runtime"


class CapturedLog(logging.Handler):
    """直接挂在被测 logger 上的记录器。

    为什么不走 pytest 的 `caplog`：项目 `meeting` 那一层 logger 是 `propagate = False`
    （`meeting/logging_setup.py` 第 58 行，为的是不污染 torch / transformers 的日志），
    记录冒不到根上，`caplog` 一条都收不到。这里自己挂 handler，判据读实测文本。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def text(self) -> str:
        return "\n".join(record.getMessage() for record in self.records)


@contextmanager
def recording_log(logger_name: str = RUNTIME_LOGGER_NAME):
    """在 `logger_name` 这一路上挂一个记录器（用完摘掉、级别与禁用状态照原样放回）。"""
    logger = logging.getLogger(logger_name)
    recorder = CapturedLog()
    previous_level = logger.level
    logger.addHandler(recorder)
    logger.setLevel(logging.DEBUG)
    logging.disable(logging.NOTSET)
    try:
        yield recorder
    finally:
        logger.removeHandler(recorder)
        logger.setLevel(previous_level)


def _post_wiring(tmp_path: Path, monkeypatch, *, events: list[str], transcriber_backend: Any):
    """把运行时推到「第 4 步待跑」那一档：dev 采集替身 + 解析到位的会后钩子落点。

    `events` 由调用方给（替身后端的 `release_gpu()` 往这一份里记一笔 —— 事件列表里
    排出的先后，就是这一步的顺序证据）。返回 `(runtime, previous)`。
    """
    from meeting.service.app import build_runtime
    from meeting.state import begin_drain

    home = tmp_path / "home-post-release"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("MEETING_CAPTURE", raising=False)
    previous = config_module.current()

    applied = {
        **config_module.DEFAULT_VALUES,
        "log.dir": str(home / "logs"),
        "capture.backend": "dev",
        "post.hook": "auto",
    }
    runtime = build_runtime(
        config_module.Config(
            applied=applied, sources={key: "默认" for key in applied}, path=""
        )
    )
    runtime._resolve_backends()
    workspace = tmp_path / "ws-post-release"
    workspace.mkdir()
    runtime.start_session(dir=str(workspace))
    # **替身后端要挂在 `start_session()` 之后**：这一串装配是按真实顺序走的 —— `_resolve_backends()`
    # 在这里由用例自己调（`runtime.py` 里它的唯一生产调用点在 `startup()` 那条 lifespan 路径上，
    # `start_session()` 里没有它），随后 `start_session()` 会把解析出来的**真**转写后端一路带下去
    # （`start()` 与 `_refresh_transcript_snapshot()` 都落在它身上）。替身只提供了 `release_gpu()`
    # 这一格，因此挂在 `start_session()` 之后，那时的会话已经开好、场次目录也在了。
    if transcriber_backend is not None:
        runtime.transcriber_backend = transcriber_backend
    begin_drain(runtime.snapshot, reason="user")
    return runtime, previous


def test_the_backend_release_still_runs_on_the_drain_post_step(tmp_path, monkeypatch) -> None:
    """**释放照做、会后不碰**：`release_gpu()` 被喊到一次，会后一个动作都不起。

    事件列表里只有 `release` 这一项 —— 会中模型当场退出显存（`docs/design-intent.md`
    第 21 条），会后那两趟活留给用户手上的两个按钮。
    """
    import asyncio

    class OrderRecorder:
        """替身转写后端：只有 `release_gpu` 这一格被用到（把调用记进事件列表）。"""

        def __init__(self, events: list[str]) -> None:
            self.events = events

        def release_gpu(self) -> bool:
            self.events.append("release")
            return True

    events: list[str] = []
    runtime = None
    previous = None
    try:
        runtime, previous = _post_wiring(
            tmp_path, monkeypatch, events=events,
            transcriber_backend=OrderRecorder(events),
        )
        asyncio.run(runtime._drain_post(deadline=time.monotonic() + 10.0))
    finally:
        config_module.set_current(previous)

    assert events == ["release"], f"释放该被喊到一次，会后一个动作都不许起：{events}"
    step = next(item for item in runtime.snapshot.ended.steps if item.id == "post")
    assert step.status == "waiting", (step.status, step.detail, step.error)
    # **没有新增 step_id**：`state.py` 的取值域只认 flush / transcode / log / post
    assert {item.id for item in runtime.snapshot.ended.steps} == {
        "flush", "transcode", "log", "post",
    }


def test_a_raising_release_still_lets_the_post_step_settle(
    tmp_path, monkeypatch, caplog
) -> None:
    """释放抛异常**不许把整场拖下水**：如实记一条 log，这一步照旧跑完。

    这一条与 `runtime.py` 相邻几步的既有姿态同源（收尾失败如实记账、别把别的步骤带走）。
    """
    import asyncio

    class ExplodingRelease:
        def __init__(self, events: list[str]) -> None:
            self.events = events

        def release_gpu(self) -> bool:
            self.events.append("release")
            raise RuntimeError("假替身按要求在释放时抛错")

    events: list[str] = []
    runtime = None
    previous = None
    try:
        runtime, previous = _post_wiring(
            tmp_path, monkeypatch, events=events,
            transcriber_backend=ExplodingRelease(events),
        )
        with recording_log() as log:
            asyncio.run(runtime._drain_post(deadline=time.monotonic() + 10.0))
            written = log.text()
    finally:
        config_module.set_current(previous)

    step = next(item for item in runtime.snapshot.ended.steps if item.id == "post")
    assert step.status == "waiting", (
        f"释放抛错不该让这一步落 failed：{step.status} / {step.detail} / {step.error}"
    )
    assert events == ["release"], events
    assert RELEASE_FAILURE_NOTE in written, f"释放失败该如实进 log：\n{written}"


def test_the_product_transcriber_backend_really_takes_this_lane(tmp_path, monkeypatch) -> None:
    """防御式取法取到的**正是真后端那个方法**：这一条走产品解析出来的 `TranscriberBackend`。

    前两条用的是替身后端；这一条把「真后端上那个 `release_gpu()` 确实经这条线被喊到」补齐 ——
    真后端此刻**没有加载过会中模型**，于是这一次释放是那四条语义里的 no-op 那一格
    （与 `meeting post` 那个独立入口的现场同形）。
    """
    import asyncio

    from meeting.backends.transcribe import TranscriberBackend

    events: list[str] = []
    runtime = None
    previous = None
    try:
        runtime, previous = _post_wiring(
            tmp_path, monkeypatch, events=events, transcriber_backend=None,
        )
        assert isinstance(runtime.transcriber_backend, TranscriberBackend), (
            f"`transcriber.backend = auto` 该解析到这个真实现，实测 "
            f"{type(runtime.transcriber_backend).__name__}"
        )
        real = TranscriberBackend.release_gpu

        def recording_release(self):
            events.append("release")
            return real(self)

        monkeypatch.setattr(TranscriberBackend, "release_gpu", recording_release)
        asyncio.run(runtime._drain_post(deadline=time.monotonic() + 10.0))
    finally:
        config_module.set_current(previous)

    assert events == ["release"], "真后端上的释放该经这条线被喊到一次"
    step = next(item for item in runtime.snapshot.ended.steps if item.id == "post")
    assert step.status == "waiting", (step.status, step.detail, step.error)


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
