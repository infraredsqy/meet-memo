"""`phase2/evidence/run_evidence.py` 的自证用例（修复轮 M3）。

那条实测证据脚本自己写 `evidence.log`（stdout 全文、UTF-8），不靠外部重定向——靠
`pwsh >` 这一类重定向落盘时，中文会先经一层控制台编码，好端端一份证据会变成乱码。
这一段验的是「脚本自己写」这件事本身，用注入的 log 落点，不改写目录里那份真证据
（那三个场次目录与真 log 由人工复跑脚本刷新，见 `evidence/evidence.md`）。

## 证据目录从哪来

证据脚本住在 `.work/nav/nodes/<uuid>/phase2/evidence/` 里。`.work` 是临时档案（不随项目
进版本控制），清理掉或换一个 checkout 之后那条路径就空了。因此这里**不再把那条路径写死**：
`MINUTES_EVIDENCE_DIR` 环境变量优先，缺席时退回本仓的默认路径（`:data:`DEFAULT_EVIDENCE_DIR`）；
脚本找不到时，那几条用例**带着路径与补救办法跳过**（`-rs` 看得到原因），路径解析本身另有
用例钉住（脚本缺席时它照绿）。

用例跑三档真进程（假 `dsh`），实测约 0.6 秒。
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
from pathlib import Path

import pytest

#: 证据目录的默认落点（仓库根 / `.work/nav/nodes/<节点 uuid>/phase2/evidence`）
DEFAULT_EVIDENCE_DIR = (
    Path(__file__).resolve().parents[1]
    / ".work"
    / "nav"
    / "nodes"
    / "41465462-cd68-4e9b-9b82-0c5f5e8a609f"
    / "phase2"
    / "evidence"
)

#: 指认证据目录的环境变量（换 checkout / 清理过 `.work` 之后靠它把路径递进来）
EVIDENCE_DIR_ENV = "MINUTES_EVIDENCE_DIR"

#: 证据脚本在这条路径下的文件名
SCRIPT_NAME = "run_evidence.py"


def evidence_dir() -> Path:
    """这一趟要用的证据目录：环境变量优先，缺席时退回本仓的默认路径。"""
    configured = os.environ.get(EVIDENCE_DIR_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_EVIDENCE_DIR


def evidence_script() -> Path | None:
    """证据脚本的落点；那个目录里没有它就交回 `None`。"""
    script = evidence_dir() / SCRIPT_NAME
    return script if script.is_file() else None


def _require_script() -> Path:
    """要那份证据脚本；不在就**跳过**（跳过原因里带着实际路径与改哪个变量）。

    跳过这件事写在用例的 stdout 上（`-rs` 看得到），因此「脚本不在」不会表现为
    「这一段静默变 0 条、没人察觉」。
    """
    script = evidence_script()
    if script is None:
        pytest.skip(
            f"证据脚本不在：{evidence_dir() / SCRIPT_NAME}"
            f"（换 checkout 或清理过 .work 时，把 {EVIDENCE_DIR_ENV} 指到放着 "
            f"{SCRIPT_NAME} 的那个目录）"
        )
    return script


def _load_module(script: Path):
    """按路径把证据脚本当模块装进来（它不在包里，因此走 `spec_from_file_location`）。"""
    spec = importlib.util.spec_from_file_location("phase2_run_evidence", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def evidence_module():
    """装上那份证据脚本（脚本不在时由 `_require_script()` 带原因跳过）。"""
    return _load_module(_require_script())


def test_the_evidence_directory_resolves_without_the_script() -> None:
    """证据目录解析得出、`MINUTES_EVIDENCE_DIR` 能把它顶上去、那里真有那份脚本。

    这一条在任何情形下都有断言要跑：脚本缺席时它只剩「路径解析」那一半（因此仍绿），
    脚本在时再把「这份文件读得动」也一起核了。
    """
    assert DEFAULT_EVIDENCE_DIR.is_absolute()
    assert DEFAULT_EVIDENCE_DIR.name == "evidence"
    expected = os.environ.get(EVIDENCE_DIR_ENV, "").strip()
    assert evidence_dir() == (Path(expected) if expected else DEFAULT_EVIDENCE_DIR)

    script = evidence_script()
    if script is None:
        pytest.skip(f"这一趟没有脚本可读（{evidence_dir() / SCRIPT_NAME}）")
    assert script.read_text(encoding="utf-8").strip(), "证据脚本该是一份有内容的东西"


def test_the_env_var_overrides_the_default_directory(monkeypatch, tmp_path: Path) -> None:
    """设了 `MINUTES_EVIDENCE_DIR` 就按它找（这就是「换 checkout」那一档的入口）。"""
    monkeypatch.setenv(EVIDENCE_DIR_ENV, str(tmp_path))

    assert evidence_script() is None, "空目录里当然没有那份脚本"


def test_a_missing_script_skips_with_the_path_and_the_way_out(monkeypatch, tmp_path: Path) -> None:
    """脚本缺席时抛的是**带原因的跳过**（路径 + 改哪个变量），那几条用例因此不会无声消失。"""
    monkeypatch.setenv(EVIDENCE_DIR_ENV, str(tmp_path))

    with pytest.raises(pytest.skip.Exception) as info:
        _require_script()

    message = str(info.value)
    assert str(tmp_path / SCRIPT_NAME) in message
    assert EVIDENCE_DIR_ENV in message


def test_it_writes_its_own_log_with_the_three_case_conclusion(
    evidence_module, tmp_path: Path, monkeypatch
) -> None:
    """**M3**：脚本自己把 stdout 全文写进 `evidence.log`，中文读得出来、结论在末行。"""
    # 本仓的测试夹具立着再入标记（防真跑子会话）：证据脚本要在**父进程那一侧**跑三档，
    # 这一条得摘掉，否则三档都走再入闸、一份状态文件都不落。
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)
    log = tmp_path / "evidence.log"
    stdout = io.StringIO()

    code = evidence_module.main([], log_path=log, stdout=stdout)

    assert code == 0
    assert log.is_file()
    written = log.read_text(encoding="utf-8")
    assert "结论：三档逐条符合预期" in written
    for case in ("case-ok", "case-zero-exit", "case-no-exe"):
        assert f'"case": "{case}"' in written
    # 头几行记着这一趟的时刻与命令行（拿 log 复现时先看的就是它们）
    head = written.splitlines()[:4]
    assert head[0].startswith("# 这一趟：")
    assert "run_evidence.py" in head[1]
    assert "# 本文件由 run_evidence.py 自己写" in written


def test_it_puts_the_path_back_and_the_log_is_the_stdout_text_verbatim(
    evidence_module, tmp_path: Path, monkeypatch
) -> None:
    """log 就是这一趟 stdout 的全文；跑完 `PATH` 原样放回（三档靠它选可执行文件）。"""
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)
    monkeypatch.setenv("PATH", r"C:\Windows\System32;C:\Program Files\Python")
    log = tmp_path / "evidence.log"
    stdout = io.StringIO()

    evidence_module.main([], log_path=log, stdout=stdout)

    assert os.environ["PATH"] == r"C:\Windows\System32;C:\Program Files\Python", (
        "跑完没把 PATH 放回去：同一进程里后面读 PATH 的东西会踩到那条被改短的"
    )
    printed = stdout.getvalue()
    written = log.read_text(encoding="utf-8")
    assert written.endswith(printed)
    assert printed.startswith("=" * 78)
    assert "结论：三档逐条符合预期" in printed


def test_it_rejects_stray_arguments(evidence_module, tmp_path: Path) -> None:
    """带参数调用当场退 2（免得有人以为它能选档跑）。"""
    log = tmp_path / "evidence.log"

    code = evidence_module.main(["case-ok"], log_path=log, stdout=io.StringIO())

    assert code == 2
    assert not log.exists()
