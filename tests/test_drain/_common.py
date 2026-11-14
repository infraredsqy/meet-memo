
from __future__ import annotations

import asyncio
import contextlib
import re
import time
from pathlib import Path
from typing import Any

import pytest

from meeting.service.runtime import (
    DRAIN_BUDGET_MAX_SECONDS,
    DRAIN_BUDGET_MIN_SECONDS,
    DRAIN_BUDGET_SEGMENT_OVERHEAD_SECONDS,
    DRAIN_DEFAULT_BACKLOG,
    DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS,
    DRAIN_LOG_MIN_BUDGET_SECONDS,
    DrainLimitExceeded,
    compute_drain_budget,
)
from meeting.session import SESSION_FILE_NAME
from meeting.state import DRAIN_STEP_IDS, DRAIN_STEP_STATUS
from tests import _drain_fakes as fakes
from tests import _helpers as helpers
from tests._helpers import wait_until
from tests._service import post_action, read_session_json


#: ⑩ 条「收尾期间可观测」那条窗口的判据：假件把某一步钉在 `running` 上的秒数
#: （经 `fakes.BLOCK_SECONDS_ENV` 交给共享假件 `BlockingThreadPostHook`）。
#:
#: **取值区间（这一条要的是「轮询一定能在阻塞结束前读到 `running`」）**：0.6 秒必须
#: **大于一次轮询往返**（`wait_until()` 的 0.05 秒间隔 + 一次 `/api/state` 读），并且
#: **远小于 30 秒预算**（`DRAIN_BUDGET_MIN_SECONDS`，这一步的调用由 `_drain_wait()` 包在
#: 剩余预算里，太大会把这一趟收尾截断成 `failed`）；它同时也是这条用例墙钟里唯一一笔成
#: 规模的等待，所以贴着「单条用例 ≤ 5 秒」红线的下界那一侧，不是「越小越好」。
DRAIN_BLOCKING_STEP_SECONDS = 0.6

#: ⑩ 条里发完 `stop` 之后先停这么一下再开始轮询：`stop` 一返回就轮询会**抢在收尾之前**
#: （四步几乎瞬间开跑），于是「`post` 停在 `running`」这一格被读成 `done`，
#: 断言会以偶发形式撞上「窗口已过」。这一小段是**把观察点挪进窗口**用的。
#:
#: 判据：偏置要 **≥ 半个窗口**（把观察点推进窗口里），同时要 **< 窗口**（否则第一次读就
#: 落在窗口之后、`post` 已落 `done`）。
DRAIN_WINDOW_ENTRY_DELAY_SECONDS = 0.2

DRAIN_POLL_TIMEOUT_SECONDS = 15

#: ⑤ 条里那一步**可控拖长**的秒数（拖长的是 `flush` 与 `post` 两步；经
#: `fakes.FINALIZE_DELAY_SECONDS_ENV` 交给共享假件 `FakeCaptureBackend`）。
#:
DRAIN_SLOW_STEP_SECONDS = 0.4

#: ⑤ 条里 `post` 那一步的拖长秒数：与 `flush` 一起把这一趟收尾的实际耗时抬到
#: `DRAIN_DRAG_TOTAL_SECONDS` 以上，于是「这一步真的等满了它自己声明的时长」可判定
DRAIN_SLOW_POST_STEP_SECONDS = 0.6

DRAIN_DRAG_TOTAL_SECONDS = 1.0

#: ⑩ 条在那个窗口里连续读 `/api/state` 的次数与间隔（几次加起来要落在窗口之内）
DRAIN_WINDOW_POLLS = 3
DRAIN_WINDOW_POLL_SECONDS = 0.05

#: **进程内**那两条（等待上限对同步路与协程路一视同仁 / 事件循环不被钉住）用的极小量：
#: 一条 0.6 秒的假步 + 一条 0.02 秒的 deadline，与「超预算 → `DrainLimitExceeded`」一一对应
DRAIN_UNIT_STEP_SECONDS = 0.6
DRAIN_UNIT_BUDGET_SECONDS = 0.02

#: 单条用例的墙钟上限。
#:
#: **取值与 `tests/conftest.py` 的看门 hook（`CASE_WALL_CLOCK_LIMIT_SECONDS`）同值**：
#: 本文件这两处 `elapsed <= E2E_CASE_WALL_CLOCK_LIMIT_SECONDS` 因此是真判据（收尾总时长
#: 收在 5 秒之内）。
E2E_CASE_WALL_CLOCK_LIMIT_SECONDS = 5.0

#: ⑤b2 条里那组**明显不小**的预算输入：`backlog_depth` 与 `segment_timeout`。
#:
#: 它们经 `fakes.BACKLOG_DEPTH_ENV` / `fakes.SEGMENT_TIMEOUT_SECONDS_ENV` 交给子进程里的
#: `FakeTranscriberBackend`（**共享假件的旋钮**，本文件不再自建一个「积压量写死在类里」的
#: 本地假件）。取 `10 × 20s` 是为了让 `compute_drain_budget()` 明显离开下限那一格
DRAIN_LARGE_BACKLOG = 10
DRAIN_LARGE_SEGMENT_TIMEOUT_SECONDS = 20.0

#: ③b「上限随传入预算走」那一条的两个量（秒）：那一步自己声明 0.2 秒，两条 deadline 一条
#: 明显**短于**它（0.02 秒：一定超）、一条明显**长于**它（0.5 秒：一定过）。
#:
#: 现场规模沿用 ⑤b 的手法（进程内 + 可注入的小 deadline），量级落在毫秒到零点几秒之间 ——
#: 「真等满一条预算」这件事因此不需要任何一条用例去等 30 秒。
DRAIN_DEADLINE_STEP_SECONDS = 0.2
DRAIN_TIGHT_DEADLINE_SECONDS = 0.02
DRAIN_WIDE_DEADLINE_SECONDS = 0.5

#: ⑤e 条（取消路径真的经 `cancelled=True` 登记）里在飞那一步的阻塞时长与轮询间隔（秒）。
#:
#: 量级与 ⑤b / ⑤c 同档（毫秒到零点几秒），于是「进 `running` 之后取消」这个同步点一定可达。
DRAIN_CANCEL_STEP_SECONDS = 0.3
DRAIN_CANCEL_POLL_SECONDS = 0.005

#: 本场时长「冻结」那一条的复查间隔（秒）：收尾落定之后再读一次，秒数不该再往前走
DRAIN_FROZEN_RECHECK_SECONDS = 0.2

DRAIN_SETTLE_WAIT_TOLERANCE_SECONDS = 0.35

#: ⑪ 条里「场次 log 摘掉之后不再有新行」的复查间隔（秒）
DRAIN_LOG_CLOSED_RECHECK_SECONDS = 0.2

#: ⑫ 条把静默阈值**显式调小**（写进临时 home 的 `config.json`；**不改代码默认值**）。
#:
#: 恢复成 3 会让第 ⑫ 条（它要等出一次告警与一次恢复）越过「单条用例 ≤ 5 秒」的红线。
TEST_SILENCE_SECONDS = 1

#: ⑫ 条把恢复阈值也调小，好让「告警恢复」那一类事件在一条用例里跑得出来
TEST_RECOVER_SECONDS = 1

#: ⑫ 条注入的那段静默时长（秒）：要长过 `TEST_SILENCE_SECONDS`（告警起得来），
#: 同时短到让「告警止」也落在同一条用例里（本机 dev 替身 0.1s 一帧，余量足够）
TEST_SILENCE_FAULT_SECONDS = 2

#: ⑫ 条的轮询超时（这一条要等出一次告警与一次恢复，比别的收尾用例宽一点）
ALERT_POLL_TIMEOUT_SECONDS = 20

#: ⑤f 条（**某一步真的越过预算 → 该步 `failed`**）里那一步的真实时长（秒）。
#:
#: 它必须在**时钟前移之前**跑完，从而成为「这一趟确实往前走了一段」的依据，同时贴着
#: 「单条用例 ≤ 5 秒」的下界那一侧取值。
DRAIN_OVERRUN_STEP_SECONDS = 0.6

#: ⑤f 条里把这一趟的钟**前移**多少秒：必须**大于这一档的预算**（下限 30 秒），
#: 否则到期那一刻剩余预算还大于 0、末尾那一步不会被判越界。
#:
#: **它换来的东西**：预算的三个数硬编码、下限 30 秒且不开配置键，「真等满 30 秒」与
#: 「单条用例 ≤ 5 秒」无法同时成立；前移这只单调钟（`_drain_wait()` 的秒表与事件循环的
#: 计时器都读它，两处一起拨的口径见 `_drain_fakes.py` 的时钟替身）于是让越界**真的发生**。
DRAIN_OVERRUN_CLOCK_OFFSET_SECONDS = float(DRAIN_BUDGET_MIN_SECONDS + 5)

#: ⑤f / ⑤g 两条里等「收尾进了在飞那一步」的轮询间隔（秒）。
#:
#: 取值 = ⑩ 条那个窗口里的读间隔（`DRAIN_WINDOW_POLL_SECONDS`）：假件进 `running` 之后至少
#: 还有 `DRAIN_OVERRUN_STEP_SECONDS` 才落定。
DRAIN_OVERRUN_POLL_SECONDS = DRAIN_WINDOW_POLL_SECONDS

#: ⑤f 条里那个「时钟偏移标记」的文件名（落在本用例的临时家目录下，随临时目录一起消失）
DRAIN_OVERRUN_MARKER_NAME = "clock-offset-marker.txt"

#: 「同一个拖长假步 + 两组不同预算」那两条（⑤f 造越过预算、⑤g 量总时长随积压量变大）用的
#: 两组积压量。**两组都喂给 `compute_drain_budget()` 取上限**（裸数一个都不抄）：
#:
#:   - 小档 `0` 与转写后端缺席时的缺省同档（`DRAIN_DEFAULT_BACKLOG`），算出来正好是下限那一格；
#:   - 大档取 ⑤b2 用过的那个量（`DRAIN_LARGE_BACKLOG` = 10），算出来明显离开下限，
#:     于是「上限随积压量变大」这件事在两组之间存在一条量得出来的差。
DRAIN_OVERRUN_BACKLOG_SMALL = 0
DRAIN_OVERRUN_BACKLOG_LARGE = 10

#: `ended.phase` 的**取值域**（`draining` / `done`）。
#: 两项都在这份文件里被**真实走过的收尾**观察到：`draining` 与 `done` 由 ① 用同一趟收尾的
#: 两次读数钉住（`{draining, done}` 逐项相等），失败那一档停在 `draining` 由 ③ / ⑧ 咬住 ——
#: 于是「域里只有这两项」这件事有实测依据，不靠一句恒真的 `in ("draining", "done")`。
ENDED_PHASE_DOMAIN = ("draining", "done")

#: `session.json` 的 `state` **取值域**（设计冻结的四档）：四档逐字写死在这里
#: ⑪ 条核结束标记里的 `state` 时按这份取值域断言成员关系。
SESSION_STATE_DOMAIN = ("recording", "draining", "done", "failed")


def _start_service(
    service_process,
    meeting_env,
    plugin: fakes.FakePlugin | None = None,
):
    """起一个真服务子进程：环境变量、假件与 `capture.backend` 由本辅助一处说清。

    **`MEETING_CAPTURE` 必须从子进程环境里摘干净**：它是 `capture.backend` 的环境变量覆盖
    （优先级：环境变量 > `config.json` > 默认），留着它（哪怕是夹具的缺省 `dev`），
    `config.json` 里那条 `capture.backend`（本文件挂假件与选替身用的键）就永远不生效 ——
    假件静默没挂上，收尾三步全落 `skipped`，用例会以「以为在验 A、其实在验 B」的形式蒙混过关。


    摘法是 `extra.pop("MEETING_CAPTURE")`：把这一项从子进程 env 里去掉。写成空串会踩另一个坑 ——
    空字符串不是合法取值，`config.load()` 会把它判成值级非法并以 `EXIT_REJECTED` 拒绝启动。

    没有假件时本用例的 `config.json` 写 `capture.backend = "dev"`（**采集替身**：
    `finalize` / `transcode` 两项能力如实为假 → 收尾第 1、2 步记 `skipped`，
    而 `start` 照常能开起来）；有假件时那一份配置里也补上这个缺省项（假件自己指定
    `capture.backend` 时以假件为准）。
    """
    extra = dict(meeting_env.env)
    extra.pop("MEETING_CAPTURE", None)
    if plugin is not None:
        extra.update(plugin.extra_env)
        plugin.config.setdefault("capture.backend", "dev")
        fakes.write_plugin_config(meeting_env.home, plugin)
    else:
        meeting_env.write_config({"capture.backend": "dev"})
    return service_process(
        home=meeting_env.home,
        capture=None,
        pythonpath=(plugin.pythonpath(helpers.REPO_ROOT) if plugin is not None else None),
        extra_env=extra,
    )


# 本文件的辅助


def _pin_fake(plugin: fakes.FakePlugin) -> None:
    """**看门断言**：假件确实被解析到了。

    写出去的那份模块在被 import 时会往标记文件写一行；少了它说明假件根本没挂上
    （registry 会静默回退替身，收尾三步于是全 `skipped`），用例会以「以为在验 A、
    其实在验 B」的形式蒙混过关。
    """
    wait_until(plugin.module_loaded, timeout=DRAIN_POLL_TIMEOUT_SECONDS)
    assert plugin.module_loaded(), f"假件模块没有被载入：{plugin.annotation_file}"


def _service_log_text(env: Any) -> str:
    return helpers.read_text(env.log_dir / "meeting.log")


def _log_with_all(path: Path, needles: list[str]) -> str | None:
    """等到日志文件里这些片段全都出现，再把整份文本给出来。

    收尾三步的落盘与读文件是两个进程的事：**等到位再断言**比「读完再抱怨」稳
    （文件刚建好那一瞬间读到的是空内容）。
    """
    text = helpers.read_text(path)
    if all(needle in text for needle in needles):
        return text
    return None


#: 场次 log **结束标记**里那一小段固定文案（`SESSION_END_MARKER` 的字面形状）。
#: 别拿「场次结束：」去找标记：`stop` 那一刻落的「场次结束：<id>（目录 …）」也含这四个字，
#: 两者都落在同一份场次 log 里，用宽的那个前缀会把结束标记之前的行也划到后面去。
END_MARKER_TEXT = "；收尾步骤："


def _wait_end_marker(session_dir: str, *, timeout: float = DRAIN_POLL_TIMEOUT_SECONDS) -> str:
    """等到场次 log 里出现**结束标记**再返回那一整份文本。

    「收尾走到终态」与「结束标记已经落盘」是两步：`log` 步把标记直接写进场次 log 的流里，
    而 `finish_drain()` 之后还有一趟写盘与一次提交 —— 直接读文件会撞上「读到早一版」的偶发红。
    """
    path = Path(session_dir) / "meeting.log"

    def probe():
        text = helpers.read_text(path)
        return text if END_MARKER_TEXT in text else None

    return wait_until(probe, timeout=timeout)


def _record_steps(record: dict[str, Any]) -> list[dict[str, Any]]:
    return list((record.get("drain") or {}).get("steps") or [])


def _step_statuses(steps: list[dict[str, Any]]) -> dict[str, str]:
    return {step["id"]: step["status"] for step in steps or []}


def _wait_state(proc: Any, predicate, *, what: str, timeout: float = DRAIN_POLL_TIMEOUT_SECONDS):
    """轮询 `/api/state` 直到 `predicate(快照)` 返回真值（超时显式给出、到点抛错）。"""

    def probe():
        snapshot = proc.snapshot()
        return snapshot if predicate(snapshot) else None

    return wait_until(probe, timeout=timeout)


def _ended_phase(snapshot: dict[str, Any]) -> str | None:
    return (snapshot.get("ended") or {}).get("phase")


def _failed_steps_of(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        step
        for step in (snapshot.get("ended") or {}).get("steps") or []
        if step["status"] == "failed"
    ]


def _drain_evidence(snapshot: dict[str, Any], log_text: str = "") -> str:
    steps = "\n".join(
        f"  {step['id']}: {step['status']} / {step.get('detail')!r} / {step.get('error')!r}"
        for step in (snapshot.get("ended") or {}).get("steps") or []
    )
    resolved = "\n".join(
        line for line in log_text.splitlines() if "后端解析" in line or "收尾" in line
    )
    return f"ended={snapshot.get('ended', {}).get('phase')}\n{steps}\n{resolved}"


def _wait_done(proc: Any, *, timeout: float = DRAIN_POLL_TIMEOUT_SECONDS) -> dict[str, Any]:
    return _wait_state(
        proc, lambda snap: _ended_phase(snap) == "done", what="ended.phase 落到 done", timeout=timeout
    )


def _warm_up_client(proc: Any) -> None:
    """先读一次 `/api/state`，把用例自己那条 HTTP 连接**建好**再进入被测窗口。

    用例的第一次读若落在测量窗口里，被测的那段墙钟就被构造代价盖住 —— 收尾三步几乎瞬间
    开跑，「某一步停在 `running`」这一格会被读成 `done`。预热因此是把观察点挪进窗口的
    必要一步（不是绕过断言：窗口之内的每一次读、每一条断言都照旧）。
    """
    proc.snapshot()


def _wait_any_failed(proc: Any) -> dict[str, Any]:
    """等到**收尾三步全部落定**且其中出现 `failed` 步，再返回那一份快照。

    只等「出现 failed 步」会撞上偶发：`flush` 一落 `failed`，后面的 `transcode` / `log` /
    `post` 还在按顺序登记（失败不中断后续登记），此刻读到的 `log` 可能还停在 `running`。
    等四步都有终态，读到的才是这一趟的最终登记。
    """
    return _wait_state(
        proc,
        lambda snap: _all_steps_settled(snap)
        and any(
            step["status"] == "failed" for step in (snap.get("ended") or {}).get("steps") or []
        ),
        what="收尾出现 failed 步且四步都已有终态",
    )


def _all_steps_settled(snapshot: dict[str, Any]) -> bool:
    """四步都落定了没有。

    **`waiting` 算落定**：第 4 步 `post` 做完该做的（释放会中模型）就停在 `waiting`
    （＝界面上那句"等你动手"）—— 那一格正是收尾跑完时的正常读数。`running` 不算
    （收尾协程还在那一步上）。
    """
    steps = (snapshot.get("ended") or {}).get("steps") or []
    return len(steps) == len(DRAIN_STEP_IDS) and all(
        step["status"] in ("done", "skipped", "failed", "waiting") for step in steps
    )


def _start(proc: Any) -> None:
    # 传进程对象本身（不是 base_url 字符串）：`post_action()` 靠它为 start 预建工作区目录
    started = post_action(proc, "start")
    assert started.status_code == 200, started.text


def _start_and_stop(proc: Any) -> None:
    _start(proc)
    stopped = post_action(proc, "stop")
    assert stopped.status_code == 200, stopped.text


def _session_dir_of_log(log_text: str) -> str:
    """从服务级 log 里取本次场次目录（`场次开始：<id>（目录 <dir>，mode=...` 那一行）。"""
    match = re.search(r"场次开始：.*?（目录 (.+?)，mode=", log_text)
    assert match, f"服务级 log 里没有「场次开始」那一行：\n{log_text}"
    return match.group(1)


def _new_workspace(tmp_path: Path, name: str) -> str:
    """测试层给一次 `start` 预建一个**已存在**的工作区目录（产品没有默认落点、也不建目录，
    `args.dir` 显式给 —— 真实使用是「人从磁盘里挑一个已有目录」，测试与它同一口径）。"""
    d = tmp_path / f"ws-{name}"
    d.mkdir(exist_ok=True)
    return str(d)


def _inprocess_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """进程内那一档的 `ServiceRuntime`（⑤d 条用：不起服务，直接调被断言的对象）。
    与设计的 `dev_runtime` 夹具同一档（家目录隔离 + 临时日志根），差别只在
    **服务级 log 的目录由本用例另给**：同一个用例里要读的 log 落在这个临时目录下。
    服务级日志按临时家目录重建、用完放回原样（走 `logging_setup` 的公开快照入口，
    不读被测模块的私有状态）。
    """
    from meeting import config as config_module
    from meeting import logging_setup
    from meeting.service.runtime import ServiceRuntime

    home = tmp_path / "home-inprocess-runtime"
    home.mkdir(parents=True, exist_ok=True)
    log_root = tmp_path / "logs-inprocess-runtime"
    log_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    previous_handlers = logging_setup.handler_snapshot()
    logging_setup.reset_for_tests()
    logging_setup.setup_service_logging(str(log_root / "logs"))
    try:
        loaded = config_module.load()
    finally:
        logging_setup.restore_handlers(previous_handlers)
    runtime = ServiceRuntime(loaded)
    register_inprocess_runtime(runtime)
    return runtime


#: 本包进程内用例造过的 `ServiceRuntime`（收场由 `tests/test_drain/conftest.py` 的
#: autouse 夹具统一收转写流水线，判据是「任意收集序下泄漏都不出现」）
_INPROCESS_RUNTIMES: list[Any] = []


def register_inprocess_runtime(runtime: Any) -> None:
    _INPROCESS_RUNTIMES.append(runtime)


def stop_registered_transcriber_pipelines() -> None:
    """对本包登记过的每台运行时调一次 `transcriber_backend.stop()`（幂等；空闲毫秒级收工）。

    生产路径上转写后端的 `stop()` 只有 `shutdown()` 一个调用点（`meeting/service/
    runtime_backends.py` 的 `shutdown()`），进程内用例 `start_session()` 起的真转写流水线
    （asr-worker + 两路 asr-reader，daemon）因此会跨文件一直活着：开场那条「进程里一条
    `asr-` 线程都不剩」的线程闸会逐条红。这里在测试层补上汇合点。假件后端
    （`FakeTranscriberBackend`）的 `stop()` 同名在位、原样安全。
    """
    for runtime in _INPROCESS_RUNTIMES:
        backend = getattr(runtime, "transcriber_backend", None)
        stop = getattr(backend, "stop", None)
        if callable(stop):
            stop()
    _INPROCESS_RUNTIMES.clear()


