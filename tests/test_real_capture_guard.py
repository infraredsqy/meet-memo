"""`tests/test_real_capture_guard.py` —— 「一碰真声卡就炸」这道看门**与导入次序无关**的验。

## 为什么这条验要跑在一个**全新解释器**里

看门（`tests/conftest.py` 的 autouse 夹具 `_no_real_audio_capture`）要证明的性质只有一条：
**在库还没被 import 的现场，它照样把入口拦下**。而本进程里 `sounddevice` / `soundcard`
早被其它用例 import 过（夹具自己也会 import 一次），在**同一个进程里**根本造不出「库还没
进 `sys.modules`」这个现场 —— 一句 `sys.modules.pop()` 只能伪造出一个不可靠的现场。

所以这里换一条路：本用例起一个**子进程** `python -m pytest`，在它里面临时落一份最小的
用例文件（落在 `tmp_path` 下，`-c` 给一份自己的 `pytest.ini`，不读仓库 `pyproject.toml`
的 addopts），那份文件**不 import 任何声卡库**，直接调 `sd.InputStream(...)` / `sd.rec(...)` /
`sd.check_input_settings(...)` / `sc.get_microphone(...)`，并预期每一次都被
`tests/conftest.py` 的桩拦下（抛 `RealCaptureForbidden`、文案含
`REAL_CAPTURE_FORBIDDEN_MESSAGE`）。

**仓库根的 `tests/conftest.py` 是显式请进那个子进程的**（临时目录里那份 conftest 写一行
`pytest_plugins = ["tests.conftest"]`）。`--rootdir` 只改 rootdir 这个名字、**不会**把
仓库的 conftest 装进来（临时目录在仓库之外，conftest 的收集顺序压根走不到 `tests/`），
所以在临时目录里放一份 conftest 把它当插件加载是这里唯一的办法；`pythonpath` 由
`PYTHONPATH=<仓库根>` 给全。

## 隐私约束（本文件自己也要守）

**子进程里没有任何一次调用落到真设备上**：四个入口在这一轮跑之前就已经被桩换掉了，
每一次调用都在进入 `sounddevice` / `soundcard` 之前抛错。本文件不跑 `sd.rec()` 的
「真录一下看看」那种探针，也不做只读枚举之外的真机动作。
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from tests import _helpers as helpers

#: 被测的四个入口（与 `tests/conftest.py` 的 `_FORBIDDEN_CAPTURE_ENTRIES` **同源**）
GUARDED_ENTRIES: tuple[tuple[str, str], ...] = (
    ("sounddevice", "InputStream"),
    ("sounddevice", "rec"),
    ("sounddevice", "check_input_settings"),
    ("soundcard", "get_microphone"),
)

#: 四个入口各自的调用形状：`(位置参数表, 关键字参数表)`。
#:
#: **都是「看起来完全正常」的调用**：`InputStream` 与 `check_input_settings` 带上一个设备
#: 序号、`rec` 带上时长、`get_microphone` 走 loopback。它们要是没被桩拦下，本机就真的会去
#: 开设备 —— 这正是这条验要防的事。
GUARDED_CALLS: dict[tuple[str, str], tuple[list[str], dict[str, str]]] = {
    ("sounddevice", "InputStream"): ([0], {}),
    ("sounddevice", "rec"): ([1], {}),
    ("sounddevice", "check_input_settings"): ([], {"device": 0, "samplerate": 16000}),
    ("soundcard", "get_microphone"): ([], {"include_loopback": True}),
}

_SUBPROCESS_TEST_FILE = "test_zz_fresh_interpreter_guard.py"

GUARD_STUB_QUALNAME_TOKEN = "blocked"
#: 看门的桩定义在哪个模块（身份断言的第二半）
GUARD_STUB_MODULE = "tests.conftest"


def _entries_for_installed_modules() -> list[tuple[str, str]]:
    """只把**这台机器上装着的**库的入口列进来（没装的库不验：它连调用都无从谈起）。

    判据用 `importlib.util.find_spec()`（**只问有没有，不 import**）：本文件自己不许把
    声卡库 import 进本进程 —— 那会让「子进程里库还没被 import」这件事看起来像是本文件
    伪造出来的。
    """
    import importlib.util

    available: list[tuple[str, str]] = []
    for module_name, entry in GUARDED_ENTRIES:
        try:
            found = importlib.util.find_spec(module_name) is not None
        except (ImportError, ValueError):  # pragma: no cover - 坏安装
            found = False
        if found:
            available.append((module_name, entry))
    return available


def _python_literal(value: object) -> str:
    """把调用形状写成**源码里的字面量**（`json.dumps` 出来的引号在这里是语法错）。"""
    if isinstance(value, dict):
        items = ", ".join(f"{key!r}: {_python_literal(item)}" for key, item in value.items())
        return "{" + items + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_python_literal(item) for item in value) + "]"
    return repr(value)


def _subprocess_case_source(entries: list[tuple[str, str]]) -> str:
    """生成子进程里那份用例的源码：**不 import 任何声卡库**，直接调四个入口。

    生成的用例在**每一次调用之前**先断言目标确实是 `tests/conftest.py` 装的那个桩
    （`"blocked" in __qualname__` 且 `__module__ == "tests.conftest"`）：这条验的安全性整个
    押在它正在验的那道看门上 —— 看门一回归，`sd.InputStream(0)` 在 `__init__` 里就调
    `Pa_OpenStream`，`sd.rec(1)` 会真的录 1 秒。桩没装上就红在断言上，调用一次都不发出去。
    """
    cases = []
    for module_name, entry in entries:
        args, kwargs = GUARDED_CALLS[(module_name, entry)]
        cases.append(
            "    (%r, %r, %s, %s),\n"
            % (module_name, entry, _python_literal(args), _python_literal(kwargs))
        )
    return (
        '"""全新解释器里的最小用例：**先不 import 声卡库**，直接调真采集入口。\n\n'
        "本文件由 `tests/test_real_capture_guard.py` 生成，只活在临时目录里。\n"
        '"""\n\n'
        "from __future__ import annotations\n\n"
        "import pytest\n\n"
        "from tests.conftest import REAL_CAPTURE_FORBIDDEN_MESSAGE, RealCaptureForbidden\n"
        "from tests.test_real_capture_guard import GUARD_STUB_MODULE, GUARD_STUB_QUALNAME_TOKEN\n\n"
        "CASES = [\n"
        + "".join(cases)
        + "]\n\n\n"
        "def test_fresh_interpreter_blocks_real_capture_entries() -> None:\n"
        '    """库第一次被 import 就撞上桩：四个入口逐条必须被看门拦下。\n\n'
        "    这里**没有一处 import 声卡库**（本文件顶上也一行都没有），库是在 `conftest.py`\n"
        "    那道看门夹具里第一次被请进来、同一刻被打上桩的 —— 所以子进程用不着先替谁 import\n"
        "    一次，调用点撞上的就是桩。修 C1 之前那种写法（只查 `sys.modules`、查不到就跳过）\n"
        "    下，这里每一次调用都会落到真设备上：`rec(1)` 交回 `PortAudioError`，而不是下面\n"
        "    点名要的 `RealCaptureForbidden`。\n\n"
        "    **每一次调用之前**先断言那个入口确实是桩：看门一旦回归，这一句断言会先把用例\n"
        "    拦下，真调用（`InputStream(0)` 在 `__init__` 里就 `Pa_OpenStream`、`rec(1)` 录\n"
        "    1 秒）一次都不会发出去。\n"
        '    """\n'
        "    import importlib\n\n"
        "    called = []\n"
        "    for module_name, entry, args, kwargs in CASES:\n"
        "        module = importlib.import_module(module_name)\n"
        "        target = getattr(module, entry)\n"
        "        where = f\"{module_name}.{entry}\"\n"
        "        qualname = str(getattr(target, '__qualname__', ''))\n"
        "        origin = str(getattr(target, '__module__', ''))\n"
        "        # 调用之前的身份断言：桩没装上就红在这里，真调用一次都不发出去\n"
        "        assert GUARD_STUB_QUALNAME_TOKEN in qualname, (\n"
        "            f'{where} 上没有看门装的桩（__qualname__ = {qualname!r}）：'\n"
        "            '再往下就会真的打开设备，因此停在这里'\n"
        ")\n"
        "        assert origin == GUARD_STUB_MODULE, (\n"
        "            f'{where} 上的桩不来自 {GUARD_STUB_MODULE}（__module__ = {origin!r}）：'\n"
        "            '停在这里'\n"
        ")\n"
        "        with pytest.raises(RealCaptureForbidden) as info:\n"
        "            getattr(module, entry)(*args, **kwargs)\n"
        "        text = str(info.value)\n"
        "        assert REAL_CAPTURE_FORBIDDEN_MESSAGE in text, text\n"
        "        assert f\"{module_name}.{entry}\" in text, text\n"
        "        called.append(f\"{module_name}.{entry}\")\n"
        "    assert called == [f\"{module}.{entry}\" for module, entry, _a, _k in CASES]\n"
    )


def test_a_fresh_interpreter_blocks_the_real_capture_entries(tmp_path: Path) -> None:
    """**在一个全新解释器里验证 C1 的修复**：库未 import 时看门照样把四个入口拦下。

    子进程里跑的是 `python -m pytest <tmp_path>`（`-p no:cacheprovider`、`-c <临时配置>`、
    `--rootdir=<仓库根>`）：临时目录里那份最小用例在调每个入口之前先断言**那个入口确实是桩**
    （`"blocked" in __qualname__` 且 `__module__ == "tests.conftest"`），「不依赖导入顺序」
    这句话因此落在子进程自己给出的读数上。任何一条入口没被拦下（或拦下来抛的不是
    `RealCaptureForbidden`、文案不对），子进程的 pytest 就非零退出，这条用例跟着红；而看门
    一旦回归，子进程红在**调用之前**那道身份断言上 —— 真设备一次都不会被碰到。
    """
    entries = _entries_for_installed_modules()
    if not entries:  # pragma: no cover - 这台机器上一个声卡库都没装
        import pytest

        pytest.skip("本机没装 sounddevice / soundcard：没有可拦的入口")

    (tmp_path / _SUBPROCESS_TEST_FILE).write_text(
        _subprocess_case_source(entries), encoding="utf-8"
    )
    (tmp_path / "conftest.py").write_text(
        'pytest_plugins = ["tests.conftest"]\n', encoding="utf-8"
    )
    # 子进程自己的 pytest 配置：给一份空配置（不去读仓库 pyproject.toml 的 addopts）。
    # 文件名必须是 `pytest.ini`：`-c` 指到别的扩展名时 pytest 按 `setup.cfg` 的规矩解析
    # `[pytest]` 段并拒绝（`[pytest] section in setup.cfg files is no longer supported`）。
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")

    with tempfile.TemporaryDirectory() as home:
        env = helpers.service_env(Path(home))
        env["PYTHONPATH"] = str(helpers.REPO_ROOT)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(tmp_path),
                "-q",
                "-p",
                "no:cacheprovider",
                "-c",
                str(config),
                "--rootdir",
                str(helpers.REPO_ROOT),
            ],
            cwd=str(helpers.REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    output = result.stdout + result.stderr
    assert result.returncode == 0, (
        "全新解释器里的看门没有拦住真采集入口 ——\n"
        f"子进程命令：{[sys.executable, '-m', 'pytest', str(tmp_path)]}\n"
        f"被抓的入口：{entries}\n"
        f"子进程输出：\n{output}"
    )
    assert "1 passed" in output, output
