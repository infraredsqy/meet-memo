"""**假 `webbrowser` 模块 —— 测试专用**（phase1 §3.5 第 5 条 / §5 第 1 条的测试隔离设施）。

它的角色是**测试隔离设施**：被测的是 `meeting` 包，这里提供的是子进程里顶替标准库的那一份
同名声明的替身。它由 `tests/_helpers.py` 的 `fake_browser_module()` 通过 `PYTHONPATH`
前置注入到**真服务子进程**里，子进程执行 `import webbrowser` 时命中的就是这一份。

存在的理由（一次事故的收口）：
  子进程里验「打开浏览器失败只警告」这一支时，曾经用 `BROWSER=no-such-browser` 造失败 ——
  这在 Windows 上**不成立**：标准库 `webbrowser.register_standard_browsers()` 先在
  `sys.platform[:3] == "win"` 分支里 `register("windows-default", WindowsDefault)`
  （Python 3.13 的 `Lib/webbrowser.py`），再处理 `BROWSER`；`BROWSER` 指向的浏览器
  `shutil.which()` 找不到时那一项落空，`open()` 于是继续往下走，**用用户默认浏览器真开了窗**
  （本机实测：`webbrowser._tryorder` 头部是
  `['no-such-browser-for-meeting-tests', 'windows-default', ...]`）。
  本模块把这条路彻底切断：它的任何函数都**只会记录、只按测试要求返回**，
  不碰注册表、不拉子进程、不开窗。

它同时补齐了标准库的那一小套接口（`register` / `get` / `register_standard_browsers` /
几个浏览器类名），因此即使哪天有人误在子进程里写了 `webbrowser.register_standard_browsers()`，
命中的也是这里的空实现，两个模块的成员混用带来的 `NameError` 因此不会出现。

两件事都由环境变量指挥（名字与 `tests/_helpers.py` 里的常量一一对应）：

  - `MEETING_TEST_FAKE_BROWSER_MARKER`：**本模块被载入**时往这个文件写一行，用来证明
    子进程里命中的确实是我这一份（而不是标准库那份）。
  - `MEETING_TEST_FAKE_BROWSER_CALLS`：`open()` **每次被调用**都往这个文件追加一行 ——
    「这一步到底有没有被调到」由它判定（在真子进程里这是唯一的观测面）。
  - `MEETING_TEST_FAKE_BROWSER_OUTCOME`：`open()` 的返回值/行为，取值
    `false`（返回 `False`）/ `true`（返回 `True`）/ `raise`（抛 `RuntimeError`），
    缺省 `false`。
"""

from __future__ import annotations

import os

#: 记日志要用**内建** `open`：本模块自己定义的 `open()`（标准库同名接口的替身）在模块内
#: 会把内建那个名字遮住，没这一手的话记录文件时就会调到自己身上、参数对不上。
from builtins import open as _builtin_open

__all__ = [
    "BackgroundBrowser",
    "GenericBrowser",
    "WindowsDefault",
    "get",
    "open",
    "register",
    "register_standard_browsers",
]

MARKER_ENV = "MEETING_TEST_FAKE_BROWSER_MARKER"
CALLS_ENV = "MEETING_TEST_FAKE_BROWSER_CALLS"
OUTCOME_ENV = "MEETING_TEST_FAKE_BROWSER_OUTCOME"


def _append(path_env: str, line: str) -> None:
    path = os.environ.get(path_env, "")
    if not path:
        return
    try:
        with _builtin_open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:  # 记录失败不影响这一步的判定口径（载入 / 调用本身已经发生）
        pass


_append(MARKER_ENV, "fake-webbrowser-loaded")

#: 假注册表（标准库同名接口的空替身：只记名字，不起任何东西）
_registry: dict = {}


def register(name, klass, instance=None, *, preferred=False):  # noqa: A002 - 照标准库签名
    """标准库 `webbrowser.register()` 的空替身：只记一笔，不构造、不启动。"""
    _registry[str(name)] = instance if instance is not None else klass
    return None


def get(using=None):
    """标准库 `webbrowser.get()` 的空替身：永远报「没有可用浏览器」。"""
    return None


def register_standard_browsers():
    """标准库同名函数的空替身：**什么也不注册**（因此没有 `windows-default` 这条退路）。"""
    return None


def _record(url) -> None:
    _append(CALLS_ENV, "{}".format(url))


def open(url, new=0, autoraise=True):  # noqa: A001 - 照标准库签名
    """标准库 `webbrowser.open()` 的假替身：**只记录，不开任何东西**。"""
    _record(url)
    outcome = os.environ.get(OUTCOME_ENV, "false").strip().lower()
    if outcome == "raise":
        raise RuntimeError("假 webbrowser：按测试要求模拟没有可用的浏览器")
    if outcome == "true":
        return True
    return False


def open_new(url):
    return open(url, 1)


def open_new_tab(url):
    return open(url, 2)


class BaseBrowser:  # pragma: no cover - 只为补齐接口，测试不走这里
    def __init__(self, name=""):
        self.name = name

    def open(self, url, new=0, autoraise=True):
        return open(url, new, autoraise)


class GenericBrowser(BaseBrowser):  # pragma: no cover - 同上
    pass


class BackgroundBrowser(GenericBrowser):  # pragma: no cover - 同上
    pass


class WindowsDefault(BaseBrowser):  # pragma: no cover - 同上
    pass
