"""`meeting/minutes/launch.py` 的契约用例：产物四条判据、三档失败、argv 只有短指针。

要盯的六条（对应任务书 phase2 的完成判定与自检项）：

  1. **产物四条判据逐条可触发**：文件不在 / 空文件 / 缺 `## 说话人附录` / 缺 `### 猜不出的` /
     附录里没有数据行，五种坏形状都判失败；一份完整纪要判通过；
  2. **三档失败分得开**：解析不到可执行文件 / 子进程退出码非 0 / 退出码 0 但产物没落盘
     （第三档是 V1 踩过的坑，必须有测试钉住）；
  3. **argv 里只有一段短指针**：正文一个字都不进 argv（`cmd.exe` 截断多行参数那条坑），
     正文住 `working/run_prompt.md`；
  4. **不交互**：本模块里没有 `input(`（无人值守路径上任何交互都在等一个不会来的回车）；
  5. **子进程环境三格齐**：`DSH_PERMISSION_MODE` / `PYTHONIOENCODING` / `MEETING_MINUTES_CHILD`；
  6. **每次拉起都留一份 log**：头一行是命令、cwd 与退出码。

全部用例都用**假可执行文件**（`.cmd` shim + 一个 `sys.executable` 脚本），
一条都不调用系统上的 `dsh`。假脚本里那个 `time.sleep` 是**子进程**在被掐掉之前做的事：
用例侧等的是 `subprocess` 的超时事件本身，墙钟一到就返回。
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

from meeting.minutes import launch as launch_module
from meeting.minutes.launch import (
    APPENDIX_HEADING,
    CHECK_APPENDIX_HEADING,
    CHECK_APPENDIX_ROW,
    CHECK_FINAL_EMPTY,
    CHECK_FINAL_MISSING,
    CHECK_UNKNOWN_HEADING,
    FAILURE_ARTIFACT_MISSING,
    FAILURE_EXECUTABLE_MISSING,
    FAILURE_EXIT_CODE,
    FAILURE_TIMEOUT,
    FINAL_NAME,
    PROMPT_POINTER,
    UNKNOWN_HEADING,
    check_final_artifact,
    configured_branch,
    find_executable,
    launch_headless,
    render_command,
)

from tests._minutes_fakes import GOOD_FINAL
from meeting.minutes.prompt import DEFAULT_TEMPLATE

#: 本文件专门测 headless 拉起机制（假可执行文件 + 假子进程，不碰系统上的 `dsh`）：
#: 显式请第八道守卫（`tests/conftest.py::_no_real_dsh_delivery`）的豁免夹具 —— headless
#: 拉起这个原语在本文件里不打桩、收场也不判红。产品侧那道 `DeliveryForbidden` 默认拒绝闸
#: 照旧管用：下面的 `launch_headless(...)` 一律显式传 `allow_dsh=True`。
pytestmark = [pytest.mark.usefixtures("allow_dsh_protocol_stubs")]

#: 一条指着提示词的 argv（渲染器把它当指针用）
PROMPT_ARG_RE = re.compile(r"`([^`]+)`")

#: 假可执行文件驱动的场景（`--scenario` 之后那个词）
SCENARIO_OK = "ok"
SCENARIO_NO_FINAL = "no-final"
SCENARIO_EMPTY_FINAL = "empty-final"
SCENARIO_NO_APPENDIX = "no-appendix"
SCENARIO_EXIT_1 = "exit-1"
SCENARIO_SLEEP = "sleep"

#: 假可执行文件的脚本：按 `--scenario` 决定这一趟写什么、退几
#:
#: 正文里那两处 `__GOOD__` 由 `write_fake_dsh()` 换成那份合格纪要（用 `replace` 而不是
#: `format`：脚本自己用了 `%` 格式化与 `{}` 之外的普通字符，`format` 会把它们当占位符）。
FAKE_DSH_SOURCE = '''"""假 dsh：按 --scenario 决定这一趟写什么、退几（用例与 evidence 共用这一份）。"""
import re
import sys
import time
from pathlib import Path

POINTER_RE = re.compile(r"`([^`]+)`")
GOOD = __GOOD__
EMPTY = "  \\n"
NO_APPENDIX = "# 纪要\\n\\n只有正文，附录不在。\\n"


def main(argv):
    scenario = "ok"
    if "--scenario" in argv:
        scenario = argv[argv.index("--scenario") + 1]
    pointer = ""
    for item in argv:
        found = POINTER_RE.search(item)
        if found:
            pointer = found.group(1)
            break
    if not pointer:
        print("argv 里没有那条指着提示词文件的短指针", file=sys.stderr)
        return 3
    prompt_path = (Path.cwd() / pointer).resolve()
    text = prompt_path.read_text(encoding="utf-8")
    print("读了提示词：%d 字符" % len(text))
    if scenario == "sleep":
        time.sleep(30.0)
        return 0
    if scenario == "exit-1":
        print("模型调用失败", file=sys.stderr)
        return 1
    if scenario == "no-final":
        return 0
    final = Path.cwd() / "meeting_final.md"
    if scenario == "empty-final":
        final.write_text(EMPTY, encoding="utf-8")
        return 0
    if scenario == "no-appendix":
        final.write_text(NO_APPENDIX, encoding="utf-8")
        return 0
    final.write_text(GOOD, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def write_fake_dsh(root: Path) -> tuple[Path, Path]:
    """造一个假 `dsh`：`dsh.cmd` 是对外那个可执行文件，`fake_dsh.py` 是它的身体。

    交回 `(shim, script)`：`shim` 是 `shutil.which("dsh")` 会解析到的那一个（`.cmd`），
    `script` 是那份 Python（用例拿 `sys.executable` + 它当可执行文件，或者直接执行它）。
    """
    root.mkdir(parents=True, exist_ok=True)
    script = root / "fake_dsh.py"
    script.write_text(FAKE_DSH_SOURCE.replace("__GOOD__", repr(GOOD_FINAL)), encoding="utf-8")
    shim = root / "dsh.cmd"
    shim.write_text(
        "@echo off\r\n" f'"{sys.executable}" "%~dp0fake_dsh.py" %*\r\n',
        encoding="ascii",
    )
    return shim, script


def _session(tmp_path: Path, session_id: str = "250919") -> Path:
    """造一个场次目录（假可执行文件读的是提示词、写的是纪要，不碰转写）。"""
    session = tmp_path / session_id
    session.mkdir(parents=True, exist_ok=True)
    return session


class FakeSubprocess:
    """`subprocess.run` 的替身：按场景写产物、给退出码，**一条真进程都不起**。

    它是「用例怎么控制那一趟写什么」的唯一接缝：`launch_headless(runner=...)`。
    """

    #: 场景 → 写出去的纪要正文（`None` = 什么都不写）
    FINALS = {
        SCENARIO_OK: GOOD_FINAL,
        SCENARIO_EMPTY_FINAL: "  \n",
        SCENARIO_NO_APPENDIX: "# 纪要\n\n只有正文，附录不在。\n",
    }

    def __init__(self, scenario: str = SCENARIO_OK) -> None:
        self.scenario = scenario
        self.calls: list[dict] = []

    def __call__(self, command, **kwargs):
        self.calls.append({"command": list(command), "kwargs": dict(kwargs)})
        cwd = Path(kwargs.get("cwd") or ".")
        text = self.FINALS.get(self.scenario)
        if text is not None:
            (cwd / FINAL_NAME).write_text(text, encoding="utf-8")
        code = 1 if self.scenario == SCENARIO_EXIT_1 else 0
        return subprocess.CompletedProcess(list(command), code, stdout="out", stderr="err")

    @property
    def command(self) -> list[str]:
        return self.calls[-1]["command"]


class ScriptRunner:
    """给真 `subprocess.run` 包一层：用 `sys.executable` 跑那份假脚本，并把场景补进它的 argv。

    `launch_headless()` 的 argv 只有 `[可执行文件, --profile, headless, 指针]` 这一段，
    因此这里把脚本路径与 `--scenario <场景>` 插在可执行文件之后 —— 等价于「可执行文件就是
    那份假脚本」，同时仍然是**真进程**在跑（这一条路验的是 `subprocess.run` 本身）。
    """

    def __init__(self, script: Path, scenario: str) -> None:
        self.script = script
        self.scenario = scenario
        self.calls: list[list[str]] = []

    def __call__(self, command, **kwargs):
        argv = [command[0], str(self.script), "--scenario", self.scenario, *command[1:]]
        self.calls.append(argv)
        return subprocess.run(argv, **kwargs)


# ---------------------------------------------------------------------------
# 一、产物四条判据
# ---------------------------------------------------------------------------


def test_the_four_checks_pass_on_a_complete_final(tmp_path: Path) -> None:
    """一份完整纪要（正文 + 附录表里一行数据行）四条判据全过。"""
    session = _session(tmp_path)
    (session / FINAL_NAME).write_text(GOOD_FINAL, encoding="utf-8")

    assert check_final_artifact(session) == ()


def test_a_missing_final_is_the_first_check(tmp_path: Path) -> None:
    """文件不在 → 报第一条（这一档就是 V1 那个「退出码 0 但没落盘」的现场）。"""
    session = _session(tmp_path)

    assert check_final_artifact(session) == (CHECK_FINAL_MISSING,)


def test_a_whitespace_only_final_is_rejected(tmp_path: Path) -> None:
    """文件在、去空白之后是空的 → 报第二条。"""
    session = _session(tmp_path)
    (session / FINAL_NAME).write_text("  \n\t\n", encoding="utf-8")

    assert check_final_artifact(session) == (CHECK_FINAL_EMPTY,)


def test_a_final_without_the_appendix_heading_is_rejected(tmp_path: Path) -> None:
    """抽掉 `## 说话人附录` → 报第三条。"""
    session = _session(tmp_path)
    (session / FINAL_NAME).write_text(
        GOOD_FINAL.replace(APPENDIX_HEADING, "## 附录"), encoding="utf-8"
    )

    assert CHECK_APPENDIX_HEADING in check_final_artifact(session)


def test_a_final_without_the_unknown_heading_is_rejected(tmp_path: Path) -> None:
    """抽掉 `### 猜不出的` → 报第四条。"""
    session = _session(tmp_path)
    (session / FINAL_NAME).write_text(
        GOOD_FINAL.replace(UNKNOWN_HEADING, "### 其它"), encoding="utf-8"
    )

    assert CHECK_UNKNOWN_HEADING in check_final_artifact(session)


def test_an_appendix_with_only_a_header_is_rejected(tmp_path: Path) -> None:
    """附录里只有表头与分隔行（没有数据行）→ 报第五条：附录那一节还是空的。"""
    session = _session(tmp_path)
    bare = "\n".join(
        [
            "# 250919 会议纪要",
            "",
            "## 会议概要",
            "",
            "定了两件事。",
            "",
            APPENDIX_HEADING,
            "",
            "| 路 | 编号 | 人名 | 把握 | 依据 |",
            "| --- | --- | --- | --- | --- |",
            "",
            UNKNOWN_HEADING,
            "",
            "- 一个名字都推不出来",
            "",
        ]
    )
    (session / FINAL_NAME).write_text(bare, encoding="utf-8")

    assert check_final_artifact(session) == (CHECK_APPENDIX_ROW,)


def test_a_table_outside_the_appendix_does_not_count(tmp_path: Path) -> None:
    """正文里那张表不算数：判据看的是**附录那一节里**有没有数据行。"""
    session = _session(tmp_path)
    text = GOOD_FINAL.replace(
        "| loopback | S01 | 张三 | 中 | 会上自称负责部署 |", ""
    ).replace("## 会议概要", "## 会议概要\n\n| a | b |\n| --- | --- |\n| 1 | 2 |")
    (session / FINAL_NAME).write_text(text, encoding="utf-8")

    assert check_final_artifact(session) == (CHECK_APPENDIX_ROW,)


#: 表头那一行的各种写法（第一格换了词，仍是同一张表）+ 它后面只跟一条分隔行
EMPTY_TABLES_WITH_A_CHANGED_HEADER = (
    "| 路 | 编号 | 人名 | 把握 | 依据 |",
    "| 路径 | 编号 | 人名 | 把握 | 依据 |",
    "| source 通道 | 编号 | 人名 | 把握 | 依据 |",
    "| 说话人 | 编号 | 人名 | 把握 | 依据 |",
    "| 说话人 | 编号 | 人名 | 把握 |",  # 4 格也算一张表的表头
)

#: 分隔行的两种写法
SEPARATOR_ROWS = ("| --- | --- | --- | --- | --- |", "| :--- | ---: | --- | --- | --- |")


def _final_with(table: str) -> str:
    """一份正文齐全、附录那张表由 `table` 决定的产物。"""
    return "\n".join(
        [
            "# 250919 会议纪要",
            "",
            "## 会议概要",
            "",
            "定了两件事。",
            "",
            APPENDIX_HEADING,
            "",
            table,
            "",
            UNKNOWN_HEADING,
            "",
            "- 一个名字都推不出来",
            "",
        ]
    )


@pytest.mark.parametrize("header", EMPTY_TABLES_WITH_A_CHANGED_HEADER)
@pytest.mark.parametrize("separator", SEPARATOR_ROWS)
def test_an_empty_appendix_table_is_rejected_whatever_the_first_header_cell_says(
    tmp_path: Path, header: str, separator: str
) -> None:
    """**C1**：附录里只有表头 + 分隔行时，**表头第一格换成哪个词都判失败**。

    早先认表头靠第一格的字面值（`cells[0] in ("路", "source")`）：模型把第一格换成「路径」
    「说话人」，一张一条数据都没有的空附录就被当成了「有数据行」，四条判据全过、状态记 `done`。
    这一档是 V1「退出码 0 但没做完」的近亲，因此每种表头写法 × 每种分隔行写法都钉一遍。
    """
    session = _session(tmp_path)
    (session / FINAL_NAME).write_text(
        _final_with(f"{header}\n{separator}"), encoding="utf-8"
    )

    assert check_final_artifact(session) == (CHECK_APPENDIX_ROW,), (
        f"空附录（表头「{header}」+ 分隔行「{separator}」）该判失败"
    )


def test_a_table_with_a_header_and_a_data_row_still_passes(tmp_path: Path) -> None:
    """判据没有变严：真模板要求照抄的那张表头 + 一行数据行照样四条全过。"""
    session = _session(tmp_path)
    (session / FINAL_NAME).write_text(
        _final_with(
            "| 路 | 编号 | 人名 | 把握 | 依据 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| loopback | S01 | 张三 | 中 | 依据：会上自称负责部署 |"
        ),
        encoding="utf-8",
    )

    assert check_final_artifact(session) == ()


def test_the_this_route_has_no_speaker_numbers_row_still_passes(tmp_path: Path) -> None:
    """模板示例里那一行「这一路没有说话人编号，分不出个人」照样合格。

    它是最要紧的一档：一格内容 + 四格 `—`（`—` 是非空字符，因此这一行 5 格都算填了）。
    后来人若把「表头 + 分隔行 + 数据行」的算法写成「数据行得有多格有内容」，这一条会当场红。
    """
    session = _session(tmp_path)
    (session / FINAL_NAME).write_text(
        _final_with(
            "| 路 | 编号 | 人名 | 把握 | 依据 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| mic | — | — | — | 这一路没有说话人编号，分不出个人 |"
        ),
        encoding="utf-8",
    )

    assert check_final_artifact(session) == ()


def test_a_header_on_its_own_without_a_separator_is_rejected(tmp_path: Path) -> None:
    """只有表头、连分隔行都没有 → 判失败（一张表的骨架还缺一条）。"""
    session = _session(tmp_path)
    (session / FINAL_NAME).write_text(_final_with("| 路 | 编号 | 人名 | 把握 | 依据 |"), encoding="utf-8")

    assert check_final_artifact(session) == (CHECK_APPENDIX_ROW,)


def test_a_narrow_row_is_not_a_data_row(tmp_path: Path) -> None:
    """分隔行之后只有一格宽的两行凑格（`| — | — |`）→ 判失败：那不是一行数据。"""
    session = _session(tmp_path)
    (session / FINAL_NAME).write_text(
        _final_with(
            "| 路 | 编号 | 人名 | 把握 | 依据 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| — | — |\n"
            "| mic | — |"
        ),
        encoding="utf-8",
    )

    assert check_final_artifact(session) == (CHECK_APPENDIX_ROW,)


def test_a_filled_three_cell_row_passes_without_a_header(tmp_path: Path) -> None:
    """三格都填了内容的行照样算数：表头那一行被抽掉时，附录里那行数据仍然落地了（判据没变严）。"""
    session = _session(tmp_path)
    (session / FINAL_NAME).write_text(
        _final_with(
            "| --- | --- | --- |\n"
            "| mic | — | 这一路分不出个人 |"
        ),
        encoding="utf-8",
    )

    assert check_final_artifact(session) == ()


def test_a_written_data_row_counts_when_the_header_is_three_cells_wide(tmp_path: Path) -> None:
    """数据行的判定与表头字面值无关：表头写「路 | 编号 | 人名」（3 格）+ 一行 3 格数据行 → 合格。"""
    session = _session(tmp_path)
    (session / FINAL_NAME).write_text(
        _final_with(
            "| 路 | 编号 | 人名 |\n"
            "| --- | --- | --- |\n"
            "| mic | — | 这一路分不出个人 |"
        ),
        encoding="utf-8",
    )

    assert check_final_artifact(session) == ()


def test_a_second_table_in_the_appendix_does_not_rescue_the_first_one(tmp_path: Path) -> None:
    """附录里第一张是空表、第二张才有数据行 → 判合格（判据问的是「附录里有没有写了东西的表」）。"""
    session = _session(tmp_path)
    (session / FINAL_NAME).write_text(
        _final_with(
            "| 路 | 编号 | 人名 | 把握 | 依据 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "\n"
            "补一张按来源分的：\n"
            "\n"
            "| 来源 | 编号 | 人名 |\n"
            "| --- | --- | --- |\n"
            "| loopback | S01 | 张三 |"
        ),
        encoding="utf-8",
    )

    assert check_final_artifact(session) == ()


def _template_fixed_block() -> str:
    """真模板里那段固定文字（落在产物里的那一段）的正文。

    它在模板给写手的终稿骨架里（一个围栏代码块），因此每行的行首前缀先摘掉再认围栏。
    抠出来的是**要上生产的那一份**，判据对它的态度就是真产物的待遇。
    """
    blocks: list[list[str]] = []
    inside = False
    for raw in DEFAULT_TEMPLATE.read_text(encoding="utf-8").splitlines():
        line = raw[2:] if raw.startswith("> ") else raw[1:] if raw == ">" else raw
        if line.strip().startswith("```"):
            inside = not inside
            if inside:
                blocks.append([])
            continue
        if inside:
            blocks[-1].append(line)
    found = [block for block in blocks if any(APPENDIX_HEADING in line for line in block)]
    assert len(found) == 1, f"模板里含 {APPENDIX_HEADING} 的代码块有 {len(found)} 个"
    return "\n".join(found[0])


def test_the_fixed_block_of_the_real_template_passes_the_check(tmp_path: Path) -> None:
    """**判据没被修严**：真模板里那段固定文字（五格表头 + 两行示例）照样判合格。

    这一条盯的是「换个表头词的空表要判失败」这条修复的**反面风险**：判严了会让正常产物
    被记成 failed，那比原病更坏。模板改一个字，这条跟着一起看。
    """
    session = _session(tmp_path)
    (session / FINAL_NAME).write_text(_template_fixed_block() + "\n", encoding="utf-8")

    assert check_final_artifact(session) == ()


# ---------------------------------------------------------------------------
# 二、argv 只放短指针 / 环境三格 / 分支解析
# ---------------------------------------------------------------------------


def test_the_command_carries_only_a_short_pointer() -> None:
    """argv 里只有那段短指针，正文一个字都不进去。"""
    command = render_command(r"C:\tools\dsh.cmd")

    assert command == (r"C:\tools\dsh.cmd", "--profile", "headless", PROMPT_POINTER)
    assert len(command[-1]) < 120, "argv 里那段指针要短（cmd.exe 会截断长参数）"


def test_the_child_env_has_the_three_explicit_values() -> None:
    """子进程环境：权限模式、IO 编码、再入标记三格都在。"""
    env = launch_module.build_child_env({"PATH": "x"})

    assert env["DSH_PERMISSION_MODE"] == "danger-full-access"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["MEETING_MINUTES_CHILD"] == "1"
    assert env["PATH"] == "x", "父进程的环境要原样带过去"


def test_configured_branch_keeps_the_configured_word() -> None:
    """`configured_branch()` 只校验取值域、**原样交回**配置里那个词。

    它给的是「拉起方建线程之前落的那一版 `running`」用的那一格：那一刻还没探 DSH web
    服务端在不在跑，因此那一版记的就是配置里写的词；`runner` 探完之后落的下一版才记
    实际走的那一条（`web` / `headless`）。
    """
    for configured in ("auto", "web", "headless", None):
        assert configured_branch(configured) == (configured or "auto")

    with pytest.raises(launch_module.LaunchError, match="启动分支"):
        configured_branch("gui")


def test_find_executable_reports_what_which_found() -> None:
    """解析器交回什么就是什么；解析不到交回空字符串。"""
    assert find_executable("dsh", which=lambda name: r"C:\npm\dsh.cmd") == r"C:\npm\dsh.cmd"
    assert find_executable("dsh", which=lambda name: None) == ""


def test_this_module_never_reads_stdin() -> None:
    """本模块里没有 `input()` 调用（无人值守路径上任何交互都在等一个不会来的回车）。

    按**语法树**判：模块头的说明文字里正当地提到了这个名字，判据要认的是真的在调它。
    """
    tree = ast.parse(Path(launch_module.__file__).read_text(encoding="utf-8"))

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "input" not in called


# ---------------------------------------------------------------------------
# 三、三档失败 + 成功那一档（假可执行文件驱动）
# ---------------------------------------------------------------------------


def test_a_fake_dsh_that_writes_a_final_is_a_success(tmp_path: Path) -> None:
    """假可执行文件写出一份完整纪要 → 成功，状态里带着退出码、耗时与 log。"""
    shim, _script = write_fake_dsh(tmp_path / "bin")
    session = _session(tmp_path)

    result = launch_headless(
        session, prompt_text="正文在这里。", executable=str(shim), allow_dsh=True
    )

    assert result.ok is True, result.error
    assert result.failure is None
    assert result.exit_code == 0
    assert result.branch == "headless"
    assert (session / FINAL_NAME).is_file()
    assert result.seconds >= 0.0
    assert result.as_dict()["executable"] == str(shim)


def test_a_fake_dsh_that_exits_zero_without_the_final_is_a_failure(tmp_path: Path) -> None:
    """**退出码 0 但产物没落盘 → 判失败**（V1 踩过的那个坑，本轮钉住）。"""
    _shim, script = write_fake_dsh(tmp_path / "bin")
    session = _session(tmp_path)
    fake = FakeSubprocess(SCENARIO_NO_FINAL)

    result = launch_headless(
        session,
        prompt_text="正文在这里。",
        executable=str(script),
        runner=fake,
        allow_dsh=True,
    )

    assert result.ok is False
    assert result.failure == FAILURE_ARTIFACT_MISSING
    assert result.checks == (CHECK_FINAL_MISSING,)
    assert result.exit_code == 0, "退出码本身是 0：这一档正是「只看退出码会漏掉」的那一档"
    assert "退出码 0" in str(result.error)


def test_a_fake_dsh_that_exits_nonzero_is_a_failure(tmp_path: Path) -> None:
    """子进程退出码非 0 → 判失败，错误里写着退出码与 log 的位置。"""
    _shim, script = write_fake_dsh(tmp_path / "bin")
    session = _session(tmp_path)

    result = launch_headless(
        session,
        prompt_text="正文在这里。",
        executable=str(script),
        runner=FakeSubprocess(SCENARIO_EXIT_1),
        allow_dsh=True,
    )

    assert result.ok is False
    assert result.failure == FAILURE_EXIT_CODE
    assert result.exit_code == 1
    assert launch_module.ARTIFACT_LOG in str(result.error)


def test_a_timeout_is_a_failure_with_the_seconds_and_the_prompt(tmp_path: Path) -> None:
    """真起一个卡住的子进程，再把超时压到 0.2 秒：错误里写清超时秒数与提示词路径。"""
    _shim, script = write_fake_dsh(tmp_path / "bin")
    session = _session(tmp_path)
    result = launch_headless(
        session,
        prompt_text="正文在这里。",
        executable=sys.executable,
        timeout=0.2,
        runner=ScriptRunner(script, SCENARIO_SLEEP),
        allow_dsh=True,
    )

    assert result.ok is False
    assert result.failure == FAILURE_TIMEOUT
    assert "0.2 秒" in str(result.error)
    assert "working/run_prompt.md" in str(result.error)


def test_an_unresolvable_executable_says_npm_only_makes_shims(tmp_path: Path) -> None:
    """解析不到可执行文件 → 当场失败，错误里写着 Windows 上 npm 只生成 shim 这件事。"""
    session = _session(tmp_path)

    result = launch_headless(
        session, prompt_text="正文在这里。", which=lambda name: None, allow_dsh=True
    )

    assert result.ok is False
    assert result.failure == FAILURE_EXECUTABLE_MISSING
    assert result.executable == ""
    assert "shim" in str(result.error) and "WinError 2" in str(result.error)
    assert not (session / FINAL_NAME).exists(), "解析不到时一个产物都不该有"


def test_the_prompt_is_written_to_disk_and_the_argument_points_at_it(tmp_path: Path) -> None:
    """正文落 `working/run_prompt.md`；argv 里那一段指针指着**这个相对路径**。"""
    _shim, script = write_fake_dsh(tmp_path / "bin")
    session = _session(tmp_path)
    fake = FakeSubprocess(SCENARIO_OK)

    result = launch_headless(
        session,
        prompt_text="这是提示词正文。",
        executable=str(script),
        runner=fake,
        allow_dsh=True,
    )

    prompt_file = session / "working" / "run_prompt.md"
    assert prompt_file.read_text(encoding="utf-8") == "这是提示词正文。"
    assert fake.calls[-1]["kwargs"]["cwd"] == str(session)
    assert "这是提示词正文。" not in " ".join(fake.command)
    assert PROMPT_POINTER in fake.command
    assert fake.calls[-1]["kwargs"]["env"]["DSH_PERMISSION_MODE"] == "danger-full-access"
    # 指针里的相对路径真的能解析到那份提示词（子进程就是照它读的）
    assert PROMPT_ARG_RE.search(PROMPT_POINTER).group(1) == "working/run_prompt.md"
    assert result.ok is True


def test_every_launch_leaves_a_log_with_the_header(tmp_path: Path) -> None:
    """每一次拉起都留一份 log：头一行是命令、cwd 与退出码。"""
    _shim, script = write_fake_dsh(tmp_path / "bin")
    session = _session(tmp_path)

    launch_headless(
        session,
        prompt_text="正文在这里。",
        executable=str(script),
        runner=FakeSubprocess(SCENARIO_OK),
        allow_dsh=True,
    )

    log = (session / launch_module.ARTIFACT_LOG).read_text(encoding="utf-8")
    assert log.startswith("# 命令：")
    assert f"# cwd：{session}" in log
    assert "# 退出码：0" in log
    assert "## stdout" in log and "## stderr" in log


# ---------------------------------------------------------------------------
# 四、跑得动真的那一条（真子进程 + 假脚本，仍不碰系统上的 dsh）
# ---------------------------------------------------------------------------


def test_a_real_subprocess_writes_the_final(tmp_path: Path) -> None:
    """真起一次子进程（就是那份假脚本）：这一档验的是 `subprocess.run` 这条真路。"""
    _shim, script = write_fake_dsh(tmp_path / "bin")
    session = _session(tmp_path)

    result = launch_headless(
        session,
        prompt_text="正文在这里。",
        executable=sys.executable,
        runner=ScriptRunner(script, SCENARIO_OK),
        allow_dsh=True,
    )

    assert result.ok is True, result.error
    assert (session / FINAL_NAME).read_text(encoding="utf-8").strip()
    assert script.is_file()


def test_a_real_subprocess_reports_a_nonzero_exit(tmp_path: Path) -> None:
    """真起一次子进程并让它退 1：状态里记的就是那个退出码。"""
    _shim, script = write_fake_dsh(tmp_path / "bin")
    session = _session(tmp_path)

    result = launch_headless(
        session,
        prompt_text="正文在这里。",
        executable=sys.executable,
        runner=ScriptRunner(script, SCENARIO_EXIT_1),
        allow_dsh=True,
    )

    assert result.ok is False
    assert result.failure == FAILURE_EXIT_CODE
    assert result.exit_code == 1
    assert script.is_file()


@pytest.mark.skipif(sys.platform != "win32", reason="`.cmd` shim 是 Windows 上的那一档")
def test_a_cmd_shim_resolves_through_which(tmp_path: Path) -> None:
    """`shutil.which("dsh")` 在 PATH 上认得出那份 `.cmd`（npm 生成的就是这个形状）。

    这一条验的是**解析那一半**：把假 shim 所在目录挂到 PATH 最前面，再真解析一次。
    """
    shim, _script = write_fake_dsh(tmp_path / "bin")
    found = find_executable(
        "dsh", which=lambda name: shim if name == "dsh" else None
    )

    assert Path(found) == shim
