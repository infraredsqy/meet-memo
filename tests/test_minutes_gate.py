"""隔离闸的守卫与接线用例（三层，缺一不可）。

背景（2026-09 夜的事故）：一个复现脚本调了真的 `run_session()` 顶层入口，收尾按设计通知了
纪要层；纪要层探到机主正在跑的 DSH web 服务端，走 web 分支**真交付** —— DSH 侧多出工作区、
按纪要流程拉起多个 subagent。机主要求在代码层把这件事封死：**整个项目与 DSH 隔离，只有
两条人工入口（`meeting post` 与「直接出纪要」按钮）显式放行**。

外加**接线完整性**：两条人工路径各自的「传 True」用替身的调用登记钉死 —— 人工链路已验证过
无数次，不重新端到端，但必须保证它还是通的。

全部用例走替身 / 假件：不起真 DSH、不发真网络请求、不加载真模型。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from meeting import cli as cli_module
from meeting import post_run as post_run_module
from meeting.backends import minutes as minutes_backend
from meeting.backends import registry as registry_module
from meeting.minutes import dsh_web
from meeting.minutes import launch as launch_module
from meeting.minutes import runner as runner_module
from meeting.minutes.launch import LaunchResult
from meeting.minutes.status import (
    STATE_DELIVERED,
    STATE_DONE,
    STATUS_NAME,
    DeliveryForbidden,
    read_status,
)
from meeting.offline import minutes_tail
from meeting.offline import runner as offline_runner

from tests.conftest import RealDshDeliveryForbidden
from tests._offline_fakes import FakeConfig, FakeEngine, synth_session


def _transcripts(session: Path) -> Path:
    """造一个两路转写都在的场次目录（`runner.run()` 要走完找输入、分块、渲染那几步）。"""
    session.mkdir(parents=True, exist_ok=True)
    (session / "meeting_transcript_mic.md").write_text(
        "\n".join(f"[21:{minute:02d}] mic 会中第 {minute} 句。" for minute in range(6)) + "\n",
        encoding="utf-8",
    )
    (session / "meeting_offline_transcript_mic.md").write_text(
        "\n".join(f"[21:{minute:02d}] **S01**: mic 会后第 {minute} 句。" for minute in range(5))
        + "\n",
        encoding="utf-8",
    )
    return session


class RecordingCall:
    """DSH 触达点的登记替身（探针 / 交付 / headless 拉起各用一份）。"""

    def __init__(self, *, outcome: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._outcome = outcome

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append({"args": args, "kwargs": kwargs})
        return self._outcome


def _headless_ok() -> LaunchResult:
    """一份「跑成了」的启动器记录（与 `RecordingLauncher` 交回的那个同形）。"""
    return LaunchResult(
        ok=True,
        branch="headless",
        executable=sys.executable,
        exit_code=0,
        seconds=0.01,
        log="working/minutes-run.log",
        prompt="working/run_prompt.md",
    )


def test_run_session_by_default_launches_no_minutes_layer(tmp_path) -> None:
    """守卫用例：**缺省参数**调 `run_session()` —— 纪要层零启动、盘上零纪要状态文件。

    这一条就是那场事故的回归钉：复现脚本直接调顶层入口、什么开关都不给，尾巴上必须
    安静跳过（钩子连解析都不解析、状态文件一个都不落）。
    """
    session = synth_session(tmp_path / "out")

    summary = offline_runner.run_session(
        session, engine=FakeEngine(), config=FakeConfig(**{"post.chunk_seconds": 450})
    )

    assert summary["state"] in ("done", "failed"), summary.get("error")
    assert summary["minutes"]["hook"] == "skipped", summary["minutes"]
    assert summary["minutes"]["announced"] is False
    assert "隔离" in str(summary["minutes"]["reason"]), summary["minutes"]
    assert not (session / STATUS_NAME).exists(), "盘上零纪要状态文件"
    assert not (session / "working").exists(), "切块与提示词一步都不该发生"


def test_run_session_still_announces_when_the_human_flag_is_on(tmp_path, monkeypatch) -> None:
    """`announce_minutes=True`（人工入口那一格）时尾巴照旧通知：闸只拦非人工那一档。"""
    session = synth_session(tmp_path / "out")
    seen: list[dict[str, Any]] = []

    def recording_announce(*args: Any, **kwargs: Any) -> dict[str, Any]:
        seen.append(dict(kwargs))
        return {"hook": "launched", "announced": True}

    monkeypatch.setattr(minutes_tail, "_announce_minutes", recording_announce)

    summary = offline_runner.run_session(
        session,
        engine=FakeEngine(),
        config=FakeConfig(**{"post.chunk_seconds": 450}),
        announce_minutes=True,
    )

    assert summary["minutes"]["hook"] == "launched"
    assert seen and seen[0]["announce_minutes"] is True, seen


def test_hook_launch_without_deliver_skips_before_mark_running(tmp_path, monkeypatch) -> None:
    """`deliver` 缺省假：返回值如实表达 `skipped`，盘上零状态文件、零线程、`run()` 零调用。"""
    session = _transcripts(tmp_path / "250919")
    runs: list[Any] = []
    monkeypatch.setattr(runner_module, "run", lambda *args, **kwargs: runs.append(kwargs))

    outcome = minutes_backend.MinutesHook().launch(str(session))

    assert outcome is not None and outcome["state"] == "skipped", outcome
    assert outcome["announced"] is False
    assert "隔离" in str(outcome["reason"]), outcome
    assert not (session / STATUS_NAME).exists(), "mark_running() 之前就返回：盘上零状态文件"
    assert not (session / "working").exists(), "不切块、不渲染提示词"
    assert runs == [], "后台那一趟一次都不该被起"
    assert not any(
        thread.name == minutes_backend.THREAD_NAME for thread in threading.enumerate()
    ), "连线程都不该建"


def test_hook_launch_gate_takes_precedence_over_force(tmp_path) -> None:
    """`force=True` 也压不过隔离闸：非人工发起的 `--force` 照样安静跳过。"""
    session = _transcripts(tmp_path / "250919")

    outcome = minutes_backend.MinutesHook().launch(str(session), force=True)

    assert outcome is not None and outcome["state"] == "skipped"
    assert not (session / STATUS_NAME).exists()


def test_hook_launch_with_deliver_claims_and_runs(tmp_path, monkeypatch) -> None:
    """`deliver=True`（人工那一档）照旧 claim + 起线程：闸加在它们上游，语义一个字不动。"""
    session = _transcripts(tmp_path / "250919")
    monkeypatch.setattr(runner_module, "run", lambda *args, **kwargs: {"state": "skipped"})

    outcome = minutes_backend.MinutesHook().launch(str(session), deliver=True)

    assert outcome is None, "放行那一趟的返回值照旧是 None（起线程就返回）"
    written = read_status(session / STATUS_NAME)
    assert written["state"] == "running", "在飞那一版照旧落在 launch() 返回之前"


def test_the_conftest_delivery_guard_stubs_are_in_place() -> None:
    """第八道守卫（autouse）：三个 DSH 触达原语在本文件里都被换成了 conftest 的桩。"""
    assert dsh_web.probe.__module__ == "tests.conftest", dsh_web.probe
    assert dsh_web.handoff.__module__ == "tests.conftest", dsh_web.handoff
    assert launch_module.launch_headless.__module__ == "tests.conftest", (
        launch_module.launch_headless
    )


def test_the_conftest_guard_stubs_record_and_raise(_no_real_dsh_delivery) -> None:
    """桩的行为：登记 + 抛 `RealDshDeliveryForbidden`（就算带了放行参数也拦）。

    本条是守卫自己的自证：它**故意**碰一次桩，随后把记录里那一笔当场认领（清掉）——
    不认领的话，收场复查会把这条自证用例自己判红。
    """
    with pytest.raises(RealDshDeliveryForbidden, match="DSH 触达原语"):
        dsh_web.probe("http://127.0.0.1:9", allow_dsh=True)
    assert len(_no_real_dsh_delivery) == 1, "这一笔该被记进记录"
    assert "dsh_web.probe" in _no_real_dsh_delivery[0]
    _no_real_dsh_delivery.clear()


def test_probe_without_the_allow_flag_refuses_before_any_tcp(allow_dsh_protocol_stubs) -> None:
    """`probe()` 缺省拒绝：TCP 一次都不发（connect 替身零调用），当场抛 `DeliveryForbidden`。

    这一条量的是**产品那道闸**，因此显式请守卫的豁免夹具（不打桩），让调用落到真函数上。
    """
    connects: list[Any] = []

    def recorder(endpoint: Any, timeout: float) -> Any:
        connects.append(endpoint)
        raise AssertionError("探针在放行之前不该碰 socket")

    with pytest.raises(DeliveryForbidden, match="隔离"):
        dsh_web.probe("http://127.0.0.1:9", connect=recorder)

    assert connects == []


def test_handoff_without_the_allow_flag_refuses_before_any_disk_or_network(
    tmp_path: Path, allow_dsh_protocol_stubs
) -> None:
    """`handoff()` 缺省拒绝：目录不建、凭据不读、三次 RPC 一次都不发。"""
    session = tmp_path / "250919"

    with pytest.raises(DeliveryForbidden, match="隔离"):
        dsh_web.handoff(session, prompt_text="正文。", url="http://127.0.0.1:9")

    assert not session.exists(), "连目录都不该被建出来"


def test_launch_headless_without_the_allow_flag_refuses_before_any_subprocess(
    tmp_path: Path, allow_dsh_protocol_stubs
) -> None:
    """`launch_headless()` 缺省拒绝：提示词不写盘、可执行文件不解析、子进程不起。"""
    session = tmp_path / "250919"
    wheres: list[str] = []

    with pytest.raises(DeliveryForbidden, match="隔离"):
        launch_module.launch_headless(
            session, prompt_text="正文。", which=lambda name: wheres.append(name)
        )

    assert wheres == [], "连 `shutil.which` 都不该被走到"
    assert not (session / "working" / "run_prompt.md").exists()


def test_runner_run_without_deliver_refuses_before_any_disk_write(tmp_path, monkeypatch) -> None:
    """`minutes.runner.run()` 缺省拒绝：状态文件一个都不写、探针一次都不发。"""
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)  # 再入闸在先，摘掉它才量得到这一道
    session = _transcripts(tmp_path / "250919")
    probes: list[Any] = []

    with pytest.raises(DeliveryForbidden, match="隔离"):
        runner_module.run(session, probe=lambda *a, **k: probes.append((a, k)))

    assert probes == []
    assert not (session / STATUS_NAME).exists(), "连那份 running 都不落"
    assert not (session / "working").exists()


def test_runner_run_hands_the_allow_flag_to_the_web_primitives(tmp_path, monkeypatch) -> None:
    """放行标记下传（web 分支）：探针与交付的替身都收到 `allow_dsh=True`。"""
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)
    session = _transcripts(tmp_path / "250919")
    probe = RecordingCall(outcome=(True, ""))
    handoff = RecordingCall(
        outcome=dsh_web.HandoffResult(ok=True, url="http://127.0.0.1:9", seconds=0.0)
    )

    document = runner_module.run(
        session, deliver=True, probe=probe, handoff=handoff, launcher=RecordingCall()
    )

    assert document["state"] == STATE_DELIVERED
    assert probe.calls and probe.calls[0]["kwargs"]["allow_dsh"] is True, probe.calls
    assert handoff.calls and handoff.calls[0]["kwargs"]["allow_dsh"] is True, handoff.calls


def test_runner_run_hands_the_allow_flag_to_the_headless_launcher(tmp_path, monkeypatch) -> None:
    """放行标记下传（headless 分支）：探针与启动器的替身都收到 `allow_dsh=True`。"""
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)
    session = _transcripts(tmp_path / "250919")
    probe = RecordingCall(outcome=(False, "端口没人应答"))
    launcher = RecordingCall(outcome=_headless_ok())

    document = runner_module.run(
        session,
        deliver=True,
        probe=probe,
        handoff=RecordingCall(),
        launcher=launcher,
        executable="fake",
    )

    assert document["state"] == STATE_DONE
    assert probe.calls and probe.calls[0]["kwargs"]["allow_dsh"] is True, probe.calls
    assert launcher.calls and launcher.calls[0]["kwargs"]["allow_dsh"] is True, launcher.calls


def test_hook_launch_hands_deliver_down_to_the_runner(tmp_path, monkeypatch) -> None:
    """`launch(deliver=True)` → 线程体 → `run(..., deliver=True)`：钩子到编排层那一格下传。"""
    session = _transcripts(tmp_path / "250919")
    runs: list[dict[str, Any]] = []
    entered = threading.Event()

    def recording_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        runs.append({"args": args, "kwargs": kwargs})
        entered.set()
        return {"state": "skipped"}

    monkeypatch.setattr(runner_module, "run", recording_run)

    minutes_backend.MinutesHook().launch(str(session), deliver=True)

    assert entered.wait(5.0), "后台线程该把 run() 调到"
    assert runs and runs[0]["kwargs"]["deliver"] is True, runs


def test_offline_tail_hands_announce_down_to_the_hook(tmp_path, monkeypatch) -> None:
    """`_announce_minutes(announce_minutes=True)` → 真实现的 `launch(deliver=True)`：尾巴下传。"""
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)  # 摘掉全局兜底标记（它先一步跳过）
    session = _transcripts(tmp_path / "250919")
    launches: list[dict[str, Any]] = []

    class RecordingImplementation:
        def launch(self, target: str, **kwargs: Any) -> None:
            launches.append({"target": str(target), **kwargs})

    class FakeResolution:
        selector = "fake"
        has_implementation = True
        implementation = RecordingImplementation()

    monkeypatch.setattr(registry_module, "resolve_minutes_hook", lambda *a, **k: FakeResolution())

    outcome = minutes_tail._announce_minutes(session, announce_minutes=True)

    assert outcome["hook"] == "launched"
    assert launches == [{"target": str(session), "force": False, "deliver": True}], launches


def test_offline_tail_without_the_human_flag_stays_quiet(tmp_path) -> None:
    """`_announce_minutes` 缺省假：不解析钩子、不调启动器、返回 skipped（对照既有安静跳过档）。"""
    session = _transcripts(tmp_path / "250919")

    outcome = minutes_tail._announce_minutes(session)

    assert outcome == {
        "hook": "skipped",
        "announced": False,
        "reason": "纪要层隔离：本次非人工发起，跳过",
    }


def test_meeting_post_passes_the_human_flag_on_both_paths(tmp_path, monkeypatch) -> None:
    """**接线钉死（命令路径）**：`meeting post` 首跑与「只补纪要」两条路都把 True 递进尾巴。

    换掉 `_announce_minutes` 登记：首跑那一趟经 `run_session(announce_minutes=True)` 的
    尾巴进来，「离线段已终态、只补纪要」那一趟经 `_launch_minutes_on_the_product_path()`
    进来 —— 两笔登记都必须带着 `announce_minutes=True`。少传的哪一条路，闸一开就全哑。
    """
    session = synth_session(tmp_path / "250919")
    calls: list[dict[str, Any]] = []

    def recording_announce(session_dir: Path, **kwargs: Any) -> dict[str, Any]:
        calls.append({"dir": str(session_dir), **kwargs})
        return {"hook": "skipped", "announced": False, "reason": "接线测试桩"}

    monkeypatch.setattr(minutes_tail, "_announce_minutes", recording_announce)

    first = post_run_module.run_post(session, engine=FakeEngine())
    second = post_run_module.run_post(session, engine=FakeEngine())

    assert len(calls) == 2, calls
    for call in calls:
        assert call["announce_minutes"] is True, call
    assert isinstance(first["exit_code"], int) and isinstance(second["exit_code"], int)
