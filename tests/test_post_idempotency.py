"""`meeting post` 的**幂等闸**（设计）。

要盯住的六条（对应设计口径「完成判定」与「验证口径」）：

  1. **同一场连跑三遍**：第一遍引擎与启动器都动过，第二、三遍**两条计数一个都不涨**、
     退出码 `EXIT_OK`，PCM 的 sha256 三遍相同、MP3 的 `mtime` 不被刷新；
  2. **只补缺的那一层**（两个方向各一条）：离线段已 `done` 时引擎调用次数为 0 且三份离线
     产物的 sha256 逐字节不变；纪要段已 `done` 时启动器调用次数为 0；
  3. **`running` 拒跑**：任一层停在 `running` 且不带 `--force` → `EXIT_REJECTED`，
     提示里点名那一个状态文件与 `--force`；带上 `--force` → 两层都重跑；
  4. **无待办**：两层都 `done`、产物齐、MP3 比 PCM 新 → `EXIT_OK`、两条计数都为 0，
     终端打出「无待办」那一句；
  5. **入口编排**：`meeting post` 经 `meeting.post_run.run_post`（起线程跑会后链的那条钩子
     入口已随收尾第 4 步的退场删除，产品入口只剩这一条）；
  6. **注入件一路向下传**：两条并发跑的链各自的假启动器互不串台（没有一处替换模块
     全局属性，也没有摘掉 `tests/conftest.py` 的再入闸标记）。

**一条真子会话都不起**：本文件每一个会走到纪要钩子的用例都注入只登记的假启动器；
产品路径那一档由 `tests/conftest.py` 的 `MEETING_MINUTES_CHILD=1` 兜底挡着，
本文件另外把 `resolve_minutes_hook()` 换成假件（第二层网）。引擎一律是
`tests/test_offline_runner.py` 的 `FakeEngine`，音频是现场合成的最小 PCM。
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
from meeting import post_run as post_run_module
from meeting.backends import minutes as minutes_backend
from meeting.capture import transcode as transcode_module
from meeting.minutes import claim as claim_module
from meeting.minutes import launch as minutes_launch
from meeting.offline import artifacts as offline_artifacts
from meeting.offline import minutes_tail
from meeting.offline import runner as offline_runner

from tests import _helpers as helpers
from tests._minutes_fakes import (
    DelayedHookImplementation,
    FakeHook,
    FakeResolution,
    RecordingConverter,
    RecordingLauncher,
    write_final,
    write_minutes_done,
)
from tests._offline_fakes import FakeEngine, synth_session

from meeting.cli import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_REJECTED,
    post,
)
from meeting.post_run import run_post

#: 慢档标记（收尾轮）：`meeting post` 是收尾功能，整文件归收尾验收。
pytestmark = pytest.mark.slow

#: 离线那一层的状态文件名（按产物契约取，不重写一遍字面量）
OFFLINE_STATUS_NAME = offline_artifacts.STATUS_NAME

#: 纪要那一层的两个产物名
MINUTES_STATUS_NAME = minutes_tail.MINUTES_STATUS_NAME
MINUTES_FINAL_NAME = minutes_tail.MINUTES_FINAL_NAME

#: 本场声明的那两路（`synth_session()` 的缺省）
SOURCES = ("mic", "loopback")

#: 等待后台线程落定的上界（秒）：后台那条是 daemon 线程，等它靠轮询、不靠 sleep 赌
BG_TIMEOUT_SECONDS = 4.0


# ---------------------------------------------------------------------------
# 假件：两个只登记的替身 + 第二层网
# ---------------------------------------------------------------------------


class FakeMinutesHook:
    """假钩子实现：`launch()` 只登记（真子会话一次都不该被拉起）。

    `resolutions` 另记「纪要那一层的启动器被解析出几次」（产品路径补纪要那一半的入口计数）。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.resolutions: list[str] = []

    def launch(self, session_dir: str) -> None:
        self.calls.append(str(session_dir))


class FakeHookImplementation:
    """纪要那一层**解析出来的那个实现**的替身：`launch()` 只登记。

    产品路径上调的是 `resolution.implementation.launch(session_dir)`，因此替身要长成
    「有 `launch()` 的对象」这个形状（与 `minutes.hook = auto` 解析出来的东西同形）。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def launch(self, session_dir: str) -> None:
        self.calls.append(str(session_dir))


@pytest.fixture(autouse=True)
def _no_real_minutes_hook(monkeypatch: pytest.MonkeyPatch) -> FakeMinutesHook:
    """**第二层网**：产品路径上解析出来的纪要钩子换成只登记的假件。

    第一层网是 `tests/conftest.py` 的 `MEETING_MINUTES_CHILD=1`（它挡在 `minutes.hook`
    的解析之前）。这一层接着兜住「标记被摘掉之后」那一档：本机 PATH 上真的有 `dsh`。

    它同时是「产品路径补纪要那一半」的判据来源：产品路径不递启动器，判据只能落在
    「这一趟解没解析出纪要那一层的启动器」上 —— 登记挂在假解算器自己的 `resolutions`
    那一格上（`_announce_minutes()` 那条路是「闸放行 → 解析实现 → 拉起」：解析到了就
    一定走到 `launch()`；`MEETING_MINUTES_CHILD=1` 那一档连解析都不发生）。
    """
    hook = FakeMinutesHook()

    def resolver(*_args: Any, **_kwargs: Any) -> FakeResolution:
        hook.resolutions.append("resolve")
        return FakeResolution(hook)

    monkeypatch.setattr("meeting.backends.registry.resolve_minutes_hook", resolver)
    return hook


@pytest.fixture
def _a_short_minutes_bound(monkeypatch: pytest.MonkeyPatch) -> float:
    """把等纪要的上界压到 0.4 秒，读的仍是**现成的配置键** `minutes.timeout_seconds`。

    真链的缺省是四小时（`meeting/config.py` 的 `DEFAULT_VALUES`），用例不能真等满它 ——
    判据要的是「在上界内收场」。这一格只把那个键的**读数**改小，键本身不新增、取值域不动。
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
    """一份文件的 sha256（「逐字节不变」那几条判据用它）。"""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest_set(root: Path, names: list[str]) -> dict[str, str]:
    """一组文件的 sha256（名字 → 摘要）。"""
    return {name: digest(root / name) for name in names}


def offline_product_names(sources: tuple[str, ...] = SOURCES) -> list[str]:
    """离线那一层的**三份产物**（逐路两份 + 一份报告）：判据里「产物齐」那一半。"""
    names = ["meeting_offline_report.md"]
    for source in sources:
        names.append(f"meeting_offline_transcript_{source}.md")
        names.append(f"meeting_offline_speakers_{source}.json")
    return names


def _run_post_entry(session: Path, *flags: str) -> tuple[int, str]:
    """走**真入口** `meeting.cli.post([目录, *flags])`，交回 `(退出码, 终端输出)`。

    `post()` 每一条出口都 `sys.exit(<常量>)`，这里把它收下；stdout 与 stderr 都收进同一个
    缓冲，好让断言落在**逐字输出**上（`post()` 自己会 `flush()`，重定向因此读得全）。
    """
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        with pytest.raises(SystemExit) as info:
            post([str(session), *flags])
    return int(info.value.code or 0), buffer.getvalue()


def write_offline_status(session: Path, *, state: str) -> Path:
    path = session / OFFLINE_STATUS_NAME
    offline_artifacts.write_status(
        path,
        session_id=session.name,
        state=state,
        started_at="2025-09-19T22:00:00",
        sources=list(SOURCES),
        artifacts={},
    )
    return path


def archive_mp3s(session: Path, *, newer_than_pcm: bool = True) -> list[Path]:
    """给两路各落一份归档 MP3，并把 `mtime` 摆到 PCM 的前面（或后面）。"""
    made: list[Path] = []
    for source in SOURCES:
        pcm = session / f"meeting_audio_{source}_001.pcm"
        mp3 = session / f"meeting_audio_{source}_001.mp3"
        mp3.write_bytes(f"mp3-{source}".encode())
        stamp = pcm.stat().st_mtime + (3600.0 if newer_than_pcm else -3600.0)
        os.utime(mp3, (stamp, stamp))
        made.append(mp3)
    return made


class SettlingLauncher(RecordingLauncher):
    """假启动器：登记之外，**把纪要那一层该落的产物补上**（替那个 headless 子会话收工）。

    真的启动器起一条 headless 子会话，子会话跑完会写 `meeting_minutes_status.json` 与
    `meeting_final.md`。假件替的就是这一步，因此它也得让这两份落下来 —— 不然「反复重跑
    不再拉起子会话」这条判据在盘上永远看不到终态，每一遍都会被那道闸放行。
    """

    def __init__(self) -> None:
        super().__init__(result={"hook": "launched", "announced": True})

    def __call__(self, session_dir: Any, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(str(session_dir))
        session = Path(session_dir)
        write_minutes_done(session)
        write_final(session)
        return dict(self.result or {})

    def launch(self, session_dir: Any) -> None:
        """产品路径拉起的是 `resolution.implementation.launch(目录)` 这个形状（同一条收工）。"""
        self(session_dir)


class AbsentResolution:
    """`resolve_minutes_hook()` 的替身：`minutes.hook` 缺席那一档（一个实现都没有）。"""

    key = "minutes.hook"
    selector = "null"
    target = None
    has_implementation = False
    implementation = None

    def skipped(self) -> bool:
        return True


def a_finished_session(root: Path) -> tuple[Path, FakeEngine, SettlingLauncher]:
    """一场**两层都收干净**的会：离线三份产物 + 两份状态文件 + 纪要 + 音频归档齐。

    离线段那三份产物由真的 `run_session()` 跑一遍落下来（假引擎；本场没有真模型），
    纪要那一层由 `SettlingLauncher` 替那个子会话收工；归档的 MP3 摆成「比 PCM 新」，
    于是后续每一遍都不该再转一次。
    """
    session = synth_session(root)
    engine = FakeEngine()
    launcher = SettlingLauncher()
    first = run_post(session, engine=engine, minutes_launcher=launcher)
    assert first["exit_code"] == EXIT_OK, first.get("error")
    assert launcher.calls == [str(session)], launcher.calls
    archive_mp3s(session, newer_than_pcm=True)
    return session, engine, launcher


# ---------------------------------------------------------------------------
# 一、连跑三遍：第二、三遍两条计数都不涨
# ---------------------------------------------------------------------------


def test_the_second_run_calls_neither_the_engine_nor_the_launcher(tmp_path: Path) -> None:
    """同一场连跑三遍：第一遍两条都动过，第二、三遍**一个都不动**，退出码 `EXIT_OK`。"""
    session, engine, launcher = a_finished_session(tmp_path / "250919")
    first_engine_calls = len(engine.calls)
    first_launcher_calls = len(launcher.calls)
    assert first_engine_calls > 0, "第一遍该真的跑过离线那一趟"
    assert first_launcher_calls == 1, f"第一遍该只拉起一次子会话，实测 {first_launcher_calls}"

    second = run_post(session, engine=engine, minutes_launcher=launcher)
    third = run_post(session, engine=engine, minutes_launcher=launcher)

    assert second["exit_code"] == EXIT_OK, second.get("error")
    assert third["exit_code"] == EXIT_OK, third.get("error")
    assert len(engine.calls) == first_engine_calls, (
        f"第二、三遍不许再调引擎，实测 {engine.calls[first_engine_calls:]}"
    )
    assert len(launcher.calls) == first_launcher_calls, (
        f"第二、三遍不许再拉起子会话，实测 {launcher.calls[first_launcher_calls:]}"
    )


def test_the_repeat_runs_leave_the_pcm_and_the_mp3_alone(tmp_path: Path) -> None:
    """三遍跑完：PCM 的 sha256 一模一样；**留着凭据的段**重跑不动 MP3（`mtime` 不变）。

    完成判据是逐段凭据（`meeting/capture/transcode.py` 那一份，全仓唯一）：凭据认账才
    跳过，「MP3 比 PCM 新」不再构成判据。对照半场照强杀探针的语义造：删掉凭据的那一段
    重做（没有凭据的产物不算成品），留着凭据的那段原地不动。
    """
    session = synth_session(tmp_path / "250919")
    engine = FakeEngine()
    launcher = SettlingLauncher()
    converter = RecordingConverter()
    first = run_post(
        session, engine=engine, minutes_launcher=launcher, archive_converter=converter
    )
    assert first["exit_code"] == EXIT_OK, first.get("error")
    assert len(converter.calls) == len(SOURCES), f"首跑该把两段都转过：{converter.calls}"

    def snapshot() -> dict[str, tuple[str, int]]:
        """两段的 `(PCM sha256, MP3 mtime_ns)` ——「动没动」全看这一格。"""
        return {
            source: (
                digest(session / f"meeting_audio_{source}_001.pcm"),
                (session / f"meeting_audio_{source}_001.mp3").stat().st_mtime_ns,
            )
            for source in SOURCES
        }

    before = snapshot()

    run_post(session, engine=engine, minutes_launcher=launcher, archive_converter=converter)
    run_post(session, engine=engine, minutes_launcher=launcher, archive_converter=converter)

    assert snapshot() == before, "留着凭据的重跑：PCM 与 MP3 一个都不该动"
    assert len(converter.calls) == len(SOURCES), "凭据认账的段不重转"

    # 对照半场（与强杀探针同一语义）：删掉凭据的那一段重做，另一段不动
    transcode_module.credential_path(session, "loopback", 1).unlink()
    third = run_post(
        session, engine=engine, minutes_launcher=launcher, archive_converter=converter
    )
    assert third["exit_code"] == EXIT_OK, third.get("error")
    assert len(converter.calls) == len(SOURCES) + 1, converter.calls
    after = snapshot()
    assert after["loopback"][1] != before["loopback"][1], "缺凭据的段该重做（MP3 重写一遍）"
    assert after["mic"] == before["mic"], "留着凭据的那段仍原地不动"


def test_the_repeat_runs_write_no_offline_product_again(tmp_path: Path) -> None:
    """三遍跑完：三份离线产物与两份状态文件的 sha256 一个字节都没变。"""
    session, engine, launcher = a_finished_session(tmp_path / "250919")
    names = offline_product_names() + [OFFLINE_STATUS_NAME, MINUTES_STATUS_NAME, MINUTES_FINAL_NAME]
    before = digest_set(session, names)

    run_post(session, engine=engine, minutes_launcher=launcher)
    run_post(session, engine=engine, minutes_launcher=launcher)

    assert digest_set(session, names) == before


# ---------------------------------------------------------------------------
# 二、无待办：两层都到终态时「什么都不跑」
# ---------------------------------------------------------------------------


def test_no_pending_work_prints_one_line_and_calls_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """两层都 `done`、产物齐、MP3 比 PCM 新 → `EXIT_OK`、两条计数都为 0、终端说「无待办」。"""
    session, engine, launcher = a_finished_session(tmp_path / "250919")
    engine_calls = len(engine.calls)
    launcher_calls = len(launcher.calls)
    # `post()` 自己那一趟也注入同一对假件（不经 `main()`，参数面归 test_cli_post.py）
    monkeypatch.setattr(
        cli_module,
        "run_post",
        lambda session_dir, **kwargs: run_post(
            session_dir, engine=engine, minutes_launcher=launcher
        ),
    )

    with pytest.raises(SystemExit) as info:
        post([str(session)])

    assert info.value.code == EXIT_OK
    out = capsys.readouterr().out
    assert "无待办" in out, out
    assert "离线段" in out and "纪要段" in out
    assert len(engine.calls) == engine_calls, "无待办那一趟一个引擎调用都不该有"
    assert len(launcher.calls) == launcher_calls, "无待办那一趟一个子会话都不该拉起"


def test_a_missing_offline_status_file_is_not_a_finished_layer(tmp_path: Path) -> None:
    """反例：状态文件不在场时**不算已完成**（跨过这道判据就会漏跑一整层）。"""
    session, engine, launcher = a_finished_session(tmp_path / "250919")
    (session / OFFLINE_STATUS_NAME).unlink()
    engine_calls = len(engine.calls)

    summary = run_post(session, engine=engine, minutes_launcher=launcher)

    assert summary["exit_code"] == EXIT_OK
    assert len(engine.calls) > engine_calls, "状态文件不在场时离线段该重跑"
    assert (session / OFFLINE_STATUS_NAME).is_file(), "重跑之后状态文件该回来"


def test_a_missing_offline_product_is_not_a_finished_layer(tmp_path: Path) -> None:
    """反例：状态文件 `done` 却缺一份产物时**不算已完成**（产物那一半判据也要在）。"""
    session, engine, launcher = a_finished_session(tmp_path / "250919")
    missing = session / "meeting_offline_transcript_loopback.md"
    missing.unlink()
    engine_calls = len(engine.calls)

    summary = run_post(session, engine=engine, minutes_launcher=launcher)

    assert summary["exit_code"] == EXIT_OK
    assert len(engine.calls) > engine_calls, "产物缺一条时离线段该重跑"
    assert missing.is_file()


def test_minutes_done_without_the_final_artifact_is_not_finished(tmp_path: Path) -> None:
    """反例：纪要状态文件 `done` 而 `meeting_final.md` 缺席时，纪要段必须重新拉起。

    离线段这一趟也重跑（纪要挂在离线链的尾巴上），但判据落在**启动器被调到一次**上：
    只看状态文件那一格就会漏掉这一趟。
    """
    session, engine, _ = a_finished_session(tmp_path / "250919")
    (session / MINUTES_FINAL_NAME).unlink()
    launcher = SettlingLauncher()

    summary = run_post(session, engine=engine, minutes_launcher=launcher)

    assert summary["exit_code"] == EXIT_OK
    assert launcher.calls == [str(session)], "缺产物时该按「缺什么补什么」再拉一次"
    assert (session / MINUTES_FINAL_NAME).is_file()


# ---------------------------------------------------------------------------
# 三、只补缺的那一层（两个方向各一条）
# ---------------------------------------------------------------------------


def test_a_done_offline_layer_is_left_alone_while_minutes_are_added(tmp_path: Path) -> None:
    """离线段已 `done`、纪要段缺失 → 引擎调用次数为 0、三份产物 sha256 不变、只补纪要。"""
    session, engine, _ = a_finished_session(tmp_path / "250919")
    # 把纪要段退回「缺失」那一档：状态文件与纪要本体都拿走
    (session / MINUTES_STATUS_NAME).unlink()
    (session / MINUTES_FINAL_NAME).unlink()

    names = offline_product_names()
    before = digest_set(session, names)
    engine_calls = len(engine.calls)
    launcher = SettlingLauncher()

    summary = run_post(session, engine=engine, minutes_launcher=launcher)

    assert summary["exit_code"] == EXIT_OK, summary.get("error")
    assert len(engine.calls) == engine_calls, f"离线段已 done，不该再调引擎：{engine.calls}"
    assert digest_set(session, names) == before, "三份离线产物该逐字节不变"
    assert launcher.calls == [str(session)], "缺的那一层（纪要）该被补上"
    assert (session / MINUTES_STATUS_NAME).is_file()


def test_a_done_minutes_layer_is_left_alone_while_offline_is_rebuilt(tmp_path: Path) -> None:
    """纪要段已 `done`、离线段 `failed` → 启动器调用次数为 0、只补离线。

    离线段照常进 `run_session`（那一趟的尾巴会走到那道闸），而闸认出纪要段已经是终态、
    产物也过得了四条判据，于是**一次都不拉起**。
    """
    session, engine, _ = a_finished_session(tmp_path / "250919")
    write_offline_status(session, state="failed")
    engine_calls = len(engine.calls)
    launcher = SettlingLauncher()

    summary = run_post(session, engine=engine, minutes_launcher=launcher)

    assert summary["exit_code"] == EXIT_OK, summary.get("error")
    assert len(engine.calls) > engine_calls, "离线段该重跑"
    assert launcher.calls == [], "纪要段已 done，不许再拉起子会话"
    assert summary["minutes"]["hook"] == "already_done"


def test_an_injected_launcher_stands_in_for_the_gate(tmp_path: Path) -> None:
    """注入假启动器那一档：闸放行时它被调到一次，闸拦下时它一次都不被调到。

    假件**照真子会话的收工样子落终态**（`SettlingLauncher`）：拉起那一档一律等这一趟
    新落的终态，于是第一遍那一格 `state` 出自**等到的那一版**（不是盘上读来的旧记录），
    第二遍因为终态与产物都齐，闸拦下、交回 `already_done`。
    """
    session = synth_session(tmp_path / "250919")
    launcher = SettlingLauncher()

    first = run_post(session, engine=FakeEngine(), minutes_launcher=launcher)
    assert first["minutes"]["hook"] == "finished", first["minutes"]
    assert first["minutes"]["state"] == "done"
    assert launcher.calls == [str(session)]

    write_minutes_done(session)
    write_final(session)
    second = run_post(session, engine=FakeEngine(), minutes_launcher=launcher)

    assert second["minutes"]["hook"] == "already_done"
    assert second["minutes"]["state"] == "done"
    assert launcher.calls == [str(session)], "已是终态时这一格不该再涨"


# ---------------------------------------------------------------------------
# 四、`running` 那一档：默认拒跑，`--force` 是出路
# ---------------------------------------------------------------------------


def test_a_running_offline_layer_rejects_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """离线段停在 `running` 且不带 `--force` → `EXIT_REJECTED`，提示点名那个状态文件。"""
    session, engine, launcher = a_finished_session(tmp_path / "250919")
    write_offline_status(session, state="running")
    engine_calls = len(engine.calls)
    launcher_calls = len(launcher.calls)

    with pytest.raises(SystemExit) as info:
        post([str(session)])

    assert info.value.code == EXIT_REJECTED
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert OFFLINE_STATUS_NAME in combined, combined
    assert "--force" in combined
    assert len(engine.calls) == engine_calls, "拒跑时一个引擎调用都不该有"
    assert len(launcher.calls) == launcher_calls, "拒跑时一个子会话都不该拉起"


def test_a_running_minutes_layer_rejects_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """纪要段停在 `running` 且不带 `--force` → `EXIT_REJECTED`，提示点名纪要状态文件。"""
    session, engine, launcher = a_finished_session(tmp_path / "250919")
    write_minutes_done(session, state="running")
    engine_calls = len(engine.calls)
    launcher_calls = len(launcher.calls)

    with pytest.raises(SystemExit) as info:
        post([str(session)])

    assert info.value.code == EXIT_REJECTED
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert MINUTES_STATUS_NAME in combined, combined
    assert "--force" in combined
    assert len(engine.calls) == engine_calls
    assert len(launcher.calls) == launcher_calls


def test_an_interrupted_offline_layer_reruns_from_scratch_without_force(
    tmp_path: Path,
) -> None:
    """被打断的那一场：不带 `--force` 也放行，离线段**从头再跑**。

    打断落的是终态 `interrupted`：幂等闸只拦 `running`（interrupted 天然放行），
    「已完成」判据只认 `done`（interrupted 照样整段重跑）—— spec 的完成判定
    「打断之后重新按按钮，能从头再跑」在命令行这一条入口上的落点。
    """
    session, engine, launcher = a_finished_session(tmp_path / "250919")
    write_offline_status(session, state="interrupted")
    engine_calls = len(engine.calls)

    summary = run_post(session, engine=engine, minutes_launcher=launcher)

    assert summary["exit_code"] == EXIT_OK, summary.get("error")
    assert len(engine.calls) > engine_calls, "打断之后重按：离线段从头再跑"
    assert json.loads((session / OFFLINE_STATUS_NAME).read_text(encoding="utf-8"))[
        "state"
    ] == "done"


def test_force_takes_the_running_layer_along_and_rebuilds_both(tmp_path: Path) -> None:
    """带 `--force` → `running` 那一档放行，**两层都重跑**（两条计数都涨）。"""
    session, engine, launcher = a_finished_session(tmp_path / "250919")
    write_offline_status(session, state="running")
    write_minutes_done(session, state="running")
    engine_calls = len(engine.calls)
    launcher_calls = len(launcher.calls)

    summary = run_post(session, engine=engine, minutes_launcher=launcher, force=True)

    assert summary["exit_code"] == EXIT_OK, summary.get("error")
    assert len(engine.calls) > engine_calls, "--force 该把离线段重跑一遍"
    assert len(launcher.calls) == launcher_calls + 1, "--force 该把纪要段重跑一遍"
    assert json.loads((session / OFFLINE_STATUS_NAME).read_text(encoding="utf-8"))["state"] == "done"


def test_force_reruns_both_layers_of_a_finished_session(tmp_path: Path) -> None:
    """两层都已是终态时带 `--force`：两层照样各跑一遍（终态不是挡箭牌）。"""
    session, engine, launcher = a_finished_session(tmp_path / "250919")
    engine_calls = len(engine.calls)
    launcher_calls = len(launcher.calls)

    summary = run_post(session, engine=engine, minutes_launcher=launcher, force=True)

    assert summary["exit_code"] == EXIT_OK, summary.get("error")
    assert len(engine.calls) > engine_calls
    assert len(launcher.calls) == launcher_calls + 1


def test_force_reaches_the_gate_not_only_the_summary(tmp_path: Path) -> None:
    """`--force` 真的透到了闸上：带它时那条「已是终态」的结论翻成**真的重跑了一遍**。

    不带 `--force` 那一趟由闸拦下（`already_done`、启动器一次都没被调到）；带 `--force`
    那一趟闸被跳过、启动器被调到一次，结论出自这一趟**新落的终态**（`finished`，
    与等待那一处的口径一致）—— 只把 `force` 抄进摘要而不透到闸上时，这一格会停在
    `already_done` 且启动器计数不涨。
    """
    session, engine, launcher = a_finished_session(tmp_path / "250919")

    plain = run_post(session, engine=engine, minutes_launcher=launcher)
    calls_after_plain = len(launcher.calls)
    forced = run_post(session, engine=engine, minutes_launcher=launcher, force=True)

    assert plain["minutes"]["hook"] == "already_done"
    assert forced["minutes"]["hook"] == "finished", (
        "`--force` 只抄进摘要而不透到闸上时，这一格会停在 already_done"
    )
    assert forced["minutes"]["state"] == "done"
    assert len(launcher.calls) == calls_after_plain + 1, "`--force` 该真的重跑一遍纪要那一层"
    assert forced["force"] is True


# ---------------------------------------------------------------------------
# 四之二、在飞那一段缝：`running` 落在 `launch()` 返回之前，紧接着的第二趟拒跑
# ---------------------------------------------------------------------------


@pytest.fixture
def _product_path_uses(monkeypatch: pytest.MonkeyPatch):
    """把产品路径解析出来的纪要实现换成指定的替身（第二层网，先换件再碰真入口）。"""

    def install(implementation: Any) -> Any:
        monkeypatch.setattr(
            "meeting.backends.registry.resolve_minutes_hook",
            lambda *a, **k: FakeResolution(implementation),
        )
        return implementation

    return install


def test_a_second_launch_on_the_same_session_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一场再来一次拉起 → **拒跑**（没有第二条线程、没有第二个子会话）。

    判据落在真实现的记记录里：`launch()` 抛 `AlreadyRunningError`、`minutes.runner.run()` 只被
    调到一次、盘上第一趟那一版状态一个字节都没被第二趟覆盖。
    """
    session = synth_session(tmp_path / "250919")
    arms: list[str] = []
    monkeypatch.setattr(
        "meeting.minutes.runner.run",
        lambda session_dir, **kwargs: arms.append(str(session_dir)),
    )
    hook = minutes_backend.MinutesHook()

    hook.launch(str(session), deliver=True)
    before = (session / MINUTES_STATUS_NAME).read_text(encoding="utf-8")

    with pytest.raises(minutes_backend.AlreadyRunningError):
        hook.launch(str(session), deliver=True)

    helpers.wait_until(lambda: arms, what="第一趟的后台线程真的跑起来过")
    assert arms == [str(session)], arms
    assert (session / MINUTES_STATUS_NAME).read_text(encoding="utf-8") == before


def test_force_carries_the_flag_all_the_way_to_the_implementation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _a_short_minutes_bound: float,
) -> None:
    """同一现场带 `--force` → 允许重跑（这一档没有把 `--force` 的出路堵死）。

    `--force` 会一路走到拉起这一步，因此这一条把再入闸（`MEETING_MINUTES_CHILD=1`）摘掉，
    并给真实现那一格钉一个桩：登记之后照在飞那一档抛 `AlreadyRunningError`（桩自己不起任何
    东西，`runner.run()` 也就没有机会被走到）。判据是**带 `--force` 的那一趟真的落到了
    「调实现」这一步**，而 `launch(..., force=True)` 那一格收到的是 `True`。
    """
    session, engine, _ = a_finished_session(tmp_path / "250919")
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)
    claim_module.mark_running(session, branch="headless")
    calls: list[bool] = []

    def stub(_self, session_dir, *, force: bool = False) -> None:
        """照真实现那一格：登记、落一版在飞（这一趟不被拒），随后替子会话收工。"""
        calls.append(bool(force))
        claim_module.mark_running(session_dir, branch="headless", force=force)
        write_final(Path(session_dir))
        write_minutes_done(Path(session_dir))

    monkeypatch.setattr(minutes_backend.MinutesHook, "launch", stub)
    # 本文件那道 autouse 的第二层网（解析结果换 `FakeMinutesHook`）在这里显式换成**真实现**：
    # `--force` 那一格要量的就是「它被调到、并且收到了 `force=True`」。
    monkeypatch.setattr(
        "meeting.backends.registry.resolve_minutes_hook",
        lambda *a, **k: FakeResolution(minutes_backend.MinutesHook()),
    )

    summary = run_post(session, engine=engine, force=True)

    assert calls == [True], f"带 --force 的那一趟该把 force=True 递给实现，实测 {calls}"
    assert summary["exit_code"] == EXIT_OK, summary.get("error")
    assert summary["minutes"]["state"] == "done", summary["minutes"]


def test_two_entries_can_inject_launchers_at_the_same_time(
    tmp_path: Path, _a_short_minutes_bound: float
) -> None:
    """两个入口**同时**跑：两条链各自的假启动器各记一笔，离线状态文件都落 `done`。

    这是「注入件经形参向下传、不落在模块全局上」的判据：替换模块全局的写法在并发下会把
    对方的替身当成原函数、整条链以 `TypeError` 收场，并把离线状态文件补成 `failed`。
    两条链的假启动器都**落本趟终态**（`SettlingLauncher`）：拉起那一档一律等落定，
    只登记的替身会各自等到上界（真链是四小时，这一格由 `_a_short_minutes_bound` 压到秒级）。
    """
    first = synth_session(tmp_path / "a")
    second = synth_session(tmp_path / "b")
    launcher_a = SettlingLauncher()
    launcher_b = SettlingLauncher()
    engine_a = FakeEngine()
    engine_b = FakeEngine()
    barrier = threading.Barrier(2)
    outcomes: dict[str, Any] = {}
    errors: list[str] = []

    def worker(session: Path, engine: FakeEngine, launcher: SettlingLauncher, tag: str) -> None:
        try:
            barrier.wait(timeout=BG_TIMEOUT_SECONDS)
            outcomes[tag] = run_post(session, engine=engine, minutes_launcher=launcher)
        except BaseException as exc:  # 任何异常都记下来，join 之后一并断言
            errors.append(f"{tag}: {type(exc).__name__}: {exc}")

    threads = [
        threading.Thread(target=worker, args=(first, engine_a, launcher_a, "a")),
        threading.Thread(target=worker, args=(second, engine_b, launcher_b, "b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=BG_TIMEOUT_SECONDS)

    assert errors == [], errors
    assert sorted(outcomes) == ["a", "b"], outcomes
    assert launcher_a.calls == [str(first)], launcher_a.calls
    assert launcher_b.calls == [str(second)], launcher_b.calls
    for session in (first, second):
        written = json.loads((session / OFFLINE_STATUS_NAME).read_text(encoding="utf-8"))
        assert written["state"] == "done", f"{session.name} 的状态文件被写成了 {written['state']}"


def test_the_minutes_tail_keeps_its_own_announce_function(
    tmp_path: Path, _a_short_minutes_bound: float
) -> None:
    """跑完之后 `minutes_tail._announce_minutes` 仍是模块自己那一个（没有留下任何替身）。"""
    session = synth_session(tmp_path / "250919")
    before = minutes_tail._announce_minutes

    run_post(
        session,
        engine=FakeEngine(),
        minutes_launcher=SettlingLauncher(),
    )

    assert minutes_tail._announce_minutes is before
    assert before.__module__ == "meeting.offline.minutes_tail"
    assert before.__qualname__ == "_announce_minutes"


# ---------------------------------------------------------------------------
# 六、**注入路径**上的等待：拉起那一档等的是**本趟新落的终态**
# ---------------------------------------------------------------------------


class ExplodingConverter:
    """转码器替身：对着 PCM **抛错**（`ffmpeg` 缺席那一档的替身）。

    它经 `sync_archive()` 的 `converter` 形参注进去（`meeting/post_run.py` 现行的用例接缝；
    `run_post()` 那一格叫 `archive_converter`，`post()` 那条真入口由用例把
    `post_run.sync_archive` 换成注入形参的一份），因此走产品路径那一趟也吃得到。
    """

    def __init__(self, message: str = "ffmpeg 缺席（用例注入）") -> None:
        self.calls: list[tuple[str, str]] = []
        self.message = message

    def __call__(self, pcm_path: Any, mp3_path: Any) -> bool:
        self.calls.append((str(pcm_path), str(mp3_path)))
        raise RuntimeError(self.message)


def offline_only_session(root: Path) -> tuple[Path, FakeEngine]:
    """一场**只把离线段收干净**的会：三份离线产物 + 离线状态文件齐、纪要那层缺席。

    离线段那一趟走真的 `run_session()`（假引擎；本场没有真模型），纪要那一层的拉起在
    子会话标记在场时被再入闸安静跳过 —— 盘上留着「离线段 `done`、纪要段没到终态」现场。
    """
    session = synth_session(root)
    engine = FakeEngine()
    offline_runner.run_session(session, engine=engine)
    assert (session / OFFLINE_STATUS_NAME).is_file()
    assert not (session / MINUTES_STATUS_NAME).exists()
    return session, engine


def a_finished_minutes_layer(session: Path, *, state: str = "done") -> None:
    """把纪要段摆成 `state` 那一档终态，并且落一份过得了四条判据的 `meeting_final.md`。"""
    write_minutes_done(session, state=state)
    write_final(session)


@pytest.mark.slow
def test_the_product_path_refills_a_missing_minutes_layer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """① 离线 `done` + 纪要状态文件**缺席** → 产品路径拉起来一次，**等它落到 `done`** 才出结论。

    走的是**真入口** `meeting.cli.post([目录])`（不递 `minutes_launcher`）。假实现照真
    `MinutesHook` 的时序来：`launch()` 起一条 daemon 线程就返回，线程体先落 `running`、
    隔一小会儿才落终态（`DelayedHookImplementation`）—— 「拉起 ≠ 落定」这一段就是本用例
    要钉住的东西：这一趟因此**必须等**，等到终态 `done` 与产物齐才按成功档收场。

    子会话的拉起由两处挡住：`tests/conftest.py` 的 `MEETING_MINUTES_CHILD=1` 是本用例显式
    摘掉的（不摘就进不了解析那一步、补纪要这半条产品路径永远走不到），而 `resolve_minutes_hook()`
    是本文件 autouse 夹具里的假件（这里再换成上面那台带时序的）—— 一次真子会话都起不来。
    """
    session, engine = offline_only_session(tmp_path / "250919")
    launcher = DelayedHookImplementation(latency=0.1)
    monkeypatch.setattr(
        "meeting.backends.registry.resolve_minutes_hook",
        lambda *a, **k: FakeResolution(launcher),
    )
    engine_calls = len(engine.calls)
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)

    with pytest.raises(SystemExit) as info:
        post([str(session)])

    for thread in launcher.threads:
        thread.join(timeout=BG_TIMEOUT_SECONDS)

    assert info.value.code == EXIT_OK, "等到的终态是 done 且产物齐，这一趟该按成功档收场"
    assert launcher.calls == [str(session)], "产品路径该把纪要那一层拉起来一次"
    # **等到了**：`post()` 返回那一刻盘上已经是 `done`（不是「刚拉起来就报 failed」）
    written = json.loads((session / MINUTES_STATUS_NAME).read_text(encoding="utf-8"))
    assert written["state"] == "done", written
    assert (session / MINUTES_FINAL_NAME).is_file()
    out = capsys.readouterr().out
    assert "步骤 3/3 会后纪要（纪要生成）：finished（state=done）" in out, out
    assert "结论：done（退出码 EXIT_OK）" in out, out
    assert len(engine.calls) == engine_calls, (
        f"离线段已是终态，补纪要不许连带重算离线那一段：{engine.calls[engine_calls:]}"
    )


@pytest.mark.slow
def test_the_product_path_still_reports_a_launch_that_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """③ 拉起点这一档的三种失败（实现抛错 / 解析抛错 / 解析不出实现）都**如实记 `failed`**。"""
    absent = AbsentResolution()

    class ExplodingImplementation:
        """解析出来的实现：`launch()` 当场抛（真纪要钩子炸了那一档的替身）。"""

        def launch(self, session_dir: Any) -> None:
            raise RuntimeError("纪要钩子炸了（用例注入）")

    def exploding_resolver(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("minutes.hook 解析不动（用例注入）")

    cases = (
        ("impl-raises", lambda *a, **k: FakeResolution(ExplodingImplementation())),
        ("resolver-raises", exploding_resolver),
        ("no-implementation", lambda *a, **k: absent),
    )
    for label, resolver in cases:
        session, engine = offline_only_session(tmp_path / label)
        engine_calls = len(engine.calls)
        monkeypatch.setattr("meeting.backends.registry.resolve_minutes_hook", resolver)
        monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)

        with pytest.raises(SystemExit) as info:
            post([str(session)])

        assert info.value.code == EXIT_FAILURE, f"{label}：这一档该如实记失败"
        out = capsys.readouterr().out
        assert "state=failed" in out, f"{label}：纪要那一层该如实报 failed：{out}"
        assert "结论：failed（退出码 EXIT_FAILURE）" in out, out
        assert len(engine.calls) == engine_calls, f"{label}：补纪要那一趟不许重算离线段"


@pytest.mark.slow
def test_the_product_path_refills_a_done_minutes_layer_without_its_final_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _a_short_minutes_bound: float
) -> None:
    """② 离线 `done` + 纪要状态 `done` 但 `meeting_final.md` 被删 → 补一次、**等到新终态**才收场。

    盘上留着上一趟的终态（`done`，而产物不在）：拉起之后**必须等这一趟新落的那一版**，
    因此假件照真启动器的时序来（`DelayedHookImplementation`：起一条 daemon 线程就返回、
    线程体先落 `running`、隔 `latency` 秒再落终态）。这一趟的子会话跑成了，于是结论是
    成功档 `EXIT_OK`、终端上那一层写 `finished（state=done）` —— 上一趟留下的 `done`
    与这一趟新落的 `done` 由状态文件自己的标记分辨（`_minutes_baseline()`）。
    """
    session, engine = offline_only_session(tmp_path / "250919")
    a_finished_minutes_layer(session)
    (session / MINUTES_FINAL_NAME).unlink()
    implementation = DelayedHookImplementation(latency=0.1, state="done")
    monkeypatch.setattr(
        "meeting.backends.registry.resolve_minutes_hook",
        lambda *a, **k: FakeResolution(implementation),
    )
    engine_calls = len(engine.calls)
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)

    code, out = _run_post_entry(session)

    for thread in implementation.threads:
        thread.join(timeout=BG_TIMEOUT_SECONDS)

    assert code == EXIT_OK, f"等到的终态是 done 且产物齐，这一趟该按成功档收场：{out}"
    assert implementation.calls == [str(session)], "产品路径该为「最终产物不在」再拉起一次"
    assert "步骤 3/3 会后纪要（纪要生成）：finished（state=done）" in out, out
    assert "结论：done（退出码 EXIT_OK）" in out, out
    assert (session / MINUTES_FINAL_NAME).is_file(), "这一趟新落的那一版里产物该在"
    assert len(engine.calls) == engine_calls, "补纪要那一趟不许重算离线段"


def test_a_minutes_launch_that_never_settles_is_not_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _a_short_minutes_bound: float
) -> None:
    """③ 补纪要那一趟拉起之后**永不落终态** → 在现成的上界内收场，如实报「还在跑」。

    盘上留着上一趟的终态（`failed`），而这一趟刚拉起的那条链一份新终态都不落：等待的判据
    是「与基线不同」，因此它**绝不**把上一趟的 `failed` 当成本趟的结论，也不会谎报成功 ——
    到上界就报现成的 `timeout` 那一档（`hook` 与 `state` 都写着 `timeout`、原话「还没落定」）。
    上界取现成的配置键 `minutes.timeout_seconds`（真链的缺省是四小时，这里把它调小）。
    """
    session, engine = offline_only_session(tmp_path / "250919")
    a_finished_minutes_layer(session, state="failed")
    implementation = FakeHookImplementation()  # 只登记：一份新终态都不落
    monkeypatch.setattr(
        "meeting.backends.registry.resolve_minutes_hook",
        lambda *a, **k: FakeResolution(implementation),
    )
    engine_calls = len(engine.calls)
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)

    begun = time.monotonic()
    code, out = _run_post_entry(session)
    elapsed = time.monotonic() - begun

    assert elapsed < BG_TIMEOUT_SECONDS, f"该在上界内收场，实测 {elapsed:.2f}s"
    assert code == EXIT_FAILURE, f"还没落定那一档的退出码不是成功档：{out}"
    assert implementation.calls == [str(session)], "这一趟确实把纪要那一层拉起来过"
    assert "还没落定" in out, out
    assert "步骤 3/3 会后纪要（纪要生成）：timeout（state=timeout）" in out, out
    assert "结论：timeout（退出码 EXIT_FAILURE）" in out, out
    assert len(engine.calls) == engine_calls, "补纪要那一趟不许重算离线段"


@pytest.mark.slow
def test_a_failed_archive_is_not_swallowed_by_the_no_pending_work_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """两层都 `done`、转码失败：退出码走失败档，且**不许**说「音频已归档」。

    「这一趟没有待办」的前置是「音频已归档」（设计口径「关键决定 4」）：归档这一层有一路
    转不动时，那一档不许早退、结论不许写成 `EXIT_OK`，终端上也不许出现与现场不符的
    「音频已归档」。
    """
    session = synth_session(tmp_path / "250919")
    engine = FakeEngine()
    offline_runner.run_session(session, engine=engine)
    a_finished_minutes_layer(session)
    for path in sorted(session.glob("meeting_audio_*.mp3")):
        path.unlink()
    converter = ExplodingConverter()
    # 真入口 `post()` 不收转码器：把 `post_run.sync_archive` 换成「往现行接缝
    # （`converter` 形参）注替身」的那一份，`run_post()` 以下整条链照真走。
    real_sync_archive = post_run_module.sync_archive

    def sync_with_converter(directory: Any, **kwargs: Any) -> Any:
        return real_sync_archive(directory, **{**kwargs, "converter": converter})

    monkeypatch.setattr(post_run_module, "sync_archive", sync_with_converter)

    with pytest.raises(SystemExit) as info:
        post([str(session)])

    assert info.value.code == EXIT_FAILURE, "归档 failed 时不许按 EXIT_OK 收场"
    assert converter.calls, "这一趟该真的去转过（现场是「MP3 缺席」）"
    out = capsys.readouterr().out
    assert "音频已归档" not in out, out
    assert "无待办" not in out, out
    assert "步骤 1/3" in out and "failed" in out, out
    assert "结论：failed" in out, out


@pytest.mark.slow
def test_a_failed_archive_keeps_the_no_pending_work_message_away(tmp_path: Path) -> None:
    """同上，判据落在摘要上：`exit_code` 失败档、`ok` 为假、`state` 如实记失败。"""
    session = synth_session(tmp_path / "250919")
    offline_runner.run_session(session, engine=FakeEngine())
    a_finished_minutes_layer(session)
    for path in sorted(session.glob("meeting_audio_*.mp3")):
        path.unlink()

    summary = run_post(session, archive_converter=ExplodingConverter())

    assert summary["archive"]["state"] == "failed"
    assert summary["exit_code"] == EXIT_FAILURE, summary.get("error")
    assert summary["ok"] is False
    assert summary["state"] == "failed"
    assert summary["minutes"]["hook"] == "already_done", "纪要那一层没到待补的份上"
    assert "音频已归档" not in str(summary.get("error") or "")


# ---------------------------------------------------------------------------
# 七、闸自己那一层：读方与判据都借现成的
# ---------------------------------------------------------------------------


def test_the_minutes_gate_rejects_a_status_file_without_the_required_fields(
    tmp_path: Path,
) -> None:
    """残文件（只有 `state`）过不了纪要那一层的读方 → 闸放行、重新拉起。"""
    session = synth_session(tmp_path / "250919")
    (session / MINUTES_STATUS_NAME).write_text('{"state": "done"}', encoding="utf-8")
    write_final(session)
    launcher = RecordingLauncher()

    outcome = minutes_tail._announce_minutes(session, launcher=launcher, announce_minutes=True)

    assert launcher.calls == [str(session)], "字段不齐的残文件该按「没跑完」放行"
    assert outcome["hook"] == "launched"


def test_the_minutes_gate_uses_the_launch_module_for_the_final_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """产物那四条判据**复用现成的** `minutes.launch.check_final_artifact()`（不是另写一份）。"""
    session = synth_session(tmp_path / "250919")
    write_minutes_done(session)
    write_final(session)
    seen: list[Path] = []

    def stand_in(session_dir):
        seen.append(Path(session_dir))
        return ()

    monkeypatch.setattr(minutes_launch, "check_final_artifact", stand_in)
    launcher = RecordingLauncher()

    outcome = minutes_tail._announce_minutes(session, launcher=launcher, announce_minutes=True)

    assert seen == [session], "闸该把产物判据交给现成的那一个函数"
    assert outcome["hook"] == "already_done"
    assert launcher.calls == []


def test_the_summary_of_a_rejected_run_is_a_rejection(tmp_path: Path) -> None:
    """拒跑那一档的摘要：`EXIT_REJECTED`、`ok` 为假、原因里点名那个状态文件。"""
    session, engine, launcher = a_finished_session(tmp_path / "250919")
    write_minutes_done(session, state="running")

    summary = run_post(session, engine=engine, minutes_launcher=launcher)

    assert summary["exit_code"] == EXIT_REJECTED
    assert summary["exit_code_name"] == "EXIT_REJECTED"
    assert summary["ok"] is False
    assert summary["state"] == "running"
    assert MINUTES_STATUS_NAME in str(summary["error"])


def test_a_failed_minutes_layer_still_takes_the_run_to_failure(tmp_path: Path) -> None:
    """纪要段这一趟跑砸时退出码走 `EXIT_FAILURE`（既有口径不退化）。

    `SettlingLauncher` 的子类在这里**不把终态落成 `done`**：它替的那个子会话跑砸了，
    于是盘上那份纪要状态文件是 `failed`。
    """
    session = synth_session(tmp_path / "250919")

    class FailingLauncher(SettlingLauncher):
        def __call__(self, session_dir: Any, *args: Any, **kwargs: Any) -> Any:
            self.calls.append(str(session_dir))
            write_minutes_done(Path(session_dir), state="failed")
            return None

    launcher = FailingLauncher()

    summary = run_post(session, engine=FakeEngine(), minutes_launcher=launcher)

    assert launcher.calls == [str(session)]
    assert summary["exit_code"] == EXIT_FAILURE, summary.get("error")
    assert summary["minutes"]["state"] == "failed"
