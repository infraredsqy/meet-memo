"""`meeting/minutes/runner.py` / `inputs.py` / `status.py` 与两个钩子的契约用例。

要盯的八条（对应任务书 phase2 的完成判定与给复审的自检项）：

  1. **一次成功的运行**：找输入 → `working/inputs.json` → 切块 → 渲染 → 拉起 → 校验 → 落状态；
  2. **一条转写都没有**：不启动 DSH、状态记 `failed` + 原因（不许静默出空纪要）；
  3. **三档失败里的两档**（假启动器那一档在 `test_minutes_launch.py` 里）：切块/渲染抛错、
     启动器报失败，都记 `failed`；
  4. **再入闸**：环境里有 `MEETING_MINUTES_CHILD=1` 时**没拉起 DSH**（假启动器记「被调用过」）；
  5. **状态文件与离线那一份是两份**：跑一次纪要生成不改动离线那三份产物（拿哈希比）；
  6. **`minutes.hook = null`**：离线跑完不产生纪要状态文件；离线尾钩被解析成缺席档；
  7. **配置键**：四个键的默认值、取值域、三分表登记；临时 `config.json` 里
     `minutes.chunk_lines = 40` 被认、`= 5` 被拒（越界）；
  8. **惰性**：全新解释器里 `import meeting.minutes.launch` 等三个新模块不拉重依赖。

全部用例都用**假启动器**或**假可执行文件**，一条都不调用系统上的 `dsh`，也不加载真模型。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from meeting import config as config_module
from meeting.backends import minutes as minutes_backend
from meeting.backends.registry import resolve_minutes_hook
from meeting.minutes import inputs as inputs_module
from meeting.minutes import prompt as prompt_module
from meeting.minutes import runner as runner_module
from meeting.minutes import status as status_module
from meeting.minutes.launch import (
    ARTIFACT_LOG,
    FINAL_NAME,
    LaunchResult,
    check_final_artifact,
)
from meeting.minutes.inputs import write_text_atomic
from meeting.minutes.status import (
    ARTIFACT_FINAL,
    ARTIFACT_INPUTS,
    STATE_DONE,
    STATE_FAILED,
    STATE_RUNNING,
    STATE_SKIPPED,
    STATUS_NAME,
)

from tests import _helpers as helpers
from tests._minutes_fakes import GOOD_FINAL

#: 后台线程收敛的轮询上限（事件驱动地等，等到了就立刻回）
CONVERGE_TIMEOUT_SECONDS = 10.0

#: 离线那三份产物（跑一次纪要生成之后逐字节比对的就是这三份 + 状态文件）
OFFLINE_ARTIFACTS = (
    "meeting_offline_transcript_mic.md",
    "meeting_offline_speakers_mic.json",
    "meeting_offline_report.md",
    "meeting_offline_status.json",
)


def _status_on_disk(session: Path) -> dict | None:
    """盘上那一份状态（还没有那份文件时交回 `None`）。"""
    path = session / STATUS_NAME
    return status_module.read_status(path) if path.is_file() else None


def _session(tmp_path: Path, session_id: str = "250919") -> Path:
    """造一个两路转写都在的场次目录（会中 + 会后，各几行）。"""
    session = tmp_path / session_id
    session.mkdir(parents=True, exist_ok=True)
    (session / "meeting_transcript_mic.md").write_text(
        "\n".join(f"[21:{minute:02d}] mic 会中第 {minute} 句。" for minute in range(25)) + "\n",
        encoding="utf-8",
    )
    (session / "meeting_offline_transcript_mic.md").write_text(
        "\n".join(f"[21:{minute:02d}] **S01**: mic 会后第 {minute} 句。" for minute in range(22))
        + "\n",
        encoding="utf-8",
    )
    (session / "meeting_transcript_loopback.md").write_text(
        "\n".join(f"[21:{minute:02d}] loopback 会中第 {minute} 句。" for minute in range(15)) + "\n",
        encoding="utf-8",
    )
    return session


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dead_probe(url: str, **kwargs) -> tuple[bool, str]:
    """假探针：DSH web「没在跑」（`run()` 落回 headless 分支，且一次真 TCP 都不发）。"""
    return False, "用例假探针（DSH web 没在跑）"


class RecordingLauncher:
    """假启动器：记下被调用过没有、参数是什么，按场景写不写那份纪要。

    真的启动器还会把提示词正文写到 `working/run_prompt.md`（argv 里只放指针），
    这一份也跟着写：状态里 `artifacts` 三格指的是盘上真的有的那三样。
    """

    def __init__(self, *, write_final: bool = True, fail: str | None = None) -> None:
        self.calls: list[dict] = []
        self.write_final = write_final
        self.fail = fail

    def __call__(self, session_dir, **kwargs) -> LaunchResult:
        self.calls.append({"session_dir": Path(session_dir), **kwargs})
        directory = Path(session_dir)
        write_text_atomic(
            directory / "working" / "run_prompt.md", str(kwargs.get("prompt_text") or "")
        )
        if self.write_final:
            (directory / FINAL_NAME).write_text(GOOD_FINAL, encoding="utf-8")
        if self.fail:
            return LaunchResult(
                ok=False,
                branch="headless",
                executable="",
                exit_code=None,
                seconds=0.0,
                log=ARTIFACT_LOG,
                prompt="working/run_prompt.md",
                failure=self.fail,
                error=f"启动器报了这一档：{self.fail}",
            )
        return LaunchResult(
            ok=True,
            branch="headless",
            executable=sys.executable,
            exit_code=0,
            seconds=0.01,
            log=ARTIFACT_LOG,
            prompt="working/run_prompt.md",
        )


def _load_config(home: Path, values: dict) -> config_module.Config:
    """写一份临时 `config.json` 再 `load()` 一遍（配置隔离靠家目录，与 `_helpers` 同口径）。"""
    helpers.write_config_json(home, values)
    return config_module.load(
        environ={"HOME": str(home), "USERPROFILE": str(home)},
        path=str(helpers.config_path_of(home)),
    )


@pytest.fixture(autouse=True)
def _no_test_process_reentry_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """本文件要把 `run()` 跑到正常收官，因此摘掉 `tests/conftest.py` 那道兜底标记。

    全局兜底立着 `MEETING_MINUTES_CHILD=1`（挡住任何真 DSH 子进程），而 `run()` 的第一件事
    就是认它 —— 立着的话每条用例都会当场返回 `skipped`。本文件的用例一律注入**假启动器**，
    因此摘掉它也不会起任何真进程；要验再入闸本身的那两条用例自己把标记递进去。
    """
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)


# ---------------------------------------------------------------------------
# 一、一次成功的运行
# ---------------------------------------------------------------------------


def test_a_successful_run_writes_the_inputs_the_chunks_and_the_status(tmp_path: Path) -> None:
    """一次成功的运行：`working/inputs.json` + 分块 + 提示词 + 状态文件四样齐。"""
    session = _session(tmp_path)
    launcher = RecordingLauncher()

    document = runner_module.run(
        session, launcher=launcher, deliver=True, probe=_dead_probe
    )

    assert document["state"] == STATE_DONE
    assert document["mode"] == "both"
    assert document["branch"] == "headless", "实际走的是 headless（配置里的 auto 不记这一格）"
    assert document["artifacts"] == {
        "final": ARTIFACT_FINAL,
        "inputs": ARTIFACT_INPUTS,
        "log": ARTIFACT_LOG,
    }
    assert document["dsh"]["exit_code"] == 0
    assert document["chunks"]["mic"]["live"]["total_lines"] == 25, "会中稿那一份的行数"
    assert document["chunks"]["mic"]["offline"]["total_lines"] == 22, "会后稿那一份的行数"
    assert document["chunks"]["mic"]["live"]["chunk_count"] == 1
    assert document["chunks"]["mic"]["offline"]["chunk_count"] == 1
    assert document["chunks"]["loopback"]["live"]["chunk_count"] == 1
    assert "offline" not in document["chunks"]["loopback"], "这一路没有会后稿：不伪造一份"
    assert launcher.calls and launcher.calls[0]["session_dir"] == session
    # 盘上的四样（两份稿的块各占一个目录）
    assert (session / "working" / "inputs.json").is_file()
    assert (session / "working" / "mic" / "live" / "00_index.md").is_file()
    assert (session / "working" / "mic" / "offline" / "00_index.md").is_file()
    assert (session / "working" / "run_prompt.md").is_file()
    assert (session / STATUS_NAME).is_file()


def test_the_inputs_document_matches_the_spec_shape(tmp_path: Path) -> None:
    """`inputs.json` 的形状与 spec 那张表逐格对上（行数用 `lines.count_lines()` 的口径）。"""
    session = _session(tmp_path)

    document = runner_module.run(
        session, launcher=RecordingLauncher(), deliver=True, probe=_dead_probe
    )

    written = json.loads((session / "working" / "inputs.json").read_text(encoding="utf-8"))
    assert written["schema_version"] == 1
    assert written["session_id"] == "250919"
    assert written["mode"] == "both"
    assert written["working_dir"] == "working"
    assert written["generated_at"]
    sources = {item["source"]: item for item in written["sources"]}
    assert set(sources) == {"mic", "loopback"}
    assert sources["mic"]["offline"] == {
        "file": "meeting_offline_transcript_mic.md",
        "lines": 22,
    }
    assert sources["mic"]["live"] == {"file": "meeting_transcript_mic.md", "lines": 25}
    assert sources["mic"]["kind"] == "both"
    assert sources["loopback"]["kind"] == "live_only"
    assert written["mode"] == document["mode"]


def test_the_read_side_rejects_an_unknown_inputs_version(tmp_path: Path) -> None:
    """读方拒收不认识的 `schema_version`（与别的产物同一条口径）。"""
    path = tmp_path / "inputs.json"
    document = {
        "schema_version": 99,
        "session_id": "250919",
        "mode": "both",
        "sources": [],
    }
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(inputs_module.InputsError, match="主版本号不认识"):
        inputs_module.read_inputs(path)


def test_the_status_file_round_trips_and_rejects_a_foreign_version(tmp_path: Path) -> None:
    """状态文件写得出、读得回；不认识的版本与缺字段都拒收。"""
    session = _session(tmp_path)
    runner_module.run(
        session, launcher=RecordingLauncher(), deliver=True, probe=_dead_probe
    )
    path = session / STATUS_NAME

    written = status_module.read_status(path)

    assert written["state"] == STATE_DONE
    assert written["schema_version"] == 1
    bad = dict(written, schema_version=7)
    path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(status_module.StatusError, match="主版本号不认识"):
        status_module.read_status(path)


# ---------------------------------------------------------------------------
# 二、一条转写都没有 / 切块渲染失败 / 启动器报失败
# ---------------------------------------------------------------------------


def test_a_session_without_inputs_never_starts_dsh(tmp_path: Path) -> None:
    """一条转写都没有 → **不启动 DSH**，状态记 `failed` + 原因，清单照落。"""
    session = tmp_path / "250919"
    session.mkdir()
    launcher = RecordingLauncher()

    document = runner_module.run(
        session, launcher=launcher, deliver=True, probe=_dead_probe
    )

    assert document["state"] == STATE_FAILED
    assert document["mode"] == "none"
    assert "一条都没有" in document["error"] or "一条都没有" in str(document["error"])
    assert launcher.calls == [], "这一档不许启动 DSH"
    assert not (session / FINAL_NAME).exists(), "不许静默出一份空纪要"
    assert (session / "working" / "inputs.json").is_file(), "清单照落：读方要看得见这一场查过了"


def test_a_launcher_failure_lands_in_the_status(tmp_path: Path) -> None:
    """启动器报失败（退出码 0 但产物缺失那一档）→ 状态记 `failed`，原因原样带上。"""
    session = _session(tmp_path)
    launcher = RecordingLauncher(write_final=False, fail="artifact_missing")

    document = runner_module.run(
        session, launcher=launcher, deliver=True, probe=_dead_probe
    )

    assert document["state"] == STATE_FAILED
    assert "artifact_missing" in str(document["error"])
    assert document["dsh"]["failure"] == "artifact_missing"
    assert document["mode"] == "both"
    assert (session / STATUS_NAME).is_file()


def test_a_template_error_lands_in_the_status(tmp_path: Path, monkeypatch) -> None:
    """模板渲染不出来 → 状态记 `failed`，且**不启动**子会话。

    这一档就是「模板缺席 / 占位符对不上 / 转写读不动」三件事的落点：渲染器抛
    `PromptTemplateError`，`runner` 把它记成失败，一条子会话都不起。
    """
    session = _session(tmp_path)
    launcher = RecordingLauncher()

    def boom(*args, **kwargs):
        raise prompt_module.PromptTemplateError("模板里缺占位符 {{INPUTS}}：nowhere.md")

    monkeypatch.setattr(runner_module, "render_session_prompt", boom)
    document = runner_module.run(
        session, launcher=launcher, deliver=True, probe=_dead_probe
    )

    assert document["state"] == STATE_FAILED
    assert "提示词渲染失败" in str(document["error"])
    assert launcher.calls == []
    assert not (session / FINAL_NAME).exists()


# ---------------------------------------------------------------------------
# 三、再入闸
# ---------------------------------------------------------------------------


def test_the_reentry_gate_stops_before_the_launcher(tmp_path: Path) -> None:
    """`MEETING_MINUTES_CHILD=1` → **没拉起 DSH**，返回 `skipped`，盘上不写状态文件。"""
    session = _session(tmp_path)
    launcher = RecordingLauncher()

    document = runner_module.run(
        session, launcher=launcher, environ={"MEETING_MINUTES_CHILD": "1"}
    )

    assert document["state"] == STATE_SKIPPED
    assert launcher.calls == []
    assert "再入闸" in str(document["error"])
    assert not (session / STATUS_NAME).exists(), "子会话不去覆盖父进程那一趟的状态文件"
    assert not (session / "working" / "inputs.json").exists()


def test_the_reentry_gate_also_quiets_the_offline_tail(tmp_path: Path, monkeypatch) -> None:
    """离线那一层的尾钩同样认这道闸：环境里有它就不再解析、不再拉起。"""
    from meeting.offline import minutes_tail

    monkeypatch.setenv("MEETING_MINUTES_CHILD", "1")
    outcome = minutes_tail._announce_minutes(tmp_path, announce_minutes=True)

    assert outcome["hook"] == "skipped"
    assert outcome["announced"] is False
    assert not (tmp_path / STATUS_NAME).exists()


def test_the_launcher_is_handed_the_rendered_prompt(tmp_path: Path) -> None:
    """启动器拿到的是**渲染好的**提示词全文（不是模板、不是清单）。"""
    session = _session(tmp_path)
    launcher = RecordingLauncher()

    runner_module.run(session, launcher=launcher, deliver=True, probe=_dead_probe)

    text = launcher.calls[0]["prompt_text"]
    assert "{{" not in text
    assert "## 说话人附录" in text
    # **M4**：交出去的这份提示词里，给模型看的路径一律相对场次目录；
    # 提示词里唯一那段绝对路径在清单那段 JSON 的 `session_dir` 一格（给程序读的字段）
    absolute = str(session).replace("\\", "/")
    assert absolute in text and absolute not in text.split("```json")[0]
    # 程序读的那一份（`working/inputs.json`）照自己的口径记场次目录名，两处互不干扰
    written = json.loads((session / "working" / "inputs.json").read_text(encoding="utf-8"))
    assert written["session_id"] == session.name
    assert launcher.calls[0]["timeout"] == float(
        config_module.DEFAULT_VALUES["minutes.timeout_seconds"]
    )


# ---------------------------------------------------------------------------
# 四、配置键
# ---------------------------------------------------------------------------


def test_a_temp_config_file_lets_chunk_lines_through_and_rejects_an_out_of_range_one(
    tmp_path: Path,
) -> None:
    """配置效果实测：临时 `config.json` 里 `minutes.chunk_lines = 40` 被认、`= 5` 被拒。"""
    home = tmp_path / "home"
    home.mkdir()

    loaded = _load_config(home, {"minutes.chunk_lines": 40})

    assert loaded.get("minutes.chunk_lines") == 40
    assert loaded.source_of("minutes.chunk_lines") == "config.json"

    helpers.write_config_json(home, {"minutes.chunk_lines": 5})
    with pytest.raises(config_module.ConfigValueError) as info:
        config_module.load(
            environ={"HOME": str(home), "USERPROFILE": str(home)},
            path=str(helpers.config_path_of(home)),
        )
    assert info.value.key == "minutes.chunk_lines"
    assert "10–1000" in info.value.legal


def test_chunk_lines_from_the_config_reaches_the_chunker(tmp_path: Path) -> None:
    """配置里的 `minutes.chunk_lines` 真的走到切块那一步（切出来的块数说话）。"""
    home = tmp_path / "home"
    home.mkdir()
    session = _session(tmp_path)
    loaded = _load_config(home, {"minutes.chunk_lines": 10})
    launcher = RecordingLauncher()

    document = runner_module.run(
        session, launcher=launcher, config=loaded, deliver=True, probe=_dead_probe
    )

    assert document["chunks"]["mic"]["offline"]["chunk_count"] == 3, "22 行按 10 行一块切"
    assert document["chunks"]["mic"]["live"]["chunk_count"] == 3, "25 行按 10 行一块切"
    assert document["chunks"]["loopback"]["live"]["chunk_count"] == 2, "15 行按 10 行一块切"


def test_minutes_hook_null_passes_validation_and_resolves_to_absent(tmp_path: Path) -> None:
    """`minutes.hook = null` 过校验，并且解析成缺席档（离线跑完不产生纪要）。"""
    home = tmp_path / "home"
    home.mkdir()

    loaded = _load_config(home, {"minutes.hook": None})

    assert loaded.get("minutes.hook") is None
    assert config_module.validate_value("minutes.hook", None) is None
    assert config_module.validate_value("minutes.hook", "auto") == "auto"
    assert config_module.validate_value("minutes.hook", "null") == "null"
    assert "null" in config_module._legal_text("minutes.hook")


def test_the_minutes_hook_resolves_the_way_post_hook_does() -> None:
    """`resolve_minutes_hook()` 与 `resolve_post_hook()` 同一个形状：`auto` 到实现、`null` 缺席。"""
    resolution = resolve_minutes_hook("auto")

    assert resolution.key == "minutes.hook"
    assert resolution.target == "meeting.backends.minutes:MinutesHook"
    assert resolution.has_implementation is True
    assert isinstance(resolution.implementation, minutes_backend.MinutesHook)
    assert type(resolution.implementation).__module__ == "meeting.backends.minutes"

    absent = resolve_minutes_hook("null")
    assert absent.has_implementation is False
    assert absent.skipped() is True


def test_the_hook_launch_returns_immediately_and_never_raises(
    tmp_path: Path, monkeypatch
) -> None:
    """`launch()` 起一条 daemon 线程就返回；线程里的异常落状态文件、不外传。"""
    from meeting.offline import minutes_tail

    session = _session(tmp_path)
    seen: list[str] = []

    def boom(session_dir, **kwargs):
        seen.append(str(session_dir))
        raise RuntimeError("这一趟炸了")

    # 离线那一层的尾钩自己按 `minutes.hook` 解析（不走注入的配置对象），这里让它解析到 `auto`
    monkeypatch.setattr(config_module, "get", _config_getter({"minutes.hook": "auto"}))
    monkeypatch.setattr(runner_module, "run", boom)

    outcome = minutes_tail._announce_minutes(session, announce_minutes=True)

    assert outcome["hook"] == "launched"
    assert outcome["announced"] is True
    assert outcome["status"] == STATUS_NAME
    helpers.wait_until(lambda: seen, what="后台线程真的被起过")
    # 线程里的异常由钩子自己兜住并落状态文件：等它把那一步做完
    helpers.wait_until(
        lambda: (session / STATUS_NAME).exists()
        and status_module.read_status(session / STATUS_NAME)["state"] == STATE_FAILED,
        what="后台线程把状态文件补成 failed",
    )
    written = status_module.read_status(session / STATUS_NAME)
    assert "这一趟炸了" in str(written["error"])


def test_the_hook_returns_before_the_background_work_finishes(tmp_path: Path, monkeypatch) -> None:
    """**返回即算启动成功**：后台那一趟还卡在闸门上，`launch()` 已经回来了。"""
    import threading
    import time

    session = _session(tmp_path)
    gate = threading.Event()
    entered = threading.Event()

    def blocked(session_dir, **kwargs):
        entered.set()
        gate.wait(CONVERGE_TIMEOUT_SECONDS)

    monkeypatch.setattr(runner_module, "run", blocked)

    begun = time.monotonic()
    minutes_backend.MinutesHook().launch(str(session), deliver=True)
    elapsed = time.monotonic() - begun

    assert elapsed < 1.0, f"launch() 该当场返回，实测 {elapsed:.3f}s"
    assert entered.wait(CONVERGE_TIMEOUT_SECONDS), "后台那一条线程该已经跑起来了"
    assert not gate.is_set()
    gate.set()


def test_the_hook_needs_a_session_dir() -> None:
    """场次目录是空值时当场抛（没有落点就装作启动过，盘上会留一版永远 running）。"""
    with pytest.raises(ValueError):
        minutes_backend.MinutesHook().launch("")


def test_the_hook_claims_the_run_in_flight_before_it_returns(
    tmp_path: Path, monkeypatch
) -> None:
    """**「在飞」在 `launch()` 返回之前就落到盘上**（同一场不再被拉起第二个子会话的落点）。

    线程体被钉住不动（它永远不落终态），于是这一条量的正是「`launch()` 交回那一刻盘上已经
    是 `running`」这件事 —— 落点停在起线程之后时，这一格读到的是「还没有这份文件」。
    """
    session = _session(tmp_path)
    entered = threading.Event()
    gate = threading.Event()

    def blocked(session_dir, **kwargs):
        entered.set()
        gate.wait(CONVERGE_TIMEOUT_SECONDS)

    monkeypatch.setattr(runner_module, "run", blocked)

    minutes_backend.MinutesHook().launch(str(session), deliver=True)

    written = status_module.read_status(session / STATUS_NAME)
    assert written["state"] == STATE_RUNNING, written
    assert written["finished_at"] == "", "还在跑的那一版收尾时刻该空着"
    assert written["session_id"] == session.name
    assert entered.wait(CONVERGE_TIMEOUT_SECONDS), "后台那一条线程该已经跑起来了"
    gate.set()


def test_a_second_launch_on_the_same_session_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    """同一场再来一次拉起：**拒跑**（没有第二条线程、没有第二个子会话）。

    盘上已经停着一版 `running`（另一趟正在飞）时，`launch()` 当场抛 `AlreadyRunningError`，
    第一趟那一版记录一个字节都不被覆盖。
    """
    session = _session(tmp_path)
    arms: list[str] = []
    monkeypatch.setattr(
        runner_module, "run", lambda session_dir, **kwargs: arms.append(str(session_dir))
    )
    hook = minutes_backend.MinutesHook()

    hook.launch(str(session), deliver=True)
    before = (session / STATUS_NAME).read_text(encoding="utf-8")

    with pytest.raises(minutes_backend.AlreadyRunningError):
        hook.launch(str(session), deliver=True)

    helpers.wait_until(lambda: arms, what="第一趟的后台线程真的跑起来过")
    assert arms == [str(session)], arms
    assert (session / STATUS_NAME).read_text(encoding="utf-8") == before


def test_force_launches_again_on_the_same_session(tmp_path: Path, monkeypatch) -> None:
    """`--force` 那一趟照常重跑：在飞那条判据在 `force=True` 时跳过（它的既有语义）。"""
    session = _session(tmp_path)
    seen: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        runner_module,
        "run",
        lambda session_dir, **kwargs: seen.append((str(session_dir), bool(kwargs))),
    )
    hook = minutes_backend.MinutesHook()

    hook.launch(str(session), deliver=True)
    hook.launch(str(session), force=True, deliver=True)

    helpers.wait_until(lambda: len(seen) == 2, what="两趟都被拉起来了")
    assert [item[0] for item in seen] == [str(session), str(session)]


def test_the_claim_lock_does_not_stay_on_disk(tmp_path: Path, monkeypatch) -> None:
    """那只 claim 锁是**瞬态**的：拉起之后场次目录里没有多出来的锁文件。"""
    session = _session(tmp_path)
    monkeypatch.setattr(runner_module, "run", lambda session_dir, **kwargs: None)

    minutes_backend.MinutesHook().launch(str(session), deliver=True)

    lock = status_module.lock_path(session)
    assert not lock.exists(), f"claim 锁该写完那一版 running 就删掉：{lock}"
    assert (session / STATUS_NAME).is_file()
    assert lock.name not in {item.name for item in session.iterdir()}


def test_the_absent_hook_never_writes_anything(tmp_path: Path) -> None:
    """缺席档（`minutes.hook = null`）的 `launch()` 一被调用就抛，且盘上不留痕。"""
    from meeting.backends.registry import _AbsentMinutesHook

    with pytest.raises(NotImplementedError):
        _AbsentMinutesHook().launch(str(tmp_path))

    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# 五、与离线那一层的关系
# ---------------------------------------------------------------------------


def test_a_minutes_run_leaves_the_offline_artifacts_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    """跑一次纪要生成**不改动离线那三份产物与离线状态文件**（拿哈希比）。"""
    from tests._offline_fakes import FakeConfig, FakeEngine, synth_session

    session = synth_session(tmp_path / "out")
    monkeypatch.setenv("MEETING_MINUTES_CHILD", "1")  # 先让离线那一趟安静地跳过尾钩
    from meeting.offline import runner as offline_runner

    offline_runner.run_session(
        session, engine=FakeEngine(), config=FakeConfig(**{"post.chunk_seconds": 450})
    )
    before = {
        name: _digest(session / name) for name in OFFLINE_ARTIFACTS if (session / name).is_file()
    }
    assert set(before) == set(OFFLINE_ARTIFACTS), "夹具该把三份产物 + 状态文件都落下来"

    monkeypatch.delenv("MEETING_MINUTES_CHILD")
    runner_module.run(
        session, launcher=RecordingLauncher(), deliver=True, probe=_dead_probe
    )

    after = {name: _digest(session / name) for name in OFFLINE_ARTIFACTS}
    assert after == before
    assert (session / STATUS_NAME).is_file(), "纪要那一份状态文件是新落的"


def test_the_offline_tail_announces_the_hook_once(tmp_path: Path, monkeypatch) -> None:
    """离线那一趟的尾巴上真的调了一次纪要钩子，并且摘要里带着那一格。"""
    from tests._offline_fakes import FakeConfig, FakeEngine, synth_session

    session = synth_session(tmp_path / "out")
    monkeypatch.setenv("MEETING_MINUTES_CHILD", "1")
    from meeting.offline import runner as offline_runner

    summary = offline_runner.run_session(
        session,
        engine=FakeEngine(),
        config=FakeConfig(**{"post.chunk_seconds": 450}),
        announce_minutes=True,
    )

    assert summary["minutes"]["hook"] == "skipped"
    assert summary["minutes"]["announced"] is False
    assert summary["state"] in ("done", "failed")
    assert not (session / STATUS_NAME).exists(), "子会话标记在场时不产生纪要状态文件"


def test_the_offline_tail_keeps_a_broken_minutes_layer_out_of_its_own_status(
    tmp_path: Path, monkeypatch
) -> None:
    """纪要那一层炸了：离线状态文件与三份产物一个字节不变，摘要里如实记一条。"""
    from tests._offline_fakes import FakeConfig, FakeEngine, synth_session

    session = synth_session(tmp_path / "out")
    monkeypatch.setenv("MEETING_MINUTES_CHILD", "1")
    from meeting.offline import minutes_tail
    from meeting.offline import runner as offline_runner

    offline_runner.run_session(
        session, engine=FakeEngine(), config=FakeConfig(**{"post.chunk_seconds": 450})
    )
    before = {name: _digest(session / name) for name in OFFLINE_ARTIFACTS}

    monkeypatch.delenv("MEETING_MINUTES_CHILD")
    monkeypatch.setattr(
        "meeting.backends.registry.resolve_minutes_hook",
        _raising_resolver,
    )
    outcome = minutes_tail._announce_minutes(session, announce_minutes=True)

    assert outcome["hook"] == "failed"
    assert outcome["announced"] is False
    assert {name: _digest(session / name) for name in OFFLINE_ARTIFACTS} == before


def _raising_resolver(*args, **kwargs):
    raise RuntimeError("注册表塌了")


def test_a_null_hook_keeps_the_minutes_status_away(tmp_path: Path, monkeypatch) -> None:
    """`minutes.hook = null` → 离线跑完不产生纪要状态文件（安静跳过）。"""
    from tests._offline_fakes import FakeConfig, FakeEngine, synth_session

    session = synth_session(tmp_path / "out")
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)
    monkeypatch.setattr(
        config_module, "get", _config_getter({"minutes.hook": None, "post.chunk_seconds": 450})
    )
    from meeting.offline import runner as offline_runner

    summary = offline_runner.run_session(
        session,
        engine=FakeEngine(),
        config=FakeConfig(**{"post.chunk_seconds": 450}),
        announce_minutes=True,
    )

    assert summary["minutes"]["hook"] == "absent"
    assert summary["minutes"]["announced"] is False
    assert not (session / STATUS_NAME).exists()


def _config_getter(values: dict):
    """交回一个 `config.get()` 的替身（`registry` 与 `runner` 都从那里取值）。"""
    original = config_module.get

    def getter(key: str, default=None):
        if key in values:
            return values[key]
        return original(key, default)

    return getter


# ---------------------------------------------------------------------------
# 六、惰性：全新解释器里的 import 链
# ---------------------------------------------------------------------------

#: 在子进程里跑的一段：import 三个新模块 + 钩子，报出重依赖与解析结论
_IMPORT_CHAIN_CODE = (
    "import sys; "
    "import meeting.minutes.launch as l; "
    "import meeting.minutes.runner as r; "
    "import meeting.backends.minutes as m; "
    "from meeting.backends.registry import resolve_minutes_hook; "
    "res = resolve_minutes_hook(); "
    "heavy = [x for x in ('torch', 'transformers', 'av', 'numpy', 'webrtcvad', 'librosa', "
    "'starlette', 'sounddevice', 'soundcard', 'uvicorn') if x in sys.modules]; "
    "print('heavy=' + ','.join(heavy)); "
    "print('hook=' + m.MinutesHook.__module__ + '/' + m.MinutesHook.__name__); "
    "print('run=' + r.run.__name__); "
    "print('launch=' + l.launch_headless.__name__); "
    "print('resolved=' + str(res.has_implementation))"
)


def test_the_new_modules_pull_no_heavy_dependency() -> None:
    """`import meeting.minutes.launch` 等三个新模块都保持轻量（全新解释器里验）。"""
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

    assert lines[0] == "heavy=", f"新模块把重依赖拉进了 sys.modules：{lines[0]!r}\n{output}"
    assert lines[1] == "hook=meeting.backends.minutes/MinutesHook", output
    assert lines[2] == "run=run", output
    assert lines[3] == "launch=launch_headless", output
    assert lines[4] == "resolved=True", output


def test_the_final_artifact_check_is_the_same_one_the_launcher_uses(tmp_path: Path) -> None:
    """`runner` 记 `done` 与启动器判 `ok` 用的是同一份判据（这里拿坏产物反向确认）。"""
    session = _session(tmp_path)
    (session / FINAL_NAME).write_text("# 只有正文\n", encoding="utf-8")

    assert check_final_artifact(session) != ()


# ---------------------------------------------------------------------------
# 七、running 那一份（拉起 DSH 之前落的）
# ---------------------------------------------------------------------------


def test_the_running_state_is_on_disk_before_the_launcher_is_called(tmp_path: Path) -> None:
    """**m1**：拉起 DSH **之前**盘上已经有一份 `running`（读方据此分得出「正在跑」）。

    假启动器在被调用的那一刻读盘：那正是「子进程刚被拉起、纪要还没写出来」的现场。
    """
    session = _session(tmp_path)
    seen: list[dict] = []

    def peek(session_dir, **kwargs) -> LaunchResult:
        seen.append(_status_on_disk(Path(session_dir)) or {})
        (Path(session_dir) / FINAL_NAME).write_text(GOOD_FINAL, encoding="utf-8")
        return LaunchResult(
            ok=True,
            branch="headless",
            executable=sys.executable,
            exit_code=0,
            seconds=0.01,
            log=ARTIFACT_LOG,
            prompt="working/run_prompt.md",
        )

    document = runner_module.run(session, launcher=peek, deliver=True, probe=_dead_probe)

    assert seen, "启动器该被调用过"
    running = seen[0]
    assert running["state"] == STATE_RUNNING, "拉起那一刻盘上该是 running"
    assert running["finished_at"] == "", "还在跑的那一份把收尾时刻留空"
    assert running["started_at"]
    assert running["mode"] == "none", "输入还没找，档位空着"
    assert running["branch"] == "headless", "实际走的分支在这一份里就记上了"
    assert running["dsh"] == {}, "这一份还没有子进程的记录"
    assert running["artifacts"] == {
        "final": ARTIFACT_FINAL,
        "inputs": ARTIFACT_INPUTS,
        "log": ARTIFACT_LOG,
    }
    assert document["state"] == STATE_DONE, "终局那一份照旧覆盖成 done"
    assert _status_on_disk(session)["state"] == STATE_DONE
    assert document["started_at"] == running["started_at"], "两份共用同一个开始时刻"


def test_a_failed_run_still_overwrites_the_running_state(tmp_path: Path) -> None:
    """**m1**：失败路径上终态照样覆盖掉那一份 `running`（不会永远停在 running）。"""
    session = _session(tmp_path)
    launcher = RecordingLauncher(write_final=False, fail="artifact_missing")

    document = runner_module.run(
        session, launcher=launcher, deliver=True, probe=_dead_probe
    )

    assert document["state"] == STATE_FAILED
    written = _status_on_disk(session)
    assert written["state"] == STATE_FAILED
    assert written["finished_at"], "终局那一份有收尾时刻"
    assert written["mode"] == "both", "终局那一份记的是真档位"


def test_a_session_without_inputs_leaves_a_failed_state_not_a_running_one(tmp_path: Path) -> None:
    """一条输入都没有那一档：`running` 也要被覆盖成 `failed`（那一档同样不悬着）。"""
    session = tmp_path / "250919"
    session.mkdir()

    runner_module.run(
        session, launcher=RecordingLauncher(), deliver=True, probe=_dead_probe
    )

    assert _status_on_disk(session)["state"] == STATE_FAILED


def test_the_reentry_gate_writes_no_status_at_all(tmp_path: Path) -> None:
    """**m1 的边界**：再入闸那一档盘上**一份状态文件都没有**（`running` 也不落）。"""
    session = _session(tmp_path)

    document = runner_module.run(
        session, launcher=RecordingLauncher(), environ={"MEETING_MINUTES_CHILD": "1"}
    )

    assert document["state"] == STATE_SKIPPED
    assert not (session / STATUS_NAME).exists(), "子会话不落 running，也不覆盖父进程那一趟"
