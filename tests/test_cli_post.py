"""`meeting post` 真入口与「跑会后一套」那段编排（`65dd3e4c` phase1）。

要盯住的六条（对应任务书「验证口径」那一节）：

  1. **参数面**：不给位置参数 → 用法说明 + `EXIT_REJECTED`；指到不存在的目录 →
     原因里带上那条路径 + `EXIT_REJECTED`；`--force` 被认下（不报用法错误）；
  2. **编排面**：注入假引擎 + 假启动器之后，摘要里场次目录、每层结论、逐条产物的
     **绝对路径**齐；终端输出里同时看得到「选了哪一场」与产物的绝对路径；
  3. **音频归档补齐**：非空 PCM + MP3 缺席 → 产出 `.mp3`（调用的就是
     `meeting.capture.archive.convert_to_mp3` 这一个函数，一次）；空 PCM → 既不产 MP3
     也不调转码；MP3 已比 PCM 新 → 不调转码、MP3 字节不变；假转码抛错 → 该路记 `failed`、
     这一趟 `EXIT_FAILURE`、**PCM 的 sha256 与转换前逐字节相同**；
  4. **编排只走一条路**：`run_post` 里的 `run_session` 是经 `runner.run_session` 这个
     模块属性取的（用例换掉它之后，替身真的被调到）；
  5. **第二条入口同源**：`PostProcessHook._run()` 调到的是同一个 `run_post`，
     而 `launch()` 仍然立刻返回；
  6. **纪要闸**：终态 `done` + `meeting_final.md` 过得了现成四条判据时**不调启动器**、
     返回 `announced is False`；把那份纪要删掉之后再调，启动器**又被调一次**。

**一条真子进程都不起**：参数面走进程内 `pytest.raises(SystemExit)`，编排面一律注入假件
（假引擎 / 假启动器 / 假转码）。本机 PATH 上真的有 `dsh`，`tests/conftest.py` 的
`MEETING_MINUTES_CHILD=1` 兜底是第一层网，这里另外把 `resolve_minutes_hook` 换成只记账的
假件；**注入假启动器不再需要摘掉那个兜底标记** —— 假启动器经
`run_post(..., minutes_launcher=…)` → `run_session(..., minutes_launcher=…)` →
`_announce_minutes(..., launcher=…)` **一路向下传**（本文件没有一处替换模块全局属性），
而那道标记是产品路径自己的再入闸、与注入的假件无关。

**注入的假启动器按真启动器的时序记账**：`launch()` 返回即算启动成功，终态由它背后那条链
写进 `meeting_minutes_status.json`，因此 `hook == "launched"` 那一档**一律等这一趟新落的
终态**（等待语义不按「谁拉起的」分叉）。需要「这一趟真的落定」的用例用 `SettlingLauncher`
（记账 + 照真子会话的收工样子落终态）；本文件的上界夹具 `a_short_minutes_bound` 把
`minutes.timeout_seconds` 的读数压到 0.4 秒，任何一档都不会把用例吊在四小时上。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from meeting import cli as cli_module
from meeting import config as config_module
from meeting.backends import post as post_module
from meeting.minutes import status as minutes_status
from meeting.offline import artifacts as offline_artifacts
from meeting.offline import progress as offline_progress
from meeting.offline import runner as offline_runner

from tests.test_offline_runner import FakeConfig, FakeEngine, synth_session
from tests.test_minutes_launch import GOOD_FINAL

from meeting.cli import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_REJECTED,
    _minutes_outcome,
    _parse_post_args,
    post,
    run_post,
    sync_archive,
)

#: 慢档标记（`68927c27` 收尾轮）：`meeting post` 是收尾功能，整文件归收尾验收。
pytestmark = pytest.mark.slow

#: 假启动器那一档的秒数（假的纪要钩子不起任何东西）
FAKE_SECONDS = 0.01

#: 离线那一层的状态文件名（按产物契约取，不重写一遍字面量）
OFFLINE_STATUS_NAME = offline_artifacts.STATUS_NAME

#: 纪要那一层的两个产物名
MINUTES_STATUS_NAME = offline_runner.MINUTES_STATUS_NAME
MINUTES_FINAL_NAME = offline_runner.MINUTES_FINAL_NAME


# ---------------------------------------------------------------------------
# 假件
# ---------------------------------------------------------------------------


class RecordingLauncher:
    """假启动器：只记账（被调了几次、每次的场次目录），**什么也不起**。

    它替的是「把 headless DSH 子会话拉起来」那一步 —— 本文件里一次真拉起都不该发生，
    因此这份记账就是「这一趟有没有再花一次钱」的权威读法。
    """

    def __init__(self, *, result: Any = None) -> None:
        self.calls: list[str] = []
        self.result = result

    def __call__(self, session_dir: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(str(session_dir))
        return self.result


class FakeHook:
    """假钩子实现（`minutes.hook = auto` 那一格解析出来的东西）：`launch()` 只记账。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def launch(self, session_dir: str) -> None:
        self.calls.append(str(session_dir))


class FakeResolution:
    """`resolve_minutes_hook()` 的替身：冒充「在场且有实现」那一档。"""

    def __init__(self, hook: FakeHook) -> None:
        self.key = "minutes.hook"
        self.selector = "auto"
        self.target = "fake:MinutesHook"
        self.has_implementation = True
        self.implementation = hook

    def skipped(self) -> bool:
        return False


class RecordingConverter:
    """假转码器：记账 + 按场景写/不写那份 MP3（真 ffmpeg 在不在场与用例无关）。"""

    def __init__(self, *, body: bytes = b"fake-mp3", raises: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.body = body
        self.raises = raises

    def __call__(self, pcm_path: Any, mp3_path: Any) -> bool:
        self.calls.append((str(pcm_path), str(mp3_path)))
        if self.raises:
            raise RuntimeError(self.raises)
        Path(mp3_path).write_bytes(self.body)
        return True


class SettlingLauncher(RecordingLauncher):
    """假启动器：记账之外**照真子会话的收工样子把终态落下来**（`state` 缺省 `done`）。

    真的启动器 `launch()` 返回即算启动成功，终态由它背后那条链写进
    `meeting_minutes_status.json`；**拉起不等于落定**，因此 `run_post()` 在
    `hook == "launched"` 那一档一律等这一趟新落的终态（`meeting/cli.py` 的
    `_wait_for_settled_minutes()`）—— 真实现与注入的假件在时序上同形，等待语义不按
    「谁拉起的」分叉。只记账、一份终态都不落的假件因此会等到上界（真链是四小时，
    本文件那一格由上界夹具压到秒级）；本文件需要「这一趟真的落定」的用例一律用它。
    """

    def __init__(self, *, state: str = "done", result: Any = None) -> None:
        super().__init__(
            result=result if result is not None else {"hook": "launched", "announced": True}
        )
        self.state = str(state)

    def __call__(self, session_dir: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(str(session_dir))
        session = Path(session_dir)
        if self.state == "done":
            write_final(session)
        write_minutes_status(session, state=self.state)
        return dict(self.result or {})


class DelayedLauncher(RecordingLauncher):
    """假启动器：照**真启动器的时序**落终态 —— `launch()` 返回即算启动成功，终态晚一步才落。

    真 `MinutesHook.launch` 起一条 daemon 线程就返回，线程体先落一版 `running`、跑完才落终态；
    这台替身照那个形状来（`latency` 是被模拟的那个子会话的耗时）。于是「拉起那一刻盘上还是
    `running`」这个现场在注入路径上也能造出来 —— 等待那一处因此必须真的等，而不是当场把
    盘上那一版当结论。
    """

    def __init__(self, *, latency: float = 0.05, state: str = "done") -> None:
        super().__init__(result={"hook": "launched", "announced": True})
        self.latency = float(latency)
        self.state = str(state)
        self.threads: list[threading.Thread] = []

    def __call__(self, session_dir: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(str(session_dir))
        session = Path(session_dir)

        def body() -> None:
            write_minutes_status(session, state="running")
            time.sleep(self.latency)
            if self.state == "done":
                write_final(session)
            write_minutes_status(session, state=self.state)

        thread = threading.Thread(target=body, daemon=True)
        self.threads.append(thread)
        thread.start()
        return dict(self.result or {})


@pytest.fixture(autouse=True)
def _no_real_minutes_hook(monkeypatch: pytest.MonkeyPatch) -> FakeHook:
    """**本文件自己的第二层网**：纪要钩子一律换成只记账的假件。

    `tests/conftest.py` 那道 `MEETING_MINUTES_CHILD=1` 兜底是第一层网（挡住任何真子会话）；
    这里把 `resolve_minutes_hook()` 整个换掉，于**产品路径**那一档（用例不注入假启动器时）
    `launch()` 落到的也是这份假的 —— 本文件因此一条真 `dsh` 都起不来。
    注入假启动器的用例根本走不到它：`_announce_minutes()` 拿到 `launcher` 就不再解析实现。
    """
    hook = FakeHook()
    monkeypatch.setattr(
        "meeting.backends.registry.resolve_minutes_hook", lambda *a, **k: FakeResolution(hook)
    )
    return hook


@pytest.fixture(autouse=True)
def a_short_minutes_bound(monkeypatch: pytest.MonkeyPatch) -> float:
    """把等纪要终态的上界压到 0.4 秒，读的仍是**现成的配置键** `minutes.timeout_seconds`。

    真链的缺省是四小时（`meeting/config.py` 的 `DEFAULT_VALUES`），用例不能真等满它。
    `hook == "launched"` 那一档一律等这一趟新落的终态，因此本文件凡是注入假启动器的用例都
    压到秒级：假件落了终态的当场收工，**只记账不落终态的假件也绝不能挂住**（它在上界内
    如实收场，而不是把用例吊死在四小时上）。键本身不新增、取值域不动，只改这一格的**读数**。
    """
    real_get = config_module.get
    monkeypatch.setattr(
        config_module,
        "get",
        lambda key, default=None: (
            0.4 if key == "minutes.timeout_seconds" else real_get(key, default)
        ),
    )
    return 0.4


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def digest(path: Path) -> str:
    """一份文件的 sha256（转码失败前后逐字节比对的判据）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_minutes_status(session: Path, *, state: str = "done") -> Path:
    """按纪要那一层的写方落一份状态文件（**走它自己的 `write_session_status()`**）。"""
    minutes_status.write_session_status(
        session,
        state=state,
        mode="offline_only",
        branch="headless",
        started_at="2025-09-19T22:00:00",
        artifacts={},
    )
    return session / MINUTES_STATUS_NAME


def write_final(session: Path) -> Path:
    """落一份过得了四条判据的 `meeting_final.md`。"""
    path = session / MINUTES_FINAL_NAME
    path.write_text(GOOD_FINAL, encoding="utf-8")
    return path


def fake_config(**values: Any) -> FakeConfig:
    """最小配置：本文件跑离线那一趟时用它（`minutes.hook = null` 那一档是兜底）。"""
    return FakeConfig(**values)


# ---------------------------------------------------------------------------
# 一、参数面
# ---------------------------------------------------------------------------


def test_post_without_a_positional_argument_prints_usage_and_rejects(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`meeting post` 不带位置参数 → 用法说明 + `EXIT_REJECTED`（不猜最新一场）。"""
    with pytest.raises(SystemExit) as info:
        post([])

    assert info.value.code == EXIT_REJECTED
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "用法：meeting post <场次目录> [--force]" in combined
    assert "<场次目录>" in combined
    assert "缺少位置参数" in combined


def test_post_with_a_missing_directory_rejects_and_names_the_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """位置参数指到不存在的目录 → 原因里带上那条路径 + `EXIT_REJECTED`。"""
    missing = tmp_path / "260920" / "没有这一场"

    with pytest.raises(SystemExit) as info:
        post([str(missing)])

    assert info.value.code == EXIT_REJECTED
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert str(missing) in combined
    assert "场次目录不存在" in combined


def test_a_file_is_not_a_session_directory(tmp_path: Path) -> None:
    """位置参数指到一个文件（存在，但落不进一个目录）同样按用法错误挡下。"""
    plain = tmp_path / "session.json"
    plain.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError) as info:
        _parse_post_args([str(plain)])

    assert "场次目录不存在" in str(info.value)


def test_force_is_accepted_and_a_bad_switch_is_not(tmp_path: Path) -> None:
    """`--force` 被认下；别的开关如实报「未定义的开关」（本轮只有这一个开关）。"""
    session = synth_session(tmp_path / "250919")

    parsed = _parse_post_args([str(session), "--force", "--force"])
    assert parsed["dir"] == session.resolve()
    assert parsed["force"] is True

    plain = _parse_post_args([str(session)])
    assert plain["force"] is False

    with pytest.raises(ValueError) as info:
        _parse_post_args([str(session), "--now"])
    assert "未定义的开关" in str(info.value)


def test_two_positionals_are_rejected(tmp_path: Path) -> None:
    """两个位置参数当场报用法错误（不挑其中一个猜）。"""
    first = synth_session(tmp_path / "250919")
    second = synth_session(tmp_path / "260920")

    with pytest.raises(ValueError) as info:
        _parse_post_args([str(first), str(second)])

    assert "只收一个" in str(info.value)


def test_post_prints_the_selected_session_and_the_artifact_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`post()` 的终端输出：选了哪一场、每步做了什么、产物的**绝对路径**、结论。"""
    session = synth_session(tmp_path / "250919")
    transcript = session / "meeting_offline_transcript_mic.md"
    transcript.write_text("[22:01] **S01**: 会上定了两件事。\n", encoding="utf-8")
    archive = tmp_path / "meeting_audio_mic_001.mp3"
    archive.write_bytes(b"mp3")

    def fake_run_post(session_dir, **_kwargs):
        return {
            "session_id": Path(session_dir).name,
            "session_dir": str(Path(session_dir).resolve()),
            "state": "done",
            "ok": True,
            "exit_code": EXIT_OK,
            "exit_code_name": "EXIT_OK",
            "archive": {"state": "done", "sources": []},
            "offline": {
                "hook": "done",
                "state": "done",
                "sources": {"mic": {"transcript": transcript.name}},
            },
            "minutes": {"hook": "finished", "state": "done"},
            "artifacts": {"mic 的离线转写": str(transcript.resolve())},
        }

    monkeypatch.setattr(cli_module, "run_post", fake_run_post)

    with pytest.raises(SystemExit) as info:
        post([str(session)])

    assert info.value.code == EXIT_OK
    out = capsys.readouterr().out
    assert "选了这一场：250919" in out
    assert str(transcript.resolve()) in out
    assert "结论：done" in out
    assert "退出码 EXIT_OK" in out


def test_a_broken_archive_takes_the_exit_code_to_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """归档那一层 `failed` → 这一趟的退出码是 `EXIT_FAILURE`（终端里写常量名）。"""
    session = synth_session(tmp_path / "250919")
    summary = {
        "session_id": "250919",
        "session_dir": str(session.resolve()),
        "state": "failed",
        "ok": False,
        "exit_code": EXIT_FAILURE,
        "exit_code_name": "EXIT_FAILURE",
        "archive": {"state": "failed", "sources": []},
        "offline": {"hook": "done", "state": "done", "sources": {}},
        "minutes": {"hook": "finished", "state": "done"},
        "artifacts": {},
    }
    monkeypatch.setattr(cli_module, "run_post", lambda *a, **k: dict(summary))

    with pytest.raises(SystemExit) as info:
        post([str(session)])

    assert info.value.code == EXIT_FAILURE


def test_a_rejected_run_beats_a_failed_archive_in_the_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """归档 `failed` 与 `running` 拒跑**同时在场**：退出码走拒跑那一档、提示给全。

    `sync_archive()` 在幂等闸之前跑，因此「某一路转不动」与「某层状态文件停在 `running`」
    可以同时成立（ffmpeg 缺席 + 上一趟崩在半路）。这一档按 `_summarize_run()` 自己写的
    先后收场：退出码 `EXIT_REJECTED`，终端点名那个状态文件、给出 `--force` 这句话；
    「结论：」那一行照实写这一趟的落点（归档那一层的 `failed` 没有被吞掉）。
    """
    session = synth_session(tmp_path / "250919")
    summary = {
        "session_id": "250919",
        "session_dir": str(session.resolve()),
        "state": "failed",
        "ok": False,
        "exit_code": EXIT_REJECTED,
        "exit_code_name": "EXIT_REJECTED",
        "archive": {"state": "failed", "sources": []},
        "offline": {"hook": "running", "state": "running", "sources": {}},
        "minutes": {
            "hook": "running",
            "state": "running",
            "status_path": str(session / "meeting_minutes_status.json"),
        },
        "artifacts": {},
    }
    monkeypatch.setattr(cli_module, "run_post", lambda *a, **k: dict(summary))

    with pytest.raises(SystemExit) as info:
        post([str(session)])

    assert info.value.code == EXIT_REJECTED
    out = capsys.readouterr().out
    assert "拒跑" in out
    assert "meeting_minutes_status.json 停在 running" in out, out
    assert "--force" in out, out
    assert "结论：failed（退出码 EXIT_REJECTED）" in out, out


def test_a_failed_archive_alone_keeps_the_failure_exit_code(tmp_path: Path) -> None:
    """归档 `failed` 而两层都没停在 `running`：退出码照旧是 `EXIT_FAILURE`（拒跑那一档不在场）。

    这一条是上一条的对照：`EXIT_REJECTED` 的优先级只盖住「拒跑」那一个现场，归档自己
    转不动那一档的口径一个字没动。
    """
    session = synth_session(tmp_path / "250919")

    summary = cli_module._summarize_run(
        {
            "session_id": "250919",
            "session_dir": str(session.resolve()),
            "started_at": 0.0,
            "archive": {"state": "failed", "sources": []},
            "offline": {"hook": "done", "state": "done", "sources": {}},
            "minutes": {"hook": "already_done", "state": "done"},
            "artifacts": {},
        }
    )

    assert summary["exit_code"] == EXIT_FAILURE
    assert summary["state"] == "failed"


def test_the_force_switch_reaches_the_orchestration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`post --force` 把 `force=True` 交到编排函数手上（本单元只如实报出来）。"""
    session = synth_session(tmp_path / "250919")
    seen: list[bool] = []

    def fake_run_post(session_dir, *, force: bool = False, **_kwargs):
        seen.append(bool(force))
        return {
            "session_id": "250919",
            "session_dir": str(Path(session_dir).resolve()),
            "state": "done",
            "ok": True,
            "exit_code": EXIT_OK,
            "exit_code_name": "EXIT_OK",
            "archive": {"state": "skipped", "sources": []},
            "offline": {"hook": "done", "state": "done", "sources": {}},
            "minutes": {"hook": "absent", "state": None},
            "artifacts": {},
        }

    monkeypatch.setattr(cli_module, "run_post", fake_run_post)

    with pytest.raises(SystemExit):
        post([str(session), "--force"])

    assert seen == [True]


# ---------------------------------------------------------------------------
# 二、音频归档补齐（PCM -> MP3）
# ---------------------------------------------------------------------------


def test_a_nonempty_pcm_without_an_mp3_is_converted_once(tmp_path: Path) -> None:
    """非空 PCM + MP3 缺席 → 转一次，且调的就是那一份现成实现。"""
    session = synth_session(tmp_path / "250919", sources=("mic",))
    converter = RecordingConverter()

    outcome = sync_archive(session, sources=("mic",), converter=converter)

    assert [row["outcome"] for row in outcome["sources"]] == ["converted"]
    assert outcome["state"] == "done"
    assert (session / "meeting_audio_mic_001.mp3").is_file()
    assert len(converter.calls) == 1


def test_the_product_path_calls_the_one_converter_the_repo_already_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """产品路径上调的**就是** `meeting.capture.archive.convert_to_mp3` 这一个函数。"""
    from meeting.capture import archive as archive_module

    session = synth_session(tmp_path / "250919", sources=("mic",))
    seen: list[tuple[str, str]] = []

    def fake_convert(pcm_path, mp3_path, *args, **kwargs) -> bool:
        seen.append((str(pcm_path), str(mp3_path)))
        Path(mp3_path).write_bytes(b"mp3-from-the-one-implementation")
        return True

    monkeypatch.setattr(archive_module, "convert_to_mp3", fake_convert)

    outcome = sync_archive(session, sources=("mic",))

    assert outcome["state"] == "done"
    assert [row["outcome"] for row in outcome["sources"]] == ["converted"]
    assert len(seen) == 1, f"转码该只被调一次，实测 {seen}"
    assert Path(seen[0][0]).name == "meeting_audio_mic_001.pcm"
    assert Path(seen[0][1]).name == "meeting_audio_mic_001.mp3"


def test_an_empty_pcm_produces_no_mp3_and_calls_no_converter(tmp_path: Path) -> None:
    """空 PCM（0 字节）→ **不产 MP3、也不调转码**（采集侧没选中的那一路就是这个形状）。"""
    session = synth_session(tmp_path / "250919", sources=("mic",), empty=("mic",))
    converter = RecordingConverter()

    outcome = sync_archive(session, sources=("mic",), converter=converter)

    assert [row["outcome"] for row in outcome["sources"]] == ["no_work"]
    assert outcome["state"] == "skipped"
    assert converter.calls == []
    assert not (session / "meeting_audio_mic_001.mp3").exists()


def test_a_one_byte_pcm_counts_as_empty(tmp_path: Path) -> None:
    """1 字节的残缺 PCM（装不下一个 int16 样本）同样按空处理。"""
    session = synth_session(tmp_path / "250919", sources=("mic",))
    (session / "meeting_audio_mic_001.pcm").write_bytes(b"\x01")
    converter = RecordingConverter()

    outcome = sync_archive(session, sources=("mic",), converter=converter)

    assert [row["outcome"] for row in outcome["sources"]] == ["no_work"]
    assert converter.calls == []
    assert not (session / "meeting_audio_mic_001.mp3").exists()


def test_an_mp3_newer_than_the_pcm_is_left_alone(tmp_path: Path) -> None:
    """MP3 的 mtime 已比 PCM 新 → **不调转码**，MP3 字节一个都不变。"""
    session = synth_session(tmp_path / "250919", sources=("mic",))
    mp3 = session / "meeting_audio_mic_001.mp3"
    mp3.write_bytes(b"already-archived")
    pcm = session / "meeting_audio_mic_001.pcm"
    old = time.time() - 600
    os.utime(pcm, (old, old))
    os.utime(mp3, (time.time(), time.time()))
    before = digest(mp3)
    converter = RecordingConverter()

    outcome = sync_archive(session, sources=("mic",), converter=converter)

    assert [row["outcome"] for row in outcome["sources"]] == ["no_work"]
    assert converter.calls == []
    assert digest(mp3) == before


def test_an_mp3_older_than_the_pcm_is_converted_again(tmp_path: Path) -> None:
    """MP3 在场但比 PCM **旧** → 转一次（这一路是新采的音频，归档得跟上）。"""
    session = synth_session(tmp_path / "250919", sources=("mic",))
    mp3 = session / "meeting_audio_mic_001.mp3"
    mp3.write_bytes(b"stale")
    pcm = session / "meeting_audio_mic_001.pcm"
    os.utime(mp3, (time.time() - 600, time.time() - 600))
    os.utime(pcm, (time.time(), time.time()))
    converter = RecordingConverter(body=b"fresh")

    outcome = sync_archive(session, sources=("mic",), converter=converter)

    assert [row["outcome"] for row in outcome["sources"]] == ["converted"]
    assert len(converter.calls) == 1
    assert mp3.read_bytes() == b"fresh"


def test_a_failed_conversion_keeps_the_pcm_byte_for_byte(tmp_path: Path) -> None:
    """转码抛错 → 该路记 `failed`、PCM 的 sha256 与转换前**逐字节相同**。"""
    session = synth_session(tmp_path / "250919", sources=("mic",))
    pcm = session / "meeting_audio_mic_001.pcm"
    before = digest(pcm)
    converter = RecordingConverter(raises="ffmpeg 不在 PATH 上")

    outcome = sync_archive(session, sources=("mic",), converter=converter)

    row = outcome["sources"][0]
    assert row["outcome"] == "failed"
    assert outcome["state"] == "failed"
    assert "ffmpeg 不在 PATH 上" in str(row["reason"])
    assert digest(pcm) == before, "转码失败不许动 PCM 一个字节"
    assert not (session / "meeting_audio_mic_001.mp3").exists()


def test_a_failed_conversion_puts_a_stale_mp3_back(tmp_path: Path) -> None:
    """转砸了那次可能写了一半：盘上那份旧 MP3 要原样放回去。"""
    session = synth_session(tmp_path / "250919", sources=("mic",))
    mp3 = session / "meeting_audio_mic_001.mp3"
    mp3.write_bytes(b"the-old-one")
    pcm = session / "meeting_audio_mic_001.pcm"
    os.utime(mp3, (time.time() - 600, time.time() - 600))
    os.utime(pcm, (time.time(), time.time()))
    before = digest(mp3)

    def half_written(pcm_path, mp3_path) -> bool:
        Path(mp3_path).write_bytes(b"half")
        raise RuntimeError("写到一半 ffmpeg 挂了")

    outcome = sync_archive(session, sources=("mic",), converter=half_written)

    assert outcome["state"] == "failed"
    assert digest(mp3) == before, "转码失败后 MP3 的现场该复原"


def test_one_broken_conversion_does_not_stop_the_other_source(tmp_path: Path) -> None:
    """两路各自独立判、独立转：一路转不动，另一路照常转出来。"""
    session = synth_session(tmp_path / "250919")

    def picky(pcm_path, mp3_path) -> bool:
        if Path(pcm_path).name.startswith("meeting_audio_mic_"):
            raise RuntimeError("mic 这一路转不动")
        Path(mp3_path).write_bytes(b"loopback-mp3")
        return True

    outcome = sync_archive(session, converter=picky)

    rows = {row["source"]: row for row in outcome["sources"]}
    assert rows["mic"]["outcome"] == "failed"
    assert rows["loopback"]["outcome"] == "converted"
    assert outcome["state"] == "failed"
    assert (session / "meeting_audio_loopback_001.mp3").read_bytes() == b"loopback-mp3"


def test_the_archive_uses_the_sources_the_session_json_declares(tmp_path: Path) -> None:
    """`sources` 缺省取 `session.json` 声明的那几路（缺省不是写死的两路全集）。"""
    session = synth_session(tmp_path / "250919", sources=("mic",))
    converter = RecordingConverter()

    outcome = sync_archive(session, converter=converter)

    assert [row["source"] for row in outcome["sources"]] == ["mic"]


def test_a_failed_archive_takes_the_whole_run_to_failure(tmp_path: Path) -> None:
    """归档那一层 `failed` → 这一趟的摘要里 `exit_code` 是 `EXIT_FAILURE`。"""
    session = synth_session(tmp_path / "250919", sources=("mic",))
    fake_engine = FakeEngine()

    summary = run_post(
        session,
        engine=fake_engine,
        archive_converter=RecordingConverter(raises="ffmpeg 缺席"),
    )

    assert summary["archive"]["state"] == "failed"
    assert summary["exit_code"] == EXIT_FAILURE
    assert summary["ok"] is False
    assert summary["exit_code_name"] == "EXIT_FAILURE"
    assert fake_engine.calls, "归档转不动不该挡住离线那一趟"


# ---------------------------------------------------------------------------
# 三、编排面：一条链跑完、摘要与终端都说得出
# ---------------------------------------------------------------------------


def test_run_post_reports_every_layer_with_absolute_paths(tmp_path: Path) -> None:
    """注入假引擎 + 假启动器跑一遍：摘要里每层结论与逐条产物**绝对路径**齐。

    这一趟的假启动器**照真子会话的收工样子落终态**（`SettlingLauncher`）：`hook ==
    "launched"` 那一档一律等这一趟新落的终态，只记账不落终态的假件会等到上界。
    """
    session = synth_session(tmp_path / "250919")
    fake_engine = FakeEngine()
    launcher = SettlingLauncher()
    converter = RecordingConverter()

    summary = run_post(
        session,
        engine=fake_engine,
        minutes_launcher=launcher,
        archive_converter=converter,
    )

    assert summary["exit_code"] == EXIT_OK, summary["error"]
    assert summary["session_dir"] == str(session.resolve())
    assert summary["session_id"] == "250919"
    assert summary["archive"]["state"] == "done"
    assert summary["offline"]["state"] == "done"
    assert summary["offline"]["hook"] == "done"
    assert summary["minutes"]["state"] == "done"
    assert summary["minutes"]["hook"] == "finished", (
        "拉起那一档的结论出自**等到的那一版终态**（`hook` 与产品路径同一套口径），"
        "而不是拉起那一刻的交回值"
    )

    artifacts = summary["artifacts"]
    assert artifacts, "这一趟该报出至少一份产物"
    for name, path in artifacts.items():
        assert Path(path).is_absolute(), f"{name} 该报绝对路径，实测 {path}"
        assert Path(path).is_file(), f"{name} 该真的在盘上：{path}"
    assert any("离线转写" in name for name in artifacts)
    assert any("归档 MP3" in name for name in artifacts)

    # 假启动器被调到一次，它落的终态就是这一趟的账（结论出自这一趟新落的那一版）
    assert launcher.calls == [str(session)]


def test_an_injected_launcher_that_settles_is_waited_for(tmp_path: Path) -> None:
    """注入的假启动器**落终态**时，这一趟等它落定再出结论（等待不按「谁拉起的」分叉）。

    盘上先留一份**上一趟**的 `running`；带 `--force` 让这一趟真的重跑纪要那一层。假件照
    真启动器的时序来（`DelayedLauncher`：返回即算启动成功，终态隔一会儿才落），因此拉起
    那一刻盘上还没有这一趟的终态 —— 等到的 `done` 是**这一趟新落的那一版**，
    `waited_seconds` 也因此量得出「真的等过」，而不是读盘读出来的旧账。
    """
    session = synth_session(tmp_path / "250919")
    write_minutes_status(session, state="running")
    launcher = DelayedLauncher(latency=0.05)

    summary = run_post(session, engine=FakeEngine(), minutes_launcher=launcher, force=True)

    for thread in launcher.threads:
        thread.join(timeout=2.0)

    assert launcher.calls == [str(session)]
    assert summary["minutes"]["hook"] == "finished", "结论出自等到的这一趟的终态"
    assert summary["minutes"]["state"] == "done", "结论按等到的这一趟的终态出"
    # 等待落在产品代码那条轮询上：上界内落定，不空等满上界
    assert summary["minutes"]["waited_seconds"] >= 0.02, summary["minutes"]
    assert (session / MINUTES_STATUS_NAME).is_file()


def test_the_fake_launcher_gets_the_session_directory(tmp_path: Path) -> None:
    """假启动器收到的是**这一场的场次目录**（编排函数把参数一路带到位）。"""
    session = synth_session(tmp_path / "250919")
    launcher = SettlingLauncher()

    run_post(session, engine=FakeEngine(), minutes_launcher=launcher)

    assert launcher.calls == [str(session)]


def test_the_child_flag_still_stops_before_the_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既有那道再入闸一个字没动：`MEETING_MINUTES_CHILD=1` 时产品路径的启动器一次都没被调到。

    这一条走的是**产品路径**（`run_post` 不注入假启动器）：子会话标记在场时那道闸挡在
    `minutes.hook` 的解析之前，`resolve_minutes_hook()` 连调都不该被调到。
    注入替身的那一档不受这道闸约束（见「注入的假启动器一路向下传」那几条用例）。
    """
    session = synth_session(tmp_path / "250919")
    resolved: list[str] = []
    monkeypatch.setenv("MEETING_MINUTES_CHILD", "1")
    monkeypatch.setattr(
        "meeting.backends.registry.resolve_minutes_hook",
        lambda *a, **k: resolved.append("resolve") or FakeResolution(FakeHook()),
    )

    summary = run_post(session, engine=FakeEngine())

    assert resolved == [], "子会话标记在场时连解析都不该发生"
    assert summary["offline"]["announce"]["hook"] == "skipped"
    assert not (session / MINUTES_STATUS_NAME).exists()


def test_an_injected_launcher_is_reachable_in_a_child_session(tmp_path: Path) -> None:
    """注入的假启动器**不受子会话标记约束**：调用方能直接核「这一趟有没有再花钱」。

    `tests/conftest.py` 的 autouse 兜底立着 `MEETING_MINUTES_CHILD=1`（挡住真 DSH 子会话），
    而注入的替身本来就是「真拉起」这一步的替身、自己什么都不起 —— 它因此照常被调到，
    用例靠它的调用记账出结论。**注入件不替换任何模块全局属性**：一个进程里两条入口并发时
    各自拿自己那一份（见 `test_two_entries_can_inject_launchers_at_the_same_time`）。

    假件照真子会话的收工样子落终态（`SettlingLauncher`）：拉起那一档要等的是**这一趟新落的
    终态**，因此这一格同时也是「注入路径也等落定」的一份实录。
    """
    session = synth_session(tmp_path / "250919")
    launcher = SettlingLauncher()

    summary = run_post(session, engine=FakeEngine(), minutes_launcher=launcher)

    assert launcher.calls == [str(session)]
    assert summary["minutes"]["hook"] == "finished", "等到的这一趟的终态"
    assert summary["minutes"]["state"] == "done", "等到的这一趟的终态"


def test_a_missing_session_directory_is_reported_not_raised(tmp_path: Path) -> None:
    """场次目录不在场时编排函数如实记一条（`post()` 的那条校验在它前面挡住）。"""
    missing = tmp_path / "260920"

    summary = run_post(missing, engine=FakeEngine(), archive_converter=RecordingConverter())

    assert summary["exit_code"] == EXIT_FAILURE
    assert "场次目录不存在" in str(summary["error"])


def test_an_exploding_offline_run_is_recorded_and_re_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """离线那一层整个抛出来时：状态文件补成 `failed`，异常**原样上抛**给调用方。

    上抛是**两条入口各自的账**都要落齐的前提：`meeting post` 接住它按 `EXIT_FAILURE`
    收场（下一条用例），收尾第 4 步的线程体接住它、按既有契约把那一格记成 `failed`
    （`tests/test_post_hook.py` 那两条盯着）。
    """
    session = synth_session(tmp_path / "250919")

    def boom(session_dir, **_kwargs):
        raise RuntimeError("离线段炸了")

    launcher = RecordingLauncher()
    monkeypatch.setattr(offline_runner, "run_session", boom)

    with pytest.raises(RuntimeError) as info:
        run_post(session, minutes_launcher=launcher, archive_converter=RecordingConverter())

    assert "离线段炸了" in str(info.value)
    written = json.loads((session / OFFLINE_STATUS_NAME).read_text(encoding="utf-8"))
    assert written["state"] == "failed"
    assert "离线段炸了" in str(written["error"])
    # 离线那一趟没走到尾钩：注入的假启动器一次都没被调到
    assert launcher.calls == []
    # 注入的假启动器**不落在模块全局上**：`_announce_minutes` 始终是模块自己那一个函数，
    # 于是一个进程里两条入口并发时谁都不会把对方的替身当成原函数
    assert offline_runner._announce_minutes.__qualname__ == "_announce_minutes"
    assert offline_runner._announce_minutes.__module__ == "meeting.offline.runner"


#: 盘上那一版兜底账里那句**更准的原因**（`engine_load_error` 这个词档只在 `runner` 那一版里）。
#: 用字面量而不是 import 一个常量：这一条要钉的是「这**一句话**别被盖掉」，
#: 换一份错的原因、或者错在别的档上，都该让这条用例红。
DISK_SIDE_REASON = "mic：engine_load_error：OSError: 模型快照读不动（合成）"


def test_the_callers_ledger_keeps_the_reason_the_runner_wrote_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """调用方盖账之后，顶层 `error` 里那个更准的原因**还读得出来**。

    这一条盯的是本节点 m2 那一档：`run_session` 逃出去之前，离线段自己**已经现场写下**
    一版（`runner._write_failed_status()`），随后 `run_post` 接住异常、调
    `_mark_offline_failed()` 把同一份文件重写一遍。从前那一版被一句通用串
    （`RuntimeError: 离线段炸了`）整句替换掉，于是「是加载失败还是跑失败」在顶层这一格上
    就没了 —— 而那句话一秒前才写好。现在改写前先读回盘上那一版，它已经是终态就沿用它的
    `error`。

    `run_session` 换成一条替身（真离线段那两条用例在 `tests/test_offline_runner.py`），
    它做的正是那一版账：落 `failed` + runner 那句原话，再把异常抛出去。
    """
    session = synth_session(tmp_path / "250919")
    status_path = session / OFFLINE_STATUS_NAME

    def an_engine_that_never_loaded(session_dir, **_kwargs):
        offline_artifacts.write_status(
            status_path,
            session_id="250919",
            state="failed",
            started_at="2025-09-19T20:00:00",
            sources=["mic", "loopback"],
            artifacts={},
            finished_at="2025-09-19T20:00:03",
            error=DISK_SIDE_REASON,
        )
        raise RuntimeError("离线段炸了")

    monkeypatch.setattr(offline_runner, "run_session", an_engine_that_never_loaded)

    with pytest.raises(RuntimeError, match="离线段炸了"):
        run_post(session, minutes_launcher=RecordingLauncher())

    written = json.loads(status_path.read_text(encoding="utf-8"))
    assert written["state"] == "failed"
    assert DISK_SIDE_REASON in str(written["error"]), written["error"]
    # 场次编号 / 开始时刻 / 路清单照读回的那一版（补账不改这几格）
    assert written["session_id"] == "250919"
    assert written["started_at"] == "2025-09-19T20:00:00"
    assert written["sources"] == ["mic", "loopback"]
    assert written["finished_at"], "收工时刻这一次补账要写上新的一版"


def test_the_callers_ledger_keeps_a_partial_run_as_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """盘上那一版是 `partial`（一路整条没交回、另一路跑成了）时，补账**不把它翻成 `failed`**。

    顶层 `failed` 那句话是「每一路都没交回」；用在这里会把「另一路跑成了」这件事抹掉，
    与逐路那一格也自相矛盾。这一条钉住补账只认「盘上已经是终态」这一点，终态是哪一档照它。
    """
    session = synth_session(tmp_path / "250919")
    status_path = session / OFFLINE_STATUS_NAME
    dead_row = {
        "state": "failed",
        "reason": "engine_error",
        "error": "mic：engine_error：这一路解不出来",
    }

    def a_run_that_gave_up_on_one_source(session_dir, **_kwargs):
        offline_artifacts.write_status(
            status_path,
            session_id="250919",
            state="partial",
            started_at="2025-09-19T20:00:00",
            sources=["mic", "loopback"],
            artifacts={"mic": dict(dead_row), "loopback": {"state": "done", "reason": None}},
            finished_at="2025-09-19T20:00:09",
            error="mic：engine_error：这一路解不出来",
        )
        raise RuntimeError("收尾那一步炸了")

    monkeypatch.setattr(offline_runner, "run_session", a_run_that_gave_up_on_one_source)

    with pytest.raises(RuntimeError, match="收尾那一步炸了"):
        run_post(session, minutes_launcher=RecordingLauncher())

    written = json.loads(status_path.read_text(encoding="utf-8"))
    assert written["state"] == "partial", written["state"]
    assert "这一路解不出来" in str(written["error"])
    assert written["artifacts"]["loopback"]["state"] == "done"
    # 逐路那一格也照读回的那一版（补账只写顶层那几格，逐路一个字不动）
    assert written["artifacts"]["mic"]["reason"] == "engine_error"


def test_post_reports_an_exploding_offline_run_as_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`post()` 接住编排函数上抛的那一档：终端有结论、退出码 `EXIT_FAILURE`。"""
    session = synth_session(tmp_path / "250919")

    def boom(session_dir, **_kwargs):
        raise RuntimeError("离线段炸了")

    monkeypatch.setattr(offline_runner, "run_session", boom)

    with pytest.raises(SystemExit) as info:
        post([str(session)])

    assert info.value.code == EXIT_FAILURE
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "结论：failed" in combined
    assert "RuntimeError" in combined


def test_run_post_needs_a_session_directory() -> None:
    """场次目录是空值时当场抛（没有落点就装作跑过，摘要会指向一个不存在的目录）。"""
    with pytest.raises(ValueError):
        run_post("")


def test_the_cli_entry_leaves_the_account_on_disk(tmp_path: Path) -> None:
    """命令行这一条入口跑完一趟，逐块账上两路各一行（页面读那一场不该说"什么都没跑"）。

    「每跑完一块就落一次盘」跟着**离线这条链**走，不挑是哪个按钮拉起的：跑完
    离线段收工入口（`meeting/cli.py` 的 `run_post()`，`meeting post` 与会后钩子共用）
    之后，场次目录里那份 `meeting_offline_progress.jsonl` 上该有每一路的行 ——
    与「生成会后精细稿」那个按钮拉起的同一趟给的是同一份读数。
    """
    session = synth_session(tmp_path / "250919")
    engine = FakeEngine()

    summary = run_post(session, engine=engine, archive_converter=RecordingConverter())

    header, rows = offline_progress.read_progress(offline_progress.ledger_path(session))
    assert summary["offline"]["state"] == "done"
    assert header is not None, "账的表头该在盘上（这一趟是命令行拉起的）"
    assert header["session_id"] == "250919"
    assert header["sources"] == ["mic", "loopback"]
    assert [row["source"] for row in rows] == ["mic", "loopback"], rows
    assert [row["chunk"] for row in rows] == [1, 1]
    assert {row["state"] for row in rows} == {"transcribed"}
    assert all(row["text"] for row in rows), "每一块的字都该在账上"
    assert engine.calls, "这一趟该真的调过引擎（账上的行出自这一趟实跑）"


def test_run_post_takes_an_injected_ledger(tmp_path: Path) -> None:
    """注入的那一份账优先：调用方自己立一份时，落盘落在它身上（下游复用这一格）。"""
    session = synth_session(tmp_path / "250919")
    injected = offline_progress.OfflineProgressLedger(
        session, session_id="250919", sources=["mic", "loopback"]
    )

    run_post(
        session,
        engine=FakeEngine(),
        archive_converter=RecordingConverter(),
        progress=injected,
    )

    _header, rows = offline_progress.read_progress(offline_progress.ledger_path(session))
    assert [row["source"] for row in rows] == ["mic", "loopback"]
    # 现场那一份（注入件自己手里的）就是这一趟的账：跑完收在终态上
    assert injected.snapshot()["state"] == "done"


# ---------------------------------------------------------------------------
# 四、编排只走一条路：`run_session` 经模块属性取
# ---------------------------------------------------------------------------


def test_run_post_calls_run_session_through_the_module_attribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """换掉 `runner.run_session` 之后**替身真的被调到**（不是 `from … import` 拿走的副本）。

    替身同时把「注入的两个参数是**经 kwargs 向下传**的」记下来：`minutes_launcher` 那一格
    收到一个可调对象（假启动器本身递到了位），`force` 那一格与这一趟的取值一致。
    """
    session = synth_session(tmp_path / "250919")
    seen: list[tuple[str, Any, Any, Any]] = []
    fake_engine = FakeEngine()

    def stand_in(session_dir, *, engine=None, minutes_launcher=None, force=False, **_kwargs):
        seen.append((str(session_dir), engine, minutes_launcher, force))
        offline_artifacts.write_status(
            Path(session_dir) / OFFLINE_STATUS_NAME,
            session_id=Path(session_dir).name,
            state="done",
            started_at="2025-09-19T22:00:00",
            sources=["mic", "loopback"],
            artifacts={},
        )
        return {"session_dir": str(session_dir), "state": "done"}

    monkeypatch.setattr(offline_runner, "run_session", stand_in)
    launcher = RecordingLauncher(result={"hook": "launched", "announced": True})

    summary = run_post(
        session,
        engine=fake_engine,
        minutes_launcher=launcher,
        archive_converter=RecordingConverter(),
    )

    assert len(seen) == 1, f"替身该被调到一次，实测 {seen}"
    assert seen[0][0] == str(session), "替身该收到这一场的目录"
    assert seen[0][1] is fake_engine, "替身该收到注入的那台引擎"
    assert callable(seen[0][2]), "假启动器该经 kwargs 向下传（不是落在模块全局上）"
    assert seen[0][3] is False, "不带 --force 时那一格是 False"
    assert summary["offline"]["state"] == "done"
    assert summary["exit_code"] == EXIT_OK


def test_the_engine_is_handed_to_the_offline_layer(tmp_path: Path) -> None:
    """注入的假引擎真的被离线那一层用上（两路各调一次）。"""
    session = synth_session(tmp_path / "250919")
    fake_engine = FakeEngine()

    run_post(session, engine=fake_engine, archive_converter=RecordingConverter())

    assert [Path(call).name for call in fake_engine.calls] == [
        "meeting_audio_mic_001.pcm",
        "meeting_audio_loopback_001.pcm",
    ]


# ---------------------------------------------------------------------------
# 五、两条入口同源
# ---------------------------------------------------------------------------


def test_the_post_hook_runs_the_same_orchestration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PostProcessHook._run()` 调到的是同一个 `run_post`（换成记账替身，断言调到一次）。"""
    session = synth_session(tmp_path / "250919")
    seen: list[tuple[str, dict]] = []

    def stand_in(session_dir, **kwargs):
        seen.append((str(session_dir), dict(kwargs)))
        return {"session_dir": str(session_dir), "state": "done", "exit_code": EXIT_OK}

    monkeypatch.setattr(cli_module, "run_post", stand_in)

    post_module.PostProcessHook()._run(str(session))

    assert len(seen) == 1, f"编排函数该被调到一次，实测 {seen}"

    # 线程体不显式传参：`force` 缺省 `False`、其余假件缺省 `None`（走产品路径）
    def fake_run_post(session_dir, **kwargs):
        seen.clear()
        seen.append((str(session_dir), dict(kwargs)))
        return {"session_dir": str(session_dir), "state": "done", "exit_code": EXIT_OK}

    monkeypatch.setattr(cli_module, "run_post", fake_run_post)
    post_module.PostProcessHook()._run(str(session))

    assert seen == [(str(session), {})], f"第二条入口该原样交给编排函数：{seen}"


def test_the_post_hook_still_returns_immediately(tmp_path: Path, monkeypatch) -> None:
    """`launch()` 仍然**立刻返回**（本单元不许把它变成同步等待）。"""
    import threading

    session = synth_session(tmp_path / "250919")
    entered = threading.Event()
    release = threading.Event()

    def blocking(session_dir, **_kwargs):
        entered.set()
        release.wait(timeout=10.0)
        return {"session_dir": str(session_dir)}

    monkeypatch.setattr(cli_module, "run_post", blocking)

    begun = time.monotonic()
    post_module.PostProcessHook().launch(str(session))
    elapsed = time.monotonic() - begun

    assert elapsed < 0.5, f"launch() 该当场返回，实测 {elapsed:.3f}s"
    assert entered.wait(timeout=10.0), "后台那一条线程该已经跑起来"
    assert not release.is_set()
    release.set()


def test_a_failing_orchestration_still_lands_in_the_offline_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """编排函数整个抛出来时，钩子把状态文件补成 `failed`，异常不外传。"""
    session = synth_session(tmp_path / "250919")

    def boom(session_dir, **_kwargs):
        raise RuntimeError("编排函数整个炸了")

    monkeypatch.setattr(cli_module, "run_post", boom)

    post_module.PostProcessHook()._run(str(session))

    document = json.loads((session / OFFLINE_STATUS_NAME).read_text(encoding="utf-8"))
    assert document["state"] == "failed"
    assert "编排函数整个炸了" in str(document["error"])


# ---------------------------------------------------------------------------
# 六、纪要那一层起跑之前那道闸
# ---------------------------------------------------------------------------


def test_the_gate_keeps_the_launcher_away_when_minutes_are_done(tmp_path: Path) -> None:
    """终态 `done` + 纪要过得了四条判据 → **不调启动器**、返回里 `announced is False`。"""
    session = synth_session(tmp_path / "250919")
    write_minutes_status(session, state="done")
    write_final(session)
    launcher = RecordingLauncher()

    outcome = offline_runner._announce_minutes(session, launcher=launcher)

    assert launcher.calls == [], "已是终态且产物齐，这一趟不许再花一次钱"
    assert outcome["announced"] is False
    assert outcome["hook"] == "already_done"
    assert outcome["state"] == "done"


def test_removing_the_final_artifact_lets_the_launcher_through_again(tmp_path: Path) -> None:
    """把 `meeting_final.md` 删掉之后再调 → 启动器**被调一次**（缺什么补什么）。"""
    session = synth_session(tmp_path / "250919")
    write_minutes_status(session, state="done")
    write_final(session)
    launcher = RecordingLauncher()
    assert offline_runner._announce_minutes(session, launcher=launcher)["hook"] == "already_done"

    (session / MINUTES_FINAL_NAME).unlink()
    outcome = offline_runner._announce_minutes(session, launcher=launcher)

    assert launcher.calls == [str(session)], "产物缺失时该按「缺什么补什么」再拉一次"
    assert outcome["announced"] is True
    assert outcome["hook"] == "launched"


def test_a_hollow_final_artifact_is_not_enough(tmp_path: Path) -> None:
    """产物在但过不了那四条判据（空壳附录）→ 照样再拉一次。"""
    session = synth_session(tmp_path / "250919")
    write_minutes_status(session, state="done")
    (session / MINUTES_FINAL_NAME).write_text(
        "# 正文\n\n## 说话人附录\n\n| 路 | 编号 |\n| --- | --- |\n\n### 猜不出的\n",
        encoding="utf-8",
    )
    launcher = RecordingLauncher()

    outcome = offline_runner._announce_minutes(session, launcher=launcher)

    assert launcher.calls == [str(session)]
    assert outcome["hook"] == "launched"


def test_a_running_minutes_layer_is_not_a_finished_one(tmp_path: Path) -> None:
    """状态文件停在 `running`（哪怕产物已经在）→ 这一趟交给启动器，闸不放行。"""
    session = synth_session(tmp_path / "250919")
    write_minutes_status(session, state="running")
    write_final(session)
    launcher = RecordingLauncher()

    outcome = offline_runner._announce_minutes(session, launcher=launcher)

    assert launcher.calls == [str(session)]
    assert outcome["hook"] == "launched"


def test_a_failed_minutes_layer_is_retried(tmp_path: Path) -> None:
    """状态文件落 `failed` → 这一趟再试一次（那一趟没交出纪要）。"""
    session = synth_session(tmp_path / "250919")
    write_minutes_status(session, state="failed")
    write_final(session)
    launcher = RecordingLauncher()

    outcome = offline_runner._announce_minutes(session, launcher=launcher)

    assert launcher.calls == [str(session)]
    assert outcome["hook"] == "launched"


def test_no_status_file_means_no_gate(tmp_path: Path) -> None:
    """状态文件不在场 → 闸不放行（第一遍跑的就是这一档）。"""
    session = synth_session(tmp_path / "250919")
    write_final(session)
    launcher = RecordingLauncher()

    outcome = offline_runner._announce_minutes(session, launcher=launcher)

    assert launcher.calls == [str(session)]
    assert outcome["hook"] == "launched"


def test_an_unreadable_status_file_lets_the_launcher_through(tmp_path: Path) -> None:
    """状态文件坏掉（不是 JSON）→ 按「没跑完」放行，交给启动器。"""
    session = synth_session(tmp_path / "250919")
    (session / MINUTES_STATUS_NAME).write_text("{ 这不是 JSON", encoding="utf-8")
    write_final(session)
    launcher = RecordingLauncher()

    outcome = offline_runner._announce_minutes(session, launcher=launcher)

    assert launcher.calls == [str(session)]
    assert outcome["hook"] == "launched"


def test_the_gate_reads_the_disk_and_writes_nothing(tmp_path: Path) -> None:
    """那道闸只读盘：跑完之后场次目录里的文件集合与内容一个字节都不变。"""
    session = synth_session(tmp_path / "250919")
    status_path = write_minutes_status(session, state="done")
    final_path = write_final(session)
    before = {
        path.name: digest(path)
        for path in sorted(session.iterdir())
        if path.is_file()
    }

    outcome = offline_runner._minutes_gate(session)

    after = {
        path.name: digest(path)
        for path in sorted(session.iterdir())
        if path.is_file()
    }
    assert after == before
    assert outcome == {
        "hook": "already_done",
        "announced": False,
        "state": "done",
        "status": MINUTES_STATUS_NAME,
        "status_path": str(status_path),
        "final": str(final_path),
        "reason": "纪要已是终态且 meeting_final.md 过得了那四条判据，不再拉起子会话",
    }


def test_the_gate_is_open_when_the_status_file_is_absent(tmp_path: Path) -> None:
    """终态文件不在场时闸交回 `None`（调用方照常解析并拉起）。"""
    session = synth_session(tmp_path / "250919")

    assert offline_runner._minutes_gate(session) is None


def test_the_offline_status_file_is_untouched_by_the_gate(tmp_path: Path) -> None:
    """既有那三条边界一条都没退化：离线状态文件与三份产物在闸跑过之后一个字节不变。"""
    session = synth_session(tmp_path / "250919")
    offline_runner.run_session(
        session, engine=FakeEngine(), config=fake_config(**{"post.chunk_seconds": 450})
    )
    tracked = [
        path
        for path in sorted(session.iterdir())
        if path.is_file() and path.name != MINUTES_STATUS_NAME
    ]
    before = {path.name: digest(path) for path in tracked}

    write_minutes_status(session, state="done")
    write_final(session)
    offline_runner._announce_minutes(session, launcher=RecordingLauncher())

    after = {path.name: digest(path) for path in tracked}
    assert after == before


# ---------------------------------------------------------------------------
# 七、等纪要终态：有界、落定就走
# ---------------------------------------------------------------------------


def test_the_wait_returns_as_soon_as_the_terminal_state_lands(tmp_path: Path) -> None:
    """状态文件一落终态就返回，不等满上界（拿一个宽裕的上界跑，实测耗时远小于它）。"""
    session = synth_session(tmp_path / "250919")
    write_minutes_status(session, state="done")

    outcome = _minutes_outcome(session, timeout=600.0)

    assert outcome["hook"] == "finished"
    assert outcome["state"] == "done"
    assert outcome["waited_seconds"] < 1.0
    assert outcome["final"] is None


def test_the_wait_gives_up_at_its_deadline(tmp_path: Path) -> None:
    """上界到点还没落定 → 记 `timeout`，退出码走 `EXIT_FAILURE`（不无限等）。"""
    session = synth_session(tmp_path / "250919")
    write_minutes_status(session, state="running")

    outcome = _minutes_outcome(session, timeout=0.0)

    assert outcome["hook"] == "timeout"
    assert outcome["state"] == "timeout"
    assert "还没落定" in str(outcome["reason"])
    assert cli_module._minutes_exit_code(outcome) == EXIT_FAILURE


def test_an_absent_minutes_layer_is_not_a_failure() -> None:
    """缺席档（`minutes.hook = null` / 子会话闸）不算这一趟失败。"""
    assert cli_module._minutes_exit_code({"hook": "absent", "state": None}) == EXIT_OK
    assert cli_module._minutes_exit_code({"hook": "skipped", "state": None}) == EXIT_OK
    assert cli_module._minutes_exit_code({"hook": "already_done", "state": "done"}) == EXIT_OK
    assert cli_module._minutes_exit_code({"hook": "finished", "state": "failed"}) == EXIT_FAILURE


def test_a_child_session_skip_on_the_product_path_is_a_quiet_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**产品路径撞上再入闸 = 安静跳过**：档位与「注入假启动器的那一档」逐字同格。

    走到 `_launch_minutes_on_the_product_path()` 兜底分支的现场是「离线段已是终态、纪要段
    没到终态」，而这一趟是 DSH 子会话自己（`MEETING_MINUTES_CHILD=1`）：`_announce_minutes()`
    交回 `hook = "skipped"`（**交回值逐字照 `meeting/offline/runner.py` 那一档抄的**）。

    这一格此前掉进兜底里：`hook` 报成 `finished`、`state` 报成 `failed`、退出码
    `EXIT_FAILURE`，还把再入闸的理由当失败原因念出来 —— 同一件事在注入那一档却是
    `skipped` / `EXIT_OK`。现在两条路对齐：**除 `launcher` 之外逐格相同**，
    **不记失败**，而 `launcher` 那一格照旧写 `product`（这一趟走的是产品路径）。

    判据落在**同一份交回值**的两条出口上：兜底分支直接交回的那一份，与
    `_minutes_from_launcher()` 处理同一份交回值的那一份 —— 两边因此必须逐格相等。
    """
    child_skip = {
        "hook": "skipped",
        "announced": False,
        "reason": "子会话（MEETING_MINUTES_CHILD=1）",
    }
    monkeypatch.setattr(
        offline_runner, "_announce_minutes", lambda *a, **k: dict(child_skip)
    )
    session = synth_session(tmp_path / "250919")

    product = cli_module._launch_minutes_on_the_product_path(
        session,
        force=False,
        deadline=None,
        baseline=cli_module._minutes_baseline(session),
    )
    injected = cli_module._minutes_from_launcher(
        session, dict(child_skip), baseline=cli_module._minutes_baseline(session)
    )

    assert product["hook"] == "skipped", product
    assert product["state"] is None, product
    assert {
        key: value for key, value in product.items() if key != "launcher"
    } == {
        key: value for key, value in injected.items() if key != "launcher"
    }, (product, injected)
    assert product["launcher"] == "product", "这一趟走的是产品路径"
    assert cli_module._minutes_exit_code(product) == EXIT_OK, "安静跳过不记失败"
    assert cli_module._minutes_exit_code(product) == cli_module._minutes_exit_code(injected)


def test_an_absent_minutes_layer_on_the_product_path_still_reports_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`minutes.hook` 已配、解析却交回「没有实现」那一档**照旧如实记失败**。

    这是「安静跳过」那条新分支的边界：它对再入闸那一档生效，而 `absent` 说的是另一件事
    （配置里写着要跑、解析结果里一个实现都没有）—— 现成的用例
    （`tests/test_post_idempotency.py::test_the_product_path_still_reports_a_launch_that_fails`
    的 `no-implementation` 一格）把它钉在 `EXIT_FAILURE` 上，这一格因此一个字不动。
    """
    monkeypatch.setattr(
        offline_runner,
        "_announce_minutes",
        lambda *a, **k: {
            "hook": "absent",
            "announced": False,
            "selector": "null",
            "reason": "minutes.hook 缺席（自动跑已关掉）",
        },
    )
    session = synth_session(tmp_path / "250919")

    outcome = cli_module._launch_minutes_on_the_product_path(
        session,
        force=False,
        deadline=None,
        baseline=cli_module._minutes_baseline(session),
    )

    assert outcome["hook"] == "finished", outcome
    assert outcome["state"] == "failed", outcome
    assert cli_module._minutes_exit_code(outcome) == EXIT_FAILURE


def test_the_wait_upper_bound_comes_from_the_minutes_wall_clock_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上界取现成的纪要墙钟键 `minutes.timeout_seconds`。"""
    monkeypatch.setattr(
        config_module, "get", lambda key, default=None: 60 if key == "minutes.timeout_seconds" else default
    )

    assert cli_module._minutes_timeout_seconds() == 60.0


# ---------------------------------------------------------------------------
# 八、退出码只走现成那五档
# ---------------------------------------------------------------------------


def test_every_exit_the_post_entry_uses_is_a_frozen_constant() -> None:
    """`post()` 的每个出口都是 `sys.exit(<常量>)`：五档取值与终端文案逐条对得上。"""
    assert cli_module.EXIT_NAMES == (
        ("EXIT_OK", EXIT_OK),
        ("EXIT_FAILURE", EXIT_FAILURE),
        ("EXIT_REJECTED", EXIT_REJECTED),
        ("EXIT_STARTUP_UNMET", cli_module.EXIT_STARTUP_UNMET),
        ("EXIT_INTERRUPTED", cli_module.EXIT_INTERRUPTED),
    )
    for name, code in cli_module.EXIT_NAMES:
        assert cli_module._exit_name(code) == name
