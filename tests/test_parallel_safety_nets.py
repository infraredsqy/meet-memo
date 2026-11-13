"""并行档两道安全网的适配（节点）：看门阈值的环境变量覆盖、xdist 收场复查搬运。

被测逻辑全部住在 `tests/conftest.py`：`case_wall_clock_limit()`（并行档放宽口径，缺省 5.0）、
`_xdist_report_worker_violations()`（worker 把违例记录写成上报文件）、
`_xdist_collect_worker_reports()`（controller 汇总打红线并整趟判红）、
`_xdist_dist_active()`（串行趟的零动作判据）。全程不真起并行趟、不碰机主真实家目录 ——
守卫读数全部造在临时目录里（与 `test_config_isolation.py` 同一口径）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from tests import conftest as guard_module


# ---------------------------------------------------------------------------
# 看门阈值的环境变量覆盖
# ---------------------------------------------------------------------------


class _Outcome:
    """钩子包裹器 `yield` 回来的那份 outcome 的最小替身（只多 `get_result()`）。"""

    def __init__(self, report: Any) -> None:
        self._report = report

    def get_result(self) -> Any:
        return self._report


class _TeardownReport:
    """teardown 段 report 的最小替身（看门改写 `outcome` 与 `longrepr`）。"""

    when = "teardown"

    def __init__(self) -> None:
        self.outcome = "passed"
        self.longrepr: Any = None


class _SegmentReport:
    """某一段（setup / call / teardown）report 的最小替身（只喂耗时给看门登记）。"""

    def __init__(self, nodeid: str, when: str, duration: float) -> None:
        self.nodeid = nodeid
        self.when = when
        self.duration = duration


class _Item:
    """用例条目的最小替身（看门只读 `nodeid`）。"""

    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid


def _run_watchdog(nodeid: str, durations: dict[str, float]) -> _TeardownReport:
    """喂三段耗时给看门的两个钩子，回传它（可能）改写后的 teardown report。"""
    guard_module._CASE_DURATIONS.clear()
    for when, seconds in durations.items():
        guard_module.pytest_runtest_logreport(_SegmentReport(nodeid, when, seconds))
    report = _TeardownReport()
    generator = guard_module.pytest_runtest_makereport(_Item(nodeid), None)
    next(generator)  # 跑到 `outcome = yield`
    try:
        generator.send(_Outcome(report))
    except StopIteration:  # 钩子体跑完的正常收场
        pass
    return report


def test_watchdog_threshold_defaults_to_5s_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不设环境变量 = 缺省 5.0 秒：串行趟的口径逐字节不动。"""
    monkeypatch.delenv(guard_module.CASE_WALL_CLOCK_LIMIT_SECONDS_ENV, raising=False)
    assert guard_module.case_wall_clock_limit() == 5.0


@pytest.mark.parametrize("raw", ["8.5", "12"])
def test_watchdog_threshold_env_override(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """设了正数就用设的值：并行档的放宽口径。"""
    monkeypatch.setenv(guard_module.CASE_WALL_CLOCK_LIMIT_SECONDS_ENV, raw)
    assert guard_module.case_wall_clock_limit() == float(raw)


@pytest.mark.parametrize("raw", ["abc", "0", "-3", "", "   "])
def test_watchdog_threshold_garbage_falls_back_to_default(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """读不出来（非数字 / ≤ 0 / 空白串）一律退回缺省 5.0：宁可从严，不让看门失明。"""
    monkeypatch.setenv(guard_module.CASE_WALL_CLOCK_LIMIT_SECONDS_ENV, raw)
    assert guard_module.case_wall_clock_limit() == 5.0


def test_watchdog_hook_reds_beyond_serial_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合计 6.0s、阈值缺省 5.0 → 判红，报文带着合计与三段各自的耗时。"""
    monkeypatch.delenv(guard_module.CASE_WALL_CLOCK_LIMIT_SECONDS_ENV, raising=False)
    report = _run_watchdog("t::a", {"setup": 1.0, "call": 4.0, "teardown": 1.0})
    assert report.outcome == "failed"
    text = str(report.longrepr)
    assert "6.00s" in text and "5.0s" in text, text


def test_watchdog_hook_spares_case_within_parallel_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一条 6.0s 的用例，并行档把阈值放宽到 8 秒 → 不判红。"""
    monkeypatch.setenv(guard_module.CASE_WALL_CLOCK_LIMIT_SECONDS_ENV, "8")
    report = _run_watchdog("t::b", {"setup": 1.0, "call": 4.0, "teardown": 1.0})
    assert report.outcome == "passed"
    assert report.longrepr is None


# ---------------------------------------------------------------------------
# xdist 收场复查搬运
# ---------------------------------------------------------------------------


def _make_dsh_guard(ledger: Path, *, claimed: int) -> guard_module.DshGuard:
    """造一道记录守卫：两行记录，`claimed` 之前的那部分算已认领。"""
    ledger.write_text("一笔已认领的记录\n一笔探针迟到的记录\n", encoding="utf-8")
    return guard_module.DshGuard(
        root=ledger.parent,
        ledger=ledger,
        shim=ledger.parent / "dsh.cmd",
        previous_path="",
        claimed=claimed,
    )


class _Reporter:
    """terminalreporter 的最小替身：把打出来的行与颜色记下来。"""

    def __init__(self) -> None:
        self.lines: list[tuple[str, bool]] = []

    def write_line(self, message: str, red: bool = False, **kwargs: Any) -> None:
        self.lines.append((str(message), bool(red)))


class _PluginManager:
    def __init__(self, reporter: Any, plugins: set[str] | None = None) -> None:
        self._reporter = reporter
        self._plugins = plugins or set()

    def get_plugin(self, name: str) -> Any:
        return self._reporter

    def hasplugin(self, name: str) -> bool:
        return name in self._plugins


class _Config:
    def __init__(self, reporter: Any) -> None:
        self.pluginmanager = _PluginManager(reporter)


class _FakeSession:
    """session 的最小替身（收场复查只读 `config.pluginmanager` 并改写 `exitstatus`）。"""

    def __init__(self, reporter: Any, exitstatus: int = 0) -> None:
        self.config = _Config(reporter)
        self.exitstatus = exitstatus


def test_worker_writes_report_file_for_unclaimed_dsh_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker 侧：DSH 记录多出一笔未认领 → 写上报文件（worker 唯一能让红出场的方式）。"""
    guard = _make_dsh_guard(tmp_path / "dsh-ledger.txt", claimed=1)
    monkeypatch.setattr(guard_module, "_ACTIVE_DSH_GUARD", guard)
    monkeypatch.setattr(guard_module, "_ACTIVE_REAL_HOME_GUARD", None)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")

    shared = tmp_path / "shared"
    shared.mkdir()
    assert guard_module._xdist_report_worker_violations(shared) is True

    report_file = shared / "worker-gw3.txt"
    assert report_file.is_file()
    text = report_file.read_text(encoding="utf-8")
    assert "一笔探针迟到的记录" in text, text


def test_worker_writes_report_file_for_real_home_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker 侧：真家目录守卫读数造在临时目录里、收工时「被建出来」就是一笔。"""
    target = tmp_path / "config-meeting"
    guard = guard_module.RealHomeGuard(config_dir=target, before={})
    target.mkdir()
    (target / "config.json").write_text('{"minutes.hook": "null"}', encoding="utf-8")
    monkeypatch.setattr(guard_module, "_ACTIVE_DSH_GUARD", None)
    monkeypatch.setattr(guard_module, "_ACTIVE_REAL_HOME_GUARD", guard)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)

    shared = tmp_path / "shared"
    shared.mkdir()
    assert guard_module._xdist_report_worker_violations(shared) is True

    text = (shared / "worker-worker.txt").read_text(encoding="utf-8")
    assert "config.json" in text and "机主真实家目录" in text, text


def test_worker_writes_nothing_when_guards_are_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker 侧：两道守卫都没红 → 不写文件（并行三趟零上报时主终端就该安静）。"""
    ledger = tmp_path / "dsh-ledger.txt"
    guard = _make_dsh_guard(ledger, claimed=2)  # 两行记录全部已认领
    config_dir = tmp_path / "config-meeting"
    config_dir.mkdir()
    home_guard = guard_module.RealHomeGuard(
        config_dir=config_dir,
        before=guard_module._real_home_snapshot(config_dir),
    )
    monkeypatch.setattr(guard_module, "_ACTIVE_DSH_GUARD", guard)
    monkeypatch.setattr(guard_module, "_ACTIVE_REAL_HOME_GUARD", home_guard)

    shared = tmp_path / "shared"
    shared.mkdir()
    assert guard_module._xdist_report_worker_violations(shared) is False
    assert list(shared.iterdir()) == []


def test_controller_reds_session_and_prints_worker_reports(tmp_path: Path) -> None:
    """controller 侧：worker 上报逐份打红线、整趟判红 —— 红必须到主终端。"""
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "worker-gw0.txt").write_text(
        "记录迟到的一笔（gw0 上报）\n", encoding="utf-8"
    )
    (shared / "worker-gw1.txt").write_text(
        "机主家目录 config.json 被建出（gw1 上报）\n", encoding="utf-8"
    )
    reporter = _Reporter()
    session = _FakeSession(reporter, exitstatus=0)

    guard_module._xdist_collect_worker_reports(session, shared)

    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    reds = [line for line, red in reporter.lines if red]
    assert any("2 个 worker 上报了漏网" in line for line in reds), reporter.lines
    assert any("gw0 上报" in line for line in reds), reporter.lines
    assert any("gw1 上报" in line for line in reds), reporter.lines


def test_controller_stays_green_without_reports(tmp_path: Path) -> None:
    """controller 侧：共享目录里没有上报文件 → 一个字不打、退出码不动。"""
    shared = tmp_path / "shared"
    shared.mkdir()
    reporter = _Reporter()
    session = _FakeSession(reporter, exitstatus=0)

    guard_module._xdist_collect_worker_reports(session, shared)

    assert session.exitstatus == 0
    assert reporter.lines == []


def test_bridge_is_silent_without_shared_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """串行趟：环境变量不存在，搬运口零动作（与从前的收场复查逐字节一致）。"""
    monkeypatch.delenv(guard_module.XDIST_GUARD_DIR_ENV, raising=False)
    reporter = _Reporter()
    session = _FakeSession(reporter, exitstatus=0)

    guard_module._xdist_sessionfinish_bridge(session)

    assert session.exitstatus == 0
    assert reporter.lines == []


def test_bridge_reports_on_worker_when_env_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """搬运口在 worker 侧（有 `workerinput`）走写文件那一半。"""
    guard = _make_dsh_guard(tmp_path / "dsh-ledger.txt", claimed=1)
    monkeypatch.setattr(guard_module, "_ACTIVE_DSH_GUARD", guard)
    monkeypatch.setattr(guard_module, "_ACTIVE_REAL_HOME_GUARD", None)
    monkeypatch.setenv(guard_module.XDIST_GUARD_DIR_ENV, str(tmp_path / "shared"))
    # 真 worker 上名字来自环境（并行趟里是 gwN），钉住它断言才不随档位漂
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw5")

    class _WorkerConfig(_Config):
        workerinput = "gw5"

    class _WorkerSession:
        config = _WorkerConfig(_Reporter())
        exitstatus = 0

    session = _WorkerSession()
    guard_module._xdist_sessionfinish_bridge(session)  # type: ignore[arg-type]

    shared = tmp_path / "shared"
    assert (shared / "worker-gw5.txt").is_file()


def test_dist_active_detector_without_xdist_plugin() -> None:
    """没装 pytest-xdist（插件不在）→ 恒 False：串行趟连共享目录都不建。"""

    class _NoXdistConfig:
        pluginmanager = _PluginManager(None)

    assert guard_module._xdist_dist_active(_NoXdistConfig()) is False


def test_dist_active_detector_with_parallel_flags() -> None:
    """`-n auto --dist loadfile` → True（`numprocesses` 未解析成 int 也认得出来）。"""

    class _XdistConfig:
        pluginmanager = _PluginManager(None, plugins={"xdist"})

        def getoption(self, name: str, default: Any = None) -> Any:
            return {"numprocesses": "auto", "dist": "loadfile"}.get(name, default)

    assert guard_module._xdist_dist_active(_XdistConfig()) is True


def test_dist_active_detector_with_serial_flags() -> None:
    """不带并行参数 → False（`-n` 缺席、`--dist no`）。"""

    class _SerialConfig:
        pluginmanager = _PluginManager(None, plugins={"xdist"})

        def getoption(self, name: str, default: Any = None) -> Any:
            return {"numprocesses": None, "dist": "no"}.get(name, default)

    assert guard_module._xdist_dist_active(_SerialConfig()) is False
