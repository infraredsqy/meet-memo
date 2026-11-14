"""跨文件共用的假件与工厂辅助（**`pytest` 不收集本文件**）。

定位与分工（写死，不留自由发挥的口子）：

  - 本模块是 `tests/` 下**跨测试文件共用的假件与工厂辅助的唯一落点**；`tests/test_drain.py`
    与 `tests/test_shutdown_signal.py` 都从这里
    取假件，**只 import、不各自定义、不各写一份**，也不把假件塞进 `tests/conftest.py`
    （`conftest.py` 只放 fixture）。
  - **五个假件**（类名与使用者一一对应）：`FakeCaptureBackend`（可控拖长、会抛错的
    `finalize()`）、`FakeTranscriberBackend`（报 `backlog_depth()` /
    `segment_timeout()`）、`FakePostHook`（`launch()` 正常返回 / 可控拖长 / 「立刻返回、
    后半段交给工作线程」）、`FailingPostHook`（`launch()` 抛错）、`BlockingThreadPostHook`
    （`launch()` 把阻塞放进工作线程、自身立刻返回）。**`FakePostHook` 与 `FailingPostHook`
    是两个独立的假件类**，类名与使用者一一对应、互不顶替。
  - **旋钮一律走环境变量**（构造参数进不了子进程：`registry` 对后端**无参实例化**）。
    `BLOCK_SECONDS_ENV` 之外还有六个：`FINALIZE_DELAY_SECONDS_ENV` / `FINALIZE_ERROR_ENV` /
    `BACKLOG_DEPTH_ENV` /
    `SEGMENT_TIMEOUT_SECONDS_ENV` / `LAUNCH_DELAY_SECONDS_ENV` / `LAUNCH_THREAD_SECONDS_ENV`。
    **有了这几个旋钮，用例侧不再需要「构造参数固定」的本地假件子类**：各档全部由
    本文件的这五个共享假件配出来，用例文件**只 import、不自行定义**。
  - **注入通道（唯一一种）**：假件写进临时目录、经 `PYTHONPATH`（= 仓库根 + 该临时目录）
    加既有配置键 `capture.backend` / `post.hook` / `transcriber.backend` 配进**真服务子进程**；
    子进程按 `module:Class` 解析。临时目录里那份模块**由本文件里的同一批类体生成**
    （`_FAKE_CLASS_SOURCE` 就是下面五个类的源码，一处维护、两处使用）—— 测试进程里 new 出来的
    那一份与子进程里解析出来的那一份因此是同源代码，不会两边各长一套。
  - **写出去的那份模块只 import 标准库与 `meeting`**（两类在子进程里都可用）：工厂函数里对
    `tests._helpers` 的 import 是**函数内 import**，那条路径只在 pytest 进程里走。

**看门断言（缺了就直接判红，不留「静默没挂上」的口子）**：子进程载入假件模块时会往
`FakePlugin.annotation_file` 写一行 `fake-modules-loaded`；用例用 `plugin.module_loaded()`
断言它 —— 假件没挂上时 registry 会静默回退替身，收尾各步于是全 `skipped`，用例会以
「以为在验 A、其实在验 B」的形式蒙混过关。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 假件自己那条 logger（写出去的那份模块里另有一份同名同形的 `_log`：那份模块只 import
#: 标准库，不能回头 import 本模块）。两个名字的作用相同 —— 假件的行为落 log，
#: 好让「假件到底跑没跑」在服务级 / 场次 log 里查得到。
_log = logging.getLogger("meeting.test.drain_fakes")

#: 假件模块（子进程经 `PYTHONPATH` + 配置键 `module:Class` 导入的那一个模块名）
FAKE_MODULE_NAME = "meeting_drain_fakes"

#: 假件模块被载入时写下的标记文件名（放在临时假件目录里）
ANNOTATION_FILE_NAME = "fake-modules-loaded.txt"

#: 假件模块**被载入**时写的那一行（测试据此证明子进程里命中的是这份假件、不是回退替身）
LOAD_MARKER = "fake-modules-loaded"

#: 承载「标记文件路径」的环境变量（**不是**本服务的环境变量，与假 webbrowser 那一类同形）
ANNOTATION_ENV = "MEETING_TEST_DRAIN_FAKES_ANNOTATION"

#: **旋钮常量与缺省值定义在下面 `_CLASS_SOURCE_BEGIN` 之后的区段里**（不在这一层）。
#: 那一段源码同时被**写出去**给子进程用（`_class_source()` 切片 + `_FAKE_MODULE_HEADER`），
#: 于是「常量名 / 取值 / 读值助手 / 五个假件」**只有一份维护点**：本进程里 `from tests._drain_fakes
#: import FakeCaptureBackend` 拿到的那一份，与子进程里 `registry` 解析出来的那一份，逐字同源。
#: 旋钮清单与它们的用处见下面那一段的注释（`BLOCK_SECONDS_ENV` / `FINALIZE_*` /
#: `BACKLOG_DEPTH_ENV` / `SEGMENT_TIMEOUT_SECONDS_ENV` / `LAUNCH_*` 共七个）。
#: 三个配置键（既有的键，本项目不新增配置键）
CONFIG_KEY_CAPTURE = "capture.backend"
CONFIG_KEY_TRANSCRIBER = "transcriber.backend"
CONFIG_KEY_POST = "post.hook"

FAKE_CLASS_NAMES: tuple[str, ...] = (
    "FakeCaptureBackend",
    "FakeTranscriberBackend",
    "FakePostHook",
    "FailingPostHook",
    "BlockingThreadPostHook",
)

#: 类体源码区段的起止标记（生成给子进程的那份模块时按它们切片）
_CLASS_SOURCE_BEGIN = "# ==== FAKE CLASS BODIES BEGIN ===="
_CLASS_SOURCE_END = "# ==== FAKE CLASS BODIES END ===="

_DOUBLE = chr(34)
_TRIPLE_QUOTE = _DOUBLE * 3

#: 生成给子进程的那份模块：文件头（只 import 标准库）
_FAKE_MODULE_HEADER = "\n".join(
    [
        _TRIPLE_QUOTE + "收尾 / 关停用例的假件（由 tests/_drain_fakes.py 的工厂生成）。",
        "",
        "内容 = tests/_drain_fakes.py 里那五个假件的类体 + 本文件头；生成之后经 PYTHONPATH +",
        "配置键 module:Class 挂进真服务子进程。测试进程里 new 出来的类与子进程里解析出来的类",
        "同源（类体逐字相同）。",
        "",
        "本模块的 import 只有标准库（logging / os / threading / time）—— 子进程里一定可用。",
        _TRIPLE_QUOTE,
        "",
        "from __future__ import annotations",
        "",
        "import logging",
        "import os",
        "import sys",
        "import threading",
        "import time",
        "",
        '_log = logging.getLogger("meeting.test.drain_fakes")',
        "",
        "#: 本模块被载入时写下的标记（测试据此证明子进程里命中的确实是这份假件）",
        'LOAD_MARKER = "fake-modules-loaded"',
        "",
        "#: 承载标记文件路径的环境变量（不是本服务的环境变量）",
        'ANNOTATION_ENV = "MEETING_TEST_DRAIN_FAKES_ANNOTATION"',
        "",
        "#: 上面那批旋钮常量、缺省值与**类体**都在下面 `_class_source()` 切出来的那一段里",
        "#: （本文件按同一段源码切片，一处维护、两处使用）。",
        "",
        "",
        "def note_module_loaded() -> str:",
        "    " + _TRIPLE_QUOTE + "本模块被载入时往标记文件追加一行；路径没给就什么也不写。" + _TRIPLE_QUOTE,
        '    path = os.environ.get(ANNOTATION_ENV, "")',
        "    if not path:",
        '        return ""',
        "    try:",
        '        with open(path, "a", encoding="utf-8") as handle:',
        "            handle.write(LOAD_MARKER + chr(10))",
        "    except OSError:  # 记录失败不影响假件本身",
        "        pass",
        "    return path",
        "",
        "",
        "note_module_loaded()",
        "",
    ]
)

#: 生成给子进程的那份模块：文件尾
_FAKE_MODULE_FOOTER = "\n".join(
    [
        "",
        "__all__ = [",
        '    "ANNOTATION_ENV",',
        '    "BACKLOG_DEPTH_ENV",',
        '    "BLOCK_SECONDS",',
        '    "BLOCK_SECONDS_ENV",',
        '    "BlockingThreadPostHook",',
        '    "DEFAULT_BACKLOG_DEPTH",',
        '    "DEFAULT_BLOCK_SECONDS",',
        '    "DEFAULT_SEGMENT_TIMEOUT_SECONDS",',
        '    "FINALIZE_DELAY_SECONDS_ENV",',
        '    "FINALIZE_ERROR_ENV",',
        '    "FakeCaptureBackend",',
        '    "FakePostHook",',
        '    "FakeTranscriberBackend",',
        '    "FailingPostHook",',
        '    "LAUNCH_DELAY_SECONDS_ENV",',
        '    "LAUNCH_THREAD_SECONDS_ENV",',
        '    "LOAD_MARKER",',
        '    "SEGMENT_TIMEOUT_SECONDS_ENV",',
        '    "note_module_loaded",',
        "]",
        "",
    ]
)


def _class_source() -> str:
    """从本文件的源码里切出五个假件的类体（唯一的维护点就在下面那一对标记之间）。

    按**行**找标记：起止标记的字面量本身也出现在本函数的源码里，按子串去切会切错位置。
    """
    lines = Path(__file__).read_text(encoding="utf-8").splitlines()
    begin = lines.index(_CLASS_SOURCE_BEGIN) + 1
    end = lines.index(_CLASS_SOURCE_END)
    return "\n".join(lines[begin:end]).strip("\n") + "\n"


#: 写出去的那份模块的完整源码
_FAKE_MODULE_SOURCE = _FAKE_MODULE_HEADER + _class_source() + _FAKE_MODULE_FOOTER


# ==== FAKE CLASS BODIES BEGIN ====
# 旋钮常量、缺省值与读值助手（**本区段整体写给子进程那一份模块**，一处维护、两处使用）
#
# 为什么旋钮走环境变量而不是构造参数：`registry` 对后端**无参实例化**（`cls()`），
# 假件的构造参数因此没法由配置键传进去。
#
# **读值一律发生在调用时**（不在 import 期固化一个死值）：值是「这一次调用听谁的」那一件事实。
# 本区段被 `_class_source()` 切出去时，`_FAKE_MODULE_HEADER` 里已经 import 了 `os` / `time` /
# `threading` 三个标准库模块，因此这七个常量与三个助手在子进程那份模块里同样可用。

#: 承载「`BlockingThreadPostHook` 阻塞多少秒」的环境变量（**不是本服务的环境变量**）：
#: 用例侧的阻塞秒数经它真的交给子进程里的假件。
BLOCK_SECONDS_ENV = "MEETING_TEST_DRAIN_BLOCK_SECONDS"

#: **其余旋钮同走环境变量**（与 `BLOCK_SECONDS_ENV` 同一手法、同一理由），各档一律用
#: 下面这五个共享假件经环境变量配出来：
#:   - `FINALIZE_DELAY_SECONDS_ENV`：`FakeCaptureBackend.finalize()` 睡多久（可控拖长）；
#:   - `FINALIZE_ERROR_ENV`：非空即让 `finalize()` 抛 `OSError`（造「有实现却抛错」）；
#:   - `BACKLOG_DEPTH_ENV` / `SEGMENT_TIMEOUT_SECONDS_ENV`：`FakeTranscriberBackend` 报的
#:     预算输入（把 `budget` 顶到公式那一档）；
#:   - `LAUNCH_DELAY_SECONDS_ENV`：`FakePostHook.launch()` 睡多久再返回；
#:   - `LAUNCH_THREAD_SECONDS_ENV`：`FakePostHook` 把这个秒数丢进工作线程（`0` = 那条线程
#:     永不返回，即「启动即算完成、不等它跑完」那一档）；
#:   - `CLOCK_OFFSET_SECONDS_ENV`：配合 `FINALIZE_MARKER_ENV`，把这只时钟往前拨多少秒
#:     （「越过预算 → 该步 `failed`」那一档用）。
CLOCK_OFFSET_SECONDS_ENV = "MEETING_TEST_DRAIN_CLOCK_OFFSET_SECONDS"
FINALIZE_DELAY_SECONDS_ENV = "MEETING_TEST_DRAIN_FINALIZE_DELAY_SECONDS"
FINALIZE_ERROR_ENV = "MEETING_TEST_DRAIN_FINALIZE_ERROR"
BACKLOG_DEPTH_ENV = "MEETING_TEST_DRAIN_BACKLOG_DEPTH"
SEGMENT_TIMEOUT_SECONDS_ENV = "MEETING_TEST_DRAIN_SEGMENT_TIMEOUT_SECONDS"
LAUNCH_DELAY_SECONDS_ENV = "MEETING_TEST_DRAIN_LAUNCH_DELAY_SECONDS"
LAUNCH_THREAD_SECONDS_ENV = "MEETING_TEST_DRAIN_LAUNCH_THREAD_SECONDS"

#: **让某一步真的越过这一趟收尾预算**的旋钮：给一个文件路径时，
#: 那一步把**时钟偏移秒数**写进那个文件。
#: 为什么需要它：这一趟收尾的总上限写死是
#: `budget = 30 + backlog × (segment_timeout + 5)`，**下限 30 秒且不开配置键** —— 「某一步
#: 越过预算 → 该步 `failed`」这条行为在真服务里不睡满 30 秒就造不出来，而用例的红线是 ≤ 5 秒。
#: 这个旋钮不碰收尾编排、也不改预算：它只把**进程自己读的那只单调钟**往前拨，
#: 于是 deadline 真的过期、越界真的由
#: `_drain_wait()` 与事件循环的计时判出来（那一步 `error` 里的两个数仍由被测代码给出）。
#: **它由哪一步写**：第 1 步 `flush`（`FakeCaptureBackend.finalize()` 返回前）——
#: 会后钩子那一步已不再被调用（「结束录音」之后会后一个动作都不自动跑），标记改由
#: 收尾里**第一个真实调用**写下，排在它后面的
#: `transcode` / `log` / `post` 三格面对的剩余预算随即过期，越界照样落在真实的调用上。
FINALIZE_MARKER_ENV = "MEETING_TEST_DRAIN_FINALIZE_MARKER"

#: 没给 `BLOCK_SECONDS_ENV` 时 `BlockingThreadPostHook` 的阻塞秒数
DEFAULT_BLOCK_SECONDS = 3.0

#: 没给对应环境变量时 `FakeTranscriberBackend` 报的预算输入（与转写后端缺席时的缺省同档：
#: `backlog = 0` / `segment_timeout = 20.0s`）
DEFAULT_BACKLOG_DEPTH = 0
DEFAULT_SEGMENT_TIMEOUT_SECONDS = 20.0


def _seconds_from_env(name, default):
    """读一个「秒数」旋钮；没给或读不出数字就用 `default`。"""
    raw = os.environ.get(name, "")
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError:  # 给了个不是数字的值：按缺省处理，不炸在假件上
        return float(default)


def _optional_seconds_from_env(name, default):
    """读一个「可选秒数」旋钮；没给或读不出数字就用 `default`（可以是 `None`）。"""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _text_from_env(name):
    """读一个「文本」旋钮；没给就是空串。"""
    return os.environ.get(name, "")


def _write_clock_offset_marker() -> None:
    """`FINALIZE_MARKER_ENV` 给了路径就把**时钟偏移**写进去（否则什么也不做）。

    偏移秒数由 `CLOCK_OFFSET_SECONDS_ENV` 给（缺省 0 = 不拨钟）；这一手是「某一步越过预算」
    那一档的同步点（说明见 `FINALIZE_MARKER_ENV` 与 `_CLOCK_SHIM_SOURCE`）。
    写文件失败只记一行 log、不影响这一步本身。
    """
    path = _text_from_env(FINALIZE_MARKER_ENV)
    if not path:
        return
    offset = _seconds_from_env(CLOCK_OFFSET_SECONDS_ENV, 0.0)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(str(float(offset)))
    except OSError as exc:  # 写不进去不影响这一步
        _log.warning("写时钟偏移标记失败（%s）：%s", path, exc)
        return
    _log.info("已写下时钟偏移标记：%s -> %ss（此后这只单调钟整体前移）", path, offset)


def _block_seconds_from_env() -> float:
    """读 `BLOCK_SECONDS_ENV`；没给或读不出数字就用 `DEFAULT_BLOCK_SECONDS`。"""
    return _seconds_from_env(BLOCK_SECONDS_ENV, DEFAULT_BLOCK_SECONDS)


#: 模块级常量（`BlockingThreadPostHook` **每次调用现读环境变量**，不只认它；保留它是为了
#: 与设计早期写下的「`BLOCK_SECONDS`」这个名字对得上）
BLOCK_SECONDS = _block_seconds_from_env()


class FakeCaptureBackend:
    """采集假件：`probe()` 如实报「没有探测结果」，`finalize()` 可控拖长或抛错。

    它在收尾用例里顶的是**真采集后端**那一档（flush 要真的调它的 `finalize()`），
    因此声明能力位 `writes_real_audio = True`（`registry.capabilities_of()` 读它）。

    构造参数全部有缺省（registry 无参实例化后端，**构造函数不许有必填参数**）：

      - `finalize_delay_seconds`：`finalize()` 睡多久再返回（`0` = 立刻返回）；
      - `finalize_error`：非空就让 `finalize()` 抛 `OSError`（造「有实现却抛错」）。

    **两个旋钮都可以由环境变量顶掉**（构造参数进不了子进程 —— `registry` 无参实例化）：
    `FINALIZE_DELAY_SECONDS_ENV` / `FINALIZE_ERROR_ENV`。**读值都发生在调用时**（不在
    `__init__` 里固化），于是「这一次 finalize() 睡多久 / 抛不抛错」是当次环境的函数，
    用例改完环境变量下一次调用就生效。`tests/test_drain.py` ③（抛错档）与 ⑤ / ⑤b2（拖长档）
    就是经这两个旋钮配出来的，本文件因此不必为它们各留一个「构造参数固定」的本地子类。

    `frame_interval = 0.1`（与 dev 替身同档）；`probe()` 返回空列表 —— 运行时会把它按
    「没有探测结果」处理，两路如实落 `unprobed`。
    """

    frame_interval = 0.1

    #: 能力声明（`registry.capabilities_of()` 读它）：收尾用例里它顶的是真采集后端
    writes_real_audio = True

    def __init__(
        self,
        *,
        finalize_delay_seconds: float = 0.0,
        finalize_error: str = "",
    ) -> None:
        self._finalize_delay_seconds = float(finalize_delay_seconds)
        self._finalize_error = str(finalize_error)
        self.bound = False
        self.started = False
        self.stopped = False
        self.finalize_calls = 0
        self.session_dir = None
        self._emit = None

    # -- 两个旋钮（环境变量优先，构造参数兜底；**每次调用现读**）-------------

    @property
    def finalize_delay_seconds(self) -> float:
        return _seconds_from_env(FINALIZE_DELAY_SECONDS_ENV, self._finalize_delay_seconds)

    @property
    def finalize_error(self) -> str:
        return _text_from_env(FINALIZE_ERROR_ENV) or self._finalize_error

    # -- 采集协议 ---------------------------------------------------------

    def bind(self, emit, *, config=None) -> None:
        self._emit = emit
        self.bound = True

    def probe(self):
        return []

    def start(self, mode: str, session_dir: str) -> None:
        self.started = True
        self.session_dir = str(session_dir)
        _log.info("假采集后端 start()：仅登记（mode=%s）", mode)

    def stop(self) -> None:
        self.stopped = True

    def finalize(self) -> None:
        self.finalize_calls += 1
        _log.info(
            "假采集后端 finalize()：延迟 %ss%s",
            self.finalize_delay_seconds,
            "（并要求抛错）" if self.finalize_error else "",
        )
        if self.finalize_delay_seconds > 0:
            time.sleep(self.finalize_delay_seconds)
        # 配了「时钟偏移标记」就把偏移写下来（`tests/test_drain.py` ⑤f 用它让**后面那几步**
        # 真的越过这一趟收尾预算：标记一落，这只单调钟就前移，`_drain_wait()` 的等待随即过期）
        _write_clock_offset_marker()
        if self.finalize_error:
            raise OSError(self.finalize_error)


class FakeTranscriberBackend:
    """转写假件：只回答收尾预算的两个输入（`backlog_depth()` / `segment_timeout()`）。

    两个值都可以由环境变量顶掉（`BACKLOG_DEPTH_ENV` / `SEGMENT_TIMEOUT_SECONDS_ENV`，
    与上面那几个旋钮同一手法）：`tests/test_drain.py` 的 ⑤b2 需要一组**明显不小**的积压量
    把 `budget` 顶离下限那一格，用这两个旋钮配出来即可，不必再自建一个「积压量写死在类里」
    的本地假件。
    """

    def __init__(self, *, backlog_depth: int = 0, segment_timeout: float = 20.0) -> None:
        self._backlog = int(backlog_depth)
        self._segment_timeout = float(segment_timeout)
        self.bound = False
        self.started = False
        self.released = 0

    def bind(self, line_sink, *, config=None) -> None:
        self.bound = True

    def start(self, mode: str, session_dir: str) -> None:
        self.started = True

    def stop(self) -> None:
        return None

    def health(self) -> str:
        return "normal"

    def backlog_depth(self) -> int:
        return int(_seconds_from_env(BACKLOG_DEPTH_ENV, self._backlog))

    def segment_timeout(self) -> float:
        return _seconds_from_env(SEGMENT_TIMEOUT_SECONDS_ENV, self._segment_timeout)

    def release_gpu(self) -> bool:
        """收尾的 `post` 那一步现在只做这件事（会后归用户手上那两个按钮）。

        它照真后端那条防御式取法被用到（`getattr(self.transcriber_backend, "release_gpu", None)`
        + 经 `_drain_wait()` 提交到工作线程），收尾编排里该跑的四步因此一步不少。
        """
        self.released += 1
        return True


class FakePostHook:
    """会后钩子假件：`launch()` 正常返回（可选拖长、可选把后半段交给工作线程）。

      - `launch_delay_seconds`：`launch()` 里先睡这么久再返回（**可控拖长**那一步用它）；
      - `thread_seconds`：非空就把「睡很久」的那半段丢进工作线程、`launch()` 立刻返回
        （`0` 表示那条线程永不返回 —— 用来证明「启动即算完成、不等它跑完」）。

    **两个旋钮都可以由环境变量顶掉**（构造参数进不了子进程）：
    `LAUNCH_DELAY_SECONDS_ENV`（拖长多少秒）与 `LAUNCH_THREAD_SECONDS_ENV`（把多少秒丢进
    工作线程；`0` = 那条线程永不返回）。两个值都**在 `launch()` 里现读**。

    `launch()` 里的 `time.sleep` 由运行时的**收尾专用 daemon 执行器**推进工作线程，
    因此这里同步睡多久都不会把事件循环钉住。
    """

    def __init__(
        self,
        *,
        launch_delay_seconds: float = 0.0,
        thread_seconds: float | None = None,
    ) -> None:
        self._launch_delay_seconds = float(launch_delay_seconds)
        self._thread_seconds = thread_seconds
        self.launched = 0
        self.session_dirs: list = []

    @property
    def launch_delay_seconds(self) -> float:
        return _seconds_from_env(LAUNCH_DELAY_SECONDS_ENV, self._launch_delay_seconds)

    @property
    def thread_seconds(self) -> float | None:
        return _optional_seconds_from_env(LAUNCH_THREAD_SECONDS_ENV, self._thread_seconds)

    def launch(self, session_dir: str) -> None:
        self.launched += 1
        self.session_dirs.append(session_dir)
        _log.info(
            "假会后钩子 launch()：延迟 %ss，工作线程 %s",
            self.launch_delay_seconds,
            "不启用" if self.thread_seconds is None else self.thread_seconds,
        )
        # 与 `FakeCaptureBackend.finalize()` 同一手：配了「时钟偏移标记」就把偏移写下来
        # （`tests/test_drain.py` ⑤f 用它让**这一步**真的越过这一趟收尾预算：标记一落，
        # 这只单调钟就前移，`_drain_wait()` 的等待随即过期）
        _write_clock_offset_marker()
        if self.thread_seconds is not None:
            worker = threading.Thread(
                target=time.sleep, args=(float(self.thread_seconds),), daemon=True
            )
            worker.start()
        if self.launch_delay_seconds > 0:
            time.sleep(self.launch_delay_seconds)


class FailingPostHook:
    """会后钩子假件：`launch()` **抛错**（造「有实现却抛错」的收尾失败现场）。"""

    def __init__(self, *, message: str = "假会后钩子按要求抛错") -> None:
        self.message = str(message)
        self.launched = 0

    def launch(self, session_dir: str) -> None:
        self.launched += 1
        _log.error("假会后钩子 launch() 抛错：%s", self.message)
        raise RuntimeError(self.message)


class BlockingThreadPostHook:
    """会后钩子假件：`launch()` 里睡 `block_seconds` 再返回（把某一步钉在 `running` 上）。

    运行时的收尾各步经**收尾专用的 daemon 执行器**把 `launch()` 整体放进工作线程再等，
    因此这一段 `time.sleep` 睡的是工作线程、事件循环空着 —— `/api/state` 在整段阻塞里照常
    应答，「轮询到某步处于运行中」这个同步点因此一定等得到（同步阻塞若真的落在事件循环上，
    那个窗口根本不会出现）。

    **阻塞秒数从 `BLOCK_SECONDS_ENV` 读**（构造参数由 `registry` 的无参实例化挡住）：
    用例把阻塞秒数经它交给子进程，没给该变量时取 3.0。
    """

    def __init__(self, *, block_seconds: float | None = None) -> None:
        self._block_seconds = block_seconds
        self.launched = 0
        self.calls: list = []

    @property
    def block_seconds(self) -> float:
        if self._block_seconds is not None:  # 用例显式给了构造参数：它说了算
            return float(self._block_seconds)
        return _seconds_from_env(BLOCK_SECONDS_ENV, DEFAULT_BLOCK_SECONDS)

    def launch(self, session_dir: str) -> None:
        self.launched += 1
        self.calls.append(session_dir)
        _log.info("假会后钩子（阻塞档）开始阻塞 %ss", self.block_seconds)
        time.sleep(self.block_seconds)


# ==== FAKE CLASS BODIES END ====


def _load_generated_module() -> Any:
    """把生成的那份模块在本进程里 exec 一遍，拿到与子进程同源的五个类。

    **不注册进 `sys.modules`**：那份模块的名字（`meeting_drain_fakes`）要在**子进程**里
    由 `import` 真去找临时目录里写下的那一份；测试进程若把它缓存进 `sys.modules`，
    同一次 pytest 会话里后起的子进程就可能看不到真文件。
    """
    module = type(sys)(FAKE_MODULE_NAME)
    module.__dict__["__file__"] = f"<generated {FAKE_MODULE_NAME}>"
    exec(compile(_FAKE_MODULE_SOURCE, module.__dict__["__file__"], "exec"), module.__dict__)
    return module


class _LazyGeneratedModule:
    """惰性加载生成模块的代理。

    **必须惰性**：写出去的那份模块在 `import` 时要 `from tests import _drain_fakes`，
    若本文件在 import 期就把生成的那份 exec 一遍（它自己又会去 import `tests._drain_fakes`），
    就会撞上「部分初始化的模块」。属性访问时才 exec 一次，循环 import 因此不会出现。
    """

    def __init__(self) -> None:
        self._module: Any = None

    def _load(self) -> Any:
        if self._module is None:
            self._module = _load_generated_module()
        return self._module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)


_GENERATED = _LazyGeneratedModule()


#: 生成模块里可以被 `from tests._drain_fakes import <name>` 取到的**模块级常量**们
#: （五个假件类名之外的那一批；`_GENERATED` 里没有的私有辅助名不在此列）。
FAKE_CONSTANT_NAMES: tuple[str, ...] = (
    "ANNOTATION_ENV",
    "BACKLOG_DEPTH_ENV",
    "BLOCK_SECONDS",
    "BLOCK_SECONDS_ENV",
    "DEFAULT_BACKLOG_DEPTH",
    "DEFAULT_BLOCK_SECONDS",
    "DEFAULT_SEGMENT_TIMEOUT_SECONDS",
    "FINALIZE_DELAY_SECONDS_ENV",
    "FINALIZE_ERROR_ENV",
    "LAUNCH_DELAY_SECONDS_ENV",
    "LAUNCH_THREAD_SECONDS_ENV",
    "LOAD_MARKER",
    "SEGMENT_TIMEOUT_SECONDS_ENV",
)


def __getattr__(name: str) -> Any:
    """按需暴露五个假件类与上面那批模块级常量（同名前缀的私有名不在此列）。"""
    if name in FAKE_CLASS_NAMES or name in FAKE_CONSTANT_NAMES:
        return getattr(_GENERATED, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# 工厂：把假件写进临时目录、拼出 PYTHONPATH 与配置键


@dataclass
class FakePlugin:
    """一次「假件写进临时目录」的现场：目录、标记文件、环境变量与配置键取值。"""

    directory: Path
    annotation_file: Path
    extra_env: dict[str, str] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    #: 时钟替身目录（只有「某一步越过预算」那一档才非空；见 `write_clock_offset_shim()`）。
    #: 非空时 `pythonpath()` 会把它也拼进 `PYTHONPATH`，子进程启动时由 `site` import。
    clock_dir: Path | None = None

    def module(self, class_name: str) -> str:
        """`module:Class` 形状的配置键取值。"""
        if class_name not in FAKE_CLASS_NAMES:
            raise KeyError(f"不是本模块登记的假件类名：{class_name!r}（{FAKE_CLASS_NAMES}）")
        return f"{FAKE_MODULE_NAME}:{class_name}"

    def pythonpath(self, repo_root: str | Path) -> str:
        """子进程的 `PYTHONPATH`：**仓库根 + 临时假件目录**（两个条目）。

        现场带了时钟替身目录时（`clock_dir` 非空）再追加第三个条目 —— 它只出现在
        「某一步越过预算」那一档的现场里，常规现场仍是两个条目。
        """
        entries = [str(repo_root), str(self.directory)]
        if self.clock_dir is not None:
            entries.append(str(self.clock_dir))
        return os.pathsep.join(entries)

    def loaded_markers(self) -> list[str]:
        try:
            text = self.annotation_file.read_text(encoding="utf-8")
        except OSError:
            return []
        return [line for line in text.splitlines() if line.strip()]

    def module_loaded(self) -> bool:
        """子进程里命中的确实是这份假件（而不是 registry 的回退替身）。"""
        return LOAD_MARKER in self.loaded_markers()


def write_fake_module(
    home: Path | str,
    *,
    config: dict[str, Any] | None = None,
    local_base: str | None = None,
    note: str = "",
) -> FakePlugin:
    """把假件模块写进 `<home>/drain-fakes/`，返回它的目录、标记与配置键取值。

    - `local_base`：非空时生成「薄模块」（`from <local_base> import *`），供**进程内用例**
      直接 import 使用（真服务子进程那一支一律用默认的完整源码）；
    - `note`：给这个现场留一行说明，落在同目录的 `README.txt` 里。

    **没有「追加自定义类」这一项**：旋钮都在上面那几个环境变量上，用例只 import
    这五个共享假件即可 —— 免得再从这条路上养出第二份假件源码。
    """
    home = Path(home)
    directory = home / "drain-fakes"
    directory.mkdir(parents=True, exist_ok=True)
    source = _FAKE_MODULE_SOURCE
    if local_base:
        source = _thin_module_source(local_base)
    (directory / f"{FAKE_MODULE_NAME}.py").write_text(source, encoding="utf-8")
    if note:
        # 薄模块里 `note_module_loaded()` 由被 star-import 的那一份负责
        (directory / "README.txt").write_text(note + "\n", encoding="utf-8")
    return FakePlugin(
        directory=directory,
        annotation_file=directory / ANNOTATION_FILE_NAME,
        extra_env={ANNOTATION_ENV: str(directory / ANNOTATION_FILE_NAME)},
        config=dict(config or {}),
    )


def _thin_module_source(local_base: str) -> str:
    """薄模块：把仓库里那一份假件整个搬过来，只补「按 `__file__` 算仓库根」这一手。"""
    return "\n".join(
        [
            _TRIPLE_QUOTE + "薄假件模块（由 tests/_drain_fakes.py 的工厂生成）。",
            "",
            "仓库里那一份 `tests/_drain_fakes.py` 的五个假件原样搬过来；本文件只多一件事：",
            "按 `__file__` 把仓库根加进 `sys.path`，好让下面那句 import 在临时目录里也成立。",
            _TRIPLE_QUOTE,
            "",
            "import os",
            "import sys",
            "",
            "sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))",
            "",
            "from " + local_base + " import *  # noqa: F401,F403",
            "from " + local_base + " import (  # noqa: F401",
            '    ANNOTATION_ENV,',
            '    LOAD_MARKER,',
            '    BlockingThreadPostHook,',
            '    FakeCaptureBackend,',
            '    FakePostHook,',
            '    FakeTranscriberBackend,',
            '    FailingPostHook,',
            '    note_module_loaded,',
            ")",
            "",
            "__all__ = [",
            '    "ANNOTATION_ENV",',
            '    "LOAD_MARKER",',
            '    "BlockingThreadPostHook",',
            '    "FakeCaptureBackend",',
            '    "FakePostHook",',
            '    "FakeTranscriberBackend",',
            '    "FailingPostHook",',
            '    "note_module_loaded",',
            "]",
            "",
        ]
    )


def write_inprocess_fake_module(home: Path | str, *, note: str = "") -> Path:
    """写一份**给进程内 import** 的薄假件模块，并把它的目录加进 `sys.path`。

    返回模块名（永远是 `FAKE_MODULE_NAME`）。用例据此在自己的进程里 import 出与子进程
    同源的假件类（`monkeypatch.setattr(runtime, "post_hook", module.FakePostHook())`）。
    """
    plugin = write_fake_module(home, local_base="tests._drain_fakes", note=note)
    directory = str(plugin.directory)
    if directory not in sys.path:
        sys.path.insert(0, directory)
    return plugin.directory


def fake_capture_plugin(home: Path | str, capture: str = "FakeCaptureBackend", **config: Any) -> FakePlugin:
    """只挂采集假件（`capture.backend`）—— 转写与会后钩子按 registry 的缺省解析。"""
    plugin = write_fake_module(home)
    plugin.config[CONFIG_KEY_CAPTURE] = plugin.module(capture)
    plugin.config.update(config)
    return plugin


def fake_capture_and_post_plugin(
    home: Path | str,
    *,
    post_class: str = "FakePostHook",
    capture_class: str = "FakeCaptureBackend",
    **config: Any,
) -> FakePlugin:
    """同时挂采集假件与会后钩子假件（`capture.backend` + `post.hook`）。"""
    plugin = write_fake_module(home)
    if capture_class:
        plugin.config[CONFIG_KEY_CAPTURE] = plugin.module(capture_class)
    plugin.config[CONFIG_KEY_POST] = plugin.module(post_class)
    plugin.config.update(config)
    return plugin


def fake_transcriber_plugin(
    home: Path | str,
    *,
    capture_class: str | None = "FakeCaptureBackend",
    **config: Any,
) -> FakePlugin:
    """挂转写假件（给收尾预算提供 `backlog` / `segment_timeout`），可选一并挂采集假件。"""
    plugin = write_fake_module(home)
    plugin.config[CONFIG_KEY_TRANSCRIBER] = plugin.module("FakeTranscriberBackend")
    if capture_class:
        plugin.config[CONFIG_KEY_CAPTURE] = plugin.module(capture_class)
    plugin.config.update(config)
    return plugin


def write_failing_post_hook(home: Path | str, *, capture: str = "dev") -> FakePlugin:
    """挂「`launch()` 抛错」的假会后钩子。

    采集侧由 `capture` 决定（缺省 `dev` 采集替身）：`POST start` 要能真开起来，
    收尾才能走到 `post` 那一步去撞那个抛错的钩子。`capture=""` 表示这一项不写进配置。
    """
    plugin = write_fake_module(home)
    plugin.config[CONFIG_KEY_POST] = plugin.module("FailingPostHook")
    if capture:
        plugin.config[CONFIG_KEY_CAPTURE] = capture
    return plugin


def write_knob_plugin(
    home: Path | str,
    *,
    capture_class: str | None = None,
    capture_error: str = "",
    capture_delay_seconds: float | None = None,
    post_class: str | None = None,
    post_delay_seconds: float | None = None,
    post_thread_seconds: float | None = None,
    backlog_depth: int | None = None,
    segment_timeout: float | None = None,
    transcriber_class: str | None = None,
    capture_key: str | None = "dev",
) -> FakePlugin:
    """**一份把「旋钮」配全的假件现场**：`tests/test_drain.py` 的 ③ / ⑤ / ⑤b2 / ⑥ 都由它配出来。

    每个非 `None` 的参数都落成两样东西里的一样 —— 配置键（选哪个假件）或环境变量（那一步的
    行为参数）。**环境变量名一律取自本模块的具名常量**，不在调用点抄裸字符串：

      - `capture_class` / `transcriber_class` / `post_class`：写进 `capture.backend` /
        `transcriber.backend` / `post.hook` 三个**既有配置键**的 `module:Class`；
      - `capture_delay_seconds` / `capture_error`：`FakeCaptureBackend` 的 `finalize()`
        行为（拖长 / 抛错）；
      - `post_delay_seconds`：`FakePostHook.launch()` 睡多久；`post_thread_seconds`：把多少秒
        丢进工作线程（`0` = 那条线程永不返回，即「启动即完成」那一档）；
      - `backlog_depth` / `segment_timeout`：`FakeTranscriberBackend` 报的预算输入；
      - `capture_key`：不挂采集假件时写进 `capture.backend` 的那一项（缺省 `"dev"` 采集替身：
        `start` 要能真开起来；`None` 表示这一项不写）。

    **采集假件在场时那一项以假件为准**（`capture_class` 优先于 `capture_key`）。
    """
    plugin = write_fake_module(home)
    if capture_class:
        plugin.config[CONFIG_KEY_CAPTURE] = plugin.module(capture_class)
    elif capture_key is not None:
        plugin.config[CONFIG_KEY_CAPTURE] = capture_key
    if transcriber_class:
        plugin.config[CONFIG_KEY_TRANSCRIBER] = plugin.module(transcriber_class)
    if post_class:
        plugin.config[CONFIG_KEY_POST] = plugin.module(post_class)
    if capture_delay_seconds is not None:
        plugin.extra_env[FINALIZE_DELAY_SECONDS_ENV] = str(float(capture_delay_seconds))
    if capture_error:
        plugin.extra_env[FINALIZE_ERROR_ENV] = capture_error
    if post_delay_seconds is not None:
        plugin.extra_env[LAUNCH_DELAY_SECONDS_ENV] = str(float(post_delay_seconds))
    if post_thread_seconds is not None:
        plugin.extra_env[LAUNCH_THREAD_SECONDS_ENV] = str(float(post_thread_seconds))
    if backlog_depth is not None:
        plugin.extra_env[BACKLOG_DEPTH_ENV] = str(int(backlog_depth))
    if segment_timeout is not None:
        plugin.extra_env[SEGMENT_TIMEOUT_SECONDS_ENV] = str(float(segment_timeout))
    return plugin


def write_failing_capture_plugin(home: Path | str, *, message: str = "假采集后端按要求抛错") -> FakePlugin:
    """挂「`finalize()` 抛错」的采集假件（`tests/test_drain.py` ③ 造「有实现却抛错」）。"""
    return write_knob_plugin(home, capture_class="FakeCaptureBackend", capture_error=message)


def write_slow_drain_plugin(
    home: Path | str,
    *,
    capture_delay_seconds: float,
    post_delay_seconds: float,
    backlog_depth: int | None = None,
    segment_timeout: float | None = None,
    transcriber_class: str | None = None,
) -> FakePlugin:
    """挂「`flush` 与 `post` 两步可控拖长」的假件现场（`tests/test_drain.py` ⑤ / ⑤b2）。

    拖长落在**采集后端的 `finalize()`** 与**会后钩子的 `launch()`** 上（四步里没有一步是转写
    后端的调用）；积压量与段长仍由转写假件报（`backlog_depth` /
    `segment_timeout` 非空时经环境变量交给 `FakeTranscriberBackend`）。
    """
    return write_knob_plugin(
        home,
        capture_class="FakeCaptureBackend",
        capture_delay_seconds=capture_delay_seconds,
        post_class="FakePostHook",
        post_delay_seconds=post_delay_seconds,
        transcriber_class=transcriber_class or "FakeTranscriberBackend",
        backlog_depth=backlog_depth,
        segment_timeout=segment_timeout,
    )


def write_immediately_returning_post_plugin(home: Path | str, *, capture: str = "dev") -> FakePlugin:
    """挂「`launch()` 立刻返回、后半段交给工作线程」的假件（`tests/test_drain.py` ⑥ 条）。

    `post_thread_seconds=0` 那一档：`launch()` 当场返回，把「后半段」丢进一条工作线程 ——
    于是「**启动即算完成、不等它跑完**」这件事与「`post` 步照样落 `done`」一一对应（收尾不等
    那条线程）。采集侧取 `capture`（缺省 dev 替身：`start` 要能真开起来）。
    """
    return write_knob_plugin(
        home,
        capture_key=capture,
        post_class="FakePostHook",
        post_thread_seconds=0.0,
    )


def write_blocking_post_hook(
    home: Path | str, *, capture: str = "dev", block_seconds: float | None = None
) -> FakePlugin:
    """挂「把阻塞放进工作线程」的假会后钩子。

    `block_seconds` 给定时，经 `BLOCK_SECONDS_ENV` 交给子进程里的假件（**这就是契约里那个
    `SHUTDOWN_BLOCKING_HOOK_SECONDS` 真正生效的地方**）；不给时假件取它自己的缺省 3.0。
    """
    plugin = write_fake_module(home)
    plugin.config[CONFIG_KEY_POST] = plugin.module("BlockingThreadPostHook")
    if capture:
        plugin.config[CONFIG_KEY_CAPTURE] = capture
    if block_seconds is not None:
        plugin.extra_env[BLOCK_SECONDS_ENV] = str(float(block_seconds))
    return plugin


def write_clock_offset_plugin(
    home: Path | str,
    *,
    finalize_delay_seconds: float,
    clock_offset_seconds: float,
    marker_path: Path | str,
    post_delay_seconds: float | None = None,
) -> FakePlugin:
    """造「某一步**真的越过这一趟收尾预算**」的现场（`tests/test_drain.py` ⑤f 用）。

    现场由四样东西拼成：

      - **采集假件**在场（`finalize` / `transcode` 两项能力如实为真）且 `finalize()` 可控拖长
        `finalize_delay_seconds` —— `flush` 因此落 `done`，这一趟的墙钟里也只有这一笔真实等待；
      - **会后钩子假件**（`FakePostHook`）在场：`launch()` 先写下时钟偏移标记，再睡
        `post_delay_seconds`（给定时）—— 它就是在预算之外跑不完的那一步；
      - 标记里那个偏移秒数（`clock_offset_seconds`）由假件写进 `marker_path`
        （`FINALIZE_MARKER_ENV` 那个旋钮）；
      - 子进程的 `PYTHONPATH` 里多一个**时钟替身目录**（`plugin.clock_dir`），替身由 `site`
        在解释器启动时 import，读到标记之后把 `meeting.service.runtime` 用的那只单调钟整体
        前移 —— 末尾那一步面对的剩余预算因此已经过期，其调用被 `_drain_wait()` 判成
        「超出收尾预算」并落 `failed`。

    **预算与判据一个字都没动**：上限仍由 `compute_drain_budget()` 按
    `DRAIN_DEFAULT_BACKLOG` / `DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS` 算（下限 30 秒），
    越界仍由 `_drain_wait()` 的 `asyncio.wait_for` 与它自己的秒表判出来。**为什么必须这么
    造**（这套旋钮的存在理由）见 `FINALIZE_MARKER_ENV` 与 `_CLOCK_SHIM_SOURCE` 的说明：
    30 秒的下限让「真等满」与「单条用例 ≤ 5 秒」无法同时成立。
    """
    plugin = write_knob_plugin(
        home,
        capture_class="FakeCaptureBackend",
        capture_delay_seconds=finalize_delay_seconds,
        capture_key=None,
        post_class="FakePostHook",
        post_delay_seconds=post_delay_seconds,
        transcriber_class="FakeTranscriberBackend",
    )
    plugin.clock_dir = write_clock_offset_shim(home)
    plugin.extra_env[FINALIZE_MARKER_ENV] = str(marker_path)
    plugin.extra_env[CLOCK_OFFSET_SECONDS_ENV] = str(float(clock_offset_seconds))
    return plugin


# 环境变量与配置文件的装配（与 `tests/_helpers.py` 同一套隔离手法）


def service_env_overrides(
    home: Path | str,
    plugin: FakePlugin | None = None,
    *,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """拼一份子进程环境变量覆盖：家目录两项 + 假件标记那一项（+ 调用方给的）。

    **不新增任何本服务的环境变量**：`MEETING_*` 五项与 `PYTHONPATH` 由设计的夹具能力
    （`service_process(*, pythonpath=..., extra_env=...)`）与调用方给全；这里只补家目录两项与
    `MEETING_TEST_DRAIN_FAKES_ANNOTATION`（测试设施自己的变量，与假 webbrowser 那一类同形）。
    """
    home = Path(home)
    overrides: dict[str, str] = {"HOME": str(home), "USERPROFILE": str(home)}
    if plugin is not None:
        overrides.update(plugin.extra_env)
    if extra:
        overrides.update(extra)
    return overrides


def write_plugin_config(home: Path | str, plugin: FakePlugin) -> Path:
    """把假件的配置键写进 `<家目录>/.config/meeting/config.json`（**点号扁平键**）。

    只有一层点号扁平键名，**嵌套形状不合法**。
    """
    from tests import _helpers as helpers

    return helpers.write_config_json(Path(home), plugin.config)


def json_dump(path: Path | str, data: dict[str, Any]) -> Path:
    """写一份合法 JSON（辅助）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


# 「某一步越过预算」那一档的控速件：把子进程读的那只单调钟按标记前移
#
# 为什么需要它：收尾预算的下限是 `DRAIN_BUDGET_MIN_SECONDS` = 30 秒、
# 封顶 300 秒，公式与三个数都**硬编码、不开配置键**。于是「某一步越过预算 →
# 该步 `failed`」这条行为在真服务里不睡满 30 秒就造不出来，而用例的红线是「单条 ≤ 5 秒」。
# 本文件因此给一条**只在用例显式开了旋钮时才生效**的控速件：
#
#   `FakeCaptureBackend.finalize()` 跑完之后（`FINALIZE_MARKER_ENV` 给了路径时）把偏移秒数
# 写进那个文件 → 子进程里被注入的 `clockoffset` 模块
# 把 `meeting.service.runtime` 与拆分进各 mixin 模块（`runtime_facts` / `runtime_sessions` /
#   `runtime_summary` / `runtime_drain`）的那几处 `time` 名字换成自己的替身 → 此后工作线程里的
#   `_remaining_budget()` 读到的单调钟整体前移 → 那一条 deadline 真的过期。
#
# **它不动被测逻辑**：预算仍由 `compute_drain_budget()` 算、越界仍由 `_drain_wait()` 的
# `asyncio.wait_for` + 它自己的秒表判出来，`error` 里的「budget=Ns，实测耗时 Ms」仍是真实读数；
# `asyncio` 内部的时间源（`loop.time()`）与 `time.sleep()` 都不受影响（它们不查 `time.monotonic`
# 这个属性）。没开旋钮时这一路完全不参与（`finalize()` 里那个分支直接返回）。

#: 时钟替身模块名（经 `PYTHONPATH` 注入子进程时 `import` 的就是它）
CLOCK_SHIM_MODULE_NAME = "clockoffset"

#: 时钟替身目录名（落在假件现场的家目录下的**兄弟目录**，于是「仓库根 + 假件目录 +
#: 时钟替身目录」三个条目互不重叠）
CLOCK_SHIM_DIR_NAME = "clock-shim"

_CLOCK_SHIM_SOURCE = '''\
"""用例注入的时钟替身：把 runtime 与各拆分模块读的那只单调钟按标记文件前移。

`ServiceRuntime` 的方法体按职责住在 `meeting.service.runtime_facts` /
`runtime_sessions` / `runtime_summary` / `runtime_drain`（聚合壳
`meeting.service.runtime` 自己也读）里，**每个模块都有自己的 `import time`
全局名**，因此要逐模块换；漏一个模块，拨钟在那个模块的路径上就不生效。

只在 `CLOCK_OFFSET_MARKER`（import 时从环境变量读一次）指向的文件出现之后才生效，
文件内容是「前移多少秒」。标记没给 / 还没出现 / 内容读不出数字时，一切照旧（偏移 0）。

由 `usercustomize.py` / `sitecustomize.py` 两个引导文件在解释器启动时 import，
因此它在收尾开始之前就装好了。
"""

import os
import time as _std_time

CLOCK_OFFSET_MARKER = os.environ.get("MEETING_TEST_DRAIN_FINALIZE_MARKER", "")

#: 本模块是否已经把替身装进 runtime 与各拆分模块了
INSTALLED = False


class _MonotonicProxy:
    """`time` 的真身 + 一只按标记文件前移 `monotonic()` 的替身（其余属性原样转发）。

    **每次调用现读标记**：偏移值由被测那一步自己写下（见 `_drain_fakes.py` 的
    `FINALIZE_MARKER_ENV`），于是「哪一刻开始拨钟、拨多少」都归用例说了算。
    """

    def __init__(self, inner):
        self._inner = inner

    def monotonic(self):
        offset = 0.0
        if CLOCK_OFFSET_MARKER:
            try:
                with open(CLOCK_OFFSET_MARKER, encoding="utf-8") as handle:
                    offset = float(handle.read().strip() or 0.0)
            except (OSError, ValueError):
                offset = 0.0
        return float(self._inner.monotonic()) + offset

    def __getattr__(self, name):
        return getattr(self._inner, name)


def install():
    """把 runtime 与各拆分模块读的 `time` 名字都换成上面那只替身（幂等）。

    **逐模块都要换**：runtime.py 是聚合壳，各 mixin 模块都有自己那份 `import time`
    全局名，谁读谁就得换（见模块 docstring）。
    而 `asyncio.wait_for` 到点靠的是**事件循环自己的计时器**（Windows 的 Proactor 事件循环
    走 `asyncio.base_events.time.monotonic`）。只换前者的话，上限虽然算出「已经过期」，
    实际等待却仍由那只没被拨动的钟推着走完 —— 越界于是不发生。
    """
    global INSTALLED
    import asyncio.base_events
    from meeting.service import (
        runtime,
        runtime_drain,
        runtime_facts,
        runtime_sessions,
        runtime_summary,
    )

    if INSTALLED:
        return False
    runtime.time = _MonotonicProxy(runtime.time)
    runtime_facts.time = _MonotonicProxy(runtime_facts.time)
    runtime_sessions.time = _MonotonicProxy(runtime_sessions.time)
    runtime_summary.time = _MonotonicProxy(runtime_summary.time)
    runtime_drain.time = _MonotonicProxy(runtime_drain.time)
    asyncio.base_events.time = _MonotonicProxy(asyncio.base_events.time)
    INSTALLED = True
    return True


install()
'''

#: 时钟替身的两个引导文件：`site` 在解释器启动时按顺序找它们，找到第一个就 import。
#: 两个都写（`usercustomize` 走 user site、`sitecustomize` 走全局），于是「哪一只入口在
#: 本机生效」不影响结果；import 失败只意味着这一档不生效，不该把子进程拖死。
_CLOCK_SHIM_BOOTSTRAP_NAMES: tuple[str, ...] = ("usercustomize.py", "sitecustomize.py")

_CLOCK_SHIM_BOOTSTRAP_SOURCE = "\n".join(
    [
        '"""时钟替身的引导文件（由 tests/_drain_fakes.py 的工厂生成）。',
        "",
        "site 在解释器启动时 import 本文件；本文件只把真正的替身模块 import 进来",
        '（`clockoffset` 在同一个目录里，经 PYTHONPATH 可见）。"""',
        "import " + CLOCK_SHIM_MODULE_NAME + "  # noqa: F401",
        "",
    ]
)


def write_clock_offset_shim(plugin_home: Path | str) -> Path:
    """写一份**时钟替身现场**并返回那个目录（调用方把它拼进子进程的 `PYTHONPATH`）。

    目录里三样东西：替身模块 `clockoffset.py` + 两个引导文件（`usercustomize.py` /
    `sitecustomize.py`）。作用与「为什么不这么做就造不出越界现场」写在文件顶部那一段
    （`_CLOCK_SHIM_SOURCE` 之前）。
    """
    home = Path(plugin_home)
    directory = home / CLOCK_SHIM_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{CLOCK_SHIM_MODULE_NAME}.py").write_text(_CLOCK_SHIM_SOURCE, encoding="utf-8")
    for name in _CLOCK_SHIM_BOOTSTRAP_NAMES:
        (directory / name).write_text(_CLOCK_SHIM_BOOTSTRAP_SOURCE, encoding="utf-8")
    return directory


__all__ = [
    "ANNOTATION_ENV",
    "ANNOTATION_FILE_NAME",
    "BACKLOG_DEPTH_ENV",
    "BLOCK_SECONDS_ENV",
    "CLOCK_OFFSET_SECONDS_ENV",
    "CLOCK_SHIM_DIR_NAME",
    "CLOCK_SHIM_MODULE_NAME",
    "CONFIG_KEY_CAPTURE",
    "CONFIG_KEY_POST",
    "CONFIG_KEY_TRANSCRIBER",
    "DEFAULT_BACKLOG_DEPTH",
    "DEFAULT_BLOCK_SECONDS",
    "DEFAULT_SEGMENT_TIMEOUT_SECONDS",
    "FAKE_CLASS_NAMES",
    "FAKE_CONSTANT_NAMES",
    "FAKE_MODULE_NAME",
    "FINALIZE_DELAY_SECONDS_ENV",
    "FINALIZE_ERROR_ENV",
    "FINALIZE_MARKER_ENV",
    "LAUNCH_DELAY_SECONDS_ENV",
    "LAUNCH_THREAD_SECONDS_ENV",
    "LOAD_MARKER",
    "SEGMENT_TIMEOUT_SECONDS_ENV",
    "BlockingThreadPostHook",
    "FakeCaptureBackend",
    "FakePlugin",
    "FakePostHook",
    "FakeTranscriberBackend",
    "FailingPostHook",
    "fake_capture_and_post_plugin",
    "fake_capture_plugin",
    "fake_transcriber_plugin",
    "json_dump",
    "service_env_overrides",
    "write_blocking_post_hook",
    "write_clock_offset_plugin",
    "write_clock_offset_shim",
    "write_failing_capture_plugin",
    "write_failing_post_hook",
    "write_fake_module",
    "write_immediately_returning_post_plugin",
    "write_inprocess_fake_module",
    "write_knob_plugin",
    "write_plugin_config",
    "write_slow_drain_plugin",
]
