
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

# ---------------------------------------------------------------------------
# 本文件的具名常量（§3.8 末「测试侧常量」那张表；用例里不留裸秒数）
#
# **取值收口的授权说明（第七轮审查 m3，措辞在下游文件里一字不改地重复）**：
# 「以上取值由指挥者授权收口（原契约表的值见 phase3.md §3.8 末表；红线：单条用例 ≤5s）」
# —— 本文件里被这一条覆盖的常量是 `TEST_SILENCE_SECONDS`、`E2E_CASE_WALL_CLOCK_LIMIT_SECONDS`、
# `DRAIN_BLOCKING_STEP_SECONDS`、`DRAIN_SLOW_STEP_SECONDS`、`DRAIN_SLOW_POST_STEP_SECONDS`；
# 下面各自那一段说明写清了「判据的形状一字未改」与「数值由谁定的」。下游不必再把这几个数
# 当成新契约读。
# ---------------------------------------------------------------------------

#: ⑩ 条「收尾期间可观测」那条窗口的判据：假件把某一步钉在 `running` 上的秒数
#: （经 `fakes.BLOCK_SECONDS_ENV` 交给共享假件 `BlockingThreadPostHook`）。
#:
#: **取值区间为什么仍成立、余量在哪（审查问题 M10 授权缩短后补的说明）**——这一条要的是
#: 「轮询一定能在阻塞结束前读到 `running`」，因此它只需要**大于一次轮询往返**：
#:
#:   - 下界（为什么不能再小）：轮询间隔是 `tests/_helpers.py` 里 `wait_until()` 的
#:     缺省 0.05 秒，而 `wait_until()` 是一次「读 `/api/state`
#:     再看条件」的往返 —— 本机实测一次往返约 1–5 ms，0.6 / 0.05 = **12 次机会**，
#:     再加上 `DRAIN_WINDOW_ENTRY_DELAY_SECONDS`（0.2 秒）先把观察点挪进窗口，
#:     余量是两位数倍；
#:   - 上界（为什么不能再大）：骨架期的总上限是 `DRAIN_BUDGET_MIN_SECONDS` = 30 秒，
#:     这一步的调用由 `_drain_wait()` 包在剩余预算里；0.6 秒远小于 30 秒，于是这一趟收尾
#:     不会被预算截断成 `failed`（余量 29.4 秒）；
#:   - **与「单条用例 ≤ 5 秒」的关系**：0.6 秒就是这条用例墙钟里唯一一笔成规模的等待
#:     （其余是进程起停与几次 HTTP 往返，实测整条 2.1–2.4 秒），因此它**贴的是用户红线的
#:     下界那一侧**，不是「越小越好」——再往下压会开始与一次 HTTP 往返同量级。
DRAIN_BLOCKING_STEP_SECONDS = 0.6

#: ⑩ 条里发完 `stop` 之后先停这么一下再开始轮询：`stop` 一返回就轮询会**抢在收尾之前**
#: （四步几乎瞬间开跑），于是「`post` 停在 `running`」这一格被读成 `done`，
#: 断言会以偶发形式撞上「窗口已过」。这一小段是**把观察点挪进窗口**用的。
#:
#: **区间为什么仍成立（审查问题 M10）**：它是「`stop` 返回 → 第一次读状态」之间的偏置，
#: 需要 **≥ 半个窗口**才把观察点推进窗口里（`DRAIN_BLOCKING_STEP_SECONDS` = 0.6 秒，
#: 偏置 0.2 秒 ≈ 窗口的 1/3），同时要 **< 窗口**（否则第一次读就落在窗口之后、`post` 已
#: 落 `done`）。0.2 秒的余量在对数意义上很宽：它比一次 HTTP 往返大两个数量级、比窗口小 3 倍。
DRAIN_WINDOW_ENTRY_DELAY_SECONDS = 0.2

#: 收尾相关的轮询超时（等 `ended` 落定 / 等某一步到某个状态）
DRAIN_POLL_TIMEOUT_SECONDS = 15

#: ⑤ 条里那一步**可控拖长**的秒数（拖长的是 `flush` 与 `post` 两步；经
#: `fakes.FINALIZE_DELAY_SECONDS_ENV` 交给共享假件 `FakeCaptureBackend`）。
#:
#: **区间为什么仍成立（审查问题 M10）**：这一条要断「被 `_drain_wait()` 包住的那一步真的
#: 等满了它自己声明的时长」，因此拖长量必须**明显大于一次调用往返**（HTTP 与线程池提交
#: 各几毫秒）—— 0.4 / 0.6 秒比它们大两个数量级；同时两步之和必须**明显小于这一趟的
#: 预算**（下限 30 秒），否则收尾会被预算截断（余量 29 秒）。
DRAIN_SLOW_STEP_SECONDS = 0.4

#: ⑤ 条里 `post` 那一步的拖长秒数：与 `flush` 一起把这一趟收尾的实际耗时抬到
#: `DRAIN_DRAG_TOTAL_SECONDS` 以上，于是「这一步真的等满了它自己声明的时长」可判定
DRAIN_SLOW_POST_STEP_SECONDS = 0.6

#: ⑤ 条里两步拖长加起来的下界（`flush` 0.4s + `post` 0.6s；实测还要再多一点）
DRAIN_DRAG_TOTAL_SECONDS = 1.0

#: ⑩ 条在那个窗口里连续读 `/api/state` 的次数与间隔（几次加起来要落在窗口之内）
DRAIN_WINDOW_POLLS = 3
DRAIN_WINDOW_POLL_SECONDS = 0.05

#: **进程内**那两条（等待上限对同步路与协程路一视同仁 / 事件循环不被钉住）用的极小量：
#: 一条 0.6 秒的假步 + 一条 0.02 秒的 deadline，与「超预算 → `DrainLimitExceeded`」一一对应
DRAIN_UNIT_STEP_SECONDS = 0.6
DRAIN_UNIT_BUDGET_SECONDS = 0.02

#: 单条用例的墙钟上限（§3.8 的 `E2E_CASE_WALL_CLOCK_LIMIT_SECONDS`）。
#:
#: **取值与 `tests/conftest.py` 的看门 hook（`CASE_WALL_CLOCK_LIMIT_SECONDS`）同值**：
#: 契约 §3.8 文末那张表原先写 60 秒，用户后来把「每一条用例 setup + call + teardown ≤ 5.0 秒」
#: 定为硬要求并由那道 hook 自动判红 —— 全局 5 秒看门取代了那条 60 秒口径（审查问题 M9 点名的
#: 同名常量两套取值）。本文件这两处 `elapsed <= E2E_CASE_WALL_CLOCK_LIMIT_SECONDS` 因此是真
#: 判据（收尾总时长收在 5 秒之内），不再形同虚设。
E2E_CASE_WALL_CLOCK_LIMIT_SECONDS = 5.0

#: ⑤b2 条里那组**明显不小**的预算输入：`backlog_depth` 与 `segment_timeout`。
#:
#: 它们经 `fakes.BACKLOG_DEPTH_ENV` / `fakes.SEGMENT_TIMEOUT_SECONDS_ENV` 交给子进程里的
#: `FakeTranscriberBackend`（**共享假件的旋钮**，本文件不再自建一个「积压量写死在类里」的
#: 本地假件）。取 `10 × 20s` 是为了让 `compute_drain_budget()` 明显离开下限那一格
#: （`30 + 10 × (20 + 5) = 280`），于是「运行时那条 deadline 就是预算函数算出来的那个数」
#: 与 ⑤ 的下限档分得开。
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
#: 量级与 ⑤b / ⑤c 同档（毫秒到零点几秒），于是「进 `running` 之后取消」这个同步点在
#: 进程内确定可达（0.3 / 0.005 = 60 次机会），整条用例的墙钟也仍在零点几秒。
DRAIN_CANCEL_STEP_SECONDS = 0.3
DRAIN_CANCEL_POLL_SECONDS = 0.005

#: 本场时长「冻结」那一条的复查间隔（秒）：收尾落定之后再读一次，秒数不该再往前走
DRAIN_FROZEN_RECHECK_SECONDS = 0.2

#: ⑤d 条（「等落定」的上限与本次收尾同源）里那个**小预算**（秒）。
#:
#: 它只用来把「等待到点返回」的墙钟压到零点几秒（真现场里这一笔是 280 秒那条预算），
#: 判据一字不改：等待的上限就该是**本次收尾那一趟算出来的预算**。
DRAIN_SETTLE_TINY_BUDGET_SECONDS = 0.2

#: ⑤d 条实测耗时的容差（秒）：轮询间隔（`DRAIN_SETTLE_POLL_SECONDS`）与调度开销落在这里面。
DRAIN_SETTLE_WAIT_TOLERANCE_SECONDS = 0.35

#: ⑤d 第二条里把上限传成当天那笔预算的几倍（于是「按预算等」与「按重算的更短上限等」
#: 在这条用例上分得开，而整条用例的墙钟仍是零点几秒）。
DRAIN_SETTLE_WAIT_MULTIPLE = 2

#: ⑪ 条里「场次 log 摘掉之后不再有新行」的复查间隔（秒）
DRAIN_LOG_CLOSED_RECHECK_SECONDS = 0.2

#: ⑫ 条把静默阈值**显式调小**（写进临时 home 的 `config.json`；**不改代码默认值**）
#:
#: **这是指挥者授权的收口（契约表 3 → 实现 1，判据形状不变、代码默认值未动）**：契约 §3.8 末
#: 「测试侧常量」那张表给 `TEST_SILENCE_SECONDS` 写的取值是 `3`，用户的硬要求「单条用例
#: ≤ 5 秒」优先于方案文字里的等待时长 —— 把这一处恢复成 3 会让第 ⑫ 条（它要等出一次告警
#: 与一次恢复）冲到 5.3 秒以上、直接越过红线。本节点因此保持 `1`，**判据的形状一字未改**
#: （仍然把阈值经 `config.json` 显式写小、仍然断 `duration_seconds >= TEST_SILENCE_SECONDS`），
#: `alerts.py` 的代码默认值（180 秒）也一个字没动。三处同值同注释的文件：
#: `tests/test_drain.py` / `tests/test_e2e_silence_alert.py` / `tests/test_e2e_multi_client.py`。
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
#: 这是那一条用例墙钟里唯一一笔真实等待：它必须在**时钟前移之前**跑完，从而成为
#: 「这一趟确实往前走了一段」的实测依据，同时贴着「单条用例 ≤ 5 秒」的下界那一侧取值
#: （0.6 秒比一次 HTTP 往返大两个数量级，离红线还差 4 秒以上）。
DRAIN_OVERRUN_STEP_SECONDS = 0.6

#: ⑤f 条里把这一趟的钟**前移**多少秒：必须**大于这一档的预算**（下限 30 秒），
#: 否则到期那一刻剩余预算还大于 0、末尾那一步不会被判越界。
#:
#: **它换来的东西**：预算的三个数硬编码、下限 30 秒且不开配置键（§3.3），「真等满 30 秒」与
#: 「单条用例 ≤ 5 秒」无法同时成立；前移这只单调钟（`_drain_wait()` 的秒表与事件循环的计时器
#: 都读它，两处一起拨的口径见 `_drain_fakes.py` 的时钟替身）于是让越界**真的发生**。
#: 用例报告里同时给出**真实墙钟**，读者一眼能看出这一条自己只花掉零点几秒。
DRAIN_OVERRUN_CLOCK_OFFSET_SECONDS = float(DRAIN_BUDGET_MIN_SECONDS + 5)

#: ⑤f / ⑤g 两条里等「收尾进了在飞那一步」的轮询间隔（秒）。
#:
#: 取值 = ⑩ 条那个窗口里的读间隔（`DRAIN_WINDOW_POLL_SECONDS`）：假件进 `running` 之后至少
#: 还有 `DRAIN_OVERRUN_STEP_SECONDS`（0.6 秒）才落定，0.05 秒一探有两位数次机会。
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

#: `ended.phase` 的**取值域**（契约 §3.8 的第 ② 条：取值只许取自 `draining` / `done`）。
#: 两项都在这份文件里被**真实走过的收尾**观察到：`draining` 与 `done` 由 ① 用同一趟收尾的
#: 两次读数钉住（`{draining, done}` 逐项相等），失败那一档停在 `draining` 由 ③ / ⑧ 咬住 ——
#: 于是「域里只有这两项」这件事有实测依据，不靠一句恒真的 `in ("draining", "done")`。
ENDED_PHASE_DOMAIN = ("draining", "done")

#: `session.json` 的 `state` **取值域**（phase2 冻结的四档）：四档逐字写死在这里
#: （「未收尾」的判据原来取自 `meeting/session.py` 的 `UNFINISHED_STATES`；启动扫描已随
#: 默认落点退场，没有别的消费点，这里不再引入中间常量）。
#: ⑪ 条核结束标记里的 `state` 时按这份取值域断言成员关系。
SESSION_STATE_DOMAIN = ("recording", "draining", "done", "failed")


# ---------------------------------------------------------------------------
# 本文件**没有本地假件类**
#
# ③ / ⑤ / ⑤b2 / ⑥ / ⑩ 各档需要的行为（有实现却抛错、某一步可控拖长、启动即完成、某一步
# 钉在 `running`）全部由 `tests/_drain_fakes.py` 的五个共享假件配出来（§3.8：「各自需要的
# 假件都从 `tests/_drain_fakes.py` 取，只 import、不各自定义、不各写一份」）：
#
#   - 选哪个假件 → 既有配置键 `capture.backend` / `post.hook` / `transcriber.backend`；
#   - 那一步的行为参数 → **环境变量旋钮**（`fakes.FINALIZE_DELAY_SECONDS_ENV` 等常量名，
#     不抄裸字符串）。理由与 `fakes.BLOCK_SECONDS_ENV` 那一处相同：`registry` 对后端
#     **无参实例化**，构造参数进不去子进程，值只能经环境变量交给假件。
#
# 本文件经 `fakes.write_knob_plugin()` / `write_slow_drain_plugin()` /
# `write_failing_capture_plugin()` / `write_immediately_returning_post_plugin()` 这几个
# 工厂配现场；**假件模块的源码只有 `_drain_fakes.py` 一份**（生成给子进程的那份模块就由
# 那一份类体拼出来），本文件不再 `inspect.getsource()` 抄第二遍。
# ---------------------------------------------------------------------------


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

    **这一步摘除是必要的、不是漂移**（审查问题 M13 点名的读法）：两条子进程路径
    （`tests/test_shutdown_signal.py` 那一支与本文件这一支）**同源**，靠的都是「把这一项从
    子进程 env 里去掉、让 `config.json` 的 `capture.backend` 说了算」这一条 —— 它要的是
    **不静默覆盖配置**（§2.4 的「两条子进程路径的环境变量必须同源」）。下一位审查者不必
    再把它读成两处各写一套。

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


# ---------------------------------------------------------------------------
# 本文件的辅助
# ---------------------------------------------------------------------------


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
    """盘上 `session.json` 的 `drain.steps`。"""
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
    """快照里全部 `failed` 的收尾步骤。"""
    return [
        step
        for step in (snapshot.get("ended") or {}).get("steps") or []
        if step["status"] == "failed"
    ]


def _drain_evidence(snapshot: dict[str, Any], log_text: str = "") -> str:
    """失败现场（断言里带上它，省得只看一句话猜原因）。"""
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

    本机新建一个 `httpx.Client` 要 0.83 秒（Windows 上 SSL 上下文的初始化，
    `tests/_helpers.py` 的 `local_get()` 上有详细说明）。用例的第一次读若落在测量窗口里，
    被测的那段墙钟就被这笔构造代价盖住 —— 收尾三步几乎瞬间开跑，「某一步停在 `running`」
    这一格会被读成 `done`。预热因此是把观察点挪进窗口的必要一步（不是绕过断言：
    窗口之内的每一次读、每一条断言都照旧）。
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

    **`waiting` 算落定**：第 4 步 `post` 按「结束录音」之后不自动拉起会后（R26），
    它做完该做的（释放会中模型）就停在 `waiting`（＝界面上那句"等你动手"）——
    那一格正是收尾跑完时的正常读数。`running` 不算（收尾协程还在那一步上）。
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
    """`start` → `stop`（两处都断言 200）。"""
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
    与 phase2 的 `dev_runtime` 夹具同一档（家目录隔离 + 临时日志根），差别只在
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
    """进程内助手每造一台运行时就登记一台，收场好按单收转写流水线（见下面那个收场函数）。"""
    _INPROCESS_RUNTIMES.append(runtime)


def stop_registered_transcriber_pipelines() -> None:
    """对本包登记过的每台运行时调一次 `transcriber_backend.stop()`（幂等；空闲毫秒级收工）。

    生产路径上转写后端的 `stop()` 只有 `shutdown()` 一个调用点（收尾编排不停转写，
    `meeting/service/runtime_backends.py` 的 `shutdown()`），进程内用例 `start_session()`
    起的真转写流水线（asr-worker + 两路 asr-reader，daemon）因此会跨文件一直活着：
    收集序把本包排到 `tests/test_asr_pipeline.py` 之前时，那边开场那条「进程里一条
    `asr-` 线程都不剩」的线程闸每条用例都红（改名序 `tests/drain` 实测 29 passed
    44 errors）。这里在测试层补上汇合点，与 `tests/test_transcode_job.py` 的
    `_join_transcriber_pipelines` 同一手法。假件后端（`FakeTranscriberBackend`）的
    `stop()` 同名在位、原样安全。
    """
    for runtime in _INPROCESS_RUNTIMES:
        backend = getattr(runtime, "transcriber_backend", None)
        stop = getattr(backend, "stop", None)
        if callable(stop):
            stop()
    _INPROCESS_RUNTIMES.clear()


