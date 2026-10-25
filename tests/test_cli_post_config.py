"""`meeting post` 这条入口把**配置层**读进来并生效（`65dd3e4c` phase3 的补齐）。

要盯住的那一件事：`meeting post <场次目录>` 走的是**产品入口**，因此这一趟里每一个取值都
必须来自「用户写的那份 `config.json`」，而不是 `meeting/config.py` 的默认值表。
`web()` 一直在启动序列的第 ② 步读它（`config_module.load()` + 值级校验 +
`config_module.set_current()`），`post()` 此前整条路都没读 —— 于是
`post.engine` / `post.model` / `post.dtype` / `post.chunk_seconds` 与
`minutes.*`（含 `minutes.hook` / `minutes.timeout_seconds`）**一律退回默认值**：

  - 用户在 `config.json` 里写 `minutes.hook = null`（明确不要会后纪要）时，`meeting post`
    照旧按默认的 `auto` 解析并**拉起 headless DSH 子会话** —— 那是真花钱的一步，
    而且违背用户显式配置；
  - 用户写 `post.engine = <替身>` 想便宜地跑一遍时，`meeting post` 会造出**真**离线引擎。

**每条用例都走真入口 `post()`**（`_parse_post_args()` → `_load_config_or_exit()` →
`run_post()`）：配置层就位这一步是这条入口自己的事，绕过它去直调 `run_post()` 的用例证明
不了这条入口的配置层已经接上。

本文件把这条入口的配置层逐格钉住，五档各一组：

  1. `post.engine` 写成替身时，**真按它取引擎**（断言落在「替身被构造出来、真被调用来
     转写」上，而不是落在「默认值是什么」上）；
  2. `minutes.hook = null` 时，**拉起那一本记录为 0**（真拉起点
     `meeting.offline.runner._launch_minutes_implementation` 一次都没被走到），
     `minutes.hook = auto` 时同一本记录里**恰好 1 笔**（正控与负控一起读，这本记录才证明得了
     「配置真的关掉了这一层」），解析那一格如实落在**缺席档**（`selector == "null"`、
     `has_implementation is False`），这一趟落安静跳过那一档；
  3. `minutes.timeout_seconds` 调小时，`meeting post` **等纪要的上界真的跟着变**：等待层
     实际拿到的上界等于配置里那个值，且**等待真的按它收场**（等待层被换成按上界真睡
     一小会儿的替身：上界是配置里那个秒级值时它按那个值收场，是 60 / 14400 时当场判红）；
  4. 配置损坏 / 取值非法两档的**退出码与终端说法与 `web()` 同源**
     （`EXIT_STARTUP_UNMET` / `EXIT_REJECTED`，两处逐字同句），且都**不进花钱那一段**
     （`run_post()` 一次都没被调到）；
  5. 家目录下**没有 `config.json`** 时行为与改动前一致（生效值逐键是默认表那一份，
     `minutes.timeout_seconds` 仍是 14400）。

**全部进程内、全部注入假件**：一条真子进程都不起，一次模型端点都不碰，一份离线权重都不
加载。四处假件分别是：

  - **真引擎的替身**（`no_real_engine_can_ever_be_built`）：`meeting.offline.engine` 里那个
    `MossOfflineEngine` 被换成一碰就炸的类 —— 任何落到 `auto` 那一档的路径都在构造现场
    当场炸掉，**一毫秒都不花、一个字节都不下载**（本节点此前真拉过一次 HF 权重，属事故）；
  - **假引擎**：`post.engine` 指向本模块的 `RecordingOfflineEngine`（`module:Class` 形状，
    配置层放行）—— 构造与 `transcribe()` 只登记，交回两段合成结果；
  - **假转码**：把 `meeting.capture.archive.convert_to_mp3` 换成一个只写几个字节的替身
    （真 ffmpeg / 真 pydub 在不在场与本文件的结论无关）；
  - **假纪要拉起**：`tests/conftest.py` 的 `MEETING_MINUTES_CHILD=1` 兜底是第一层网，
    本文件另加两处登记：`registry.resolve_minutes_hook` 的**包装间谍**（真解析照旧走，
    只多记一笔）与**真拉起点** `meeting.offline.runner._launch_minutes_implementation`
    （`runner.py:1408` 定义、`:1371` 调）的登记替身。后者把真拉起点整个换掉，并按真启动器
    时序交终态（先 `running`、迟一小会儿落 `done` + 合规的 `meeting_final.md`），于是
    **真子会话一次都起不来**：`minutes.hook = auto` 那一趟解析出来的真实现
    （`meeting.backends.minutes.MinutesHook`）永远不会被调到。**解析次数与拉起次数是两个
    独立的读数**：第 2 档要的是「解析 1 次、拉起 0 笔」，`auto` 那一档要「解析 1 次、
    拉起恰好 1 笔」。

本文件是**快档**：不做真子进程、不做真墙钟等待（等纪要那一档的等待层被换成按上界真睡
一小会儿的替身，上界是秒级）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from meeting import cli as cli_module
from meeting import config as config_module
from meeting.backends import registry as registry_module
from meeting.cli import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_REJECTED,
    EXIT_STARTUP_UNMET,
    post,
)
from meeting.offline import minutes_tail as minutes_tail_module
from meeting.offline import runner as offline_runner
from meeting.offline.engine import OfflineSegment

#: 落终态的现成替身（本仓既有那一族：`SettlingLauncher` / `DelayedHookImplementation`）
#: ——本文件不新造一套，拉起那一处的登记替身交给它收工。
from tests.test_post_idempotency import DelayedHookImplementation

#: 假引擎在配置里的取值（`module:Class` 形状：配置层的 `BACKEND_SELECTORS` 对这一档放行）
FAKE_ENGINE_TARGET = "tests.test_cli_post_config:RecordingOfflineEngine"

#: 本文件合成的场次目录里那一路音频的秒数（够过数字静音门的下限即可）
SAMPLE_SECONDS = 3.0

SAMPLE_RATE = 16000


# ---------------------------------------------------------------------------
# 假件零：真引擎的替身（本文件的红线）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_real_engine_can_ever_be_built(monkeypatch: pytest.MonkeyPatch) -> Any:
    """**本文件的红线**：真离线引擎一次都不许被构造（绝不加载权重、绝不联网）。

    真引擎（`meeting.offline.engine:MossOfflineEngine`）的构造函数会去 HF 快照里加载权重，
    本机缺快照时还会**回退联网下载** —— 一条用例只要漏挡一处，就会真拉一次权重（本节点
    此前发生过一次，属事故）。因此这里把那个类本身换成「一碰就炸」的替身：
    `resolve_engine("auto")` 是**调用时**才 `importlib` 取那个属性的，于是任何落到 `auto`
    那一档的路径都在构造现场当场炸掉。

    这一层网同时是一条看门断言：配置层若没被读进来，`post.engine` 就是 `auto`，用例会以
    `AssertionError("真引擎不许被构造")` 收场，而不是悄悄跑一趟真模型。
    """
    from meeting.offline import engine as engine_module

    class _NeverBuiltEngine:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise AssertionError("真引擎不许被构造：本文件的用例必须在假引擎上跑完")

    monkeypatch.setattr(engine_module, "MossOfflineEngine", _NeverBuiltEngine)
    return _NeverBuiltEngine


# ---------------------------------------------------------------------------
# 假件一：假离线引擎（`post.engine` 指向它；构造与调用都只登记）
# ---------------------------------------------------------------------------


class RecordingOfflineEngine:
    """假离线引擎：**一个权重都不加载**，只记「被构造过」与「被调用过」。

    `build_engine()` 按 `post.engine` 解析到它之后，用
    `cls(model=<post.model>, dtype=<post.dtype>)` 构造它 —— 这两个值同样来自配置层，
    因此这里逐次记下，用例据此证明「取引擎这一步真的按生效配置走」。

    `transcribe()` 照 `meeting/offline/engine.py::OfflineSegment` 的形状交回两段合成结果：
    够 `run_session()` 落三份产物，且**一次真推理都没发生**。

    类级登记（`constructor_args` / `calls`）让用例不必持有实例就能读到记录。
    """

    constructor_args: list[dict[str, Any]] = []
    calls: list[str] = []

    def __init__(self, *, model: str = "", dtype: str = "", device: str = "cpu") -> None:
        self.model = model
        self.dtype = dtype
        # 真引擎构造时就把 `auto` 解析成 `cuda:0` / `cpu` 存在这一格上；产物读的是它
        self.device = device
        RecordingOfflineEngine.constructor_args.append(
            {"model": model, "dtype": dtype, "device": device}
        )

    def transcribe(self, audio_path: Any) -> list[OfflineSegment]:
        RecordingOfflineEngine.calls.append(str(audio_path))
        name = Path(str(audio_path)).name
        return [
            OfflineSegment(0.0, 1.0, "S01", f"{name} 的一句"),
            OfflineSegment(1.0, 2.5, "S02", f"{name} 的另一句"),
        ]


@pytest.fixture(autouse=True)
def a_clean_engine_ledger() -> Any:
    """每条用例都从一份空记录开始（假引擎的类级登记在用例之间不许串）。"""
    RecordingOfflineEngine.constructor_args = []
    RecordingOfflineEngine.calls = []
    yield RecordingOfflineEngine
    RecordingOfflineEngine.constructor_args = []
    RecordingOfflineEngine.calls = []


# ---------------------------------------------------------------------------
# 假件二：假转码（真 ffmpeg / 真 pydub 在不在场与本文件无关）
# ---------------------------------------------------------------------------


class RecordingConverter:
    """假转码器：把那份 MP3 写出来，**不碰真 ffmpeg**。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, pcm_path: Any, mp3_path: Any) -> bool:
        self.calls.append((str(pcm_path), str(mp3_path)))
        Path(mp3_path).write_bytes(b"fake-mp3")
        return True


@pytest.fixture(autouse=True)
def a_fake_converter(monkeypatch: pytest.MonkeyPatch) -> RecordingConverter:
    """把全仓唯一那份转码实现换成假件（归档那一层的判据不是本文件要盯的东西）。"""
    from meeting.capture import archive as archive_module

    converter = RecordingConverter()
    monkeypatch.setattr(archive_module, "convert_to_mp3", converter)
    return converter


# ---------------------------------------------------------------------------
# 假件三：纪要层的两本记录（解析次数 / 拉起次数）——拉起那一本挂在**真名字**上
# ---------------------------------------------------------------------------


class MinutesLedger:
    """纪要那一层的**两本独立记录**：解析了几次、真拉起几次、解析那一刻配置层是谁。

    `resolutions` 记 `registry.resolve_minutes_hook()` 每一次的交回值
    （`selector` / `has_implementation` / 那一刻生效配置的路径）；`launch_calls` 记真的走到
    「起子会话」那一步的每一笔 —— 挂点是**真名字**
    `meeting.offline.runner._launch_minutes_implementation`（`runner.py:1408` 定义、`:1371` 调）。

    此前这一本记录挂在 `meeting.backends.minutes._launch_minutes_implementation` 上，而**那个
    名字在 `meeting.backends.minutes` 里不存在**（那个 `raising=False` 的 `setattr` 只是现场
    造了一个死属性）：`launch_calls` 恒为 0，于是「`minutes.hook = null` 时一次都不拉起」
    这条**关于花钱的**断言什么都没证明，「被走到就当场抛」那层网也一次都没被走到。挂在真名字
    上之后这本记录才看得见拉起 —— 判据是 `auto` 那一档上**恰好 1 笔**（正控）。

    `launch()` 就是那个登记替身：被调用时记一笔，再交给**现成的落终态替身**
    （`tests/test_post_idempotency.py` 的 `DelayedHookImplementation`，照真启动器时序：先落
    `running`、迟一小会儿落 `done` + 合规的 `meeting_final.md`），`meeting post` 那段
    「等这一趟新落的终态」因此正常收场。**真子会话一次都起不来**：真拉起点整个被换掉了。
    """

    def __init__(self, settle: Any) -> None:
        self.resolutions: list[dict[str, Any]] = []
        self.launch_calls: list[dict[str, Any]] = []
        self._settle = settle
        #: 挂在真拉起点那个名字上的替身本身（接线自检读它）
        self.launch_point: Any = None

    def resolution_count(self) -> int:
        return len(self.resolutions)

    def launch_count(self) -> int:
        return len(self.launch_calls)

    def launch(
        self,
        implementation: Any,
        session_dir: Any,
        *,
        force: bool = False,
        deliver: bool = False,
    ) -> None:
        """真拉起点（`runner._launch_minutes_implementation`）的替身：登记 + 交终态。"""
        self.launch_calls.append(
            {
                "session_dir": str(session_dir),
                "implementation": (
                    f"{type(implementation).__module__}."
                    f"{type(implementation).__qualname__}"
                ),
                "force": bool(force),
                "deliver": bool(deliver),
            }
        )
        self._settle.launch(str(session_dir))


@pytest.fixture(autouse=True)
def a_minutes_ledger(monkeypatch: pytest.MonkeyPatch) -> MinutesLedger:
    """包装 `resolve_minutes_hook()`（真解析照旧走，只多记一笔）+ 换掉**真拉起点**。

    `tests/conftest.py` 的 `MEETING_MINUTES_CHILD=1` 兜底仍在（第一层网）。**要观察「解析」
    与「拉起」这两格的用例自己 `monkeypatch.delenv(...)` 摘掉它** —— 摘掉之后本文件这两处
    替身照旧挡在真子会话与真模型之前：解析那一处是包装间谍（真解析照旧走），拉起那一处把
    真拉起点（`meeting.offline.runner._launch_minutes_implementation`）整个换成登记替身，
    因此 `minutes.hook = auto` 那一趟解析出来的**真实现**
    （`meeting.backends.minutes.MinutesHook`）一次都不会被调到；配置层若没生效（`minutes.hook`
    退回 `auto`），这一档也只是让记录里多一笔、用例当场红在「这一档不该拉起」这句上，真会话与
    真模型仍然起不来。

    打桩点用真名字（`monkeypatch.setattr` 不带 `raising=False`）：名字若被改名，这里当场红，
    不会像此前那样现造一个死属性、让记录默默恒为 0。
    """
    ledger = MinutesLedger(DelayedHookImplementation())
    real_resolve = registry_module.resolve_minutes_hook

    def spy_resolve(explicit: str | None = None) -> Any:
        resolution = real_resolve(explicit)
        current = config_module.current()
        ledger.resolutions.append(
            {
                "selector": resolution.selector,
                "has_implementation": bool(resolution.has_implementation),
                "config_path": None if current is None else current.config_path(),
            }
        )
        return resolution

    def spy_launch(
        implementation: Any, session_dir: Any, *, force: bool = False, deliver: bool = False
    ) -> None:
        ledger.launch(implementation, session_dir, force=force, deliver=deliver)

    ledger.launch_point = spy_launch
    monkeypatch.setattr(registry_module, "resolve_minutes_hook", spy_resolve)
    # 拆分后真拉起点在 `minutes_tail`（`_announce_minutes` 的函数体在那里解析这个名字），
    # `offline_runner` 上那格是 re-export 的同一函数 —— 两处一并打桩：net 真拦得住，
    # 下面的自检（`offline_runner._launch_minutes_implementation is …`）也照旧绿。
    monkeypatch.setattr(minutes_tail_module, "_launch_minutes_implementation", spy_launch)
    monkeypatch.setattr(offline_runner, "_launch_minutes_implementation", spy_launch)
    return ledger


def test_the_safety_nets_this_file_relies_on_are_really_wired(
    a_minutes_ledger: MinutesLedger,
    a_fake_converter: RecordingConverter,
    tmp_path: Path,
) -> None:
    """本文件的四层网**此刻真的在岗**（改名 / 打桩落空 → 当场红）。

    这一条是本文件的**接线自检**，它不当任何一档的行为证据（那五档各有自己的用例）。
    逐层**真调一次、看记录动不动**：

      - **真引擎替身**：`meeting.offline.engine` 上那个 `MossOfflineEngine` 此刻不是真类
        （真类的构造会去 HF 快照加载权重、缺快照时回退联网下载）；
      - **解析那一处**：`registry.resolve_minutes_hook` 此刻是本文件夹具的登记替身 ——
        真调一次，`a_minutes_ledger.resolutions` 上必须多出一行；
      - **拉起那一处**：**真拉起点** `meeting.offline.runner._launch_minutes_implementation`
        （`runner.py:1408` 定义、`:1371` 调）此刻是本文件夹具的登记替身 —— 真调一次，
        `a_minutes_ledger.launch_calls` 上必须多出一行。此前这里只断一个 `callable(...)`，
        而名字挂在 `meeting.backends.minutes` 上一个**不存在**的属性上（`raising=False`
        现造的死属性）：那一问恒真，也完全看不见真拉起点；
      - **转码那一处**：`meeting.capture.archive.convert_to_mp3` 此刻是本文件夹具的登记
        替身 —— 真调一次，替身的 `calls` 上必须多出一行（不会去碰真 ffmpeg）。
    """
    from meeting.capture import archive as archive_module
    from meeting.offline import engine as engine_module

    assert (
        getattr(engine_module.MossOfflineEngine, "__name__", "")
        == "_NeverBuiltEngine"
    ), (
        "真引擎替身没在岗：meeting.offline.engine.MossOfflineEngine 还是真类"
        f"（{engine_module.MossOfflineEngine!r}）"
    )

    before = a_minutes_ledger.resolution_count()
    registry_module.resolve_minutes_hook("null")
    assert a_minutes_ledger.resolution_count() == before + 1, (
        "解析那一处的登记替身没在岗（打桩落空）：调一次解析，记录里没多出一行"
    )

    assert offline_runner._launch_minutes_implementation is a_minutes_ledger.launch_point, (
        "拉起那一处的登记替身没在岗：meeting.offline.runner._launch_minutes_implementation "
        "不是本文件夹具那个替身（真拉起点还在岗 = 这一趟可能真起子会话）"
    )
    launch_before = a_minutes_ledger.launch_count()
    wired = tmp_path / "wired-launch"
    wired.mkdir()
    a_minutes_ledger.launch_point(None, wired)
    assert a_minutes_ledger.launch_count() == launch_before + 1, (
        "拉起那一处的登记替身没记上一笔（打桩落空）：调一次，记录里没多出一行"
    )

    converter_calls = len(a_fake_converter.calls)
    assert archive_module.convert_to_mp3 is a_fake_converter, (
        "转码那一处的假件没在岗：archive.convert_to_mp3 不是本文件夹具那个替身"
    )
    a_fake_converter(tmp_path / "in.pcm", tmp_path / "out.mp3")
    assert len(a_fake_converter.calls) == converter_calls + 1, (
        "转码替身没记上一笔：真 ffmpeg 可能被碰到了"
    )


# ---------------------------------------------------------------------------
# 等待层的登记替身（记下上界，并按上界真睡一小会儿）
# ---------------------------------------------------------------------------


class WaitReceipt:
    """换掉 `cli._wait_for_settled_minutes`：记下**等待层实际拿到的上界**，不真等。

    这一格钉的是「上界是多少、由谁给的」：

      - `budget` 是**这一趟进等待层那一刻** `cli._minutes_timeout_seconds()` 现读的
        `minutes.timeout_seconds`（配置层那一格，或者没配时的默认值）；
      - `timeout` / `deadline` 是编排函数递给等待层的两个参数。

    `sleep_seconds` 是替身自己真睡多久（缺省 0，也就是不睡）。要另钉「等待真的按那个上界
    收场」的用例把它设成小值，再读最后一个 `timeout` —— 见
    `test_the_wait_upper_bound_really_bounds_the_wait`。

    **它不是「等某件事」的轮询**：真实现等的是盘上那份纪要状态文件（超时那一档还要真睡满
    上界），这里换成替身正是为了不真等。
    """

    def __init__(self, *, sleep_seconds: float | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.sleep_seconds = sleep_seconds

    def __call__(self, directory: Path, **kwargs: Any) -> dict[str, Any]:
        budget = cli_module._minutes_timeout_seconds()
        sleep_for = budget if self.sleep_seconds is None else self.sleep_seconds
        self.calls.append(
            {
                "directory": str(directory),
                # 编排函数递给等待层的那一格：绝对时刻那一趟没给第二份预算，交给这里现读
                "timeout": kwargs.get("timeout"),
                "deadline": kwargs.get("deadline"),
                "budget": budget,
            }
        )
        if sleep_for:
            time.sleep(sleep_for)
        return {
            "hook": "timeout",
            "state": "timeout",
            "reason": f"登记替身：按上界 {budget} 秒收场（本替身不真等）",
            "status_path": str(Path(directory) / offline_runner.MINUTES_STATUS_NAME),
            "final": None,
            "waited_seconds": round(float(sleep_for), 3),
        }

    def upper_bound(self) -> float:
        """等待层拿到的上界（配置层现读的那一格）。"""
        return float(self.calls[-1]["budget"])


@pytest.fixture()
def waiting(monkeypatch: pytest.MonkeyPatch) -> WaitReceipt:
    """把等待层换成不真等的登记替身（上界那一格照记）。"""
    receipt = WaitReceipt(sleep_seconds=0.0)
    monkeypatch.setattr(cli_module, "_wait_for_settled_minutes", receipt)
    return receipt


@pytest.fixture(autouse=True)
def restore_the_current_config() -> Any:
    """收场把模块级生效配置放回去（本文件的入口会 `set_current()`）。"""
    previous = config_module.current()
    yield
    config_module.set_current(previous)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def write_config(home: Path, values: dict[str, Any]) -> Path:
    """把点号扁平的生效值写进 `<临时家目录>/.config/meeting/config.json`。"""
    path = home / ".config" / "meeting" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
    return path


def synth_session(root: Path, *, sources: tuple[str, ...] = ("mic",)) -> Path:
    """合成一个场次目录：一路 16k 单声道 int16 裸 PCM + 一份最小的 `session.json`。

    PCM 里放的是**非零样本**（`b"\\x00\\x01"` 反复）：全零会被数字静音门判成没声，
    引擎调用就一次都不发生，第 1 档要的那条证据也就没了。
    """
    root.mkdir(parents=True, exist_ok=True)
    for source in sources:
        (root / f"meeting_audio_{source}_001.pcm").write_bytes(
            b"\x00\x01" * int(SAMPLE_SECONDS * SAMPLE_RATE)
        )
    (root / "session.json").write_text(
        json.dumps(
            {
                "session_id": "250919",
                "dir": str(root),
                "mode": "both" if len(sources) > 1 else sources[0],
                "state": "done",
                "sources": [
                    {"id": source, "archive": f"meeting_audio_{source}_001.pcm"}
                    for source in sources
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return root


def a_config_for_a_real_run(
    hook: Any = None, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """一份「能跑完但一个权重都不加载」的配置：假引擎 + 假纪要拉起 + 秒级的纪要上界。

    `hook` 是 `minutes.hook` 那一格（缺省 `None` = JSON `null`，也就是「不要会后纪要」）；
    `extra` 是逐键的补充 / 覆盖（点号键名照配置文件的写法）。

    `minutes.timeout_seconds` 取键的取值域下限 60（`meeting/config.py` 的
    `INT_LIMITS["minutes.timeout_seconds"]`）：它与默认值 14400 分得开，「上界跟着配置变」
    这一条才有读数。本文件不真等它 —— 等待层是按上界真睡一小会儿的替身。
    """
    config = {
        "post.engine": FAKE_ENGINE_TARGET,
        "post.hook": "null",
        "minutes.hook": hook,
        "minutes.timeout_seconds": 60,
    }
    if extra:
        config.update(extra)
    return config


def write_minutes_running(session: Path) -> Path:
    """盘上先放一版 `running` 的纪要状态：拉起之后的等待因此真的要走一遍。"""
    from meeting.minutes import status as minutes_status

    minutes_status.write_session_status(
        session,
        state="running",
        mode="offline_only",
        branch="headless",
        started_at="2025-09-19T22:00:00",
        artifacts={},
    )
    return session / offline_runner.MINUTES_STATUS_NAME


def write_offline_done(session: Path, *, sources: tuple[str, ...] = ("mic",)) -> None:
    """把离线段写成**已是终态**：状态文件 `done` + 逐路三份产物齐。

    判据照 `meeting/cli.py::_offline_layer_done()` 那一套（状态文件终态 + 产物齐）。
    """
    from meeting.offline import artifacts as offline_artifacts

    per_source: dict[str, dict[str, str]] = {}
    for source in sources:
        transcript = session / f"meeting_offline_transcript_{source}.md"
        speakers = session / f"meeting_offline_speakers_{source}.json"
        transcript.write_text("# 离线转写\n", encoding="utf-8")
        speakers.write_text("{}\n", encoding="utf-8")
        per_source[source] = {
            "transcript": transcript.name,
            "speakers": speakers.name,
            "report": offline_artifacts.REPORT_NAME,
        }
    (session / offline_artifacts.REPORT_NAME).write_text("# 离线报告\n", encoding="utf-8")
    offline_artifacts.write_status(
        session / offline_artifacts.STATUS_NAME,
        session_id=session.name,
        state="done",
        started_at="2025-09-19T22:00:00",
        finished_at="2025-09-19T22:10:00",
        sources=list(sources),
        artifacts=per_source,
    )


# ---------------------------------------------------------------------------
# 一、`post.engine` 写成替身：这条路上真按它取引擎
# ---------------------------------------------------------------------------


def test_the_post_engine_selector_comes_from_config_json(
    isolated_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`config.json` 里 `post.engine = <替身>` → `meeting post` 真按它取引擎。

    这一档要挡住的正是「造出**真**离线引擎」那条路：配置里那个替身若被当成默认的 `auto`，
    这一趟就会去 `meeting.offline.engine:MossOfflineEngine` 造真引擎（真加载权重）——
    本文件那条红线替身会让它当场炸。

    证据三条，缺一不可：

      1. **构造**：假引擎被构造了，且构造参数里的 `model` / `dtype` 就是生效配置里那两个值
         —— 构造这一步真的是 `build_engine()` 按生效配置做出来的；
      2. **调用**：假引擎的 `transcribe()` 真被调到（`mic` 一路一次），说明这台引擎是这一趟
         真用的那一台，而不是只被解析出来摆着；
      3. **配置层已在场**：走到这一步时 `config_module.current()` 指向本用例写的那份
         `config.json`（不是默认值表、也不是上一场留下的那一份）。
    """
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)
    config_path = write_config(
        isolated_config_dir.parent.parent, a_config_for_a_real_run()
    )
    session = synth_session(tmp_path / "250919")

    with pytest.raises(SystemExit) as info:
        post([str(session)])

    assert len(RecordingOfflineEngine.constructor_args) == 1, (
        RecordingOfflineEngine.constructor_args
    )
    assert RecordingOfflineEngine.constructor_args == [
        {
            "model": config_module.get("post.model"),
            "dtype": config_module.get("post.dtype"),
            "device": "cpu",
        }
    ], RecordingOfflineEngine.constructor_args
    assert [Path(call).name for call in RecordingOfflineEngine.calls] == [
        "meeting_audio_mic_001.pcm"
    ], RecordingOfflineEngine.calls

    assert config_module.get("post.engine") == FAKE_ENGINE_TARGET
    current = config_module.current()
    assert current is not None
    assert Path(current.config_path()) == config_path
    assert info.value.code == EXIT_OK


def test_the_engine_selector_reaches_the_offline_layer_as_a_config_read(
    isolated_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`build_engine()` 拿到的选择器就是 `config.json` 里那一格（配置层读进来的值）。

    `run_session(..., engine=None)` 走的是 `build_engine(config)` 这条**真产品路径**；
    这里把 `build_engine` 包一层登记，读的是它**现读生效配置**的结果，再让真的那一份去造。
    """
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)
    write_config(isolated_config_dir.parent.parent, a_config_for_a_real_run())
    session = synth_session(tmp_path / "250919")
    seen: list[str] = []
    real_build_engine = offline_runner.build_engine

    def watched_build_engine(config: Any = None) -> Any:
        seen.append(str(offline_runner._engine_options(config)["selector"]))
        return real_build_engine(config)

    monkeypatch.setattr(offline_runner, "build_engine", watched_build_engine)

    with pytest.raises(SystemExit) as info:
        post([str(session)])

    assert info.value.code == EXIT_OK
    assert seen == [FAKE_ENGINE_TARGET], seen
    assert len(RecordingOfflineEngine.constructor_args) == 1, (
        RecordingOfflineEngine.constructor_args
    )


# ---------------------------------------------------------------------------
# 二、`minutes.hook = null`：不拉起纪要层，落安静的缺席档
# ---------------------------------------------------------------------------


def test_a_null_minutes_hook_is_never_launched_and_lands_on_the_absent_tier(
    isolated_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    a_minutes_ledger: MinutesLedger,
) -> None:
    """`config.json` 里 `minutes.hook = null` → 这一趟**不拉起纪要层**，落安静的缺席档。

    **这是最要命的那一档**：没有配置层时 `minutes.hook` 退回默认的 `auto`，`meeting post`
    会照旧拉起 headless DSH 子会话去跑纪要 —— 真花钱，而且违背用户显式写的「不要会后
    纪要」。下面 `delenv` 摘掉的是**再入闸那一层网**（摘掉之后解析这一格才会真的被走到），
    本文件自己那两处登记照旧挡在真子会话之前。

    判据三条一起读：

      - **解析那一格如实落在缺席档**：`resolution_count() == 1` 且 `selector == "null"`、
        `has_implementation is False` —— 配置层没生效时它交回的是 `selector = "auto"`
        与一个真实现，于是这一格就是「配置真的被读到」在纪要层上的收据；
      - **拉起那一本记录为 0**：真拉起点（`meeting.offline.runner._launch_minutes_implementation`）
        一次都没被调到。那一处是登记替身：被走到就多出一笔，于是这一档红在记录里 ——
        配置层若没生效（`hook` 退回 `auto`），真会话仍然起不来（真拉起点被整个换掉了），
        但这一条断言当场转红。**正控是相邻那一条**（`auto` 那一档上恰好 1 笔）：没有它，
        「0 笔」分不出「配置真的关掉了这一层」与「这本记录根本看不见拉起」；
      - **怎么落这一档**：`hook == "absent"`、`state is None`、`announced is False`、
        退出码 `EXIT_OK`，且**一份纪要状态文件都不写**。
    """
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)
    write_config(
        isolated_config_dir.parent.parent, a_config_for_a_real_run(None)
    )
    session = synth_session(tmp_path / "250919")

    with pytest.raises(SystemExit) as info:
        post([str(session)])

    assert a_minutes_ledger.resolution_count() == 1, (
        f"这一层该被解析一次并落到缺席档，实测 {a_minutes_ledger.resolutions}"
    )
    assert a_minutes_ledger.resolutions[0]["selector"] == "null", (
        a_minutes_ledger.resolutions
    )
    assert a_minutes_ledger.resolutions[0]["has_implementation"] is False, (
        a_minutes_ledger.resolutions
    )
    assert a_minutes_ledger.resolutions[0]["config_path"] is not None, (
        a_minutes_ledger.resolutions
    )
    assert a_minutes_ledger.launch_count() == 0, (
        f"实测拉起 {a_minutes_ledger.launch_calls}"
    )
    assert info.value.code == EXIT_OK
    assert not (session / offline_runner.MINUTES_STATUS_NAME).exists()
    # 离线那一趟照常跑完（这一档只关掉纪要那一层，不带走离线链）
    assert (session / offline_runner.artifacts.STATUS_NAME).is_file()


def test_an_auto_minutes_hook_is_launched_exactly_once_on_the_ledger(
    isolated_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    a_minutes_ledger: MinutesLedger,
) -> None:
    """**正控**：`minutes.hook = auto` 时同一本记录里**恰好 1 笔**，这一层落 `done` / `EXIT_OK`。

    上一条（`null` → 0 笔）单独读是分不出「配置真的关掉了这一层」与「这本记录根本看不见拉起」
    的 —— 此前正是后者：记录挂在 `meeting.backends.minutes` 上一个**不存在**的名字上，恒为 0。
    两档各一条一起读才有证明力：同一处登记、同一份配置形状，只有 `minutes.hook` 那一格不同，
    记录里是 0 与 1。

    拉起那一笔由登记替身承担（真拉起点整个被换掉，真子会话一次都起不来），它照真启动器时序
    收场：先落 `running`、迟一小会儿落 `done` + 合规的 `meeting_final.md`，于是
    `meeting post` 那段「等这一趟新落的终态」真的等到了**这一趟**的终态（不是盘上残留的那一版，
    本用例的盘上本来一份都没有）。

    判据四条一起读：

      - 解析那一格：`resolution_count() == 1`、`selector == "auto"`、
        `has_implementation is True`（配置这一档要的正是真实现在场）；
      - 拉起那一本记录：`launch_count() == 1`，那一笔记的场次目录就是本用例这一场，实现是
        **真实现** `meeting.backends.minutes.MinutesHook`（解析出来的是它，只是没被调到），
        `force is False`；
      - 这一层的终态：`meeting_minutes_status.json` 的 `state == "done"`，且
        `meeting_final.md` 在场（两半判据缺一不可）；
      - 退出码 `EXIT_OK`（离线段也跑完了）。
    """
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)
    write_config(isolated_config_dir.parent.parent, a_config_for_a_real_run("auto"))
    session = synth_session(tmp_path / "250919")

    with pytest.raises(SystemExit) as info:
        post([str(session)])

    assert a_minutes_ledger.resolution_count() == 1, a_minutes_ledger.resolutions
    assert a_minutes_ledger.resolutions[0]["selector"] == "auto", (
        a_minutes_ledger.resolutions
    )
    assert a_minutes_ledger.resolutions[0]["has_implementation"] is True, (
        a_minutes_ledger.resolutions
    )
    assert a_minutes_ledger.launch_count() == 1, (
        f"`minutes.hook = auto` 这一档该恰好 1 笔（这本记录要真的看得见拉起），"
        f"实测 {a_minutes_ledger.launch_calls}"
    )
    row = a_minutes_ledger.launch_calls[0]
    assert Path(row["session_dir"]) == session.resolve(), row
    assert row["implementation"] == "meeting.backends.minutes.MinutesHook", row
    assert row["force"] is False, row
    assert row["deliver"] is True, (
        row,
        "`meeting post` 是人工入口：隔离闸的放行标记该一路递到真拉起点",
    )

    status = json.loads(
        (session / offline_runner.MINUTES_STATUS_NAME).read_text(encoding="utf-8")
    )
    assert status["state"] == "done", status
    assert (session / offline_runner.MINUTES_FINAL_NAME).is_file()
    assert info.value.code == EXIT_OK, info.value.code


def test_the_null_hook_stays_absent_on_the_second_pass_too(
    isolated_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    a_minutes_ledger: MinutesLedger,
) -> None:
    """连跑两遍：两遍都解析到缺席档、两遍的拉起计数都停在 0。

    同一份场次目录连跑两趟（题眼那个「反复重跑」的现场）：第一趟把离线那一趟跑完，
    第二趟走的是「只补纪要」那一支。两趟都读同一份 `config.json`，因此两趟里
    `minutes.hook` 都落在缺席档、拉起那一步一次都没被走到 —— 这一档正是「用户说了不要
    会后纪要，就别去花钱拉起子会话」在重跑现场上的落点。

    第二趟的退出码如实走**现成的缺席档**（`_launch_minutes_on_the_product_path()` 里
    「解析不出实现」那一格：盘上没有纪要终态，按两半判据如实记失败，也就是
    `EXIT_FAILURE`）—— 这一格是产品路径的既有口径，本文件照读、不改。
    """
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)
    write_config(
        isolated_config_dir.parent.parent, a_config_for_a_real_run(None)
    )
    session = synth_session(tmp_path / "250919")

    codes = []
    for _ in range(2):
        with pytest.raises(SystemExit) as info:
            post([str(session)])
        codes.append(info.value.code)

    assert codes == [EXIT_OK, EXIT_FAILURE], codes

    assert a_minutes_ledger.resolution_count() == 2, a_minutes_ledger.resolutions
    assert [row["selector"] for row in a_minutes_ledger.resolutions] == ["null", "null"], (
        a_minutes_ledger.resolutions
    )
    assert all(
        row["has_implementation"] is False for row in a_minutes_ledger.resolutions
    ), a_minutes_ledger.resolutions
    assert a_minutes_ledger.launch_count() == 0, a_minutes_ledger.launch_calls
    assert not (session / offline_runner.MINUTES_STATUS_NAME).exists()


# ---------------------------------------------------------------------------
# 三、`minutes.timeout_seconds` 调小：等纪要的上界真的跟着变
# ---------------------------------------------------------------------------


def test_the_minutes_wait_upper_bound_follows_the_config_key(
    isolated_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    waiting: WaitReceipt,
) -> None:
    """`minutes.timeout_seconds` 调小时，`meeting post` 等纪要的上界真的跟着变。

    走的是**真入口** `post()`：拉起之后那一处等待（登记替身）拿到的上界由 `run_post()`
    从配置层现读（`_minutes_timeout_seconds()` → `config_module.get("minutes.timeout_seconds")`）。
    替身不真等，因此这一档不花 60 秒的墙钟。

    这一档要挡住的是「上界退回默认的 14400 秒」：配置里那个值与默认值分得开，
    两种口径不可能同值（反面读数见下一条）。

    **盘上特意不放任何纪要状态文件**：盘上停着 `running` 时 `run_post()` 的幂等闸会当场拒跑
    （那一档退出码是 `EXIT_REJECTED`），这一趟就走不到等待那一步了。
    """
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)
    write_config(
        isolated_config_dir.parent.parent,
        a_config_for_a_real_run("auto", {"minutes.timeout_seconds": 60}),
    )
    session = synth_session(tmp_path / "250919")

    with pytest.raises(SystemExit) as info:
        post([str(session)])

    assert waiting.calls, "这一趟该走到「等纪要」那一步（拉起成功之后一律等）"
    assert waiting.upper_bound() == 60.0, waiting.calls
    assert info.value.code == EXIT_FAILURE, "到上界还没落定 → 如实报 timeout（不是成功档）"
    assert config_module.get("minutes.timeout_seconds") == 60


def test_the_default_wait_upper_bound_is_the_frozen_default(
    isolated_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    waiting: WaitReceipt,
) -> None:
    """不写这个键时上界仍是默认值表里那一档 ——「跟着配置变」的反面读数。

    同一处证据两条一起读：进等待层那一刻现读的那一格（`waiting.upper_bound()`）与
    `config_module.get()` 的读数都等于默认值表那一份；配置里写 60 的那一档则读到 60.
    """
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)
    config_path = write_config(
        isolated_config_dir.parent.parent,
        {"post.engine": FAKE_ENGINE_TARGET, "post.hook": "null", "minutes.hook": "auto"},
    )
    session = synth_session(tmp_path / "250919")

    with pytest.raises(SystemExit) as info:
        post([str(session)])

    default = config_module.DEFAULT_VALUES["minutes.timeout_seconds"]
    assert info.value.code == EXIT_FAILURE, "这一档也走到上界（替身不真等）"
    assert waiting.upper_bound() == float(default), waiting.calls
    assert config_module.get("minutes.timeout_seconds") == default
    assert cli_module._minutes_timeout_seconds() == float(default)
    current = config_module.current()
    assert current is not None
    assert Path(current.config_path()) == config_path


def test_the_wait_upper_bound_really_bounds_the_wait(
    isolated_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**等待按 `budget` 那个止点收场**：等待层停在上界那一格上。

    这一格直接调那一处等待（`cli._wait_for_settled_minutes`）本身，把两份预算摆在一起看：
    显式传秒级的 `timeout` 时它按秒级停；不给那一格时它现读配置层那一格（本用例里是 60）。
    两处读数都落在同一个「到上界还没落定」的档上，说明「上界跟着配置走」这条有实测撑腰。

    **盘上先放一版停在 `running` 的状态**：不会有终态来，于是这一处等待走的是「到上界还没
    落定 → 如实报 `timeout`」那一档，等的是时间本身（秒级那一趟因此零点几秒就收场）。
    """
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)
    write_config(
        isolated_config_dir.parent.parent,
        a_config_for_a_real_run("auto", {"minutes.timeout_seconds": 60}),
    )
    session = synth_session(tmp_path / "250919")
    write_minutes_running(session)
    config_module.set_current(config_module.load())

    assert cli_module._minutes_timeout_seconds() == 60.0

    begun = time.monotonic()
    quick = cli_module._wait_for_settled_minutes(
        session,
        baseline=cli_module._minutes_baseline(session),
        timeout=0.05,
    )
    quick_elapsed = time.monotonic() - begun

    assert quick["hook"] == "timeout", quick
    assert quick["state"] == "timeout", quick
    assert 0.05 <= quick_elapsed < 5.0, f"秒级上界该在秒级收场，实测 {quick_elapsed:.3f}s"
    # 这一处等待只读盘、不写盘：状态文件的现场一个字都没动
    assert (session / offline_runner.MINUTES_STATUS_NAME).is_file()


# ---------------------------------------------------------------------------
# 四、配置损坏 / 取值非法两档：退出码与终端说法与 `web()` 同源
# ---------------------------------------------------------------------------


def test_a_corrupt_config_takes_the_post_entry_to_the_startup_unmet_exit(
    isolated_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """配置损坏（截断 JSON）→ `EXIT_STARTUP_UNMET`，且**不进花钱那一段**。

    同一条现场在 `web()` 上的落点是「启动前提不成立」；这里把两个入口的终端首行逐字比对：
    **同一句话**、同一个退出码。
    """
    (isolated_config_dir / "config.json").write_text(
        '{"web.port": 8765,', encoding="utf-8"
    )
    session = tmp_path / "250919"
    session.mkdir()
    spent: list[Any] = []
    monkeypatch.setattr(cli_module, "run_post", lambda *a, **k: spent.append((a, k)))

    with pytest.raises(SystemExit) as info:
        post([str(session)])
    post_stderr = capsys.readouterr().err

    assert info.value.code == EXIT_STARTUP_UNMET
    assert post_stderr.startswith("启动前提不成立："), post_stderr
    assert spent == [], "配置损坏时连花钱那一段都不许进"

    with pytest.raises(SystemExit) as web_info:
        cli_module.web()
    web_stderr = capsys.readouterr().err

    assert web_info.value.code == info.value.code == EXIT_STARTUP_UNMET
    assert web_stderr.splitlines()[0] == post_stderr.splitlines()[0], (
        f"两个入口的说法该逐字同源：post={post_stderr!r} web={web_stderr!r}"
    )


@pytest.mark.parametrize(
    "values",
    [
        {"web.port": "8765"},
        {"web.port": 70000},
        {"asr.max_segment_seconds": 30, "asr.timeout_seconds": 20},
    ],
)
def test_an_illegal_config_value_takes_the_post_entry_to_the_rejected_exit(
    isolated_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    values: dict[str, Any],
) -> None:
    """取值非法三档（类型 / 范围 / 键间规则）→ `EXIT_REJECTED`，说法与 `web()` 逐字同源。"""
    write_config(isolated_config_dir.parent.parent, values)
    session = tmp_path / "250919"
    session.mkdir()
    spent: list[Any] = []
    monkeypatch.setattr(cli_module, "run_post", lambda *a, **k: spent.append((a, k)))

    with pytest.raises(SystemExit) as info:
        post([str(session)])
    post_stderr = capsys.readouterr().err

    assert info.value.code == EXIT_REJECTED
    assert post_stderr.startswith("配置值非法："), post_stderr
    assert spent == [], "取值非法时连花钱那一段都不许进"

    with pytest.raises(SystemExit) as web_info:
        cli_module.web()
    web_stderr = capsys.readouterr().err

    assert web_info.value.code == info.value.code == EXIT_REJECTED
    assert web_stderr.splitlines()[0] == post_stderr.splitlines()[0], (
        f"两个入口的说法该逐字同源：post={post_stderr!r} web={web_stderr!r}"
    )


def test_a_config_file_that_is_a_directory_is_reported_not_raised(
    isolated_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`config.json` 的位置被同名目录顶替 → 同样落 `EXIT_STARTUP_UNMET`，不抛裸异常。"""
    (isolated_config_dir / "config.json").mkdir(parents=True, exist_ok=True)
    session = tmp_path / "250919"
    session.mkdir()
    monkeypatch.setattr(
        cli_module, "run_post", lambda *a, **k: pytest.fail("不许进花钱那一段")
    )

    with pytest.raises(SystemExit) as info:
        post([str(session)])

    assert info.value.code == EXIT_STARTUP_UNMET
    assert capsys.readouterr().err.startswith("启动前提不成立：")


def test_a_usage_error_still_wins_over_the_config_load(
    isolated_config_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """用法错误仍走 `EXIT_REJECTED` + 用法说明：参数面在配置层**之前**（既有行为不动）。"""
    (isolated_config_dir / "config.json").write_text("{ 这不是 JSON", encoding="utf-8")

    with pytest.raises(SystemExit) as info:
        post([])

    assert info.value.code == EXIT_REJECTED
    captured = capsys.readouterr()
    assert "用法：meeting post" in captured.err
    assert "启动前提不成立" not in captured.err


# ---------------------------------------------------------------------------
# 五、家目录下没有 `config.json`：行为与改动前一致（默认值）
# ---------------------------------------------------------------------------


def test_a_missing_config_file_keeps_the_entry_on_the_frozen_defaults(
    isolated_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """家目录下没有 `config.json` → 生效值逐键就是默认值表那一份（改动前同形）。

    证据两条：① `config_module.load()` 交回的生效值**逐键等于**
    `{key: default_for(key)}`（`log.dir` 这一格由 `default_for()` 按家目录解析，
    因此比的是解析后的值；「输出根」那一格已随默认落点退场）；② 这一趟照旧按默认的 `post.engine = auto` 取引擎 ——
    本文件的假引擎**一次都没被构造**（`auto` 不会落到它身上），于是「没配置 = 走默认那一档」
    这条既有行为在补齐配置层之后一个字没变。`minutes.timeout_seconds` 也仍是默认那一档。
    """
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)
    assert not (isolated_config_dir / "config.json").exists()
    loaded = config_module.load()

    expected = {key: config_module.default_for(key) for key in config_module.DEFAULT_VALUES}
    assert loaded.as_dict() == expected, "没配置文件时生效值该是默认表那一份"

    session = synth_session(tmp_path / "250919")

    with pytest.raises(SystemExit) as info:
        post([str(session)])

    assert RecordingOfflineEngine.constructor_args == [], (
        "默认 post.engine = auto 不许落到本文件的假替身上"
    )
    assert config_module.get("minutes.timeout_seconds") == (
        config_module.DEFAULT_VALUES["minutes.timeout_seconds"]
    )
    assert config_module.get("minutes.hook") == "auto"
    # 默认 `auto` 那一档落到的是真引擎（本文件那条红线替身让它当场炸）：离线那一层如实
    # 落 failed，这一趟因此**如实按失败档收场**（不是成功档、也不是别的码 —— 这一格与
    # 改动前同形，实测的终端尾行是「结论：failed，退出码 EXIT_FAILURE」）
    assert info.value.code == EXIT_FAILURE, info.value.code


def test_the_entry_reads_the_config_before_the_idempotency_gate(
    isolated_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读取时机在**幂等闸与花钱之前**：进编排那一段时生效配置已经就位。

    `run_post()` 一进门就要按 `minutes.timeout_seconds` 定等待上界、按 `post.engine` 造引擎，
    因此配置层必须在它之前。这一格把 `run_post` 包一层登记，读的是「进它那一刻
    `config_module.current()` 是谁」——必须是本用例写的那份 `config.json`。
    """
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)
    config_path = write_config(
        isolated_config_dir.parent.parent, a_config_for_a_real_run()
    )
    session = synth_session(tmp_path / "250919")
    seen: list[str | None] = []
    real_run_post = cli_module.run_post

    def watched_run_post(*args: Any, **kwargs: Any) -> Any:
        current = config_module.current()
        seen.append(None if current is None else current.config_path())
        return real_run_post(*args, **kwargs)

    monkeypatch.setattr(cli_module, "run_post", watched_run_post)

    with pytest.raises(SystemExit) as info:
        post([str(session)])

    assert info.value.code == EXIT_OK
    assert seen == [str(config_path)], seen


# ---------------------------------------------------------------------------
# 六、`log.dir` / `log.level` 这几个键真的被用上（`web()` 第 ③ 步那一格）
# ---------------------------------------------------------------------------


def test_the_post_entry_rebuilds_the_service_log_in_the_configured_dir(
    isolated_config_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`log.dir` 指到自有目录 → `meeting post` 的服务日志真的落在那里。

    这一格补的是「被读进来却一次都没被用」的那一类：配置层补齐之后 `log.dir` / `log.level`
    进了生效清单，但 `post()` 只在 bootstrap 那一步按**默认**日志目录建过一次，配的落点
    一个字不生效（`web()` 有第 ③ 步「按生效配置重建」，`post()` 此前没有）。现在两个入口
    同源：**post 之后的服务级 log 就在生效 `log.dir` 下**。

    判据两条都要能分辨现场，读的是那几行文字本身（光看文件在不在分辨不出来）：

      - 生效那一份（`logging_setup.service_log_path()`）落在用户配的目录里，且里面有
        `会后流程开始` 那一行（它是第 ③ 步**之后**才写的）；
      - 默认目录那一份里**没有**那一行（bootstrap 那一份到此为止）—— 没有第 ③ 步时，
        正是反过来：默认目录里有、配的目录里空着。

    `MEETING_LOG_DIR` 先摘掉：环境变量优先于 `config.json`，不摘的话这一格量的是环境变量。
    """
    from meeting import logging_setup

    monkeypatch.delenv("MEETING_LOG_DIR", raising=False)
    monkeypatch.delenv("MEETING_MINUTES_CHILD", raising=False)
    custom_dir = tmp_path / "customlogs"
    write_config(
        isolated_config_dir.parent.parent,
        a_config_for_a_real_run(None, {"log.dir": str(custom_dir)}),
    )
    session = synth_session(tmp_path / "250919")
    previous_handlers = logging_setup.handler_snapshot()
    try:
        with pytest.raises(SystemExit) as info:
            post([str(session)])

        assert info.value.code == EXIT_OK, info.value.code
        configured_path = custom_dir / config_module.SERVICE_LOG_FILE_NAME
        assert logging_setup.service_log_path() == str(configured_path), (
            logging_setup.service_log_path()
        )
        assert "会后流程开始" in configured_path.read_text(encoding="utf-8"), (
            "生效 log.dir 下那一份里没有第 ③ 步之后才写的那一行"
        )

        default_path = (
            Path(config_module.default_log_dir()) / config_module.SERVICE_LOG_FILE_NAME
        )
        default_text = default_path.read_text(encoding="utf-8") if default_path.is_file() else ""
        assert "会后流程开始" not in default_text, (
            "日志照旧落在默认目录里：第 ③ 步（按生效配置重建）没生效"
        )
    finally:
        logging_setup.restore_handlers(previous_handlers)
