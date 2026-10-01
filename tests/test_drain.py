"""收尾四步、自适应预算与失败出路的黑盒用例（phase3 §5.1 / §3.8 的 12 条 + 审查修复追加的几条）。

**用例计数（照实登记，数目可核对）**

  - **契约点名的 12 个函数，逐字在位、各自独立**（§3.8 的 `tests/test_drain.py` 行逐条列出；
    正文与 `phase3.md` 的用例数据此对齐）：`test_drain_step_order_and_terminal_states`（①）/
    `test_drain_reaches_done_when_no_step_failed`（②）/
    `test_skipped_and_failed_follow_registry_resolution_without_exception_swallowing`（③）/
    `test_drain_budget_formula_and_bounds`（④）/ `test_drain_wait_limit_follows_backlog_budget`（⑤）/
    `test_post_step_completes_on_launch_return`（⑥）/ `test_drain_logs_budget_and_elapsed`（⑦）/
    `test_failed_step_keeps_service_alive_and_start_conflicts`（⑧）/
    `test_failed_step_records_both_ways_out`（⑨）/ `test_state_stays_readable_while_draining`（⑩）/
    `test_session_log_attached_then_detached_with_end_marker`（⑪）/
    `test_all_log_event_categories_findable_before_end_marker`（⑫）；
  - **审查修复追加的 6 个函数**（名字与上面 12 个不相撞、也不顶替其中任何一条；
    出处逐条写在各自 docstring 里）：
    `test_drain_budget_follows_a_large_backlog`（审查问题 M3 / M4：运行时那条 deadline 与预算
    函数的输出逐值相等）、`test_drain_wait_wraps_every_call_in_the_remaining_budget`（C1：同步路
    与协程路都受剩余预算约束）、`test_drain_wait_upper_bound_tracks_the_passed_deadline`
    （M3 / M4：上限跟着传进来的预算走）、`test_drain_wait_does_not_pin_the_event_loop`
    （收尾期间事件循环不被钉住）、`test_drain_settle_wait_shares_the_drain_budget`（M5：等待上限
    与本次收尾同源）、`test_drain_settle_wait_honours_the_shared_budget`（M5：等待真的按那条上限
    等）、`test_drain_default_budget_inputs_when_transcriber_is_absent`（M3：`DRAIN_DEFAULT_BACKLOG`
    / `DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS` 这两个点名常量真的被走通）；
  - **第六轮审查再追加的 2 个函数**（同样不与上面任何一条相撞）：原文那一句「追加的 6 个函数」
    的实际条数是 7（上面已逐条列全），这里只登记本轮新增的两条 ——
    `test_step_over_budget_lands_failed_and_service_survives`（**M5**：某一步**真的越过预算**
    → 该步 `failed` + `error` 里有「超出收尾预算」与实测耗时 + 盘上 `state == "failed"` +
    服务不自杀；**进程级**，用一只按标记前移的时钟把 30 秒的下限压到零点几秒）、
    `test_drain_whole_pass_upper_bound_grows_with_the_backlog`（**M3**：同一个拖长假步配两组
    `backlog`，可等待的上限随 `compute_drain_budget()` 的输出变大；**进程内**）。
    **本轮之后的实测收集数**：`python -m pytest tests/test_drain.py --collect-only -q` = **23 条**
    （契约点名的 12 个函数一条不少、各自独立；其余为历次审查修复追加的条目，
    `test_drain_wait_wraps_every_call_in_the_remaining_budget` 带两个参数档占 2 条）。
  - **第七轮审查再追加的 3 个函数**（同样不与上面任何一条相撞，各自独立）：
    `test_drain_steps_share_one_deadline_with_shrinking_remaining_budget`（**C1**：四步共享
    同一条 deadline —— 越界那一步落 `failed`，四步各自开跑时的剩余预算逐级变小、读到的截止
    时刻逐值相等；**进程内**，预算函数被换成一个固定的小值）、
    `test_cancelled_drain_keeps_draining_even_with_a_failed_step_before_it`（**C2**：强制打断档
    里前面已有 `failed` 步时，取消路径的落盘仍把 `state` 钉在 `draining`；**进程内**）、
    `test_disk_has_no_ended_at_while_draining_only_at_the_terminal_version`（**m8**：收尾进行中
    盘上没有 `ended_at`、终态那一版才有；**进程内**，逐版检查这一趟写下去的每一版 `session.json`）。
    **本文件当前实测收集数**：`python -m pytest tests/test_drain.py --collect-only -q` = **26 条**
    （契约点名的 12 个函数一条不少、各自独立；其余为历次审查修复追加的条目，
    `test_drain_wait_wraps_every_call_in_the_remaining_budget` 带两个参数档占 2 条）。
  - **第八轮审查再追加的 5 个函数**（同样不与上面任何一条相撞，各自独立）：
    `test_sync_callable_without_invoke_true_fails_fast_and_keeps_the_loop_running`（**m1**：
    同步 callable 忘传 `invoke=True` 时在**调用之前**就抛 `TypeError` —— 那个同步实现一次都
    没被调到、墙钟远小于它自己的时长；**进程内**）、
    `test_step_over_budget_still_leaves_later_steps_room`（**m1 的进程级形态**：走真的
    `drain()` 四步，第一步丢掉 `invoke=True` → 那一步 `failed`、事件循环一直在转、
    **后续步照常推到终态**；**进程内**）、
    `test_remaining_budget_and_drain_wait_reject_a_missing_deadline`（**m2**：
    `_remaining_budget(None)` 与 `_drain_wait(..., deadline=None)` 显式抛 `ValueError`，
    文案点名 `compute_drain_budget()`，并且**在开跑之前**抛；**进程内**）、
    `test_log_step_lands_failed_when_the_session_log_write_reports_a_problem`（**m3**：场次日志
    写失败 → `log` 步 `failed`、`error` 带如实文案与问题原文、收尾停在 `draining`、其余步
    照常推进；**进程内**，`write_session_end_marker()` 换成返回非空 problem 的实现）、
    `test_later_steps_keep_a_worker_after_an_earlier_step_is_abandoned`（**m7**：前一步越过窗口
    被放弃之后，`log` 那一步仍在自己的最小预算里落定并写盘；**进程内**，一条闸门占住前一步
    的工作线程）。
    **本文件当前实测收集数**：`python -m pytest tests/test_drain.py --collect-only -q` = **31 条**
    （契约点名的 12 个函数一条不少、各自独立；其余为历次审查修复追加的条目，
    `test_drain_wait_wraps_every_call_in_the_remaining_budget` 带两个参数档占 2 条）。

**测试层级（本文件写死的那一种）**：

  - 凡**通过服务可观测**的判据（① ② ③ ⑦ ⑧ ⑨ ⑩ ⑪ ⑫）统一走**真服务子进程**，一律复用
    phase2 的 `service_process` 夹具与 `tests/_service.py` 的 `ServiceProcess` ——
    **本文件不另起一套子进程管理、不自带子进程起停**；
  - **假件一律从 `tests/_drain_fakes.py` 取，本文件只 import、不自行定义、不各写一份**
    （§3.8 的共享通道与命名分工）：③ 的「有实现却抛错」、⑤ / ⑤b2 的「某一步可控拖长」、
    ⑥ 的「启动即完成」、⑩ 的「某一步钉在 `running`」全部由那五个共享假件配出来 ——
    行为参数经**环境变量旋钮**交进子进程（`FINALIZE_ERROR_ENV` / `FINALIZE_DELAY_SECONDS_ENV` /
    `LAUNCH_DELAY_SECONDS_ENV` / `LAUNCH_THREAD_SECONDS_ENV` / `BLOCK_SECONDS_ENV` /
    `BACKLOG_DEPTH_ENV` / `SEGMENT_TIMEOUT_SECONDS_ENV`；旋钮的说明在 `_drain_fakes.py` 里），
    选哪个假件经**既有配置键** `capture.backend` / `post.hook` / `transcriber.backend`。
    **本文件里没有一个本地假件类**，也没有第四份假件源码；
  - 假件**写进临时目录、经 `PYTHONPATH`（= 仓库根 + 该临时目录）**配进子进程，子进程侧的断言
    一律读**临时目录里的日志文件**（`<场次目录>/meeting.log` 与服务级 `meeting.log`）——
    **本文件不使用 `caplog`**（`caplog` 只抓当前进程的日志，与真服务子进程的日志不是同一份，
    二者混用会让断言读不到东西）；
  - 只有**纯函数口径**的断言留在进程内（④ 的预算公式与边界、⑤b 对 `_drain_wait()` 等待上限
    本身的两条、⑤h 的四步共享 deadline、⑤i 的强制打断落盘、⑤j 的盘上 `ended_at`、
    M2 的取消记账、M3 的缺省预算输入），那些断言不起服务、直接调用被断言的对象。

**「单条用例 ≤ 5 秒」怎么落（用户硬要求）**：契约点名的 12 个函数各自独立，每个只做自己那一遍
必需的现场操作（一次 `start` + `stop` 的建场-收尾路径、读一次 log 文件），自造的等待常量贴着
下界取（见下面每个常量上的说明），看门由 `tests/conftest.py` 的耗时 hook 自动判红。
**⑤b 那两条**把「超预算 → `DrainLimitExceeded`」与「阻塞型同步步不钉住事件循环」搬到
**进程内可注入的小 deadline** 上（0.02 秒），于是墙钟与断言语义两不误。

**等到某条件一律带显式超时**：所有「等到某条件」都走 `tests/_service.py` 的 `wait_until()`，
超时值取本文件下面的具名常量，**不留裸秒数**。

**每一路健康迁移与告警起止由谁造出来**：由 `MEETING_CAPTURE=dev` 的两路合成电平与
`dev_inject` 三档故障在**真服务子进程**里造出来（⑫ 条据此断言）；「收尾每一步的开始 / 结束 /
耗时与 `budget` / `backlog`」由 phase3 的编排在同一个子进程里落；③ 的「有实现却抛错」与
⑤ 的「某一步可控拖长」由注入子进程的共享假件（`FakeCaptureBackend` / `FakePostHook`）
造出来。**「服务级 log」与「场次 log」是两个不同的文件**：⑫ 条断的是**场次 log**
（`<场次目录>/meeting.log`）；服务级 log 那半边由 `tests/test_shutdown_signal.py` 第 ⑤ 条负责。
"""

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
from tests._service import post_action, read_session_json, wait_until

#: 慢档标记：`pytest.mark.slow` 的归位判据与实测数据见 `docs/service.md` 第 7 章。
pytestmark = pytest.mark.slow

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
#:   - 下界（为什么不能再小）：轮询间隔是 `tests/_service.py` 的
#:     `WAIT_POLL_INTERVAL_SECONDS` = 0.05 秒，而 `wait_until()` 是一次「读 `/api/state`
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

#: ⑤e 条（取消路径真的经 `cancelled=True` 记账）里在飞那一步的阻塞时长与轮询间隔（秒）。
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
    假件静默没挂上，收尾四步全落 `skipped`，用例会以「以为在验 A、其实在验 B」的形式蒙混过关。

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
    （registry 会静默回退替身，收尾四步于是全 `skipped`），用例会以「以为在验 A、
    其实在验 B」的形式蒙混过关。
    """
    wait_until(plugin.module_loaded, timeout=DRAIN_POLL_TIMEOUT_SECONDS)
    assert plugin.module_loaded(), f"假件模块没有被载入：{plugin.annotation_file}"


def _service_log_text(env: Any) -> str:
    return helpers.read_text(env.log_dir / "meeting.log")


def _log_with_all(path: Path, needles: list[str]) -> str | None:
    """等到日志文件里这些片段全都出现，再把整份文本给出来。

    收尾四步的落盘与读文件是两个进程的事：**等到位再断言**比「读完再抱怨」稳
    （文件刚建好那一瞬间读到的是空内容）。
    """
    text = helpers.read_text(path)
    if all(needle in text for needle in needles):
        return text
    return None


#: 场次 log **结束标记**里那一小段固定文案（`SESSION_END_MARKER` 的字面形状）。
#: 别拿「场次结束：」去找标记：`stop` 那一刻落的「场次结束：<id>（目录 …）」也含这四个字，
#: 两者都落在同一份场次 log 里，用宽的那个前缀会把结束标记之前的行也划到后面去。
END_MARKER_TEXT = "；收尾四步："


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
    被测的那段墙钟就被这笔构造代价盖住 —— 收尾四步几乎瞬间开跑，「某一步停在 `running`」
    这一格会被读成 `done`。预热因此是把观察点挪进窗口的必要一步（不是绕过断言：
    窗口之内的每一次读、每一条断言都照旧）。
    """
    proc.snapshot()


def _wait_any_failed(proc: Any) -> dict[str, Any]:
    """等到**收尾四步全部落定**且其中出现 `failed` 步，再返回那一份快照。

    只等「出现 failed 步」会撞上偶发：`flush` 一落 `failed`，后面的 `transcode` / `log` /
    `post` 还在按顺序记账（失败不中断后续记账），此刻读到的 `log` 可能还停在 `running`。
    等四步都有终态，读到的才是这一趟的最终记账。
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
    steps = (snapshot.get("ended") or {}).get("steps") or []
    return len(steps) == len(DRAIN_STEP_IDS) and all(
        step["status"] in ("done", "skipped", "failed") for step in steps
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
    return ServiceRuntime(loaded)


# ---------------------------------------------------------------------------
# ① 四步顺序与终态
# ---------------------------------------------------------------------------


def test_drain_step_order_and_terminal_states(service_process, meeting_env):
    """`stop` 后 `ended.phase` 经 `draining` 到 `done`；四步顺序与终态照契约。

    **`draining` 这一格在盘上与状态接口上都真的被观察到、也真的被断言**（审查问题 C2）：
    骨架构建下四步几乎瞬间跑完，靠抢时间窗口去读 `draining` 会 flaky —— 这里用**在工作线程里
    睡 `DRAIN_BLOCKING_STEP_SECONDS`** 的假会后钩子（⑩ 条造同一扇窗口用的同一个假件）把第 4 步
    钉在 `running` 上，收尾因此在这 `DRAIN_BLOCKING_STEP_SECONDS` 秒里稳稳停在 `draining`：
    轮询到 `ended.phase == "draining"`、至少一步还没落定、盘上那一版同时写着 `state == "draining"`，
    **再**等它落定成 `done`。
    本用例末尾把那两次读到的相位与 `ENDED_PHASE_DOMAIN` 逐项对上 —— `draining` 与 `done`
    两格都由这一趟收尾自己走过。

    采集侧是 dev 替身（`config.json` 里 `capture.backend = "dev"`，**非产品路径**）：
    它不写真实音频，`finalize` / `transcode` 两项能力如实为假，因此 `flush` / `transcode`
    两步记 `skipped`；第 4 步 `post` 由假钩子接住、落 `done`；`log` 这一格在任何构建里
    都有真实实现 → `done`。四步 `status` 全部收在 `DRAIN_STEP_STATUS` 的 `done` / `skipped`
    两个成员上。
    """
    plugin = fakes.write_fake_module(meeting_env.home)
    plugin.config["capture.backend"] = "dev"
    plugin.config["post.hook"] = plugin.module("BlockingThreadPostHook")
    plugin.extra_env[fakes.BLOCK_SECONDS_ENV] = str(float(DRAIN_BLOCKING_STEP_SECONDS))
    fakes.write_plugin_config(meeting_env.home, plugin)

    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _pin_fake(plugin)
    # 把用例自己那条 HTTP 连接先建好：观察点才落得进收尾那扇窗口（见 `_warm_up_client`）
    _warm_up_client(proc)
    _start(proc)

    assert post_action(proc.base_url, "stop").status_code == 200

    def draining_with_unsettled_steps(snap):
        """收尾停在 `draining`：至少一步还没落定，且盘上那一版也写着 `draining`。"""
        if _ended_phase(snap) != "draining" or _all_steps_settled(snap):
            return None
        record = read_session_json(snap["session"]["dir"])
        return snap if record is not None and record["state"] == "draining" else None

    during = _wait_state(
        proc,
        draining_with_unsettled_steps,
        what="收尾停在 draining 且至少一步还没落定",
    )
    assert _ended_phase(during) == "draining"
    assert during["top_state"] == "ended"
    assert not _all_steps_settled(during), _drain_evidence(
        during, _service_log_text(meeting_env)
    )
    # 窗口里第 4 步正被假钩子钉住（这条断言只是把「还没落定的那一步是谁」说清楚，
    # 它跟着 `draining_with_unsettled_steps` 一起成立：窗口期一定落在 post 那一步上）
    assert _step_statuses(during["ended"]["steps"])["post"] in ("waiting", "running")
    assert read_session_json(during["session"]["dir"])["state"] == "draining"

    # 窗口关上之后才走到终态：`done`
    snapshot = _wait_done(proc)
    ended = snapshot["ended"]
    assert ended["phase"] == "done"
    assert [step["id"] for step in ended["steps"]] == list(DRAIN_STEP_IDS)
    statuses = [step["status"] for step in ended["steps"]]
    assert "failed" not in statuses
    assert _step_statuses(ended["steps"])["log"] == "done"

    # 「`draining` → `done`」这半句的实测依据：这一趟收尾先后读到的两个相位**正好是取值域的
    # 那两项**（少一项或冒出第三项，这一行当场红）
    assert {_ended_phase(during), _ended_phase(snapshot)} == set(ENDED_PHASE_DOMAIN)

    # 收尾终态不回 `idle`：`top_state` 停在 `ended`、`session` 保留（本场时长冻结）
    assert snapshot["top_state"] == "ended"
    assert snapshot["session"] is not None
    assert snapshot["session"]["frozen"] is True


# ---------------------------------------------------------------------------
# ② `done` 判据
# ---------------------------------------------------------------------------


def test_drain_reaches_done_when_no_step_failed(service_process, meeting_env):
    """四步全落在 `done` / `skipped` → `ended.phase == "done"`；`top_state` 停在 `ended`。

    同一趟顺带核盘上那一版（`state` / `ended_at` / `interrupted` / `drain.steps`）与「本场时长
    冻结」（收尾之后 `elapsed_seconds` 与 `frozen` 都不再变）。

    「`budget` 与每一步的耗时逐条进 log」这一类口径归**第 ⑦ 条**
    （`test_drain_logs_budget_and_elapsed`）：契约的 ② 与 ⑦ 是两条独立口径，各占一个函数名，
    读 log 那一段因此写在 ⑦ 里按它自己的口径走（两条用例各起一个子进程、各自只做自己那一遍
    必需的现场操作 —— 这样两条的墙钟都留得住余量）。

    `ended.phase` 的取值域（只许取自 `draining` / `done`）由 ① 用同一趟收尾里真实走过的两格
    钉住；失败那一档停在 `draining` 由 ③ 与 ⑧ 各自咬住。
    """
    proc = _start_service(service_process, meeting_env)
    proc.wait_ready()
    _start_and_stop(proc)

    snapshot = _wait_done(proc)
    ended = snapshot["ended"]
    assert all(step["status"] in ("done", "skipped") for step in ended["steps"])
    # 本档落在取值域里 `done` 那一格；另一格（失败后停在 `draining`）与整个取值域
    # 由 ① / ③ / ⑧ 三条各自用实测现场咬住（`ENDED_PHASE_DOMAIN` 上写着出处）
    assert ended["phase"] == "done"

    assert snapshot["top_state"] == "ended"
    assert snapshot["session"] is not None
    frozen_elapsed = snapshot["session"]["elapsed_seconds"]
    frozen_frozen = snapshot["session"]["frozen"]
    time.sleep(DRAIN_FROZEN_RECHECK_SECONDS)  # 本场时长冻结：再读一次不该变大
    reread = proc.snapshot()["session"]
    assert reread["elapsed_seconds"] == frozen_elapsed
    assert reread["frozen"] is frozen_frozen is True

    record = read_session_json(snapshot["session"]["dir"])
    assert record is not None
    assert record["state"] == "done"
    assert record["ended_at"]
    assert record["interrupted"] is False
    assert [step["id"] for step in _record_steps(record)] == list(DRAIN_STEP_IDS)


# ---------------------------------------------------------------------------
# ③ `skipped` vs `failed` 的边界
# ---------------------------------------------------------------------------


def test_skipped_and_failed_follow_registry_resolution_without_exception_swallowing(
    service_process, meeting_env
):
    """沿用 registry 的解析结论：**缺席记 `skipped`、有实现却抛错记 `failed`**。

    行为断言证明「异常吞成 `skipped`」这条路不在实现里：给在场实现注入一个非预期异常
    （`finalize()` 抛 `OSError`），那一步必须落到 `failed`；同一趟里采集假件带着
    `transcode` 能力，第 2 步照常真的跑完、落 `done`（「缺席记 `skipped`」那一档由
    替身构建的用例钉住，如 `test_state_machine.py` 的 dev 替身那一条）。
    """
    plugin = fakes.write_failing_capture_plugin(meeting_env.home)
    fakes.write_plugin_config(meeting_env.home, plugin)

    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _pin_fake(plugin)
    _start_and_stop(proc)

    snapshot = _wait_any_failed(proc)
    statuses = _step_statuses(snapshot["ended"]["steps"])
    detail = _service_log_text(meeting_env)
    assert statuses["flush"] == "failed", _drain_evidence(snapshot, detail)
    assert statuses["transcode"] == "done"  # 采集假件带 transcode 能力 → 第 2 步真的跑完
    assert statuses["log"] == "done"
    flush = next(step for step in snapshot["ended"]["steps"] if step["id"] == "flush")
    assert flush["error"] and "OSError" in flush["error"]
    assert snapshot["ended"]["phase"] == "draining"


# ---------------------------------------------------------------------------
# ④ 预算公式与边界（纯函数口径：不起服务）
# ---------------------------------------------------------------------------


def test_drain_budget_formula_and_bounds():
    """`budget = 30 + backlog × (segment_timeout + 5)`，下限 30、封顶 300。

    五档边界逐条断言；V1 那个写死的 10 秒在这里根本不存在（下限就是 30 秒）。
    """
    assert DRAIN_BUDGET_MIN_SECONDS == 30
    assert DRAIN_BUDGET_MAX_SECONDS == 300
    assert compute_drain_budget(0, 20) == 30
    assert compute_drain_budget(1, 20) == 55
    assert compute_drain_budget(10, 20) == 280
    assert compute_drain_budget(100, 20) == 300
    assert compute_drain_budget(0, 0) == 30
    # 下限与封顶之间单调：加积压就一定变大
    assert compute_drain_budget(2, 20) < compute_drain_budget(3, 20)


# ---------------------------------------------------------------------------
# ⑤ 等待上限来自预算函数（行为口径）
# ---------------------------------------------------------------------------


def test_drain_wait_limit_follows_backlog_budget(service_process, meeting_env):
    """一趟收尾的等待上限来自 `compute_drain_budget()`。

    行为口径，与 `_drain_wait()` 的包装形态一一对应：拖长落在四步里的 `flush` 与 `post`
    两步（假采集后端的 `finalize()` 与假会后钩子的 `launch()`），积压量与段长仍由
    **假转写后端**报。

    **两件事一起断**：被 `_drain_wait()` 包住的那两步都真的等满了它们自己声明的时长
    （实测耗时确实抬到了拖长量之上），且这一趟收尾的总时长不超过预算函数算出的总上限
    （四步共享同一条 deadline，另加 `log` 步那条最小预算）。
    """
    plugin = fakes.write_slow_drain_plugin(
        meeting_env.home,
        capture_delay_seconds=DRAIN_SLOW_STEP_SECONDS,
        post_delay_seconds=DRAIN_SLOW_POST_STEP_SECONDS,
    )
    fakes.write_plugin_config(meeting_env.home, plugin)

    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _pin_fake(plugin)

    backlog, segment_timeout = 0, 20.0  # `FakeTranscriberBackend` 的缺省（没给旋钮就是它）
    budget = compute_drain_budget(backlog, segment_timeout)
    assert budget == DRAIN_BUDGET_MIN_SECONDS

    began = time.monotonic()
    _start_and_stop(proc)
    snapshot = _wait_done(proc)
    elapsed = time.monotonic() - began

    statuses = _step_statuses(snapshot["ended"]["steps"])
    # 拖长的是 `flush` 与 `post` 两步；`transcode` 在这一趟里也有真实实现（假采集后端
    # 带着 transcode 能力），于是它跑到 `done`（替身构建下它是 `skipped`，两档都算落定）
    assert statuses["flush"] == "done", snapshot["ended"]
    assert statuses["post"] == "done", snapshot["ended"]
    assert statuses["transcode"] in ("done", "skipped")
    assert elapsed >= DRAIN_DRAG_TOTAL_SECONDS, f"这两步该真的等满（实测 {elapsed:.2f}s）"
    upper = budget + DRAIN_LOG_MIN_BUDGET_SECONDS
    assert elapsed <= upper, f"一趟收尾 {elapsed:.2f}s 超出预算上限 {upper}s（deadline 没生效？）"
    assert elapsed <= E2E_CASE_WALL_CLOCK_LIMIT_SECONDS
    # **这一档的上限不是恒真式吗？**：本档的 `budget` 就是下限 30 秒，因此上面那条上界读起来
    # 宽（30 + 5 = 35 秒）。它在这里只承担「总时长确实收在预算里」这半边；「上限随积压量变化」
    # 那半边（审查问题 M3 / M4）由下面两条盯住 —— ⑤b2 用封顶那一档的预算 + 进 log 的 `budget`
    # 值断言「运行时用的是预算函数算出来的那条 deadline」，⑤c 用两组小 deadline 断言
    # 「上限跟着传进来的预算走」。三条各管一段，没有哪一条是唯一的防线。

    # 预算的输入与结果逐条进 log
    log_text = _service_log_text(meeting_env)
    assert f"backlog={backlog}" in log_text
    assert f"segment_timeout={segment_timeout}" in log_text
    assert "budget=" in log_text


# ---------------------------------------------------------------------------
# ⑤b2 预算换一组输入时，运行时用的那条 deadline 与预算函数的输出逐值相等（**进程级**）
# ---------------------------------------------------------------------------


def test_drain_budget_follows_a_large_backlog(service_process, meeting_env):
    """积压量明显变大时，**运行时用的那条 deadline 就是预算函数算出来的那一个数**（M3 / M4）。

    取一组与 ⑤ 明显不同的输入（假转写后端报 `backlog = 10` / `segment_timeout = 20`），
    于是「一趟收尾的上限」不再是 30 秒那条下限，而是公式算出来的那个数：

      - 服务级 log 里 `收尾开始：...` 那一行落下的 `backlog` / `segment_timeout` / `budget`
        三个值，与 `compute_drain_budget()` 对同一组输入的输出**逐值相等** —— 上限来自预算函数
        这条链因此是实测的；
      - 这一趟仍然在几秒之内落定（假件是可控拖长，不会真等满那条上限），且总时长收在
        「预算 + `log` 步最小预算」之内。

    「上限随预算变化」的另一半由 ⑤c 在进程内用两组小 deadline 直接断言（那边能把
    「上限跟着传进来的预算走」量到毫秒级）。
    """
    plugin = fakes.write_slow_drain_plugin(
        meeting_env.home,
        capture_delay_seconds=DRAIN_SLOW_STEP_SECONDS,
        post_delay_seconds=DRAIN_SLOW_POST_STEP_SECONDS,
        backlog_depth=DRAIN_LARGE_BACKLOG,
        segment_timeout=DRAIN_LARGE_SEGMENT_TIMEOUT_SECONDS,
    )
    fakes.write_plugin_config(meeting_env.home, plugin)

    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _pin_fake(plugin)

    backlog = DRAIN_LARGE_BACKLOG
    segment_timeout = DRAIN_LARGE_SEGMENT_TIMEOUT_SECONDS
    budget = compute_drain_budget(backlog, segment_timeout)
    # 这一组输入算出来的上限明显离开下限那一档（否则这一条与 ⑤ 验的是同一件事）
    assert budget > DRAIN_BUDGET_MIN_SECONDS
    assert budget == (
        DRAIN_BUDGET_MIN_SECONDS
        + backlog * (segment_timeout + DRAIN_BUDGET_SEGMENT_OVERHEAD_SECONDS)
    )

    began = time.monotonic()
    _start_and_stop(proc)
    snapshot = _wait_done(proc)
    elapsed = time.monotonic() - began

    statuses = _step_statuses(snapshot["ended"]["steps"])
    assert statuses["log"] == "done", snapshot["ended"]
    assert set(statuses.values()) <= {"done", "skipped"}

    # 运行时实际用的那条 deadline：log 里落下的三个值与预算函数的输出逐值相等
    log_text = _service_log_text(meeting_env)
    assert f"backlog={backlog}" in log_text, log_text[-1500:]
    assert f"segment_timeout={segment_timeout}s" in log_text, log_text[-1500:]
    logged = re.search(r"budget=([\d.]+)s", log_text)
    assert logged, f"服务级 log 里没有落预算结果：{log_text[-1500:]}"
    assert float(logged.group(1)) == float(budget), (
        f"预算函数算出 {budget}s，log 里落下的却是 {logged.group(1)}s —— "
        "运行时用的那条 deadline 与预算函数的输出对不上"
    )

    # 拖长是可控的：这一趟仍在几秒内落定，且收在「预算 + log 步最小预算」之内
    upper = budget + DRAIN_LOG_MIN_BUDGET_SECONDS
    assert elapsed <= upper, f"一趟收尾 {elapsed:.2f}s 超出预算上限 {upper}s（deadline 没生效？）"
    assert elapsed <= E2E_CASE_WALL_CLOCK_LIMIT_SECONDS


# ---------------------------------------------------------------------------
# ⑤f **某一步真的越过预算 → 该步 `failed`**（进程级，审查问题 M5）
#
# 契约 §3.8 第 ⑤ 条把这一档点了名（「…或者**落到** `failed`（越过上限，另起一条用例造）」），
# §3.3 给出 `error` 的原文形状（「超出收尾预算（budget=Ns，实测耗时 Ms）」），§4 落点表指着
# `meeting/service/runtime.py` 的 `_drain_wait()`。在此之前**没有任何用例把这一步走到过**：
# ⑤ 走的是下限档 30 秒的宽式上界（那一条只说明「收得住」），越界那一档一条都没有。
#
# **这一条判的是什么（口径如实写清，审查问题 C1 点名的读法）**：越界**由时钟前移造出来**
# （手法见下一段），它判的是「**预算耗尽 → 该步 `failed` + `error` 文案**」这件事本身。
# **它不判预算从哪里来** —— 越界能发生，靠的是注入的时钟替身把子进程里读的单调钟往前拨，
# 与「这一趟的上限是 `compute_drain_budget()` 算出来的那个数」无关：把四处调用点改成
# `deadline=None`（每步各拿一份下限），这一条**照旧是绿的**（那处写歪因此由 ⑤h 咬住 ——
# 它按行为口径断言「四步共享同一条 deadline、越界那一步落 `failed`」，见下面 ⑤h 那一段）。
#
# **预算下限 30 秒与「单条用例 ≤ 5 秒」怎么同时成立**：预算的三个数硬编码、不开配置键
# （§3.3），真等满 30 秒就必然越过红线。这一条因此把这一趟的**时钟往前拨**（可控手段）：
# `post` 那一步的假钩子写下偏移标记，子进程里被注入的时钟替身
# （`tests/_drain_fakes.py` 的 `write_clock_offset_shim()`）随即把 `meeting.service.runtime`
# 与事件循环（`asyncio.base_events`）两处读的单调钟一起前移 —— 两处**必须一起拨**：上限由
# 运行时那只秒表量，而 `wait_for` 到点靠事件循环自己的计时器，只拨一处的话越界不会发生。
# 这一趟的 deadline 因此真的过期，末尾 `post` 那一步的调用被 `_drain_wait()` 判成越界。
# **预算函数、`_drain_wait()` 的判据、`error` 的原文都一个字没动**：越界仍由 `asyncio.wait_for`
# 与它自己的秒表判出来，`error` 里那两个数（budget 与实测耗时）也都由它给出。
# ---------------------------------------------------------------------------


def test_step_over_budget_lands_failed_and_service_survives(service_process, meeting_env):
    """某一步越过这一趟收尾预算 → 该步 `failed` + 盘上 `state == "failed"` + 服务不自杀（M5）。

    **判据的边界（如实口径）**：越界**由时钟前移造出来**（见上面那一段），这一条判的是
    「预算耗尽 → 该步 `failed` + `error` 文案」，**不判预算从哪里来**。「上限来自
    `compute_drain_budget()`、四步共享同一条 deadline」这件事由 ⑤h 按行为口径咬住。

    现场：采集假件在场、`finalize()` 睡 `DRAIN_OVERRUN_STEP_SECONDS`
    再返回，返回后写下 `DRAIN_OVERRUN_CLOCK_OFFSET_SECONDS` 的时钟偏移标记；这一趟的预算仍是
    `compute_drain_budget(DRAIN_DEFAULT_BACKLOG, DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS)` = 下限
    30 秒（**用例按常量读它，不抄裸数**）。

    四件事一起断，缺一件这一档就还是没验到：

      1. **真的越界了**：末尾那一步（`post`）的 `error` 里有 §3.3 点名的「超出收尾预算」
         与**实测耗时**（`budget=` 与 `实测耗时 ` 两段都在），且这一步的 `status == "failed"`；
      2. **越界前面那一步照常落定**：`flush` 落 `done` —— 这一趟因此确实跑到了末尾那一步，
         预算拦下的是末尾这一步的调用；
      3. **盘上那一版**：`state == "failed"`、`ended_at` 有值，`drain.steps` 里那一步也写着
         `failed`（同一份现场盘上也查得到）；
      4. **服务不自杀**：进程照常活着、`/api/state` 仍 200、`ended.phase` 停在 `draining`。
    """
    plugin = fakes.write_clock_offset_plugin(
        meeting_env.home,
        finalize_delay_seconds=DRAIN_OVERRUN_STEP_SECONDS,
        clock_offset_seconds=DRAIN_OVERRUN_CLOCK_OFFSET_SECONDS,
        marker_path=meeting_env.home / DRAIN_OVERRUN_MARKER_NAME,
        post_delay_seconds=DRAIN_OVERRUN_STEP_SECONDS,
    )
    fakes.write_plugin_config(meeting_env.home, plugin)

    budget = compute_drain_budget(
        DRAIN_DEFAULT_BACKLOG, DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS
    )
    # 这一档的预算就是下限那一格（越界现场与「等满 30 秒」因此是同一件事的两种读法）
    assert budget == DRAIN_BUDGET_MIN_SECONDS, budget
    assert DRAIN_OVERRUN_CLOCK_OFFSET_SECONDS > budget, (
        f"时钟偏移 {DRAIN_OVERRUN_CLOCK_OFFSET_SECONDS}s 必须大于这一档的预算 {budget}s，"
        "否则到期时剩余预算还大于 0、这一步不会被判越界"
    )

    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _pin_fake(plugin)
    _warm_up_client(proc)
    began = time.monotonic()
    _start_and_stop(proc)

    snapshot = _wait_any_failed(proc)
    elapsed = time.monotonic() - began
    statuses = _step_statuses(snapshot["ended"]["steps"])
    evidence = _drain_evidence(snapshot, _service_log_text(meeting_env))

    # ① 越界那一步：末尾的 `post`（它的 `launch()` 被预算拦在 deadline 之外）
    assert statuses["post"] == "failed", evidence
    post_step = next(step for step in snapshot["ended"]["steps"] if step["id"] == "post")
    error = post_step["error"] or ""
    assert "超出收尾预算" in error, f"越界那一步的 error 该写明超出收尾预算：{error!r}"
    assert re.search(r"budget=[\d.]+s", error), f"error 里该带那一次的 budget：{error!r}"
    assert re.search(r"实测耗时 [\d.]+s", error), f"error 里该带实测耗时：{error!r}"
    assert f"meeting post {snapshot['session']['dir']}" in error, error
    # **真的越界了**：那一行自报的两个数摆在一起就说得清 —— 实测耗时**大于**那一次的预算
    # （这一条把「越界」钉死；数字本身由 `_drain_wait()` 的秒表给出）
    overran_budget = float(re.search(r"budget=([\d.]+)s", error).group(1))
    measured_spent = float(re.search(r"实测耗时 ([\d.]+)s", error).group(1))
    assert measured_spent > overran_budget, (
        f"error 里自报的实测耗时 {measured_spent}s 该大于那一次的 budget {overran_budget}s：{error!r}"
    )
    # ② 越界前面那一步照常落定（这一趟确实跑到了末尾那一步）
    assert statuses["flush"] == "done", evidence
    # ③ 盘上那一版：失败档的 `state`，`ended_at` 有值（收尾走到了终态）
    record = read_session_json(snapshot["session"]["dir"])
    assert record is not None, "盘上读不出 session.json"
    assert record["state"] == "failed", record
    assert record["ended_at"], record
    disk_steps = _step_statuses(_record_steps(record))
    assert disk_steps["post"] == "failed", record
    # ④ 服务不自杀：进程照常活着、状态接口照常应答、相位停在 draining
    assert proc.poll() is None, "收尾有步骤 failed 时服务不该自杀"
    assert proc.snapshot()["top_state"] == "ended"
    assert snapshot["ended"]["phase"] == "draining", evidence

    print(
        f"[M5 证据] 这一趟的预算 = compute_drain_budget({DRAIN_DEFAULT_BACKLOG}, "
        f"{DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS}) = {budget}s"
        f"（DRAIN_BUDGET_MIN_SECONDS={DRAIN_BUDGET_MIN_SECONDS}）；"
        f"假件 finalize() 拖长 {DRAIN_OVERRUN_STEP_SECONDS}s，post 那一步的 launch() 写下 "
        f"{DRAIN_OVERRUN_CLOCK_OFFSET_SECONDS}s 的时钟前移标记（> 预算 → 那条 deadline 过期）\n"
        f"  越界那一步：post -> {statuses['post']}；error 自报 budget={overran_budget}s / "
        f"实测耗时 {measured_spent}s（后者大于前者 = 真的越界）\n"
        f"  error 原文：{error}\n"
        f"  盘上那一版：state={record['state']!r} / ended_at={record['ended_at']!r} / "
        f"steps={disk_steps}\n"
        f"  服务不自杀：poll()={proc.poll()!r}；top_state={proc.snapshot()['top_state']!r}；"
        f"这一趟**真实**墙钟 {elapsed:.2f}s（上限 {E2E_CASE_WALL_CLOCK_LIMIT_SECONDS}s）"
    )
    assert elapsed <= E2E_CASE_WALL_CLOCK_LIMIT_SECONDS


# ---------------------------------------------------------------------------
# ⑤h 一趟收尾把**同一条 deadline** 交给四步，每一步拿到的剩余预算逐级变小
#     （**进程内**，审查问题 C1 的直接判据）
#
# 契约 §3.3 把两件事分开点名：「**四步共享一条 deadline**」与「等待上限**来自
# `compute_drain_budget()`**」。⑤ / ⑤b2 / ⑤g 咬的是后一件事（预算函数的输出与运行时那条
# deadline 逐值相等、上限随积压量变大），⑤f 咬的是「越界那一步落 `failed`」。**前一件事
# 一条断言都没有** —— 把 `_run_drain_steps()` 里四处调用一律改成 `deadline=None`（退化成
# 「每步各拿一份 30 秒」，正是 §3.3 点名要避免的写歪），整个测试包照旧全绿。
#
# 这一条把那处写歪正面咬住，判据取在**运行时的行为**上（读运行期真发生的事，跟源码文本长什么
# 样子无关），量的是四步各自**实际拿到的剩余预算**：
#
#   - `compute_drain_budget()` 被换成一个很小的值（0.05 秒）—— 「预算总共就这么点」因此是
#     用例自己定的事实，30 秒那条下限在这里不参与；
#   - `flush` 那一步的假件睡 0.3 秒（预算的六倍）：**预算耗尽这件事真的发生**，那一步落
#     `failed`，`error` 是 §3.3 点名的文案。写在前面那一步是为了让「后面的步骤拿到的剩余预算
#     比前面小」有一整段可观测的差（后面那几步开跑时已经过去 0.3 秒）；
#   - 每一步实际拿到的「剩余预算」与「这一刻的截止时刻」都记下来：预算序列**逐级变小**、
#     截止时刻**始终是同一条** —— 这两件一起成立，才叫「共享同一条 deadline」。
#
# **改成 `deadline=None` 时这一条必红两处**：第一步不再是 `failed`（它拿满 30 秒、0.3 秒的
# 假件照常跑完）、四步的剩余预算也不再逐级变小（每步各拿一份完整预算）。两处都由报告里的
# 变异自测贴出来。
#
# 为什么不真等：这一条全程零点几秒（唯一一笔等待是那个 0.3 秒的假件），量级与 ⑤b / ⑤c 同档。
# ---------------------------------------------------------------------------

#: ⑤h 里那一趟收尾的总预算（秒）：小到后面某一步自己要用的时长就能把它用光 ——
#: 「预算耗尽」与「剩余预算逐级变小」这两件事因此都在零点几秒里量得出来。
DRAIN_SHARED_BUDGET_SECONDS = 0.05

#: ⑤h 里 `flush` 那一步的假件睡多久（秒）：留一小段**真实耗时**（几十毫秒量级），
#: 后面的步骤因此看得见「同一条 deadline 已经被用掉一截」。
DRAIN_SHARED_STEP_SECONDS = 0.01

#: ⑤h 里 `transcode` 那一步的假件睡多久（秒）：**明显长于那时剩下的预算** ——
#: 它落在倒数第三个位置上，因此同时给出两件事：这一步真的越过预算（落 `failed`）、
#: 而它的开跑时刻已经把 deadline 用掉了一大截（剩余预算比 `flush` 那一格小一档）。
DRAIN_SHARED_TRANSCODE_SECONDS = 0.06


def test_drain_steps_share_one_deadline_with_shrinking_remaining_budget(tmp_path, monkeypatch):
    """四步共享**同一条** deadline：越界那一步落 `failed`，后一步拿到的剩余预算比前一步小（C1）。

    两件事一起断（缺一件「共享一条 deadline」就还是没验到）：

      1. **预算耗尽真的发生**：`transcode` 那一步的假件睡 `DRAIN_SHARED_TRANSCODE_SECONDS`
         （明显长于它开跑时剩下的那一截预算），它的 `status == "failed"`，`error` 是 §3.3
         点名的「超出收尾预算」那一段；
      2. **共享 deadline 的直接证据**：四步无一例外都读到**同一条**截止时刻（逐值相等），
         而它们各自开跑时「离那条截止时刻还剩多久」**逐级单调不增**、末一步比第 2 步少掉
         一大截（差的就是前两步实测花掉的那一段）—— 每步各拿一份**完整**预算时，
         第 2 条当场红。

    **把四处调用改成 `deadline=None` 时这一条必红**（变异自测见交付报告）：那时四步各自拿到
    一份**完整**的下限预算、读到的截止时刻互不相同，上面第 2 条的两半都不成立；`transcode`
    也不再落 `failed`（它拿满下限，0.06 秒的假件照常跑完）。

    预算函数由 `monkeypatch` 换成一个固定的小值（`0.05` 秒）：这一条要验的是「四步怎么**用**
    这笔预算」，那笔预算**算多大**由 ④ / ⑤ / ⑤b2 各自负责。
    """
    from meeting.backends.registry import BackendResolution
    from meeting.service import runtime as runtime_module
    from meeting.state import begin_drain

    runtime = _inprocess_runtime(tmp_path, monkeypatch)

    # 采集假件在场：`finalize()` 睡 `DRAIN_SHARED_STEP_SECONDS`（一小段真实耗时）、
    # `transcode()` 睡 `DRAIN_SHARED_TRANSCODE_SECONDS`（明显长于它开跑时剩下的那截预算）
    backend = fakes.FakeCaptureBackend(
        finalize_delay_seconds=DRAIN_SHARED_STEP_SECONDS,
        transcode_delay_seconds=DRAIN_SHARED_TRANSCODE_SECONDS,
    )
    runtime.capture_backend = backend
    runtime.capture_capabilities = {"finalize": True, "transcode": True, "dev_inject": False}
    runtime.capture_resolution = BackendResolution(
        key="capture.backend",
        selector="fake",
        target="meeting_drain_fakes:FakeCaptureBackend",
        implementation=backend,
        has_implementation=True,
        detail="本用例造的现场（进程内直接注入运行时成员）",
    )
    # **四步都要真的经 `_drain_wait()`**（§3.1 第 3 条的「无一例外」）：第 2 步的闸门只看
    # **采集侧**（上面已给足 `capture_resolution` 与 transcode 能力，转写侧与它无关），
    # 这里只差会后钩子侧一个在场、立刻返回的假件 —— 不然第 4 步会因为「构建期缺席」记
    # `skipped`、根本不进等待层，四步里就少一步能给出「共享同一条 deadline」的证据。
    # 假件从共享支持模块取（`FakePostHook`），本文件不另写一份。
    post_hook = fakes.FakePostHook()
    monkeypatch.setattr(runtime, "post_hook", post_hook)
    monkeypatch.setattr(
        runtime,
        "post_resolution",
        BackendResolution(
            key="post.hook",
            selector="fake",
            target="meeting_drain_fakes:FakePostHook",
            implementation=post_hook,
            has_implementation=True,
            detail="本用例造的现场（进程内直接注入运行时成员）",
        ),
    )
    monkeypatch.setattr(runtime, "_resolve_backends", lambda: None)

    # 这一趟的总预算：换成一个固定的小值（预算**怎么算**由 ④ / ⑤ / ⑤b2 负责）
    calls = {"budget": 0}

    def tiny_budget(backlog, segment_timeout):
        del backlog, segment_timeout
        calls["budget"] += 1
        return DRAIN_SHARED_BUDGET_SECONDS

    monkeypatch.setattr(runtime_module, "compute_drain_budget", tiny_budget)

    # 记下每一步**实际经 `_drain_wait()` 开跑**那一刻的剩余预算与截止时刻，以及这一步
    # 真跑完之后的剩余预算 —— 这一条的全部判据都从这里读。
    seen: list[dict[str, Any]] = []
    real_wait = runtime._drain_wait

    async def recording_wait(step_id, call, **kwargs):
        deadline = kwargs.get("deadline")
        began = time.monotonic()
        record = {
            "step": step_id,
            "remaining": None if deadline is None else max(0.0, float(deadline) - began),
            "deadline": None if deadline is None else float(deadline),
            "after": None,
        }
        seen.append(record)
        try:
            return await real_wait(step_id, call, **kwargs)
        finally:
            if deadline is not None:
                record["after"] = max(0.0, float(deadline) - time.monotonic())

    monkeypatch.setattr(runtime, "_drain_wait", recording_wait)

    async def one_pass() -> None:
        runtime.start_session(dir=_new_workspace(tmp_path, "deadline-share"))
        begin_drain(runtime.snapshot, reason="user")
        await runtime.drain()

    asyncio.run(one_pass())

    # 四步无一例外都经 `_drain_wait()`（§3.1 第 3 条），且预算函数恰好被喊了一次
    assert [item["step"] for item in seen] == list(DRAIN_STEP_IDS), seen
    assert calls["budget"] == 1, f"这一趟该只算一次预算，实测喊了 {calls['budget']} 次"

    # ① 预算耗尽真的发生：超预算的那一步照 §3.3 点名的话落 `failed`
    statuses = {step.id: step.status for step in runtime.snapshot.ended.steps}
    # 变异（四处调用点改成 `deadline=None`）会让这一格读到 `None` —— 下面这句如实报出来，
    # 不靠格式化把信号盖掉
    assert seen[1]["remaining"] is not None, (
        f"第 2 步开跑时手上没有 deadline（四处调用点没把这一趟那条传下来）：{seen}"
    )
    assert statuses["transcode"] == "failed", (
        f"{statuses}（这一步的假件睡了 {DRAIN_SHARED_TRANSCODE_SECONDS}s，"
        f"而它开跑时只剩 {seen[1]['remaining']:.4f}s）"
    )
    transcode_step = next(step for step in runtime.snapshot.ended.steps if step.id == "transcode")
    assert "超出收尾预算" in (transcode_step.error or ""), transcode_step.error

    # ② 共享 deadline 的直接证据
    for item in seen:
        assert item["deadline"] is not None, (
            f"{item['step']} 那一步拿到的 deadline 是 None —— 每步各拿一份下限预算，"
            f"四步没有共享同一条 deadline：{seen}"
        )
    deadlines = {item["deadline"] for item in seen}
    assert len(deadlines) == 1, (
        f"四步该读到**同一条**截止时刻，实测有 {len(deadlines)} 条："
        f"{[(item['step'], item['deadline']) for item in seen]}"
    )
    # 每一步开跑时「这条 deadline 还剩多少」：同一趟收尾里逐级单调不增 —— 这正是「共享」的
    # 形状；每步各拿一份完整预算时这几格会一样大（甚至后一步比前一步大）
    remainings = [item["remaining"] for item in seen]
    assert all(
        later <= earlier for earlier, later in zip(remainings, remainings[1:])
    ), f"四步开跑时的剩余预算该逐级单调不增：{[(i['step'], i['remaining']) for i in seen]}"
    # 逐级变小是**实测**的：前两步真的花掉了各自那段时长，末一步拿到的那一截明显更小
    spent_before_last = seen[0]["remaining"] - seen[-1]["remaining"]
    assert spent_before_last >= DRAIN_SHARED_STEP_SECONDS, (
        f"第 1 步到末一步之间该量到前几步花掉的那一段（第 1 步的假件睡了 "
        f"{DRAIN_SHARED_STEP_SECONDS}s），实测只少了 {spent_before_last:.4f}s：{seen}"
    )
    # 收尾期间那条 deadline 真的走到了头：越界那一步跑完之后，剩余预算已经是 0
    assert seen[1]["after"] == 0.0, (
        f"transcode 跨过预算之后，这条 deadline 的剩余预算该是 0，实测 {seen[1]['after']!r}：{seen}"
    )
    print(
        f"[C1 证据] 预算固定为 compute_drain_budget(){DRAIN_SHARED_BUDGET_SECONDS}s，"
        f"第 1 步的假件睡 {DRAIN_SHARED_STEP_SECONDS}s、"
        f"第 2 步的假件睡 {DRAIN_SHARED_TRANSCODE_SECONDS}s（明显长于它开跑时的剩余预算）：\n"
        f"  每一步开跑时的剩余预算 / 截止时刻："
        f"{[(item['step'], round(item['remaining'], 4), round(item['deadline'], 4)) for item in seen]}\n"
        f"  超预算那一步：transcode -> {statuses['transcode']}"
        f"（error={transcode_step.error!r}）\n"
        f"  四步读到同一条截止时刻：{len(deadlines) == 1}"
        f"（{list(deadlines)[0]:.4f}）；"
        f"剩余预算 {remainings[0]:.4f}s -> {remainings[-1]:.4f}s"
        f"（少掉 {spent_before_last:.4f}s）"
    )


# ---------------------------------------------------------------------------
# ④b 转写后端缺席 → 两个点名缺省常量 → budget = 30（**进程内**，审查问题 M3）
#
# 契约 §3.3 与 §4 落点表 ③ 点名了两个常量：`DRAIN_DEFAULT_BACKLOG`（= 0）与
# `DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS`（= 20.0）。它们是「转写后端缺席时预算取什么」这
# 一条链的输入端，在此之前**没有任何断言引用过它们**（预算那几条用例给的都是显式输入）。
# 这一条把那条链逐段钉住：`_drain_inputs()` 的取缺省 → `compute_drain_budget()` 的输出
# → `_drain_wait_timeout()` 的上限（三者都**按常量名**引用，不抄裸数）。
# ---------------------------------------------------------------------------


def test_drain_default_budget_inputs_when_transcriber_is_absent(tmp_path, monkeypatch):
    """转写后端缺席 → `backlog = DRAIN_DEFAULT_BACKLOG` / `segment_timeout =
    DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS` → `budget = DRAIN_BUDGET_MIN_SECONDS`（审查问题 M3）。

    **契约依据**：§3.3「`backlog` 与 `segment_timeout` 从转写后端拿；没有后端时取 `0 / 20s`」
    与 §4 落点表 ③（这两个缺省常量的名字与取值都落在 `meeting/service/runtime.py`）。

    **三件事一起断，链子才完整**：

      1. `_drain_inputs()` 在后端缺席时**返回的就是那两个常量**（不是另抄一份字面量，
         也不是别的什么默认值）；
      2. 把它们的返回值喂进 `compute_drain_budget()`，得到的正是下限那一档
         （`30 + 0 × (20 + 5) = 30`）—— 公式与缺省值两处一起对得上；
      3. 「等这一趟收尾落定」用的上限（`_drain_wait_timeout()`）也走同一条链：收尾没在跑、
         手上没有 deadline 时它报 `budget + DRAIN_LOG_MIN_BUDGET_SECONDS`，其中那个 `budget`
         就是 `compute_drain_budget(*_drain_inputs())` 的输出。

    阈值、后端缺席这两件事都由**进程内**直接读被断言的对象造出来（不起服务）：真服务那一支
    的「骨架构建下转写后端缺席」由 ⑤（`budget == DRAIN_BUDGET_MIN_SECONDS`，取自 log 里那一行）
    在子进程里咬住。
    """
    runtime = _inprocess_runtime(tmp_path, monkeypatch)
    # 转写后端缺席：`_resolve_backends()` 那一档的等价现场（`transcriber_backend is None`）
    runtime.transcriber_backend = None

    backlog, segment_timeout = runtime._drain_inputs()
    assert backlog == DRAIN_DEFAULT_BACKLOG, (
        f"转写后端缺席时 `backlog` 该取点名常量 DRAIN_DEFAULT_BACKLOG="
        f"{DRAIN_DEFAULT_BACKLOG}，实测 {backlog!r}"
    )
    assert segment_timeout == DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS, (
        f"转写后端缺席时 `segment_timeout` 该取点名常量 DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS="
        f"{DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS}，实测 {segment_timeout!r}"
    )

    budget = compute_drain_budget(backlog, segment_timeout)
    assert budget == DRAIN_BUDGET_MIN_SECONDS, (
        f"({backlog}, {segment_timeout}) 这一组缺省输入算出来的预算该是下限那一档 "
        f"{DRAIN_BUDGET_MIN_SECONDS}s，实测 {budget}s"
    )

    # 同一条链走到「等落定」的上限上（收尾没在跑、也没有 leftover deadline 的那一格）
    runtime._drain_budget = budget
    runtime._drain_deadline = None
    assert runtime._drain_wait_timeout() == float(DRAIN_BUDGET_MIN_SECONDS) + float(
        DRAIN_LOG_MIN_BUDGET_SECONDS
    )

    print(
        f"[M3 证据] 转写后端缺席 -> _drain_inputs()=({backlog}, {segment_timeout})"
        f"（DRAIN_DEFAULT_BACKLOG={DRAIN_DEFAULT_BACKLOG} / "
        f"DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS={DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS}）"
        f" -> budget={budget}（DRAIN_BUDGET_MIN_SECONDS={DRAIN_BUDGET_MIN_SECONDS}）"
        f" -> _drain_wait_timeout()={runtime._drain_wait_timeout()}"
    )


# ---------------------------------------------------------------------------
# ⑤b 等待上限对同步与协程两条路一视同仁（**进程内**，极小 deadline）
#
# 这一条是审查问题 C1 的直接判据：`_drain_wait()` 曾经在「同步 lambda」这条路上直接调用、
# 不经 `asyncio.wait_for`，于是同步步完整跑完、`budget` 与 `DRAIN_LOG_MIN_BUDGET_SECONDS`
# 成了死值。这里用**可注入的小 deadline**（0.02 秒）把墙钟代价压到毫秒级，
# 而断言的语义与「真等 30 秒预算」完全一致：超预算 → `DrainLimitExceeded`。
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "invoke, mode",
    [
        (True, "sync-callable-in-worker-thread"),
        (False, "coroutine-on-the-loop"),
    ],
)
def test_drain_wait_wraps_every_call_in_the_remaining_budget(dev_runtime, invoke, mode):
    """同步路（工作线程）与协程路**都**受同一个剩余预算约束（超出即 `DrainLimitExceeded`）。

    判据取 `DrainLimitExceeded` 自报的**实测耗时**（`_drain_wait()` 里那把秒表量出来的那一段）：
    它必须贴近 0.02 秒那条上限，**远小于**那一步自己声明的 0.6 秒。C1 那个 bug 的签名正是
    「同步 lambda 完整跑完、连异常都不抛」——两条断言各自都咬得住它。

    外层墙钟只做一个宽松的上界（< 5 秒）：这一条跑在 pytest 进程里，收尾执行器的线程**首次
    使用要现起线程**（Windows 上数百毫秒），把它算进「这一步花了多久」会冤枉被测的那段逻辑；
    真服务子进程里线程池早已就位（⑩ 条在那边断的是同一件事的进程级形态）。
    """
    runtime = dev_runtime
    elapsed = DRAIN_UNIT_STEP_SECONDS
    if invoke:
        # 真服务里四步调用点用的形态：同步 callable + `invoke=True`（提交到工作线程）
        def step():
            time.sleep(elapsed)

    else:
        # 协程形态：走 `call()` 的返回值（`ensure_future` 包成任务）再等
        async def step():
            await asyncio.sleep(elapsed)

    # deadline 落在「已经过去」（剩余预算为 0）→ 实际生效的上限就是 `min_budget`
    started = time.monotonic()
    with pytest.raises(DrainLimitExceeded) as caught:
        asyncio.run(
            runtime._drain_wait(
                "flush",
                step,
                deadline=started - 1.0,
                min_budget=DRAIN_UNIT_BUDGET_SECONDS,
                invoke=invoke,
            )
        )
    spent = time.monotonic() - started
    message = str(caught.value)
    assert "超出收尾预算" in message, message
    match = re.search(r"实测耗时 ([\d.]+)s", message)
    assert match, message
    inside = float(match.group(1))
    assert inside < elapsed / 2, (
        f"{mode} 这条路绕过了等待上限：{elapsed}s 的假步照跑完（{message}）"
    )
    assert inside <= DRAIN_UNIT_BUDGET_SECONDS * 20, message
    assert spent < 5.0, f"{mode} 这条路的墙钟 {spent:.3f}s 该在量级上就收住（{message}）"

    # 反向对照：预算够宽时同一条路正常返回，且实测耗时以 `_drain_wait()` 的返回值为准。
    # 协程那一档给的是货真价实的 `async def`（`inspect.iscoroutinefunction()` 为真）：
    # 写成本文件从前那种 `lambda: _returns_after(...)` 会被下面那道「同步 callable 必须传
    # `invoke=True`」的看门（审查问题 m1）拦下来 —— 那正是它想拦住的那类调用点。
    healthy = (lambda: "落定") if invoke else _coroutine_step
    value, measured = asyncio.run(
        runtime._drain_wait(
            "flush",
            healthy,
            deadline=time.monotonic() + elapsed + 5.0,
            invoke=invoke,
        )
    )
    assert value == "落定"
    assert 0.0 <= measured < elapsed + 5.0

    # 同步 callable 忘了传 `invoke=True` 这一档：这条错法在**调用之前**就被认出来
    # （`TypeError`，文案点名 `invoke=True`），不会退化成「先把同步实现跑在事件循环上」。
    # 完整的判据（含「同步实现一次都没被调到」与「墙钟远小于假步时长」）在下面 ⑤b1 那一条。
    if invoke:
        with pytest.raises(TypeError, match="invoke=True"):
            asyncio.run(
                runtime._drain_wait(
                    "flush",
                    lambda: time.sleep(0.0) or "普通值",
                    deadline=time.monotonic() + 5.0,
                )
            )


async def _coroutine_step() -> str:
    """`_drain_wait()` 协程路的健康对照：`async def` 形态的「一步调用」。"""
    return await _returns_after(DRAIN_UNIT_STEP_SECONDS)


def test_remaining_budget_and_drain_wait_reject_a_missing_deadline(dev_runtime):
    """`deadline=None` 显式报错，不给任何上限（**进程内**，审查问题 m2）。

    契约 §3.3 与 §8 的坑表把这条上限的来源钉在一处：**只能来自 `compute_drain_budget()`**。
    `None` 换不出一个说得清的数 —— 退成 `float("inf")` 就是「没有上限」、退成预算下限是另一条
    凭空的保守上界。两条入口因此都当场说话，并且**在等待之前**说话（下面的阻塞步一次都不该
    被跑到：真去等的话这一趟要挂 10 秒）。

    断的是三件事：抛的是 `ValueError`、文案里点名 `compute_drain_budget()`、以及这一步的
    调用**没有被跑到**。
    """
    runtime = dev_runtime
    ran = {"n": 0}

    def blocking_step():
        ran["n"] += 1
        time.sleep(DRAIN_UNIT_STEP_SECONDS * 10)
        return "跑完了"

    with pytest.raises(ValueError) as caught:
        runtime._remaining_budget(None)
    message = str(caught.value)
    assert "compute_drain_budget()" in message, message

    started = time.monotonic()
    with pytest.raises(ValueError) as caught_wait:
        asyncio.run(
            runtime._drain_wait(
                "flush",
                blocking_step,
                deadline=None,
                invoke=True,
            )
        )
    spent = time.monotonic() - started
    wait_message = str(caught_wait.value)
    assert "compute_drain_budget()" in wait_message, wait_message
    assert ran["n"] == 0, (
        f"报错之前先把这一步跑到了 {ran['n']} 次 —— 缺 deadline 该在等待之前就说话"
    )
    assert spent < DRAIN_UNIT_STEP_SECONDS, (
        f"缺 deadline 的那一趟等了 {spent:.3f}s：它该在开跑之前就抛出来"
    )
    print(
        f"[m2 证据] _remaining_budget(None) -> {type(caught.value).__name__}：{message}\n"
        f"[m2 证据] _drain_wait(..., deadline=None) -> {type(caught_wait.value).__name__}："
        f"{wait_message}（该步被跑到的次数={ran['n']}，实测墙钟={spent:.4f}s）"
    )


async def _returns_after(seconds: float) -> str:
    await asyncio.sleep(seconds)
    return "落定"


# ---------------------------------------------------------------------------
# ⑤b1 同步 callable 忘传 `invoke=True`：在开跑之前就刺耳（**进程内**，审查问题 m1）
#
# 从前这条错法是「先跑完、再抛错」：`call()` 在事件循环上把那个同步实现整段跑掉（审查实测
# 3.00 秒的 `time.sleep` 把循环钉满 3 秒，`min_budget` 拦不住），之后 `wait_for` 才认不出
# 普通值、抛 `TypeError`。收尾期间 `/api/state`、SSE 与 `POST /api/actions` 在这 3 秒里一起
# 停摆 —— 与「收尾必须可观测」相抵（契约 §3.1 第 6 条）。
#
# 现在这个错法在**调用之前**被认出来：`invoke` 为假 + 同步可调用对象 → 当场抛 `TypeError`，
# 同步实现一行都不跑（下面的计数器是这条的直接判据），那一步立刻落 `failed`，收尾的其余
# 步骤照常推进、事件循环一直在转。
# ---------------------------------------------------------------------------

#: ⑤b1 里那个「不该被跑」的同步步的时长（秒）：足够长，长到它一旦真的跑起来就会
#: 把事件循环钉住、把这条用例的墙钟顶起来。
DRAIN_FORGOTTEN_INVOKE_STEP_SECONDS = 0.2

#: ⑤b1 里那一步的窗口（秒）：比假步短，于是「假步真跑起来」会额外体现为「等满上限」。
DRAIN_FORGOTTEN_INVOKE_WINDOW_SECONDS = 0.05


def test_sync_callable_without_invoke_true_fails_fast_and_keeps_the_loop_running(dev_runtime):
    """忘传 `invoke=True` → 同步实现一行都不跑、事件循环照常转、那一步落 `failed`（m1）。

    三件事一起断，缺一件这条错法就还是「先跑完再抛错」：

      1. 那个同步实现**一次都没被调到**（它自己的计数器停在 0），于是「事件循环被它钉住」
         这件事在成因上就不存在；
      2. 抛的是 `TypeError`，文案里点名 `invoke=True`（说得清是哪一处调用点的问题）；
      3. **报错发生在开跑之前**：这一趟的实测墙钟远小于那个假步自己的时长（假步若真跑起来，
         墙钟至少要 0.2 秒）。
    """
    runtime = dev_runtime
    ran = {"n": 0}

    def step():
        ran["n"] += 1
        time.sleep(DRAIN_FORGOTTEN_INVOKE_STEP_SECONDS)
        return "跑完了"

    started = time.monotonic()
    with pytest.raises(TypeError) as caught:
        asyncio.run(
            runtime._drain_wait(
                "flush",
                step,
                deadline=time.monotonic() + DRAIN_FORGOTTEN_INVOKE_WINDOW_SECONDS,
            )
        )
    spent = time.monotonic() - started
    message = str(caught.value)
    assert "invoke=True" in message, message
    assert ran["n"] == 0, (
        f"同步实现被跑到了 {ran['n']} 次 —— 这条错法该在调用之前就被认出来"
        f"（实测墙钟 {spent:.3f}s）"
    )
    assert spent < DRAIN_FORGOTTEN_INVOKE_STEP_SECONDS, (
        f"报错前先跑了那个同步实现：墙钟 {spent:.3f}s 不小于假步自己的 "
        f"{DRAIN_FORGOTTEN_INVOKE_STEP_SECONDS}s"
    )
    print(
        f"[m1 证据] 同步 callable 未传 invoke=True：{type(caught.value).__name__}"
        f"（{message}）；同步实现被调到的次数={ran['n']}；实测墙钟={spent:.4f}s"
    )


def test_step_over_budget_still_leaves_later_steps_room(tmp_path, monkeypatch):
    """某一步的调用出事之后，**后续步仍能推进并落定**（m1 的收尾编排形态）。

    这一条走的是真正的收尾编排（`drain()` 四步），只在过程内把第一步的调用换成「同步
    可调用对象 + 漏掉 `invoke=True`」这个错法（`_drain_flush()` 的记账照原样照抄：出错
    经 `except` 落 `failed`，**整段 `drain` 不许因此抛出去**）。丢掉了 `invoke=True` 的
    同步步从前会先跑在事件循环上、`_drain_flush()` 于是在整段睡眠里卡住，下面这个 ticker
    一次都跑不到 —— 收尾期间 `/api/state` 与 SSE 也就一起停摆。

    断言因此分三处：事件循环一直在转（ticker 计数）、那一步落 `failed` 且文案点名
    `invoke=True`、**后续步照常推进到终态**（`transcode` 按采集替身没有 transcode 能力、
    `post` 按钩子缺席记 `skipped`，`log` 记 `done`）。

    **观察窗口从第一步挪到整趟收尾**：修好之后第一步在开跑之前就落定（零点几毫秒），单看它
    那一段量不到「循环一直在转」。于是本用例给 `log` 那一步挂一个睡觉的假替身
    （`DRAIN_FORGOTTEN_INVOKE_STEP_SECONDS`，走真正的工作线程路径），收尾因此有一段真实的
    时长可观测，而「后续步在一个已经出错的收尾里照常推进」这件事也走得更实。
    """
    from meeting import logging_setup as logging_setup_module
    from meeting.state import begin_drain

    runtime = _inprocess_runtime_with_backends(tmp_path, monkeypatch)

    async def flush_with_the_forgotten_invoke(deadline: float) -> None:
        """`_drain_flush()` 的等价体：调用点丢掉 `invoke=True`，出错照原样落 `failed`。"""
        runtime._begin_step("flush", "停采集并把这一场的归档落盘（后端 finalize()）")
        try:
            await runtime._drain_wait(
                "flush",
                lambda: time.sleep(DRAIN_FORGOTTEN_INVOKE_STEP_SECONDS) or "同步实现跑完了",
                deadline=deadline,
            )
        except Exception as exc:
            runtime._fail_step("flush", exc, None, "采集归档落盘失败")
            return
        runtime._done_step("flush", "归档已落盘（finalize() 返回）", None)

    monkeypatch.setattr(runtime, "_drain_flush", flush_with_the_forgotten_invoke)

    def slow_end_marker(*, state: str, steps) -> None:
        """`log` 那一步的假替身：在**工作线程**里睡一小段（真路径走的就是那条线程）。"""
        time.sleep(DRAIN_FORGOTTEN_INVOKE_STEP_SECONDS)
        return None

    monkeypatch.setattr(
        logging_setup_module, "write_session_end_marker", slow_end_marker
    )

    async def scenario() -> int:
        ticks = {"n": 0}

        async def ticker() -> None:
            while True:
                ticks["n"] += 1
                await asyncio.sleep(0.005)

        beat = asyncio.ensure_future(ticker())
        try:
            runtime.start_session(dir=_new_workspace(tmp_path, "loop-pinned"))
            begin_drain(runtime.snapshot, reason="user")
            await runtime.drain()
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
        return ticks["n"]

    ticks = asyncio.run(scenario())
    statuses = {step.id: step.status for step in runtime.snapshot.ended.steps}
    assert ticks >= 10, (
        f"收尾期间事件循环只转了 {ticks} 次 —— 同步步被留在事件循环上了（收尾不可观测）"
    )
    assert statuses["flush"] == "failed", statuses
    flush_step = next(step for step in runtime.snapshot.ended.steps if step.id == "flush")
    assert "invoke=True" in (flush_step.error or ""), flush_step.error
    assert statuses["log"] == "done", (
        f"第 1 步超出窗口之后，后续步该照常推进到终态：{statuses}"
    )
    print(
        f"[m1 证据] 第一步丢掉 invoke=True：flush -> {statuses['flush']}"
        f"（error={flush_step.error!r}）；收尾期间事件循环转了 {ticks} 次；"
        f"后续四步状态={statuses}"
    )


# ---------------------------------------------------------------------------
# ⑤b2 `log` 步的写失败要如实落 `failed`（**进程内**，审查问题 m3）
#
# `meeting/service/runtime.py` 的 `log` 步有一条 `if problem:` 分支：场次日志的 flush /
# 结束标记 / 摘除返回一条非空的问题说明时，这一步记 `failed`（契约 §3.7：写失败例如磁盘满 →
# 不中断录音，`drain.steps.log` 记 `failed`、状态里出告警、主 log 留一条，**不许静默**）。
# 在这一条之前，那一格没有任何用例走到过 —— 把它整段删掉、或者让 `problem` 被吞掉，
# 全量回归照旧全绿。
#
# 这一条把 `write_session_end_marker()` 换成一个「返回非空 problem」的实现（进程内，
# 不起服务、不碰磁盘），然后走真正的 `drain()` 四步：那一步落 `failed`、`error` 里带如实
# 文案与问题原文、进程照常活着、其余步照常推进。
# ---------------------------------------------------------------------------

#: ⑤b2 里那个假替身返回的问题说明（与磁盘满同形的一条如实文案）
DRAIN_LOG_PROBLEM_TEXT = "写结束标记失败：OSError: [Errno 28] No space left on device"


def _inprocess_runtime_with_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, post_hook_absent: bool = True
):
    """进程内那一档的 `ServiceRuntime`，**后端已解析**（可直接开一场、跑一趟收尾）。

    与 `_inprocess_runtime()` 同一档，差别只在两处：

      - 配置由本用例现给（`capture.backend = "dev"`：采集替身在场、`finalize` / `transcode`
        两项能力如实为假），于是 `start_session()` 有一个可用的采集后端（缺它会回 503）；
      - 走真正的后端解析（`_resolve_backends()`），四条收尾编排因此按 registry 的结论记账 ——
        「采集替身 → `flush` / `transcode` 记 `skipped`」这条骨架构建下的面孔是真的走出来的，
        用例自己的假件只要往 `post` 那一格或步骤方法上注入就够了。

    `post_hook_absent=False` 时不写 `post.hook` 这一格（取值直接来自 `config.DEFAULT_VALUES`
    里的产品缺省 `auto`）：⑤b3 那一条量的是产品缺省那一档。
    """
    from meeting import config as config_module
    from meeting.service.app import build_runtime

    monkeypatch.setenv("HOME", str(tmp_path / "home-inprocess-backends"))
    monkeypatch.delenv("MEETING_CAPTURE", raising=False)
    # `build_runtime()` 会把这份配置设成当前配置（`config._LOADED`）；用例结束时放回原样
    # —— 进程内用例共用同一份模块级当前配置，留着自己那一份会渗到别的用例里。
    monkeypatch.setattr(config_module, "_LOADED", config_module.current())
    runtime = build_runtime(
        _inprocess_config(
            tmp_path, {"capture.backend": "dev"}, post_hook_absent=post_hook_absent
        )
    )
    # 后端解析在真服务里由 `startup()` 的 ④ 步触发（本档不起 lifespan），这里显式走一遍 ——
    # 于是 `start_session()` 有一个在位的采集替身，四条收尾也按 registry 的结论记账。
    runtime._resolve_backends()
    return runtime


def _inprocess_config(
    tmp_path: Path, overrides: dict[str, Any], *, post_hook_absent: bool = True
):
    """进程内用例的一小份配置：**默认值表原样** + 本用例的输出根 + 点名要改的那几个键。

    默认值表直接取 `config.DEFAULT_VALUES`（不手抄一份：`settings` 视图、告警阈值与后端解析
    都要读全键集，抄一份迟早漏键）。

    `post_hook_absent=True`（缺省）时，`post.hook` 在这里显式取 `null`（缺席档）：会后钩子
    （`7c6952e5` phase2 的 `meeting.backends.post:PostProcessHook`）落地之后，产品缺省 `auto`
    会让收尾第 4 步真的起一条后台线程跑整条会后链 —— 那是会后节点自己的账。本文件里数收尾
    编排那几步账的用例（⑤b2 那一条逐字断言 `"post": "skipped"`）靠这一格把「缺席」这一档
    摆出来，第 4 步按缺席记 `skipped` 才是它们该看到的样子。

    `post_hook_absent=False` 时这一格整个不写，`post.hook` 于是取到 `config.DEFAULT_VALUES`
    里的产品缺省 `auto` —— ⑤b3 那一条量的就是产品缺省那一档（实现落地之后第 4 步该记 `done`）。
    """
    from meeting.config import DEFAULT_VALUES, Config

    applied = {
        **DEFAULT_VALUES,
        "log.dir": str(tmp_path / "out-inprocess-backends" / "logs"),
        "transcriber.backend": "auto",
        **({"post.hook": "null"} if post_hook_absent else {}),
        **overrides,
    }
    return Config(applied=applied, sources={key: "默认" for key in applied}, path="")


def test_log_step_lands_failed_when_the_session_log_write_reports_a_problem(
    tmp_path, monkeypatch
):
    """场次日志写失败 → `log` 步 `failed`、`error` 如实带上那一段、服务不自杀（审查问题 m3）。

    **契约依据**：§3.7（写失败不中断录音，`drain.steps.log` 记 `failed`、状态里出告警、
    主 log 留一条，**不许静默**）。`log` 是四步里唯一在任何构建里都有真实实现的步骤，
    「有实现却没做成」按 §3.0 的裁决就是 `failed`。

    断的是四件事：那一步 `failed`；`error` 里有「场次日志写失败」这类如实文案与问题原文；
    收尾就此停在 `draining`（`ended.phase` 保持这一档 —— 失败现场留在快照与盘上）；
    **其余步照常推进**（第 4 步 `post` 仍按缺席档落 `skipped`，收尾编排没有整段抛出去）。
    最后一条同时是本文件里「缺席 → `skipped`」的落点：它走的是真 `drain()` 四步里的
    真 `_drain_post()`（另断步骤说明与解析结论，证明走到的是缺席那一档）；产品缺省
    `post.hook = auto` 那一档由 ⑤b3 单独覆盖。
    """
    from meeting import logging_setup as logging_setup_module
    from meeting.state import begin_drain

    runtime = _inprocess_runtime_with_backends(tmp_path, monkeypatch)
    calls: list[dict[str, Any]] = []

    def fake_write_session_end_marker(*, state: str, steps) -> str:
        calls.append({"state": state, "steps": list(steps)})
        return DRAIN_LOG_PROBLEM_TEXT

    monkeypatch.setattr(
        logging_setup_module, "write_session_end_marker", fake_write_session_end_marker
    )

    async def one_pass() -> None:
        runtime.start_session(dir=_new_workspace(tmp_path, "log-write-problem"))
        begin_drain(runtime.snapshot, reason="user")
        await runtime.drain()

    asyncio.run(one_pass())

    assert len(calls) == 1, f"这一步该被调到恰好一次：{calls}"
    statuses = {step.id: step.status for step in runtime.snapshot.ended.steps}
    log_step = next(step for step in runtime.snapshot.ended.steps if step.id == "log")
    assert log_step.status == "failed", (log_step.status, log_step.detail, log_step.error)
    assert "场次日志写失败" in (log_step.error or ""), log_step.error
    assert DRAIN_LOG_PROBLEM_TEXT in (log_step.error or ""), log_step.error
    # 失败那一档：收尾停在 draining（不判 done、也不自杀）
    assert runtime.snapshot.ended.phase == "draining", runtime.snapshot.ended.phase
    assert runtime.snapshot.top_state == "ended", runtime.snapshot.top_state
    # 其余步照常推进（第 4 步按缺席落 skipped，收尾编排没有整段抛出去）
    assert statuses["post"] == "skipped", statuses
    # 「缺席 → skipped」这半边由这一条真跑：走到的正是缺席那一档的 `_drain_post()`
    # （步骤说明里有「缺席」、解析结论的 `skipped()` 为真）—— ⑤b3 量的是产品缺省那一档。
    post_step = next(step for step in runtime.snapshot.ended.steps if step.id == "post")
    assert "缺席" in (post_step.detail or ""), post_step.detail
    assert runtime.post_resolution.skipped() is True, runtime.post_resolution
    assert set(statuses.values()) <= set(DRAIN_STEP_STATUS), statuses
    print(
        f"[m3 证据] 场次日志写失败：log 步 -> {log_step.status}"
        f"（error={log_step.error!r}）；ended.phase={runtime.snapshot.ended.phase}；"
        f"四步状态={statuses}"
    )


# ---------------------------------------------------------------------------
# ⑤b3 收尾第 4 步在**产品缺省**那一档落 `done`（**进程内**，审查问题 C2）
#
# `_inprocess_config()` 把 `post.hook` 显式钉成 `null`（缺席档），好让 ⑤b2 那条逐字断言
# `"post": "skipped"` 继续为真 —— 那是「这一格不写实现时记 skipped」的覆盖，值钱的正是
# 「缺席」这半边。代价是本文件从此不再观察**产品缺省**：`post.hook = auto` 配上
# `meeting/backends/post.py` 这条真实现，第 4 步该按「有实现且启动成功」记 `done`。
# 这一条把那半边补回来：`post.hook` 整个不写（`post_hook_absent=False`，取值直接来自
# `config.DEFAULT_VALUES`）、走真正的 `_resolve_backends()` 与真正的 `drain()` 四步，
# 只把 `launch()` 换成一条只记账的替身 —— 真 `launch()` 会起后台线程按 `post.engine = auto`
# 真去加载权重，`tests/conftest.py` 不许任何一个用例碰真模型。
# ---------------------------------------------------------------------------


def test_post_step_lands_done_under_the_product_default_hook(tmp_path, monkeypatch):
    """产品缺省 `post.hook = auto`：第 4 步解析到真实现、跑完落 `done`（审查问题 C2）。

    与 ⑤b2 同一条进程内路子，差别只在 `post.hook` 这一格：这里**不写它**，
    配置里那一格的取值就是 `config.DEFAULT_VALUES` 里的产品缺省（先断言它确实是 `auto`，
    免得缺省值哪天改了这条用例悄悄换成另一档）。断言这几件事：解析落到本单元的钩子类且
    `skipped()` 为假、第 4 步 `done`、`run_ok is True`、钩子收到的正是本场的场次目录、
    收尾终态 `ended.phase == "done"`。
    """
    from meeting.backends import post as post_module
    from meeting.config import DEFAULT_VALUES
    from meeting.state import begin_drain

    assert DEFAULT_VALUES["post.hook"] == "auto", DEFAULT_VALUES["post.hook"]

    runtime = _inprocess_runtime_with_backends(
        tmp_path, monkeypatch, post_hook_absent=False
    )
    # 接线第一段：产品缺省那一档真的解析到本实现（`null` 那一档才落缺席）
    assert runtime.config.get("post.hook") == "auto", runtime.config.get("post.hook")
    assert runtime.post_resolution.target == "meeting.backends.post:PostProcessHook", (
        runtime.post_resolution
    )
    assert runtime.post_resolution.skipped() is False
    assert isinstance(runtime.post_hook, post_module.PostProcessHook)

    launched: list[str] = []

    def fake_launch(self, session_dir: str) -> None:
        """替身 `launch()`：只记下收到的场次目录（真那条会起线程去加载模型）。"""
        launched.append(str(session_dir))

    monkeypatch.setattr(post_module.PostProcessHook, "launch", fake_launch)

    async def one_pass() -> None:
        runtime.start_session(dir=_new_workspace(tmp_path, "post-default-hook"))
        begin_drain(runtime.snapshot, reason="user")
        await runtime.drain()

    asyncio.run(one_pass())

    statuses = {step.id: step.status for step in runtime.snapshot.ended.steps}
    assert statuses["post"] == "done", (statuses, runtime.snapshot.ended)
    assert runtime.post_resolution.run_ok is True
    assert launched == [runtime.snapshot.session.dir], launched
    assert runtime.snapshot.ended.phase == "done", runtime.snapshot.ended.phase
    print(
        f"[C2 证据] 产品缺省 post.hook=auto：解析到 {runtime.post_resolution.target}；"
        f"第 4 步 -> {statuses['post']}（run_ok={runtime.post_resolution.run_ok}）；"
        f"launch 收到的场次目录={launched}；四步状态={statuses}"
    )


# ---------------------------------------------------------------------------
# ⑤k 一步被放弃之后，后续步的线程仍可用（**进程内**，审查问题 m7）
#
# 收尾的工作池从前只有一条线程：某一步被按剩余预算判超时之后，它的**等待**被放弃，已经提交
# 给那条线程的同步调用却照跑到底（§3.5 明写这一条），后续步的调用于是只能排队等 ——
# 「一步超时」因此把后续步的**可用性**一起堵住。这一条把那件事量出来：让第 1 步 `flush` 的
# 调用卡在一条闸门上，超时之后 `log` 那一步仍要在**它自己的最小预算**里落定。
#
# **闸门为什么挂在第 1 步而不是最后一步**（探针量过才定的）：四步里只有 `flush` 与 `log`
# 真的走工作线程（缺席的步骤根本不进等待层）。挂到 `post` 上时，被放弃的那次调用与 `log`
# 的调用**并列在池里**，一条线程与四条线程都能让 `log` 立刻开跑，这一条就分不出两种池
# （实测：改回 1 条线程它照样全绿）。挂在第 1 步上时，被放弃的调用**先占住**线程、`log` 在
# 它之后才要线程 —— 那时池里还有没有空线程，正是这一条要量的东西。
# ---------------------------------------------------------------------------

#: ⑤k 里整趟收尾的预算（秒）：小到第 1 步一定会越过它
DRAIN_ABANDONED_TOTAL_BUDGET_SECONDS = 0.05

#: ⑤k 里第 1 步的窗口（秒）：整趟预算就是它的全部额度，越过即被放弃
DRAIN_ABANDONED_FLUSH_WINDOW_SECONDS = 0.05

#: ⑤k 里闸门的等待上限（秒）：闸门由用例在收尾跑完之后放开，这只是「万一没放开」的兜底
DRAIN_ABANDONED_GATE_SECONDS = 10.0

#: ⑤k 里整趟收尾墙钟的上界换算：`log` 那一步的额度（`DRAIN_LOG_MIN_BUDGET_SECONDS`）乘这个
#: 倍数。前一步被放弃之后，后续步该**立刻**拿到一条线程 —— 整趟墙钟因此落在「那一步自己的
#: 预算」附近，而线程池只有一条时它会顶到整份额度（5 秒）那一档。
DRAIN_ABANDONED_WALL_CLOCK_FACTOR = 0.8


def test_later_steps_keep_a_worker_after_an_earlier_step_is_abandoned(tmp_path, monkeypatch):
    """前一步被放弃之后，**后续步仍在自己的预算里推进并落定**（审查问题 m7）。

    现场：第 1 步 `flush` 的 `finalize()` 卡在一条闸门上（不出声地占着一条工作线程），整趟
    收尾的预算被压到 0.05 秒 —— 那一步因此真的越过窗口、被 `_drain_wait()` 放弃（落
    `failed`），而那次调用**还在线程里跑**。此时 `log` 那一步**尚有一条线程可用**：它的
    flush、结束标记与摘除在 `DRAIN_LOG_MIN_BUDGET_SECONDS` 之内跑完、落 `done`，并真的把
    `session.json` 写下来（下面读盘核对）。

    **线程池只有一条线程时这一条必红**：`log` 的那次调用排在闸门后面，整趟收尾的墙钟要等
    到闸门被放开那一下才收场；`log` 的调用则要等光它那 5 秒的最小预算才轮到、那一步落
    `failed`，盘上那份 `session.json` 也不会出现。用例结束前把闸门放开，那条被放弃的调用
    随即收场，不留悬挂线程。
    """
    import threading

    from meeting.backends.registry import BackendResolution
    from meeting.state import begin_drain

    runtime = _inprocess_runtime_with_backends(tmp_path, monkeypatch)
    gate = threading.Event()
    finalized = {"n": 0}

    class GatedCapture:
        """第 1 步卡在闸门上、其余调用立刻返回的采集后端（一次 `finalize()` 占住一条线程）。"""

        def start(self, mode, session_dir) -> None:
            return None

        def stop(self) -> None:
            return None

        def finalize(self) -> None:
            finalized["n"] += 1
            gate.wait(timeout=DRAIN_ABANDONED_GATE_SECONDS)
            return None

        def transcode(self) -> None:
            return None

    capture = GatedCapture()
    monkeypatch.setattr(runtime, "capture_backend", capture)
    monkeypatch.setattr(
        runtime,
        "capture_capabilities",
        {"finalize": True, "transcode": True, "dev_inject": False},
    )
    monkeypatch.setattr(
        runtime,
        "capture_resolution",
        BackendResolution(
            key="capture.backend",
            selector="gated",
            target=f"{__name__}:GatedCapture",
            implementation=capture,
            has_implementation=True,
            detail="本用例造的现场（一条闸门占着工作线程）",
        ),
    )
    monkeypatch.setattr(runtime, "_resolve_backends", lambda: None)
    monkeypatch.setattr(
        "meeting.service.runtime.compute_drain_budget",
        lambda backlog, segment_timeout: DRAIN_ABANDONED_TOTAL_BUDGET_SECONDS,
    )

    still_flying = {"value": None}
    ws_path = Path(_new_workspace(tmp_path, "abandoned-flush"))
    try:

        async def one_pass() -> None:
            runtime.start_session(dir=str(ws_path))
            begin_drain(runtime.snapshot, reason="user")
            await runtime.drain()

        started = time.monotonic()
        asyncio.run(one_pass())
        spent = time.monotonic() - started
        # 收尾收场那一刻的现场：被放弃的那次 `finalize()` 还卡在闸门上（下面才放开）
        still_flying["value"] = not gate.is_set()
    finally:
        gate.set()

    statuses = {step.id: step.status for step in runtime.snapshot.ended.steps}
    flush_step = next(step for step in runtime.snapshot.ended.steps if step.id == "flush")
    log_step = next(step for step in runtime.snapshot.ended.steps if step.id == "log")
    assert finalized["n"] == 1, f"第 1 步该被调到恰好一次：{finalized}"
    assert flush_step.status == "failed", (flush_step.status, flush_step.error)
    assert "超出收尾预算" in (flush_step.error or ""), flush_step.error
    assert log_step.status == "done", (
        f"前一步被放弃之后 log 那一步仍该落定：{statuses}"
        f"（log 的 detail={log_step.detail!r}、error={log_step.error!r}）"
    )
    # 这一步做的是真 IO，判据因此落在盘上：本场的场次目录与 `session.json` 都该在
    disk = ws_path / SESSION_FILE_NAME
    assert disk.exists(), f"log 那一步该把这一场写下来：{disk} 不存在"
    # 被放弃的那次调用在收尾收场那一刻还卡在闸门上，而整趟收尾已经结束 —— 墙钟因此只落在
    # `log` 那一步自己的预算之内；池里只有一条线程时它会顶到整份额度那一档。
    assert still_flying["value"] is True, (
        "收尾收场那一刻，被放弃的那次调用该还在飞（闸门还没放开）"
    )
    assert spent < float(DRAIN_LOG_MIN_BUDGET_SECONDS) * DRAIN_ABANDONED_WALL_CLOCK_FACTOR, (
        f"这一趟收尾花了 {spent:.3f}s：前一步被放弃之后，后续步该立刻拿到一条线程"
    )
    print(
        f"[m7 证据] flush 卡在闸门上（窗口 {DRAIN_ABANDONED_FLUSH_WINDOW_SECONDS}s）-> "
        f"flush {flush_step.status}（error={flush_step.error!r}）；"
        f"log {log_step.status}（detail={log_step.detail!r}）；"
        f"盘上 {disk.name} 已写出；被放弃的 finalize 收场时仍在飞={still_flying['value']}；"
        f"整趟实测墙钟={spent:.3f}s"
    )


# ---------------------------------------------------------------------------
# ⑤c 上限跟着**传进来的那条预算**走（**进程内**，两组小 deadline）
#
# 审查问题 M3 / M4 要的那一半就在这一条：上限来自预算函数、且它随预算变化。两组毫秒级的
# deadline 把「随预算变化」量成可判定的事实，而不必真等满一条 30 秒的预算。
# ---------------------------------------------------------------------------


def test_drain_wait_upper_bound_tracks_the_passed_deadline(dev_runtime):
    """一趟收尾的**等待上限就是传进来的那条预算**，不会是别的什么秒数（审查问题 M3 / M4 的判据）。

    「不许再出现写死的 10 秒收尾等待」这条判据要能咬住两件事：上限**来自预算**、且它**随预算
    变化**。同一条 0.2 秒的假步配两条 deadline：

      - **紧**的那条（0.02 秒）在假步跑完之前就到期 → 这一步落 `DrainLimitExceeded`，
        自报的实测耗时贴着 0.02 秒（**远小于**假步那 0.2 秒）；
      - **宽**的那条（0.5 秒）把同一段假步放行 → 调用正常返回，`_drain_wait()` 报出的实测耗时
        落在这一段假步自己的时长上（0.2 秒上下），**不会**被迫等在一条更长的预算上。

    两条合起来说明：上限跟着传进来的 deadline 走。若它是个写死的常量（V1 那个 `timeout=10`），
    紧的那一趟会**照跑完 0.2 秒**、第一条断言当场红；宽的那一趟也不会刚好在 0.2 秒上下收住。
    这个现场跑在进程内、量级在毫秒到零点几秒之间，「真等满一条预算」因此不必去等 30 秒。
    """
    runtime = dev_runtime

    def step() -> str:
        time.sleep(DRAIN_DEADLINE_STEP_SECONDS)
        return "跑完了"

    # 紧的那条 deadline：上限明显短于这一步自己的时长 → 超出即 `DrainLimitExceeded`
    started = time.monotonic()
    with pytest.raises(DrainLimitExceeded) as caught:
        asyncio.run(
            runtime._drain_wait(
                "flush",
                step,
                deadline=started + DRAIN_TIGHT_DEADLINE_SECONDS,
                invoke=True,
            )
        )
    message = str(caught.value)
    match = re.search(r"实测耗时 ([\d.]+)s", message)
    assert match, message
    measured = float(match.group(1))
    assert measured < DRAIN_DEADLINE_STEP_SECONDS / 2, (
        f"{DRAIN_TIGHT_DEADLINE_SECONDS}s 的上限没能拦住这一步：它跑了 {measured}s"
        f"（自己的时长是 {DRAIN_DEADLINE_STEP_SECONDS}s）—— 上限没跟着传进来的 deadline 走"
        f"（{message}）"
    )

    # 宽的那条 deadline：同一段假步被放行，实测耗时就是这一步自己的时长（比紧的那趟宽得多）
    value, wide_measured = asyncio.run(
        runtime._drain_wait(
            "flush",
            step,
            deadline=time.monotonic() + DRAIN_WIDE_DEADLINE_SECONDS,
            invoke=True,
        )
    )
    assert value == "跑完了"
    assert DRAIN_DEADLINE_STEP_SECONDS <= wide_measured < DRAIN_DEADLINE_STEP_SECONDS * 3, (
        f"宽 deadline 那一趟的实测耗时 {wide_measured}s 该落在"
        f" [{DRAIN_DEADLINE_STEP_SECONDS}, {DRAIN_DEADLINE_STEP_SECONDS * 3}) 之内 —— "
        "上限跟着预算走，这一步才走得出这个区间"
    )


def test_drain_wait_does_not_pin_the_event_loop(dev_runtime):
    """被 `_drain_wait()` 包住的**阻塞型同步步**期间，事件循环照常推进（收尾可观测）。

    `finalize()` / `launch()` / 场次 log 的 flush 与摘除都是同步 IO：它们经**收尾专用的
    daemon 执行器**提交到工作线程再连同等待一起包进上限。若哪一条路把同步调用留在
    事件循环上，下面的计数任务在整段阻塞里就一次都跑不到 —— 收尾期间 `/api/events` 与
    `GET /api/state` 也就一起停摆（⑩ 条在真服务子进程里断的是同一件事的进程级形态）。
    """
    runtime = dev_runtime

    async def scenario() -> tuple[Any, int, float]:
        ticks = {"n": 0}

        async def ticker() -> None:
            while True:
                ticks["n"] += 1
                await asyncio.sleep(0.005)

        beat = asyncio.ensure_future(ticker())
        try:
            value, measured = await runtime._drain_wait(
                "post",
                lambda: time.sleep(DRAIN_BLOCKING_STEP_SECONDS) or "启动即完成",
                deadline=time.monotonic() + DRAIN_BLOCKING_STEP_SECONDS + 5.0,
                invoke=True,
            )
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
        return value, ticks["n"], measured

    value, ticks, measured = asyncio.run(scenario())
    assert value == "启动即完成"
    assert measured >= DRAIN_BLOCKING_STEP_SECONDS, measured
    assert ticks >= 10, (
        f"阻塞 {DRAIN_BLOCKING_STEP_SECONDS}s 期间事件循环只转了 {ticks} 次 —— "
        "同步阻塞被留在了事件循环上"
    )


# ---------------------------------------------------------------------------
# ⑤d 「等收尾落定」的上限与本次收尾同源（**进程内**，审查问题 M5）
#
# `_wait_drain_settled()` 原先自己再读一次预算输入（`_drain_inputs()`）：真转写后端的
# `backlog_depth()` 会随收尾推进而下降，第二次读到一个更小的积压量就得到一条更短的上限，
# 等待于是在收尾还没跑完时放弃、留下 `draining` 现场。修法是由收尾那一趟把预算交给运行时
# （`_drain_budget` / `_drain_deadline`），等待直接沿用。下面这一条把「同源」钉成两件实测的
# 事：**输入只读一次**，以及**等待上限 = 本次那笔预算 + `log` 步最小预算**。
# ---------------------------------------------------------------------------


class _DecreasingBacklogTranscriber:
    """进程内的**读数**替身：`backlog_depth()` 一次比一次**小**（真后端收尾推进时的形状）。

    `segment_timeout()` 恒为 20 秒，于是预算就是 `30 + backlog × 25` 那一支。

    **它不是 `tests/_drain_fakes.py` 那一套「挂进子进程的假后端」**（§3.8 的五个假件是
    `FakeCaptureBackend` / `FakeTranscriberBackend` / `FakePostHook` / `FailingPostHook` /
    `BlockingThreadPostHook`）：这一条要的是「同一个对象被读两次、第二次的读数更小」这件事本身
    （⑤d「预算输入只读一次」的判据），**必须在本进程里被用例直接持有、且带 `reads` 计数**，
    子进程里那份假件没法把这个计数带回用例进程。它经 `runtime.transcriber_backend = ...` 直接
    注入进程内运行时，不写临时模块、不经配置键、`pytest` 也不收集它。
    """

    def __init__(self) -> None:
        self.reads = 0

    def backlog_depth(self) -> int:
        self.reads += 1
        return max(0, 10 - 5 * (self.reads - 1))

    def segment_timeout(self) -> float:
        return 20.0


def test_drain_settle_wait_shares_the_drain_budget(tmp_path, monkeypatch):
    """「等落定」的上限与本次收尾**同源**：预算输入只读一次，上限就是那一笔预算（M5）。

    **「第二次读数更小」那个场合怎么造出来**：假转写后端的 `backlog_depth()` 一次比一次小
    （`10 → 5 → 0`，真后端在收尾推进时就是这个形状）。于是「再读一次」会让上限从 `280s`
    掉到 `30s` —— 这正是原实现的来路：转写后端在场时，收尾跑到一半重算预算会得到一条比收尾
    自己那条 deadline 更短的等待上限，等待于是在收尾还没跑完时就放弃、留下 `draining` 现场，
    而退出码看着还是干净档。

    本用例断的是**同源这件事本身**（两件互相独立的事实）：

      - **预算输入只被读了一次**（`inputs_reads == 1`）：第二次读那条路已经不在实现里 ——
        等待沿用收尾那一趟算出来的预算（`_drain_budget`），重算那条路已经消失；
      - **等待上限 = 本次那笔预算 + `log` 步最小预算**（`_drain_wait_timeout()` 的返回值）：
        `log` 步的 flush 与摘除是共享预算之外唯一的一笔（§3.3）。

    上限真的被 `_wait_drain_settled()` 用上这一半，由
    `test_drain_settle_wait_honours_the_shared_budget` 用一条两倍大的上限量出来（零点几秒，
    真等 280 秒那件事不必发生）。
    """
    runtime = _inprocess_runtime(tmp_path, monkeypatch)
    backend = _DecreasingBacklogTranscriber()
    runtime.transcriber_backend = backend
    inputs_reads = {"n": 0}

    def drain_inputs() -> tuple[int, float]:
        """这一趟收尾的预算输入（与 `ServiceRuntime._drain_inputs()` 同形）。"""
        inputs_reads["n"] += 1
        backlog = int(backend.backlog_depth())
        return backlog, float(backend.segment_timeout())

    runtime._drain_inputs = drain_inputs  # type: ignore[method-assign]
    backlog, segment_timeout = runtime._drain_inputs()
    budget = compute_drain_budget(backlog, segment_timeout)
    assert budget > DRAIN_BUDGET_MIN_SECONDS, f"这一档该离开下界那一格：budget={budget}"

    # 第二次读会拿到**更小**的积压量（这一格就是原实现短路的来路）；这两行只是把假件的形状
    # 摆出来，它们自己也会让 `backend.reads` 往上走，因此下面数的是 `inputs_reads`
    assert backend.backlog_depth() < backlog, "假件该是一次比一次小的形状"
    assert compute_drain_budget(backend.backlog_depth(), segment_timeout) < budget

    # ① 上限跟着**本次那笔预算**走（真现场里这一笔是 280 秒，这里就是它算出来的那个数）
    runtime._drain_budget = budget
    runtime._drain_deadline = None
    assert runtime._drain_wait_timeout() == budget + float(DRAIN_LOG_MIN_BUDGET_SECONDS)

    # ② 收尾还在跑的那一档：上限 = 这条 deadline 的**剩余时间** 加 `log` 步最小预算
    runtime._drain_deadline = time.monotonic() + DRAIN_DEADLINE_STEP_SECONDS
    remaining = runtime._drain_wait_timeout() - float(DRAIN_LOG_MIN_BUDGET_SECONDS)
    assert 0.0 < remaining <= DRAIN_DEADLINE_STEP_SECONDS, remaining

    # ③ 全程只有这一次读：等待没用第二次读数另算一条上限
    assert inputs_reads["n"] == 1, (
        f"预算输入被读了 {inputs_reads['n']} 次 —— 「等落定」又自己算了一条更短的上限"
    )
    print(
        f"[M5 证据] 第一次读数 backlog={backlog} -> budget={budget}s；"
        f"「第二次读数更小」那一格实测 backlog={max(0, 10 - 5)} -> "
        f"budget={compute_drain_budget(0, segment_timeout)}s；"
        f"等待用的是本次那笔预算：_drain_wait_timeout()="
        f"{budget + float(DRAIN_LOG_MIN_BUDGET_SECONDS)}s；预算输入全程只读 "
        f"{inputs_reads['n']} 次"
    )


def test_drain_settle_wait_honours_the_shared_budget(tmp_path, monkeypatch, capsys):
    """`_wait_drain_settled()` 真的按那条上限等（到点如实落一行、不静默）。

    现场：手上有预算、没有 deadline、也没有收尾在跑（收尾早跑完了的那一档），上限传成
    `DRAIN_SETTLE_TINY_BUDGET_SECONDS` 的两倍 —— 于是等待到点返回的耗时落在两条线之间：
    明显长于当天那笔预算（小于它就说明等待被算短了），且不超过传进去的那条上限加容差。
    上限**从哪里来**这一半由 `test_drain_settle_wait_shares_the_drain_budget` 断
    （预算输入只读一次、`_drain_wait_timeout()` 的返回值就是那笔预算加 `log` 步最小预算）。

    到点那一行 `等收尾落定超时（…s）：按当前落盘的那一版关停，不静默` 同时被断言（§8 的
    「不许静默」在等待这一路上的落点）。
    """
    runtime = _inprocess_runtime(tmp_path, monkeypatch)
    budget = float(DRAIN_SETTLE_TINY_BUDGET_SECONDS)
    runtime._drain_budget = budget
    runtime._drain_deadline = None
    timeout = budget * DRAIN_SETTLE_WAIT_MULTIPLE

    began = time.monotonic()
    asyncio.run(runtime._wait_drain_settled(timeout=timeout))
    waited = time.monotonic() - began

    assert waited >= budget, (
        f"等待 {waited:.3f}s 比当天那笔预算 {budget}s 还短 —— 上限被算短了"
    )
    assert waited <= timeout + DRAIN_SETTLE_WAIT_TOLERANCE_SECONDS, (
        f"等待 {waited:.3f}s 超出了传进去的那条上限 {timeout}s"
    )
    assert "等收尾落定超时" in capsys.readouterr().err, "到点该如实落一行，不许静默"


# ---------------------------------------------------------------------------
# ⑤g 「一趟收尾的总时长随积压量变大」—— 量的那一半（**进程内**，审查问题 M3）
#
# ⑤（`backlog = 0`）与 ⑤b2（`backlog = 10`）各起一个真服务、各走一趟收尾，两条的**实际
# 耗时**都在 1 秒上下（假件是可控拖长，谁都等不到自己那条 30 秒 / 280 秒的上限），因此
# 「积压量调大 → 一趟收尾的总时长随之变大」这条关系在那两条里量不到。⑤b2 已经钉住的另一半
# 是「运行时用的那条 deadline 就是预算函数对同一组输入算出来的那个数」（log 里的数值与
# 函数输出逐值相等），⑤c 钉住的是「上限跟着传进来的 deadline 走」。
#
# 这一条补上剩下的一半，判据仍然是**同一个拖长假步配两组不同的 `backlog`**：两组的上限都
# 取自 `compute_drain_budget()`（不抄裸数），量的是运行时**真的把这一趟的等待上限抬上去了**
# —— 「等这一趟收尾落定」用的那条上限（`_drain_wait_timeout()`）在大积压档上比小积压档大出
# 预算函数算出的那一整笔差额，而且它逐值等于 `compute_drain_budget(这一趟的输入) + log 步
# 最小预算`。**积压量真的变了、可等待的上限真的跟着变了**，两件事都由这一条量到。
#
# 为什么不真等：这一档的上限是 30 秒与 280 秒，真等满两条都要跨过「单条用例 ≤ 5 秒」的红线。
# 拖长的那一步（`finalize()`）只睡零点几秒 —— 它在这里的作用是**把测量点钉在收尾确实还在跑
# 的那一段里**（上限落到「本次预算 + log 步最小预算」那一格），而不是把上限等满。
# ---------------------------------------------------------------------------


def test_drain_whole_pass_upper_bound_grows_with_the_backlog(tmp_path, monkeypatch):
    """同一个拖长假步配两组 `backlog`：可等待的上限随 `compute_drain_budget()` 的输出变大（M3）。

    两组现场只差 `backlog_depth()` 报的数（`DRAIN_OVERRUN_BACKLOG_SMALL` / `..._LARGE`），
    `segment_timeout()` 都是 `DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS`：

      - **小积压档**：`compute_drain_budget()` 的返回值就是下限那一格
        （`DRAIN_BUDGET_MIN_SECONDS`）；
      - **大积压档**：同一函数产出公式那一档（明显离开下限），运行时那一趟的可等待上限
        因此比小积压档**大出预算函数算出的那一整笔差额**。

    每档三件事一起断：这一趟的收尾**真的在跑**（在飞的那一步停在 `running`，测量点因此落在
    「本次预算」那一格）、「等落定」的上限**贴着预算函数对同一组输入算出来的值加上 `log` 步
    最小预算**（只差测量那一刻收尾用掉的时间，容差 `DRAIN_SETTLE_WAIT_TOLERANCE_SECONDS`）、
    两档的上限之差**就是两组预算之差**（同一条容差）。收尾真的还在跑、两档的上限真的差出
    一整笔预算 —— 这两件事都量到了。
    """
    backlog_small = DRAIN_OVERRUN_BACKLOG_SMALL
    backlog_large = DRAIN_OVERRUN_BACKLOG_LARGE
    segment_timeout = DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS
    budget_small = compute_drain_budget(backlog_small, segment_timeout)
    budget_large = compute_drain_budget(backlog_large, segment_timeout)
    assert budget_small == DRAIN_BUDGET_MIN_SECONDS, budget_small
    assert budget_large > budget_small, (budget_small, budget_large)

    from meeting.backends.registry import BackendResolution
    from meeting.state import begin_drain

    runtime = _inprocess_runtime(tmp_path, monkeypatch)

    # 采集假件：`finalize()` 拖长 `DRAIN_OVERRUN_STEP_SECONDS`，于是收尾有一小段「确实还在跑」
    # 的窗口可以把上限读出来。**三样一起给**（与 ⑤e 条同一处口径）：`_flush` 那一步的判定读的
    # 是**解析结论**（`capture_resolution` 缺席就记 `skipped`，拖长的假件根本不会被调到）。
    # `transcode` 这一项如实开成 `False`（本用例不需要第 2 步跑起来），收尾第 2 步因此照常记
    # `skipped`，这一条量的仍然是「同一趟收尾的上限」
    backend = fakes.FakeCaptureBackend(finalize_delay_seconds=DRAIN_OVERRUN_STEP_SECONDS)
    runtime.capture_backend = backend
    runtime.capture_capabilities = {"finalize": True, "transcode": False, "dev_inject": False}
    runtime.capture_resolution = BackendResolution(
        key="capture.backend",
        selector="fake",
        target="meeting_drain_fakes:FakeCaptureBackend",
        implementation=backend,
        has_implementation=True,
        detail="本用例造的现场（进程内直接注入运行时成员）",
    )
    monkeypatch.setattr(runtime, "_resolve_backends", lambda: None)

    transcriber = fakes.FakeTranscriberBackend(
        backlog_depth=backlog_small, segment_timeout=segment_timeout
    )
    monkeypatch.setattr(runtime, "transcriber_backend", transcriber)
    # 预算输入读的是本用例那两档（与 `ServiceRuntime._drain_inputs()` 同形）
    monkeypatch.setattr(
        runtime,
        "_drain_inputs",
        lambda: (int(transcriber.backlog_depth()), float(transcriber.segment_timeout())),
    )

    samples: dict[int, dict[str, Any]] = {}

    async def one_pass(backlog: int) -> dict[str, Any]:
        transcriber._backlog = int(backlog)
        session = runtime.start_session(dir=_new_workspace(tmp_path, f"budget-{int(backlog)}"))
        begin_drain(runtime.snapshot, reason="user")
        task = asyncio.get_running_loop().create_task(runtime.drain())
        drain_started = time.monotonic()
        # **确定的同步点**：轮询到在飞的那一步进入 `running`（收尾确实在跑），才把上限读出来。
        # `drain()` 把这一趟的预算交给运行时的那一刻起算，量到这里花掉的时间：上限就是
        # 「本次预算 − 已用时间 + log 步最小预算」（§3.3 的「剩余预算」那一句）
        deadline = time.monotonic() + DRAIN_POLL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if runtime.snapshot.drain_step("flush").status == "running":
                break
            await asyncio.sleep(DRAIN_OVERRUN_POLL_SECONDS)
        else:  # pragma: no cover - 收尾一定先进 running
            raise AssertionError(
                f"等不到 flush 那一步进入 running（实测 "
                f"{runtime.snapshot.drain_step('flush').status!r}）"
            )
        sampled_at = time.monotonic()
        wait_timeout = float(runtime._drain_wait_timeout())
        # 收尾确实还在跑：读数与「这一趟还没落定」同时成立
        running = not runtime.drain_has_settled()
        await task
        return {
            "backlog": int(backlog),
            "wait_timeout": wait_timeout,
            "spent": sampled_at - drain_started,
            "running_when_sampled": running,
            "session_dir": session.dir,
            "phase": runtime.snapshot.ended.phase,
        }

    for backlog in (backlog_small, backlog_large):
        samples[backlog] = asyncio.run(one_pass(backlog))

    for backlog, sample in samples.items():
        budget = compute_drain_budget(backlog, segment_timeout)
        # 这一档的收尾**确实还在跑**（在飞的那一步停在 `running`）：读数因此落在
        # 「本次那笔预算」那一格（§3.3 的「等待上限 = 这条 deadline 的剩余时间 加 log 步最小预算」）
        assert sample["running_when_sampled"], (
            f"backlog={backlog} 那一档的读数落在「收尾还在跑」之外：{sample}"
        )
        # 上限**取自这一档的预算**：读到的值 = 那笔预算 − 收尾已经用掉的时间 + `log` 步保底。
        # 减掉实测的 `spent` 之后，它与预算函数对同一组输入算出来的值逐值相等（差额只是一次
        # 轮询往返的调度开销，容差 `DRAIN_SETTLE_WAIT_TOLERANCE_SECONDS`）
        predicted = budget - sample["spent"] + float(DRAIN_LOG_MIN_BUDGET_SECONDS)
        assert abs(sample["wait_timeout"] - predicted) <= DRAIN_SETTLE_WAIT_TOLERANCE_SECONDS, (
            f"backlog={backlog} 那一档的可等待上限该贴着 "
            f"compute_drain_budget({backlog}, {segment_timeout}) − 已用 "
            f"{sample['spent']:.3f}s + DRAIN_LOG_MIN_BUDGET_SECONDS = {predicted:.3f}s"
            f"（容差 {DRAIN_SETTLE_WAIT_TOLERANCE_SECONDS}s），实测 {sample['wait_timeout']:.3f}s"
        )
    # **这一条要量的关系**：积压量调大 → 可等待的上限随之变大，差额就是两组预算之差
    grew = samples[backlog_large]["wait_timeout"] - samples[backlog_small]["wait_timeout"]
    budget_gap = budget_large - budget_small
    spend_gap = samples[backlog_large]["spent"] - samples[backlog_small]["spent"]
    expected_gap = budget_gap - spend_gap
    assert abs(grew - expected_gap) <= DRAIN_SETTLE_WAIT_TOLERANCE_SECONDS, (
        f"两档可等待的上限之差该是两组预算之差 {budget_gap}s 扣掉两趟测量点之差 {spend_gap:.3f}s "
        f"= {expected_gap:.3f}s（容差 {DRAIN_SETTLE_WAIT_TOLERANCE_SECONDS}s），实测 {grew:.3f}s "
        "—— 一趟收尾的总时长没有随积压量变大"
    )
    assert grew > DRAIN_OVERRUN_STEP_SECONDS, grew
    # 反过来读一遍这条关系：小档的可等待上限落在**大档那条预算之下**（两组分得开）
    assert samples[backlog_small]["wait_timeout"] < budget_large, (
        samples[backlog_small],
        budget_large,
    )

    print(
        f"[M3 证据] 同一个拖长假步（finalize() 睡 {DRAIN_OVERRUN_STEP_SECONDS}s）配两组积压量：\n"
        f"  小档 backlog={backlog_small} → compute_drain_budget={budget_small}s → "
        f"可等待上限实测 {samples[backlog_small]['wait_timeout']:.3f}s"
        f"（收尾在跑：{samples[backlog_small]['running_when_sampled']}、终态 "
        f"{samples[backlog_small]['phase']!r}）\n"
        f"  大档 backlog={backlog_large} → compute_drain_budget={budget_large}s → "
        f"可等待上限实测 {samples[backlog_large]['wait_timeout']:.3f}s"
        f"（收尾在跑：{samples[backlog_large]['running_when_sampled']}、终态 "
        f"{samples[backlog_large]['phase']!r}）\n"
        f"  两档之差实测 {grew:.3f}s ≈ 预算函数两档之差 {budget_gap}s"
    )


# ---------------------------------------------------------------------------
# ⑤e 取消路径真的经 `_settle_step(..., cancelled=True)`（**进程内**，审查问题 M2）
#
# 契约 §3.5 第 3 条与 §2.4 要求「取消时按收尾步骤表落盘」，并给出三条**不覆盖约束**：
# 不许把第 1 步写下的 `interrupted: true` 抹回 `false`、不许改 `state`、不许补 `ended_at`。
# `ServiceRuntime._settle_step(..., cancelled=True)`（它把 `frozen_ended` / `frozen_interrupted`
# 双双置真）就是这条机制 —— 但在修本条之前**一个调用点都没有**，`frozen_*` 收到的永远是
# 默认的 `False`，那三条约束没有一条被实际走到（相2 的
# `tests/test_state_machine.py::test_cancelled_drain_keeps_the_same_face_without_done`
# 只经 `finish_drain()`，走不到 `_settle_step` 这一层）。这一条把「真的接线了」钉住。
# ---------------------------------------------------------------------------


def test_cancelled_drain_routes_through_the_frozen_accounting(tmp_path, monkeypatch):
    """收尾被 `cancel()` 时，**取消路径真的经 `cancelled=True` 那条记账机制**（审查问题 M2）。

    现场（进程内、可控、毫秒级）：把在飞的那一步钉住（假采集后端的 `finalize()` 睡
    `DRAIN_CANCEL_STEP_SECONDS`），等它进入 `running` 之后 `cancel()` 这一趟收尾。三条都断：

      1. **机制真的被走到**：`finish_drain_step` 收到的 `frozen_ended` 与 `frozen_interrupted`
         都是 `True`（`_settle_step(..., cancelled=True)` 的两处实参），`_finish_drain` 收到的
         `cancelled` 也是 `True`；
      2. **在飞那一步不伪造终态**：它照实记 `running`（取消打断的这一步既没跑完也没抛错），
         已落定的与未开跑的一个都不改 —— 契约 §3.5 第 3 条的「已落定的步骤保持原样、
         未开跑的维持 `waiting`」；
      3. **三条不覆盖约束在盘上成立**：先按强制打断的顺序写一版
         `state="draining"` + `interrupted=true` + `ended_at=None`（就是 `mark_interrupted()`
         那一版），取消之后**逐字段不变**：`interrupted` 还是 `true`、`state` 还是 `draining`、
         `ended_at` 还是 `None`。

    整条用例不起服务、只跑一趟被取消的 `drain()`，墙钟在零点几秒的量级。
    """
    from meeting.backends.registry import BackendResolution
    from meeting.state import DRAIN_STEP_STATUS, begin_drain

    runtime = _inprocess_runtime(tmp_path, monkeypatch)

    # **先把后端的解析结论钉住，再建场次**（顺序是硬要求）：`start_session()` 内部会经
    # `_resolve_backends()` 重算 `capture_backend` / `capture_capabilities` / `capture_resolution`
    # 三样，先注入假件就会被它整个盖掉 —— 那样第 1 步会因为「采集替身没有 `finalize` 能力」
    # 记 `skipped`，「在飞的那一步被取消」这个同步点根本造不出来。
    backend = fakes.FakeCaptureBackend(finalize_delay_seconds=DRAIN_CANCEL_STEP_SECONDS)
    runtime.capture_backend = backend
    runtime.capture_capabilities = {"finalize": True, "transcode": False, "dev_inject": False}
    runtime.capture_resolution = BackendResolution(
        key="capture.backend",
        selector="fake",
        target="meeting_drain_fakes:FakeCaptureBackend",
        implementation=backend,
        has_implementation=True,
        detail="本用例造的现场（进程内直接注入运行时成员）",
    )
    monkeypatch.setattr(runtime, "_resolve_backends", lambda: None)

    session = runtime.start_session(dir=_new_workspace(tmp_path, "resolve-pinned"))
    assert runtime.capture_backend is backend, "解析结论该是本用例注入的那一份（没有被重算盖掉）"
    begin_drain(runtime.snapshot, reason="user")
    runtime._write_session(interrupted=True)  # 强制打断的**先落盘**那一版
    interrupted_before = read_session_json(session.dir)
    assert interrupted_before["state"] == "draining", interrupted_before
    assert interrupted_before["interrupted"] is True, interrupted_before
    assert interrupted_before["ended_at"] is None, interrupted_before

    # **记下 `finish_drain_step` / `_finish_drain` 收到的那些关键字**（相2 的记账出口）。
    # 补丁打在 **`meeting.service.runtime` 的模块级名字**上：`runtime.py` 是
    # `from meeting.state import finish_drain_step` 这样取进来的，`_settle_step()` 读的是
    # **它自己模块的那个全局名** —— 改 `meeting.state` 上的同名属性不会被它看见（这一步是
    # 「补丁打在哪一个命名空间」的问题，不是记账没发生）。
    from meeting.service import runtime as runtime_module

    seen: dict[str, Any] = {"finish_drain_step": [], "finish_drain": []}
    real_finish_step = runtime_module.finish_drain_step

    def spy_finish_step(step_id, status, detail=None, error=None, **kwargs):
        seen["finish_drain_step"].append(
            {
                "step_id": step_id,
                "status": status,
                "frozen_ended": kwargs.get("frozen_ended"),
                "frozen_interrupted": kwargs.get("frozen_interrupted"),
            }
        )
        return real_finish_step(step_id, status, detail, error, **kwargs)

    monkeypatch.setattr(runtime_module, "finish_drain_step", spy_finish_step)
    real_finish_drain = runtime._finish_drain

    def spy_finish_drain(*, cancelled: bool = False):
        seen["finish_drain"].append(cancelled)
        return real_finish_drain(cancelled=cancelled)

    monkeypatch.setattr(runtime, "_finish_drain", spy_finish_drain)

    async def cancel_during_the_blocked_step() -> None:
        task = asyncio.get_running_loop().create_task(runtime.drain())
        # 确定同步点：轮询到 `flush` 那一步进了 `running`（收尾确实在跑）再取消
        deadline = time.monotonic() + DRAIN_POLL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if runtime.snapshot.drain_step("flush").status == "running":
                break
            await asyncio.sleep(DRAIN_CANCEL_POLL_SECONDS)
        else:  # pragma: no cover - 收尾一定先进 running
            raise AssertionError(
                f"等不到 flush 那一步进入 running（实测 {runtime.snapshot.drain_step('flush').status!r}）"
            )
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(cancel_during_the_blocked_step())

    # ① 机制真的被走到：两处 `frozen_*` 都是 True，终态提交也带 cancelled=True
    frozen_calls = [item for item in seen["finish_drain_step"] if item["frozen_ended"] is True]
    assert frozen_calls, f"取消路径没有把 frozen_ended 置真：{seen['finish_drain_step']}"
    assert all(item["frozen_interrupted"] is True for item in frozen_calls), seen[
        "finish_drain_step"
    ]
    assert seen["finish_drain"] == [True], seen["finish_drain"]
    print(f"[M2 证据] 取消路径上的记账调用：{seen['finish_drain_step']}；终态提交：{seen['finish_drain']}")

    # ② 在飞那一步照实记 running（不伪造终态）；其余步骤维持原样
    statuses = {step.id: step.status for step in runtime.snapshot.ended.steps}
    assert statuses["flush"] == "running", statuses
    assert set(statuses.values()) <= set(DRAIN_STEP_STATUS), statuses
    assert "failed" not in statuses.values(), statuses
    for step_id in DRAIN_STEP_IDS[1:]:
        assert statuses[step_id] == "waiting", statuses
    assert runtime.snapshot.ended.phase == "draining", runtime.snapshot.ended
    assert runtime._drain_task is None, "走完（含被取消那趟）后句柄该置空"

    # ③ 三条不覆盖约束在盘上成立：先落盘那一版逐字段不变
    after = read_session_json(session.dir)
    assert after["interrupted"] is True, after
    assert after["state"] == "draining", after
    assert after["ended_at"] is None, after
    print(
        f"[M2 证据] 取消之后盘上那一版：state={after['state']!r} / "
        f"interrupted={after['interrupted']!r} / ended_at={after['ended_at']!r}"
        f"（与先落盘那一版逐字段相同）"
    )


# ---------------------------------------------------------------------------
# ⑤i 强制打断档：现场**前面已有 `failed` 步**时，取消路径的落盘仍把 `state` 钉在 `draining`
#     （**进程内**，审查问题 C2）
#
# §3.5 表第三 / 第四行给这一档写死的盘上三件套是「`state: "draining"`、`interrupted: true`、
# 无 `ended_at`」，第 3 条的第二条不覆盖约束又补了一句「**不许把 `state` 从 `draining` 改成
# 别的值**」。但取消路径的落盘走的是 `state_for()` 的四档映射：现场里前面已经有一步 `failed`
# 时，那份映射按 `has_failed_step → failed` 算出 `"failed"`，把 `mark_interrupted()` 刚写下的
# `draining` **覆盖掉**——于是「强制打断 + 前面有失败步」这一格盘上留的是 `state: "failed"`，
# 与契约要求的那三件套对不上。
#
# **收口的位置在 phase3 这条路径上**：`state_for()` 的四档映射是 phase2 冻结的跨 Phase 契约
# （取值域与优先级一个字都不动），改的是**取消路径的写值** —— `_settle_step(..., cancelled=True)`
# 落盘时显式把 `state="draining"` 传下去（见 `ServiceRuntime._write_session()` 的 `state=` 参数），
# 不让映射在这一格上接管。这一条把该行为钉住。
# ---------------------------------------------------------------------------


def test_cancelled_drain_keeps_draining_even_with_a_failed_step_before_it(tmp_path, monkeypatch):
    """强制打断档：前面已有 `failed` 步 + 收尾在飞 → 盘上仍是 `draining` + `interrupted`（C2）。

    现场（进程内、毫秒级）：第 1 步 `flush` 的假件**抛错**（有实现却没做成 → 落 `failed`），
    第 4 步 `post` 的假钩子把这一刻钉住（`BlockingThreadPostHook` 在工作线程里阻塞，收尾因此
    停在「`log` 已落定、`post` 正在跑」这一格）；随后按 §3.5 的顺序走**强制打断**那一趟：
    先 `mark_interrupted()` 落盘（`draining` + `interrupted: true` + 无 `ended_at`），
    再 `cancel()` 收尾 task。

    四件事一起断：

      1. **盘上 `state` 仍是 `draining`**（前面那一步 `failed` 还在，`ended.phase` 也仍是
         `draining`）—— 取消路径的落盘不许把它改成 `failed`；
      2. `interrupted is True`（先落盘那一版没被抹回否）；
      3. **无 `ended_at`**（强制打断路径不写它）；
      4. **原 `steps` 保留**：`flush` 仍是 `failed`、`transcode` 仍是自己的终态、
         `log` 仍是 `done`、在飞那一步照实记 `running`（不伪造终态）；
         退出码那一档由 `meeting/cli.py` 的 `_drain_exit_code()` 给出 `EXIT_INTERRUPTED`。
    """
    from meeting.backends.registry import BackendResolution
    from meeting.cli import EXIT_INTERRUPTED, _drain_exit_code
    from meeting.state import begin_drain

    runtime = _inprocess_runtime(tmp_path, monkeypatch)

    # 第 1 步「有实现却抛错」：采集假件的 finalize() 抛错（与 ③ 条同一手法，经共享假件配出来）
    backend = fakes.FakeCaptureBackend(finalize_error="C2 现场的采集归档落盘失败")
    runtime.capture_backend = backend
    runtime.capture_capabilities = {"finalize": True, "transcode": False, "dev_inject": False}
    runtime.capture_resolution = BackendResolution(
        key="capture.backend",
        selector="fake",
        target="meeting_drain_fakes:FakeCaptureBackend",
        implementation=backend,
        has_implementation=True,
        detail="本用例造的现场（进程内直接注入运行时成员）",
    )
    # 第 4 步钉住这一刻：`launch()` 在工作线程里阻塞（事件循环空着，取消随时到得了）
    post_hook = fakes.BlockingThreadPostHook(block_seconds=DRAIN_CANCEL_STEP_SECONDS)
    runtime.post_hook = post_hook
    runtime.post_resolution = BackendResolution(
        key="post.hook",
        selector="fake",
        target="meeting_drain_fakes:BlockingThreadPostHook",
        implementation=post_hook,
        has_implementation=True,
        detail="本用例造的现场（进程内直接注入运行时成员）",
    )
    monkeypatch.setattr(runtime, "_resolve_backends", lambda: None)

    session = runtime.start_session(dir=_new_workspace(tmp_path, "resolve-pinned"))

    async def interrupt_while_the_last_step_runs() -> None:
        # `begin_drain()` 与收尾协程都必须在事件循环这一侧（要在同一条循环上跑得起来）
        begin_drain(runtime.snapshot, reason="user")
        task = asyncio.get_running_loop().create_task(runtime.drain())
        runtime._drain_task = task
        # 确定同步点：`post` 那一步真的进了 `running`（收尾确实还在跑）再打断
        deadline = time.monotonic() + DRAIN_POLL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if runtime.snapshot.drain_step("post").status == "running":
                break
            await asyncio.sleep(DRAIN_CANCEL_POLL_SECONDS)
        else:  # pragma: no cover - 收尾一定走到 post 那一步
            raise AssertionError(
                f"等不到 post 那一步进入 running（实测 "
                f"{runtime.snapshot.drain_step('post').status!r}）"
            )
        # 强制打断那一趟的顺序（§3.5）：**先落盘**、**再 cancel()**
        runtime._shutdown_interrupted = True
        runtime.mark_interrupted()
        await runtime._cancel_drain_task()

    asyncio.run(interrupt_while_the_last_step_runs())

    # 现场确实是我们说的那一格：前面有 `failed` 步，且收尾这一趟已经被打断
    snapshot = runtime.snapshot
    assert snapshot.ended.phase == "draining", snapshot.ended
    statuses = {step.id: step.status for step in snapshot.ended.steps}
    assert statuses["flush"] == "failed", statuses
    assert statuses["log"] in ("done", "skipped"), statuses
    assert statuses["post"] == "running", statuses

    # 换实现之前的那一版会在这里拿到 `state == "failed"`（映射按「有 failed 步」算出来的）
    after = read_session_json(session.dir)
    assert after is not None, "盘上读不出 session.json"
    assert after["state"] == "draining", (
        f"强制打断档盘上该留在 draining（前面那一步 failed 不该把它顶成 failed）：{after}"
    )
    assert after["interrupted"] is True, after
    assert after["ended_at"] is None, after
    # 原 `steps` 保留：已落定的保持原样、在飞那一步照实记 running、不伪造终态
    disk_steps = _step_statuses(_record_steps(after))
    assert disk_steps == statuses, (disk_steps, statuses)

    # 退出码那一档（`cli.py` 的辅助按「本次关停是不是强制打断」挑）
    assert runtime.shutdown_interrupted is True
    assert _drain_exit_code(runtime, fallback=0) == EXIT_INTERRUPTED
    print(
        f"[C2 证据] 强制打断 + 前面已有 failed 步这一格：\n"
        f"  盘上 state={after['state']!r} / interrupted={after['interrupted']!r} / "
        f"ended_at={after['ended_at']!r}\n"
        f"  steps（盘上与快照逐项相同）：{disk_steps}\n"
        f"  ended.phase={snapshot.ended.phase!r}；退出码 = EXIT_INTERRUPTED"
        f"（{_drain_exit_code(runtime, fallback=0)}）"
    )


# ---------------------------------------------------------------------------
# ⑤j 收尾进行中盘上没有 `ended_at`，终态那一版才有（**进程内**，审查问题 m8）
#
# §2.4 的只读契约那一行写死了：**`ended_at` 只在收尾走到终态（`done` / `failed`）时写**
# （这两处都由 `finish_drain()` 落值），强制打断路径不写。此前 `log` 那一步也在写它 ——
# 于是盘上会出现「`state=draining` + `ended_at` 有值」的瞬态（那一刻收尾还没走完），
# 重启扫描与 `meeting post` 读到的现场就多了一个自相矛盾的字段。
#
# 这一条把「盘上每一版都不许出现那种组合」变成可判定的：代理 `write_session_json()`，
# 把这一场**每一次落盘的内容**都抄一份，再逐版检查。收尾进行中（`log` 步刚落下那一刻）
# 的每一版都该是 `ended_at is None`；终态那一版才该有值。
# ---------------------------------------------------------------------------


def test_disk_has_no_ended_at_while_draining_only_at_the_terminal_version(tmp_path, monkeypatch):
    """收尾进行中盘上没有 `ended_at`，收尾走到终态那一版才有（m8）。

    现场：假会后钩子的 `launch()` 抛错（第 4 步 `failed`）——收尾因此会走到**失败终态**那一版
    （`state == "failed"` + `ended_at` 有值，§3.4 第 2 条），于是「进行中」与「终态」两版在
    同一条用例里都出现得了。

    判据分两半：

      - **逐版扫一遍**：这一场写下去的每一版里，**没有哪一版**是「`state == "draining"`
        却有 `ended_at`」的组合（那正是 `log` 步提前写它造出来的瞬态）；
      - **两个端点各自成立**：收尾还在跑的那几版 `ended_at` 全是 `None`，而最后一版
        （终态）`ended_at` 有值、`state == "failed"`。
    """
    from meeting.backends.registry import BackendResolution
    from meeting.state import begin_drain

    runtime = _inprocess_runtime(tmp_path, monkeypatch)

    backend = fakes.FakeCaptureBackend()
    runtime.capture_backend = backend
    runtime.capture_capabilities = {"finalize": True, "transcode": False, "dev_inject": False}
    runtime.capture_resolution = BackendResolution(
        key="capture.backend",
        selector="fake",
        target="meeting_drain_fakes:FakeCaptureBackend",
        implementation=backend,
        has_implementation=True,
        detail="本用例造的现场（进程内直接注入运行时成员）",
    )
    # 第 4 步抛错 → 收尾走到**失败终态**（`ended_at` 有值的那一版）
    post_hook = fakes.FailingPostHook()
    runtime.post_hook = post_hook
    runtime.post_resolution = BackendResolution(
        key="post.hook",
        selector="fake",
        target="meeting_drain_fakes:FailingPostHook",
        implementation=post_hook,
        has_implementation=True,
        detail="本用例造的现场（进程内直接注入运行时成员）",
    )
    monkeypatch.setattr(runtime, "_resolve_backends", lambda: None)

    # 把这一场**每一次落盘**的内容抄下来（代理写在 store 的方法上，运行时的写盘入口不变）
    versions: list[dict[str, Any]] = []
    real_write = runtime.store.write_session_json

    def recording_write(session_dir, record):
        versions.append({"state": record.get("state"), "ended_at": record.get("ended_at")})
        return real_write(session_dir, record)

    monkeypatch.setattr(runtime.store, "write_session_json", recording_write)

    async def one_pass() -> None:
        runtime.start_session(dir=_new_workspace(tmp_path, "end-marker-versions"))
        begin_drain(runtime.snapshot, reason="user")
        await runtime.drain()

    asyncio.run(one_pass())

    assert versions, "这一趟该至少落过一版盘"
    # ① 逐版扫：没有哪一版是「state=draining + ended_at 有值」这种自相矛盾的组合
    bad = [item for item in versions if item["state"] == "draining" and item["ended_at"]]
    assert not bad, (
        f"盘上出现了「收尾中却写着 ended_at」的版本（`log` 步提前写它就是这么来的）：{bad}\n"
        f"这一趟全部版本：{versions}"
    )
    # ② 两个端点：收尾还在跑的那几版 `ended_at` 都是 None
    draining_versions = [item for item in versions if item["state"] == "draining"]
    assert draining_versions, f"这一趟该有过「收尾中」那一版：{versions}"
    assert all(item["ended_at"] is None for item in draining_versions), draining_versions
    # ③ 终态那一版才有 `ended_at`，且盘上与快照都对得上
    final = versions[-1]
    assert final["state"] == "failed", versions
    assert final["ended_at"], f"终态那一版该写 ended_at：{versions}"
    record = read_session_json(runtime.snapshot.session.dir)
    assert record["state"] == "failed" and record["ended_at"], record
    print(
        f"[m8 证据] 这一趟盘上共落了 {len(versions)} 版：\n"
        f"  收尾进行中的每一版 ended_at 都是 None（共 "
        f"{len(draining_versions)} 版）；「state=draining 且有 ended_at」的版本：{len(bad)}\n"
        f"  终态那一版：state={final['state']!r} / ended_at={final['ended_at']!r}"
    )


# ---------------------------------------------------------------------------
# ⑥ `post` 启动即完成
# ---------------------------------------------------------------------------


def test_post_step_completes_on_launch_return(service_process, meeting_env):
    """`post` 那一步「启动即算完成、不等它跑完」。

    假钩子的 `launch()` **立刻返回**，其后半段交给一条永不结束的工作线程；收尾仍在预算内
    正常落 `done`，盘上 `state == "done"`。
    """
    plugin = fakes.write_immediately_returning_post_plugin(meeting_env.home)
    fakes.write_plugin_config(meeting_env.home, plugin)

    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _pin_fake(plugin)

    _start_and_stop(proc)
    snapshot = _wait_done(proc)
    statuses = _step_statuses(snapshot["ended"]["steps"])
    assert statuses["post"] == "done"
    assert statuses["log"] == "done"

    record = read_session_json(snapshot["session"]["dir"])
    assert record is not None and record["state"] == "done"


# ---------------------------------------------------------------------------
# ⑦ `budget` 与耗时进 log（契约 §3.8 点名的那个函数名，独立成条）
# ---------------------------------------------------------------------------


def test_drain_logs_budget_and_elapsed(service_process, meeting_env):
    """收尾每一步的**开始 / 结束 / 耗时**与预算的**输入 / 结果**在日志文件里查得到。

    读的是**两个日志文件**（本文件不使用 `caplog`）：`MEETING_LOG_DIR` 下的**服务级 log**
    与场次目录下的**场次 log**。两者的分工照实情写：

      - **服务级 log** 是四步齐备的那一份 —— 场次 log 在 `log` 步落定那一刻就被摘掉，
        `log` 步自己的「结束」与「耗时」写在摘掉之后，只进得了服务级 log；
      - **场次 log** 里核前三步（`flush` / `transcode` / `log` 的开始，加上先落定的那两步的
        结束与耗时），它同时是「收尾开始 + 预算输入 + 预算结果」落在本场那一份里的证据。

    这条用例只做自己那一遍必需的现场操作（一次 `start` + `stop`），读 log 一律走
    `wait_until()` 等两个文件都写到位再断言 —— 跨进程读文件撞上「还差最后一行」是偶发红的
    常见来源。
    """
    proc = _start_service(service_process, meeting_env)
    proc.wait_ready()
    _start_and_stop(proc)
    snapshot = _wait_done(proc)
    session_dir = Path(snapshot["session"]["dir"])

    # 场次 log：收尾起点、预算的输入与结果，加上前三步的开始（后两步的结束写在摘掉之后）
    session_log = wait_until(
        lambda: _log_with_all(
            session_dir / "meeting.log",
            ["收尾开始：", "budget=", "backlog="]
            + [f"收尾步骤开始：{step_id}" for step_id in DRAIN_STEP_IDS[:3]]
            + [f"收尾步骤结束：{step_id}" for step_id in DRAIN_STEP_IDS[:2]]
            + [f"收尾步骤耗时：{step_id}" for step_id in DRAIN_STEP_IDS[:2]],
        ),
        timeout=DRAIN_POLL_TIMEOUT_SECONDS,
    )
    assert session_log, "场次 log 里该有收尾开始与前三步的账"
    assert "segment_timeout=" in session_log, "预算的另一个输入也该落在本场那一份里"

    # 服务级 log：四步齐备的一份 —— 每步都有开始、结束与耗时，预算的输入与结果也在这
    service_log_path = meeting_env.log_dir / "meeting.log"
    service_log = wait_until(
        lambda: _log_with_all(
            service_log_path,
            ["收尾开始：", "budget=", "backlog="]
            + [
                f"收尾步骤开始：{step_id}"
                for step_id in DRAIN_STEP_IDS
            ]
            + [
                f"收尾步骤结束：{step_id}"
                for step_id in DRAIN_STEP_IDS
            ]
            + [
                f"收尾步骤耗时：{step_id}"
                for step_id in DRAIN_STEP_IDS
            ],
        ),
        timeout=DRAIN_POLL_TIMEOUT_SECONDS,
    )
    assert service_log, "服务级 log 里该有四步齐备的账"

    # 逐步点名：四步的「开始 / 结束 / 耗时」三样在服务级 log 里逐条查得到（缺哪一步就报哪一步）
    for step_id in DRAIN_STEP_IDS:
        for needle, label in (
            (f"收尾步骤开始：{step_id}", "开始"),
            (f"收尾步骤结束：{step_id}", "结束"),
            (f"收尾步骤耗时：{step_id}", "耗时"),
        ):
            assert needle in service_log, (
                f"服务级 log 里查不到 {step_id} 那一步的{label}（{needle}）：\n"
                f"{service_log[-1500:]}"
            )
    # 预算的输入与结果：积压量、段长与算出来的那条上限都写着（`budget` 的取值域由 ⑤ 与 ⑤b2 核）
    assert "budget=" in service_log
    assert "backlog=" in service_log
    assert "segment_timeout=" in service_log


# ---------------------------------------------------------------------------
# ⑧ 失败后不自杀
# ---------------------------------------------------------------------------


def test_failed_step_keeps_service_alive_and_start_conflicts(service_process, meeting_env):
    """某步 `failed` 后服务照常活着；此时 `POST start` 回 409 `draining`。

    响应里带**失败那一步**与 **log 路径**；`ended.phase` 停在 `draining`、盘上 `state != "done"`。
    """
    plugin = fakes.write_failing_post_hook(meeting_env.home)
    fakes.write_plugin_config(meeting_env.home, plugin)

    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _pin_fake(plugin)
    _start_and_stop(proc)

    snapshot = _wait_any_failed(proc)
    ended = snapshot["ended"]
    assert ended["phase"] == "draining"
    assert ended["error"]

    # 服务不自杀：`GET /api/state` 仍 200（`proc.snapshot()` 自己就断言状态码）
    assert proc.snapshot()["top_state"] == "ended"
    assert proc.poll() is None

    record = read_session_json(snapshot["session"]["dir"])
    assert record is not None
    assert record["state"] == "failed"
    assert record["ended_at"]

    conflict = post_action(
        proc.base_url,
        "start",
        args={"dir": helpers.allocate_start_dir(proc.sessions_root)},
    )
    assert conflict.status_code == 409
    payload = conflict.json()
    assert payload["code"] == "draining"
    detail = payload["detail"]
    failed_ids = [step["id"] for step in _failed_steps_of(snapshot)]
    assert detail["failed_steps"], f"拒绝路径要带上失败那一步：{detail}"
    assert [item["id"] for item in detail["failed_steps"]] == failed_ids
    assert detail["log_path"], f"拒绝路径要带上 log 路径：{detail}"


# ---------------------------------------------------------------------------
# ⑨ 两条出路进状态与 log
# ---------------------------------------------------------------------------


def test_failed_step_records_both_ways_out(service_process, meeting_env):
    """收尾失败时，log 里能找到 `meeting post` 与「重启」两条出路（§5.3 行 249）。"""
    plugin = fakes.write_failing_post_hook(meeting_env.home)
    fakes.write_plugin_config(meeting_env.home, plugin)

    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _pin_fake(plugin)
    _start_and_stop(proc)

    snapshot = _wait_any_failed(proc)
    failed = _failed_steps_of(snapshot)[0]
    session_dir = snapshot["session"]["dir"]
    assert f"meeting post {session_dir}" in failed["error"]
    assert snapshot["ended"]["error"] and "meeting post" in snapshot["ended"]["error"]

    service_log = _service_log_text(meeting_env)
    assert "meeting post" in service_log
    assert "重启" in service_log


# ---------------------------------------------------------------------------
# ⑩ 收尾期间可观测
# ---------------------------------------------------------------------------


def test_state_stays_readable_while_draining(service_process, meeting_env):
    """收尾窗口里 `/api/state` 一直 200 且 `ended.phase == "draining"`（协程没把事件循环钉住）。

    用**钉在某一步 `running` 上的假件**造出这个窗口：`BlockingThreadPostHook` 的阻塞必须
    **离开事件循环**（放工作线程、那一步经收尾专用的 daemon 执行器调用）；同步阻塞若留在被调用的
    那一步里会把事件循环占住，这个窗口根本等不到。
    """
    plugin = fakes.write_fake_module(meeting_env.home)
    plugin.config["capture.backend"] = "dev"
    plugin.config["post.hook"] = plugin.module("BlockingThreadPostHook")
    plugin.extra_env[fakes.BLOCK_SECONDS_ENV] = str(float(DRAIN_BLOCKING_STEP_SECONDS))
    fakes.write_plugin_config(meeting_env.home, plugin)

    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _pin_fake(plugin)
    _warm_up_client(proc)
    _start(proc)

    assert post_action(proc.base_url, "stop").status_code == 200
    # 先把观察点挪进窗口（见 `DRAIN_WINDOW_ENTRY_DELAY_SECONDS` 的说明），再轮询
    time.sleep(DRAIN_WINDOW_ENTRY_DELAY_SECONDS)
    during = _wait_state(
        proc,
        lambda snap: _step_statuses((snap.get("ended") or {}).get("steps") or []).get("post")
        == "running",
        what="post 那一步停在 running",
    )
    assert during["ended"]["phase"] == "draining", _drain_evidence(during, _service_log_text(meeting_env))

    # 窗口里连着读几次 `/api/state`：每一次都 200，且「post 还在 running」与「phase 仍是
    # draining」两件事一起成立 —— 窗口之内的读才算数（现场证据只在断言真要报错时才去收集，
    # 免得读日志把窗口本身吃掉）
    for index in range(DRAIN_WINDOW_POLLS):
        snapshot = proc.snapshot()
        steps = _step_statuses(snapshot["ended"]["steps"])
        if snapshot["ended"]["phase"] != "draining" or steps["post"] != "running":
            raise AssertionError(
                f"第 {index} 次读落在窗口之外：{_drain_evidence(snapshot, _service_log_text(meeting_env))}"
            )
        time.sleep(DRAIN_WINDOW_POLL_SECONDS)

    _wait_done(proc)


# ---------------------------------------------------------------------------
# ⑪ 场次 log 挂摘
# ---------------------------------------------------------------------------


def test_session_log_attached_then_detached_with_end_marker(service_process, meeting_env):
    """录音期场次 log 在长；收尾后该文件存在、**最后一行是结束标记**、且此后不再增长。

    **「最后一行」照 §3.7 的严格口径断**（审查问题 M3）：标记所在行的**下标**必须等于最后
    一个非空行的下标 —— 也就是**标记之后没有任何非空行**。⑫ 条用的 `marker_index` 与这里
    同一个口径（那条看的是标记**之前**的每一类事件）。

    **标记的内容逐字段核一遍**（审查问题 M8，§3.7「一行带**场次 id** 与收尾结果（含 `state`
    与四步 `status` 概要）的记录」）：场次 id = 场次目录名、`state` = 盘上那一版、
    四步概要按 `id=status` 逐项与盘上 `drain.steps` 对齐、服务级 log 路径与快照里的
    `service.log_path` 一致。
    """
    proc = _start_service(service_process, meeting_env)
    proc.wait_ready()

    _start(proc)
    session_dir = _session_dir_of_log(_service_log_text(meeting_env))
    session_log_path = Path(session_dir) / "meeting.log"

    # **挂上**：`start` 成功之后场次 log 就在位（录音期本身没有 INFO 级事件，文件先空着）
    wait_until(session_log_path.exists, timeout=DRAIN_POLL_TIMEOUT_SECONDS)
    assert session_log_path.exists(), f"`start` 之后场次 log 该已经挂上：{session_log_path}"

    assert post_action(proc.base_url, "stop").status_code == 200
    snapshot = _wait_done(proc)

    session_log = _wait_end_marker(session_dir)
    lines = [line for line in session_log.splitlines() if line.strip()]
    assert lines, "场次 log 不该为空"
    # 严格口径：标记所在行就是最后那个非空行（标记之后不再有任何非空行）
    marker_index = next(
        (index for index, line in enumerate(lines) if END_MARKER_TEXT in line), None
    )
    assert marker_index is not None, f"场次 log 里没有结束标记：\n{session_log}"
    assert marker_index == len(lines) - 1, (
        f"结束标记必须是最后一行：它落在第 {marker_index + 1} 行，"
        f"而最后一个非空行是第 {len(lines)} 行\n{lines[marker_index:]}"
    )

    # **标记逐字段核**（审查问题 M8、§3.7）：场次 id、`state`、四步概要、服务级 log 路径。
    # 标记是 `log` 步**在它自己那一刻**写下的：那一刻 `state` 还是 `draining`（收尾没走完），
    # 四步概要是 `log=running`（正在写它）、`post=waiting`（还没开跑）。这两件事因此按
    # 「**在 `log` 步那一刻成立的取值**」逐项核 —— 而不是拿收尾走完之后盘上那一版去套，
    # 那样会把「标记**照实**写下了它那一刻的收尾进度」这条事实读成一句对不上。
    record = read_session_json(session_dir)
    assert record is not None, f"盘上读不出 {session_dir}/session.json"
    marker = lines[marker_index]
    # 场次 id：格式串里的 `session=%(session)s`（场次级 handler 的 `SessionFilter` 写的就是它）
    assert f"session={Path(session_dir).name} " in marker, (
        f"标记该带场次 id（= 场次目录名）：{marker!r}"
    )
    # `state`：取值落在 `session.json` 的取值域里
    state = marker.split("场次结束：state=", 1)[1].split("；", 1)[0]
    assert state in SESSION_STATE_DOMAIN, f"标记里的 state 落在取值域之外：{state!r}（{marker!r}）"
    # 四步概要：**四档按 `DRAIN_STEP_IDS` 的原顺序**逐项报出（格式串把 `status` 列表按顺序
    # 用 `, ` 连起来），每一项的 `status` 落在 `DRAIN_STEP_STATUS` 的取值域里
    summary = marker.split("收尾四步：", 1)[1].split("；", 1)[0]
    statuses = [item.strip() for item in summary.split(",")]
    assert len(statuses) == len(DRAIN_STEP_IDS), (
        f"四步概该正好 {len(DRAIN_STEP_IDS)} 项（按 {list(DRAIN_STEP_IDS)} 的原顺序）：{marker!r}"
    )
    assert set(statuses) <= set(DRAIN_STEP_STATUS), marker
    by_id = dict(zip(DRAIN_STEP_IDS, statuses))
    # 写这一行的是 `log` 步自己：它那一刻是 `running`，它后面的 `post` 还没开跑
    assert by_id["log"] == "running", marker
    assert by_id["post"] == "waiting", marker
    # 它前面那两步在骨架构建下是 `skipped`（有真实实现就记 `done`；两者都算落定）
    assert by_id["flush"] in ("done", "skipped"), marker
    assert by_id["transcode"] in ("done", "skipped"), marker
    # 收尾走完之后盘上那一版：`done` + 四步都有终态（标记之后收尾照常把剩下的走完）
    assert record["state"] == "done", record
    assert set(step["status"] for step in _record_steps(record)) <= {"done", "skipped"}, record
    # 服务级 log 路径：与本次启动的服务级日志路径逐字相同
    assert marker.rstrip().endswith(f"服务级 log：{snapshot['service']['log_path']}"), (
        f"标记末尾该是服务级 log 路径（{snapshot['service']['log_path']!r}）：{marker!r}"
    )

    size_after = session_log_path.stat().st_size
    time.sleep(DRAIN_LOG_CLOSED_RECHECK_SECONDS)  # 摘掉之后这一场不再有新行
    assert session_log_path.stat().st_size == size_after


# ---------------------------------------------------------------------------
# ⑫ 结束标记前逐类事件可查（G6 的自动化对应物）
# ---------------------------------------------------------------------------


def test_all_log_event_categories_findable_before_end_marker(service_process, meeting_env):
    """结束标记**之前**这一场的事件逐类可查（方案 §6.3 行 304 的清单）。

    事件由 dev 采集替身与采集后端的收尾调用造出来：**每一路健康迁移**与告警起止由
    `MEETING_CAPTURE=dev` 的两路合成电平与 `dev_inject` 在**真服务子进程**里驱动，
    收尾每一步的开始 / 结束 / 耗时与 `budget` / `backlog` 由 phase3 的编排落。
    阈值只**显式调小**（写进临时 home 的 `config.json`，**不改代码默认值**）。

    **故障档取 `silence`**（审查问题 C1 的证据口径）：它一路把 loopback 从 `capturing`
    推到 `lost`（阈值 `alert.silence_seconds`）、恢复判定再推到 `restored`，于是「每一路健康
    迁移」这一类在一趟里出现**三段真实迁移**，且每段都带着是哪一路、从什么值到什么值、
    原因与阈值。`lost` 那一档同样落这一类事件（`麦克风/回环 → lost`），但它一跳就到、看不出
    `restored` 那一段，因此这里用 `silence` 把两段都留在场次 log 里（结束标记之前）。

    **「场次开始与目录」这一类取服务级 log（审查问题 M3 的落地读法）**：场次 log 是 `start`
    成功之后才挂上的 handler，而那一行按时间顺序落在挂上之前 —— 它进不了场次 log。契约点名的
    这一条因此按**两类 log 联合**读：场次 log 里取本场的 session id / 目录 / 「场次结束」，
    服务级 log 里取「场次开始」那一行（带同一个 id 与目录）。别为了迎合字面去场次 log 里
    伪造一行。

    **「挂载顺序」这件事与 phase2 的核对结论（审查问题 m10，照实写清）**：`start` 里那一行
    `场次开始：<id>（目录 <dir>，mode=…）` 落在 `logging_setup.attach_session_log(session_dir)`
    **之前**，因此它只可能出现在服务级 log 里。这个顺序是 **phase2 的既成事实**：拿 HEAD 的
    上一个提交核过 —— `git show 56fcf33:meeting/service/runtime.py` 的 `start_session()` 里
    `_log.info("场次开始：…")` 同样排在 `attach_session_log(session_dir)` 之前；
    `git diff --ignore-cr-at-eol 56fcf33 -- meeting/service/runtime.py` 在这个位置只多了一行
    「每一路健康迁移」的记账（phase3 补的），**挂载顺序一处没动**（phase3 §1.3 也没把
    「重排 start 的挂载顺序」列进本 Phase 的动作）。本 Phase 因此照现行的两类 log 联合断言写，
    没有把这一行搬进场次 log。
    """
    plugin = fakes.write_fake_module(meeting_env.home)
    plugin.config.update(
        {
            "capture.backend": "dev",
            "alert.silence_seconds": TEST_SILENCE_SECONDS,
            "alert.recover_seconds": TEST_RECOVER_SECONDS,
        }
    )
    proc = _start_service(service_process, meeting_env, plugin)
    proc.wait_ready()
    _warm_up_client(proc)

    _start(proc)
    session_dir = _session_dir_of_log(_service_log_text(meeting_env))

    injected = post_action(
        proc.base_url,
        "dev_inject",
        {
            "source": "loopback",
            "fault": "silence",
            "after_seconds": 0,
            "duration_seconds": TEST_SILENCE_FAULT_SECONDS,
        },
    )
    assert injected.status_code == 200, injected.text

    _wait_state(
        proc,
        lambda snap: snap["top_state"] == "alert",
        what="一路静默把 top_state 推到 alert",
        timeout=ALERT_POLL_TIMEOUT_SECONDS,
    )
    # 等它恢复（`alert.recover_seconds` 已调小、静默时长到这里就到点），好让「告警起止」
    # 这一类事件在一趟里齐活（观察窗口 `TEST_SILENCE_FAULT_SECONDS` 因此要长过静默阈值）
    _wait_state(
        proc,
        lambda snap: snap["top_state"] == "recording",
        what="告警恢复回 recording",
        timeout=ALERT_POLL_TIMEOUT_SECONDS,
    )

    assert post_action(proc.base_url, "stop").status_code == 200
    _wait_done(proc)

    session_log = _wait_end_marker(session_dir)
    marker_index = session_log.find(END_MARKER_TEXT)
    assert marker_index > 0, f"场次 log 里没有结束标记：\n{session_log}"
    before = session_log[:marker_index]

    def check(needle: str, label: str) -> None:
        assert needle in before, f"结束标记之前查不到「{label}」（{needle}）：\n{before}"

    session_id = session_dir.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    # **「场次开始与目录」这一类事件按时间顺序只可能落在服务级 log**（审查问题 M3 的口径，
    # 写在这里是为了不让人对着契约那半句去找一条不存在的场次 log 行）：场次 log 是 `start`
    # 成功之后才挂上的 handler，而「场次开始：<id>（目录 <dir>，mode=…）」这一行落在挂上之前
    # —— 场次 log 里能找到的是**同一场的**「场次结束：<id>（目录 …）」与 session id / 目录本身。
    # 两类 log 因此**联合断言**：本场身份（id 与目录）两边各取一半，合起来覆盖契约点名的这一条。
    #
    # **这个顺序的出处（审查问题 m10）**：它是 **phase2 的挂载顺序**，
    # `git show 56fcf33:meeting/service/runtime.py` 的 `start_session()` 里
    # `_log.info("场次开始：…")` 就已排在 `attach_session_log(session_dir)` 之前；
    # `git diff --ignore-cr-at-eol 56fcf33 -- meeting/service/runtime.py` 在这个位置只多了一行
    # 「每一路健康迁移」的记账，**挂载顺序一处没改**（phase3 未动）。下面这条联合断言因此
    # 照现行顺序写，没有把这一行搬进场次 log（搬了反而会与 phase2 的既成事实对不上）。
    check("场次结束：", "场次结束与目录（本场那一份里的收尾痕迹）")
    check(session_id, "场次 id")
    check(session_dir, "场次目录路径")
    check("告警起：", "告警起")
    check("状态迁移：alert -> recording", "告警止（恢复）")
    check("alarm.sound", "出声开关（抑制口径）")
    # **每一路健康迁移**（契约点名的必落 log 事件；审查问题 C1 补上的那一类）。
    # 断言按路写：**mic 那一行**在场（它整场都在采，这一档不掺别的路的话就查不到），
    # 以及 loopback 那一路的三段真实迁移 —— 采集替身注入的静默把它从 `capturing` 推到
    # `lost`（阈值 `alert.silence_seconds` 已被显式调小），恢复判定再把它推到 `restored`，
    # `stop` 进收尾时推到 `stopped`。
    check("每一路健康迁移：mic", "每一路健康迁移（mic 那一路）")
    check("每一路健康迁移：loopback", "每一路健康迁移（loopback 那一路）")
    check(
        f"每一路健康迁移：loopback capturing -> lost（原因：这一路起致命级告警",
        "一路从 capturing 迁到 lost（含原因）",
    )
    check(
        "每一路健康迁移：loopback lost -> restored（原因：连续有信号达恢复阈值",
        "一路从 lost 迁回 restored（含恢复原因）",
    )
    check("阈值：静默阈值 alert.silence_seconds=", "迁到 lost 那一条的阈值")
    check("阈值：恢复阈值 alert.recover_seconds=", "迁回 restored 那一条的阈值")
    check("收尾开始：", "收尾起点")
    check("backlog=", "预算输入 backlog")
    check("budget=", "预算结果")
    check("耗时", "每一步的耗时")
    for step_id in DRAIN_STEP_IDS[:2]:
        check(f"收尾步骤开始：{step_id}", f"{step_id} 的开始")
        check(f"收尾步骤结束：{step_id}", f"{step_id} 的结束")

    # 服务级 log 那半边：**「场次开始与目录」落在这里**（理由见上面 session_id 那一段的注释
    # —— 场次 log 是 `start` 成功之后才挂上的，这一行按时间顺序在它之前），加上两路设备探测
    # 与四步齐备的收尾账。字段级断言：这一行要带**本场的 id 与目录**，与场次 log 里那一半对上。
    service_log = _service_log_text(meeting_env)
    assert "场次开始：" in service_log, "服务级 log 缺「场次开始」"
    assert session_id in service_log, "服务级 log 的「场次开始」该带本场的 id"
    assert session_dir in service_log, "服务级 log 的「场次开始」该带本场的目录"
    assert "设备探测：mic" in service_log, "服务级 log 缺 mic 那一路的探测记录"
    assert "设备探测：loopback" in service_log, "服务级 log 缺 loopback 那一路的探测记录"
    for step_id in DRAIN_STEP_IDS:
        assert f"收尾步骤开始：{step_id}" in service_log
        assert f"收尾步骤结束：{step_id}" in service_log

    # 结束标记之后不再有这一场的行（摘掉之后的消息只走服务级 log）
    after = session_log[marker_index:]
    assert "收尾步骤" not in after, f"结束标记之后还有这一场的行：\n{after}"
