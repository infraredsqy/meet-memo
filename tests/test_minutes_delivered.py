"""`delivered` 这一档的流转用例：交付即完事、按产物判完成、不许重复拉起。

用例一律注入假探针与假交付件，**一条都不连真实的 3080**（真往那台打的请求由
`tests/conftest.py` 的第七道兜底拦下）；也不调用系统上的 `dsh`。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from meeting import config as config_module
from meeting.minutes import dsh_web
from meeting.minutes import runner as runner_module
from meeting.minutes import status as status_module
from meeting.minutes.launch import LaunchResult
from meeting.minutes.status import (
    STATE_DELIVERED,
    STATE_DONE,
    STATE_FAILED,
    STATE_RUNNING,
    STATUS_NAME,
)

from tests import _helpers as helpers
from tests._minutes_fakes import GOOD_FINAL

#: 交付这一步用的那个地址（用例自己塞进配置，不会是真实那一台）
FAKE_DSH_URL = "http://127.0.0.1:54321"


def _transcripts(session: Path) -> Path:
    """造一个两路转写都在的场次目录（runner 要走完找输入、分块、渲染那几步）。"""
    session.mkdir(parents=True, exist_ok=True)
    (session / "meeting_transcript_mic.md").write_text(
        "\n".join(f"[21:{minute:02d}] mic 第 {minute} 句。" for minute in range(25)) + "\n",
        encoding="utf-8",
    )
    (session / "meeting_offline_transcript_mic.md").write_text(
        "\n".join(f"[21:{minute:02d}] **S01**: mic 会后第 {minute} 句。" for minute in range(22))
        + "\n",
        encoding="utf-8",
    )
    (session / "meeting_transcript_loopback.md").write_text(
        "\n".join(f"[21:{minute:02d}] loopback 第 {minute} 句。" for minute in range(15)) + "\n",
        encoding="utf-8",
    )
    return session


def _write_final(session: Path) -> None:
    (session / "meeting_final.md").write_text(GOOD_FINAL, encoding="utf-8")


def _config(home: Path, values: dict[str, Any] | None = None) -> config_module.Config:
    """写一份临时 `config.json` 再 `load()` 一遍（家目录拿 `_helpers` 的口径隔离）。"""
    helpers.write_config_json(home, {"minutes.dsh_url": FAKE_DSH_URL, **(values or {})})
    return config_module.load(
        environ={"HOME": str(home), "USERPROFILE": str(home)},
        path=str(helpers.config_path_of(home)),
    )


def _delivered_status(session: Path, *, session_id: str = "session-abc") -> dict[str, Any]:
    """落一份 `delivered` 的终态状态（web 分支交付之后盘上就是这个形状）。"""
    return status_module.write_session_status(
        session,
        state=STATE_DELIVERED,
        mode="both",
        branch="web",
        started_at=status_module.now_iso(),
        finished_at=status_module.now_iso(),
        artifacts={"final": "meeting_final.md"},
        dsh={
            "branch": "web",
            "url": FAKE_DSH_URL,
            "workspace_id": "ws-260927",
            "session_id": session_id,
            "steps": ["workspace/create", "session/create", "session/prompt"],
        },
    )


class RecordingProbe:
    """假探针：记下被问的地址，按构造时给的那一档回答。"""

    def __init__(self, alive: bool, detail: str = "", *, raises: Exception | None = None) -> None:
        self.alive = alive
        self.detail = detail
        self.raises = raises
        self.calls: list[str] = []

    def __call__(self, url: str, **kwargs: Any) -> tuple[bool, str]:
        self.calls.append(str(url))
        if self.raises is not None:
            raise self.raises
        return self.alive, self.detail


class RecordingHandoff:
    """假交付件：真形状的 `HandoffResult`，并在被调到时看一眼盘上那一版状态。

    `observe` 是那一眼的落点（用例用它钉「交付那一刻盘上已经是 `running`、`branch` 已经是
    `web`」）；返回值一律是 `dsh_web.HandoffResult` —— 与产品路径交回的那一个同形。
    """

    def __init__(
        self,
        *,
        ok: bool = True,
        failure: str | None = None,
        error: str | None = None,
        observe: Any = None,
    ) -> None:
        self.ok = ok
        self.failure = failure
        self.error = error
        self.observe = observe
        self.calls: list[dict[str, Any]] = []
        self.seen_status: dict[str, Any] | None = None

    def __call__(
        self, session_dir: Any, *, prompt_text: str, url: str, timeout: float, **kwargs: Any
    ) -> dsh_web.HandoffResult:
        session = Path(session_dir)
        self.calls.append(
            {
                "session_dir": session,
                "prompt_text": prompt_text,
                "url": url,
                "timeout": timeout,
                "allow_dsh": bool(kwargs.get("allow_dsh")),
            }
        )
        status_path = session / STATUS_NAME
        self.seen_status = (
            status_module.read_status(status_path) if status_path.is_file() else None
        )
        if self.observe is not None:
            self.observe(session)
        return dsh_web.HandoffResult(
            ok=self.ok,
            url=str(url),
            seconds=0.01,
            workspace_id="ws-260927",
            workspace_title="260927",
            session_id="session-abc" if self.ok else "",
            failure=None if self.ok else (self.failure or dsh_web.FAILURE_PROMPT),
            error=None if self.ok else (self.error or "交付这一步报告失败"),
            steps=("workspace/create", "session/create", "session/prompt") if self.ok else (),
        )


class CountingLauncher:
    """headless 那一趟的替身：只记「有没有被拉到」（本文件不该走到它）。"""

    def __init__(self) -> None:
        self.calls: list[Path] = []

    def __call__(self, session_dir: Any, **kwargs: Any) -> LaunchResult:
        self.calls.append(Path(session_dir))
        return LaunchResult(
            ok=True,
            branch="headless",
            executable="",
            exit_code=0,
            seconds=0.0,
            log="working/minutes-run.log",
            prompt="working/run_prompt.md",
        )


@pytest.fixture(autouse=True)
def _no_test_process_reentry_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """摘掉 `tests/conftest.py` 那道全局再入闸标记（本文件的用例要把 `run()` 跑完）。"""
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)


def test_a_live_dsh_web_server_takes_the_delivery_branch(tmp_path: Path) -> None:
    """探到服务端在跑 → 交付、终态 `delivered`、`branch=web`，而且**不校验产物**。"""
    session = _transcripts(tmp_path / "260927")
    config = _config(tmp_path / "home")
    probe = RecordingProbe(True)
    handoff = RecordingHandoff()
    headless = CountingLauncher()

    document = runner_module.run(
        session,
        config=config,
        deliver=True,
        launcher=headless,
        probe=probe,
        handoff=handoff,
    )

    assert document["state"] == STATE_DELIVERED
    assert document["branch"] == "web"
    assert document["mode"] == "both"
    assert document["dsh"]["session_id"] == "session-abc"
    assert probe.calls == [FAKE_DSH_URL], "探的是配置里那个地址"
    assert headless.calls == [], "走交付那一趟就不该再起 headless 子进程"
    assert not (session / "meeting_final.md").exists(), "这一趟不等产物、也不校验它"
    assert len(handoff.calls) == 1
    assert "## 说话人附录" in handoff.calls[0]["prompt_text"], "送过去的是渲染好的提示词全文"
    assert "{{" not in handoff.calls[0]["prompt_text"], "模板占位符已经渲染干净"


def test_the_running_version_is_on_disk_before_the_delivery(tmp_path: Path) -> None:
    """交付那一刻盘上已经是 `running`，而且 `branch` 已经是实际走的 `web`。"""
    session = _transcripts(tmp_path / "260927")
    config = _config(tmp_path / "home")
    handoff = RecordingHandoff()

    runner_module.run(
        session,
        config=config,
        deliver=True,
        launcher=CountingLauncher(),
        probe=RecordingProbe(True),
        handoff=handoff,
    )

    assert handoff.seen_status is not None, "交付之前盘上必须已经有那一版 running"
    assert handoff.seen_status["state"] == STATE_RUNNING
    assert handoff.seen_status["branch"] == "web", "在飞那一版也记实际走的那一条"
    assert handoff.seen_status["finished_at"] == "", "还在跑的那一版收尾时刻空着"


def test_a_dead_dsh_web_server_falls_back_to_headless(tmp_path: Path) -> None:
    """探到没在跑 → 走现成的 headless 那一趟，终态 `done`（那一条的判据照旧）。"""
    session = _transcripts(tmp_path / "260927")
    config = _config(tmp_path / "home")
    probe = RecordingProbe(False, "端口没人应答")
    handoff = RecordingHandoff()
    headless = CountingLauncher()

    document = runner_module.run(
        session,
        config=config,
        deliver=True,
        launcher=headless,
        probe=probe,
        handoff=handoff,
    )

    assert document["state"] == STATE_DONE
    assert document["branch"] == "headless"
    assert headless.calls == [session]
    assert handoff.calls == []
    assert probe.calls == [FAKE_DSH_URL]


def test_a_broken_probe_falls_back_to_headless(tmp_path: Path) -> None:
    """探针自己炸了也按「没在跑」算：这一趟落回 headless，不许被探针带走。"""
    session = _transcripts(tmp_path / "260927")
    config = _config(tmp_path / "home")
    probe = RecordingProbe(False, raises=RuntimeError("探针写坏了"))
    headless = CountingLauncher()

    document = runner_module.run(
        session,
        config=config,
        deliver=True,
        launcher=headless,
        probe=probe,
        handoff=RecordingHandoff(),
    )

    assert document["state"] == STATE_DONE
    assert headless.calls == [session]


def test_a_failed_delivery_is_recorded_as_failed(tmp_path: Path) -> None:
    """交付失败如实记 `failed`，**不偷偷改走 headless**（交付失败要看得见）。"""
    session = _transcripts(tmp_path / "260927")
    config = _config(tmp_path / "home")
    handoff = RecordingHandoff(
        ok=False, failure=dsh_web.FAILURE_SESSION, error="session/create 失败：没了"
    )
    headless = CountingLauncher()

    document = runner_module.run(
        session,
        config=config,
        deliver=True,
        launcher=headless,
        probe=RecordingProbe(True),
        handoff=handoff,
    )

    assert document["state"] == STATE_FAILED
    assert "session/create" in document["error"]
    assert document["dsh"]["failure"] == dsh_web.FAILURE_SESSION
    assert headless.calls == [], "交付失败也不改走另一条路"


def test_the_child_gate_skips_before_any_probe(tmp_path: Path) -> None:
    """子会话闸在最前面：这一档连探针都不问（它是 DSH 子会话自己，什么都不该拉起）。"""
    session = _transcripts(tmp_path / "260927")
    probe = RecordingProbe(True)
    handoff = RecordingHandoff()

    document = runner_module.run(
        session,
        environ={"MEETING_MINUTES_CHILD": "1"},
        probe=probe,
        handoff=handoff,
        launcher=CountingLauncher(),
    )

    assert document["state"] == status_module.STATE_SKIPPED
    assert probe.calls == []
    assert handoff.calls == []
    assert not (session / STATUS_NAME).exists(), "这一档不写状态文件"


def test_the_status_round_trip_keeps_delivered(tmp_path: Path) -> None:
    """`delivered` 写得进、读得回，而且它在四档里（读方不拒收）。"""
    session = tmp_path / "260927"
    session.mkdir()

    written = _delivered_status(session)
    read_back = status_module.read_status(session / STATUS_NAME)

    assert STATE_DELIVERED in status_module.STATES
    assert written["state"] == STATE_DELIVERED
    assert read_back["state"] == STATE_DELIVERED
    assert read_back["dsh"]["session_id"] == "session-abc"


def test_the_gate_treats_a_delivered_meeting_with_its_final_as_done(tmp_path: Path) -> None:
    """交付过、产物也过四条判据 → 这一场就是完成了（`already_done`，`state` 照实报 `delivered`）。"""
    from meeting.offline import minutes_tail

    session = tmp_path / "260927"
    session.mkdir()
    _delivered_status(session)
    _write_final(session)

    gate = minutes_tail._minutes_gate(session)

    assert gate is not None
    assert gate["hook"] == "already_done"
    assert gate["state"] == STATE_DELIVERED, "状态那一格照盘上如实报"


def test_the_gate_refuses_a_second_session_while_a_delivery_is_pending(tmp_path: Path) -> None:
    """交付过、产物还没落 → 交回 `delivered`（这就是「不许重复拉起」那一档）。"""
    from meeting.offline import minutes_tail

    session = tmp_path / "260927"
    session.mkdir()
    _delivered_status(session)

    gate = minutes_tail._minutes_gate(session)

    assert gate is not None, "这一档必须拦下第二个会话"
    assert gate["hook"] == "delivered"
    assert gate["state"] == STATE_DELIVERED
    assert gate["announced"] is False


def test_the_gate_still_lets_everything_else_through(tmp_path: Path) -> None:
    """其余四档照旧放行给启动器：`done` 缺产物、`running`、`failed`、没有状态文件。"""
    from meeting.offline import minutes_tail

    session = tmp_path / "260927"
    session.mkdir()

    status_module.write_session_status(
        session, state=STATE_DONE, mode="both", branch="headless", started_at="2026-09-27T21:00:00"
    )
    assert minutes_tail._minutes_gate(session) is None, "done 但产物没落：放行重跑"

    _write_final(session)
    gate = minutes_tail._minutes_gate(session)
    assert gate is not None and gate["hook"] == "already_done"
    assert minutes_tail._minutes_gate(session, force=True) is None, "--force 无视终态"

    status_module.write_session_status(
        session, state=STATE_FAILED, mode="both", branch="headless", started_at="2026-09-27T21:00:00"
    )
    assert minutes_tail._minutes_gate(session) is None, "failed 那一档得让它再试"

    status_module.write_session_status(
        session,
        state=STATE_DELIVERED,
        mode="both",
        branch="web",
        started_at="2026-09-27T21:00:00",
        dsh={"session_id": "session-abc"},
    )
    (session / "meeting_final.md").unlink()
    assert minutes_tail._minutes_gate(session) is not None, "交付过而产物没落：拦下"


def test_the_announce_path_does_not_launch_a_second_session(tmp_path: Path) -> None:
    """产品路径那一头同样不拉起第二个会话：`_announce_minutes()` 交回 `delivered`，启动器零调用。"""
    from meeting.offline import minutes_tail

    session = tmp_path / "260927"
    session.mkdir()
    _delivered_status(session)
    calls: list[str] = []

    outcome = minutes_tail._announce_minutes(
        session, launcher=lambda target: calls.append(target), announce_minutes=True
    )

    assert outcome["hook"] == "delivered"
    assert outcome["announced"] is False
    assert calls == [], "已经交付过的一档不许再拉起一个子会话"
    assert "force" in str(outcome["reason"]), "理由里给出路"


def test_the_cli_reads_completion_from_the_artifact_and_the_delivery_from_the_state(
    tmp_path: Path,
) -> None:
    """`meeting post` 那一侧：完成看产物、`delivered` 看状态那一格，两者各管一件事。"""
    from meeting import post_run as post_run_module

    session = tmp_path / "260927"
    session.mkdir()
    status = _delivered_status(session)

    assert post_run_module._minutes_is_delivered(status) is True
    assert post_run_module._minutes_layer_done(session, status) is False, "产物还没落就不算完成"

    _write_final(session)
    assert post_run_module._minutes_layer_done(session, status) is True, "产物一落就算完成"

    done = {"state": STATE_DONE, "status_name": STATUS_NAME}
    (session / "meeting_final.md").unlink()
    assert post_run_module._minutes_layer_done(session, done) is False, "done 也得看产物"

    failed = {"state": STATE_FAILED, "status_name": STATUS_NAME}
    _write_final(session)
    assert post_run_module._minutes_layer_done(session, failed) is False, "failed 那一档不算完成"
    assert post_run_module._minutes_is_delivered(failed) is False


def test_a_delivery_counts_as_success_for_the_exit_code(tmp_path: Path) -> None:
    """退出码那一格：`delivered` 按成功算，`failed` 照旧按失败算。"""
    from meeting import cli as cli_module
    from meeting import post_run as post_run_module

    session = tmp_path / "260927"
    session.mkdir()
    _delivered_status(session)

    outcome = post_run_module._skipped_minutes_outcome(
        session, _delivered_status(session), running=False, delivered=True
    )
    assert outcome["hook"] == "delivered"
    assert outcome["skipped"] is True
    assert "session-abc" in outcome["reason"], "理由点名是哪一条 DSH 会话"
    assert post_run_module._minutes_exit_code(outcome) == cli_module.EXIT_OK
    assert (
        post_run_module._minutes_exit_code({"hook": "finished", "state": STATE_FAILED})
        == cli_module.EXIT_FAILURE
    )
    assert (
        post_run_module._minutes_exit_code({"hook": "timeout", "state": STATE_RUNNING})
        == cli_module.EXIT_FAILURE
    )
