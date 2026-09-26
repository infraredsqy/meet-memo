"""配置隔离的回归用例（`65dd3e4c` phase3 **critical-2** 的钉子）。

## 要钉住的现象（本轮复审复算出来的现场）

`65dd3e4c` phase3 给 `meeting post` 的入口加了一步配置前奏：
`meeting/cli.py::post()` → `_load_config_or_exit()`（`config.load()` + 值级校验）→
`cli.py:250 config_module.set_current(loaded)`。这条前奏本身是本单元要的东西，副作用是
**在进程内走真入口的用例会去读 `~/.config/meeting/config.json`**：

  - 没做家目录隔离的用例读的是**机主真实那一份**（本机当时是
    `{"post.engine": "auto", "minutes.hook": "null"}`）；
  - `set_current()` 设的是**进程级全局**，用例收场不还 —— `minutes.hook = "null"`
    于是跟着后面每一条用例走；
  - 产品路径上 `meeting/backends/minutes.py::_mark_running()` 读的是
    `minutes_hook_selector()`（`config.get("minutes.hook")`，**无视用例对
    `registry.resolve_minutes_hook` 的打桩**），于是 `launch_module.resolve_branch("null")`
    当场抛 `LaunchError`：24 条用例（`test_minutes_runner.py` 18、
    `test_post_idempotency.py` 4、`test_minutes_evidence.py` 2）被串档打红。

## 本文件钉的四件事

  1. 走真入口的那一步读到的生效配置**落在本用例的临时家目录**，不是机主那一份
     （`test_the_real_entry_reads_the_isolated_home_not_the_host_one`）；
  2. 走真入口的用例确实把生效配置设上了（否则第 1 条与第 3 条都钉不住），而**下一条用例
     开工时它是干净的** —— 这两条按定义顺序成对（`_..._leaks...` 在前、
     `test_the_next_case_starts_from_no_effective_config` 紧跟着它）；
  3. 端到端复现：把 `HOME` / `USERPROFILE` 指向一份含 `minutes.hook = "null"` 的
     `config.json`（模拟「机主真实配置里有非默认值」），**泄露源头那条用例 + 被串档打红的
     那一条**一起在真子进程里跑仍然是全绿；
  4. 第六道守卫（`tests/conftest.py` 的 `_no_writes_to_the_real_home`）不是恒真断言：
     对一个临时目录造出「被创建 / 被改动」两种现场，它当场读得出来。

第 3 条为什么用**真子进程**：夹具一定先于用例把 `HOME` 指到临时家目录，因此「外部环境里
那份配置」在进程内是造不出来的 —— 只有从外面把 `HOME` 传进来才复现得了复审那一幕。
子进程**不弹窗**（`_hidden_startupinfo()`：`STARTF_USESHOWWINDOW` + `SW_HIDE`，与宿主
`-WindowStyle Hidden` 同一件事；本机的 `AGENTS.md` 明令不用 `CREATE_NO_WINDOW`，而
`windowsHide` 是 Node 那一侧的选项名、Python 的 `Popen` 不收它），只跑两条用例（实测
0.4 秒级），不真起服务、不碰模型。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from meeting import cli as cli_module
from meeting import config as config_module
from meeting.cli import EXIT_OK, post

from tests import _helpers as helpers

#: **泄露源头**（复审现场里第一条把机主那份配置设上且不还的用例）：走真入口 `post()`
#: 而自己不做家目录隔离。它现在读的是本用例的临时家目录（`tests/conftest.py::_isolated_home`）。
LEAK_SOURCE_CASE = (
    "tests/test_cli_post.py::test_post_prints_the_selected_session_and_the_artifact_paths"
)

#: **被串档打红的那一条**（24 条之一）：它走真实现 `MinutesHook`，`_mark_running()` 会读
#: 生效的 `minutes.hook` —— 泄露在场时读到 `"null"` 就抛 `LaunchError`。
VICTIM_CASE = (
    "tests/test_minutes_runner.py::test_the_hook_claims_the_run_in_flight_before_it_returns"
)

#: 模拟「机主真实配置里有非默认值」的那一份（逐字取复审现场里机主那份的内容）
HOST_CONFIG_VALUES: dict[str, Any] = {"post.engine": "auto", "minutes.hook": "null"}


def _hidden_startupinfo() -> Any:
    """Windows 上让这个子进程**不弹窗**（`SW_HIDE`，与宿主 `-WindowStyle Hidden` 同一件事）。

    本机 `AGENTS.md` 明令不用 `CREATE_NO_WINDOW`（那会让子进程以 `STATUS_DLL_INIT_FAILED`
    死亡），Python 的 `Popen` 也不收 `windowsHide`（那是 Node 那一侧的选项名）——
    `STARTF_USESHOWWINDOW` + `SW_HIDE` 是这两条约束下同一件事的落法。
    """
    if os.name != "nt":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return info


def _ok_summary(session_dir: Any) -> dict[str, Any]:
    """`post()` 认的那份摘要（形状照 `tests/test_cli_post.py` 的假件）。"""
    return {
        "session_id": Path(session_dir).name,
        "session_dir": str(Path(session_dir).resolve()),
        "state": "done",
        "ok": True,
        "exit_code": EXIT_OK,
        "exit_code_name": "EXIT_OK",
        "archive": {"state": "done", "sources": []},
        "offline": {"hook": "done", "state": "done", "sources": {}},
        "minutes": {"hook": "finished", "state": "done"},
        "artifacts": {},
    }


# ---------------------------------------------------------------------------
# ① 生效配置的落点是本用例的临时家目录（不是机主那一份）
# ---------------------------------------------------------------------------


def test_the_real_entry_reads_the_isolated_home_not_the_host_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_writes_to_the_real_home
) -> None:
    """走真入口的这一步，`config.load()` 读的是本用例的临时家目录。

    读数是**生效配置自己报出来的落点**（`loaded.config_path()`）：夹具设环境变量只是布置，
    这一格回答的才是「这一趟真读了哪一份」。
    """
    guard = _no_writes_to_the_real_home
    session = tmp_path / "250919"
    session.mkdir()
    monkeypatch.setattr(
        cli_module, "run_post", lambda session_dir, **kwargs: _ok_summary(session_dir)
    )

    with pytest.raises(SystemExit) as info:
        post([str(session)])

    assert info.value.code == EXIT_OK
    loaded = config_module.current()
    assert loaded is not None, "真入口这一步必须把生效配置设上（不然这条用例钉不住下一条）"
    read_path = Path(loaded.config_path())
    home = Path(os.environ["HOME"])
    host_home = guard.config_dir.parent.parent
    assert home != host_home, (
        f"本用例的家目录该是自己的临时目录（夹具 `_isolated_home` 负责布置）：{home}"
    )
    assert read_path == home / ".config" / "meeting" / "config.json", (
        f"生效配置的落点该是本用例的临时家目录那一份：{read_path} != {home}"
    )
    assert read_path != guard.config_dir / "config.json", (
        f"生效配置的落点**不许**是机主真实那一份：{read_path}"
    )
    # 而且这一趟确实走到了「按生效配置重建服务级日志」那一格：日志落在临时家目录下，
    # 机主那份 `~/.config/meeting/logs/` 一个字节都不添（第六道守卫会在收场复查它）。
    assert loaded.get("minutes.hook") == "auto", (
        "隔离后生效值是产品缺省（`auto`）——真读到机主那份的话这里会是 'null'"
    )


def test_the_next_case_starts_from_no_effective_config() -> None:
    """**上一条用例的现场不许渗到这一条**（它刚把生效配置设上过）。

    这条按定义顺序紧跟着 `test_the_real_entry_reads_the_isolated_home_not_the_host_one`：
    `tests/conftest.py::_restore_effective_config` 在每条用例收场时把
    `config_module.set_current()` 的取值原样放回。少了那道夹具，这里读到的就是上一条
    留下的那份配置（复审现场里它一路渗到 24 条用例被判红）。
    """
    assert config_module.current() is None, (
        "上一条用例留下的模块级生效配置没有被放回："
        f"{config_module.current()!r}（`_restore_effective_config` 该在收场时还原）"
    )


# ---------------------------------------------------------------------------
# ③ 端到端：外部那份含 `minutes.hook = "null"` 的家目录配置进不了测试进程的用例
# ---------------------------------------------------------------------------


def test_the_affected_cases_stay_green_under_a_host_config_with_a_null_hook(
    tmp_path: Path,
) -> None:
    """泄露源头 + 被串档打红的那一条，在真子进程里一起跑 → 全绿。

    子进程的环境就是复审现场那一刻的外部环境：`HOME` / `USERPROFILE` 指向一份含
    `minutes.hook = "null"` 的 `config.json`（本用例的临时目录，**不动机主真实那一份**）。
    本仓的用例一律靠本用例的临时家目录读配置，因此那份外部配置连读都读不到。
    """
    host_home = tmp_path / "host-home"
    helpers.write_config_json(host_home, HOST_CONFIG_VALUES)

    env = {key: value for key, value in os.environ.items() if key not in helpers.SERVICE_ENV_NAMES}
    env.pop("MEETING_MINUTES_CHILD", None)
    env["HOME"] = str(host_home)
    env["USERPROFILE"] = str(host_home)
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            LEAK_SOURCE_CASE,
            VICTIM_CASE,
        ],
        cwd=str(helpers.REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        startupinfo=_hidden_startupinfo(),  # **不许弹窗**
    )
    output = f"{proc.stdout}\n{proc.stderr}"
    assert proc.returncode == 0, output
    assert "2 passed" in proc.stdout, output


# ---------------------------------------------------------------------------
# ④ 第六道守卫有牙：被创建 / 被改动都读得出来
# ---------------------------------------------------------------------------


def test_the_real_home_guard_notices_a_created_and_a_changed_file(tmp_path: Path) -> None:
    """守卫的判据本身（**不碰机主那一份**：现场造在临时目录里）。"""
    from tests import conftest as guard_module

    target = tmp_path / "config-meeting"
    target.mkdir()
    (target / "config.json").write_text("{}", encoding="utf-8")
    guard = guard_module.RealHomeGuard(
        config_dir=target, before=guard_module._real_home_snapshot(target)
    )
    assert guard.changed() == [], "一个字节都没动时不许报红（否则它是一道恒真断言）"

    (target / "config.json").write_text('{"minutes.hook": "null"}', encoding="utf-8")
    (target / "logs").mkdir()
    (target / "logs" / "meeting.log").write_text("x", encoding="utf-8")
    lines = guard.changed()
    assert any("config.json" in line for line in lines), lines
    assert any("logs/meeting.log" in line for line in lines), lines
    message = guard_module._real_home_violation(guard, lines, when="test_node_id")
    assert str(target) in message and "test_node_id" in message, message
    # 「改成了什么」也读得出来：改过的那份文件内容直接带在那一行里
    assert '"minutes.hook": "null"' in message, message

    absent = tmp_path / "absent"
    fresh = guard_module.RealHomeGuard(
        config_dir=absent, before=guard_module._real_home_snapshot(absent)
    )
    assert fresh.changed() == []
    absent.mkdir()
    assert fresh.changed() != [], "配置目录**被创建**也要算一笔"


def test_the_session_finish_check_reds_the_session_and_prints_the_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """会话**收场**那一半：改动落在最后一条用例收场之后也跑不掉。

    这一步直调 `pytest_sessionfinish()`（**不碰机主那一份**：读数造在临时目录里，只把守卫
    内存里的开工快照换成「那时什么都没有」），断言它把整场会话判红，并打印出被创建的路径与
    前后读数 —— 复审要的正是这两样。
    """
    from tests import conftest as guard_module

    target = tmp_path / "config-meeting"
    target.mkdir()
    guard = guard_module.RealHomeGuard(config_dir=target, before={})
    (target / "config.json").write_text('{"minutes.hook": "null"}', encoding="utf-8")
    monkeypatch.setattr(guard_module, "_ACTIVE_REAL_HOME_GUARD", guard)

    printed: list[str] = []

    class _Reporter:
        def write_line(self, message: str, red: bool = False, **kwargs: Any) -> None:
            printed.append(str(message))

    class _PluginManager:
        def get_plugin(self, name: str) -> Any:
            return _Reporter()

    class _Config:
        pluginmanager = _PluginManager()

    class _Session:
        config = _Config()
        exitstatus = int(pytest.ExitCode.OK)

    session = _Session()
    guard_module.pytest_sessionfinish(session, int(pytest.ExitCode.OK))

    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED, "收场复查该把这一趟判红"
    lines = [message for message in printed if "机主真实家目录" in message]
    assert lines, printed
    assert str(target) in lines[0], lines[0]
    assert "config.json" in lines[0], lines[0]
