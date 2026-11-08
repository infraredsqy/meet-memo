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

from tests.test_drain._common import (
    DRAIN_DRAG_TOTAL_SECONDS,
    DRAIN_LARGE_BACKLOG,
    DRAIN_LARGE_SEGMENT_TIMEOUT_SECONDS,
    DRAIN_OVERRUN_CLOCK_OFFSET_SECONDS,
    DRAIN_OVERRUN_MARKER_NAME,
    DRAIN_OVERRUN_STEP_SECONDS,
    DRAIN_SLOW_POST_STEP_SECONDS,
    DRAIN_SLOW_STEP_SECONDS,
    E2E_CASE_WALL_CLOCK_LIMIT_SECONDS,
    _drain_evidence,
    _inprocess_runtime,
    _new_workspace,
    _pin_fake,
    _record_steps,
    _service_log_text,
    _start_and_stop,
    _start_service,
    _step_statuses,
    _wait_any_failed,
    _wait_done,
    _warm_up_client,
)

#: 慢档标记：`pytest.mark.slow` 的归位判据见 `docs/service.md`。
pytestmark = pytest.mark.slow

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

    行为口径，与 `_drain_wait()` 的包装形态一一对应：拖长落在第 1 步 `flush` 上
    （假采集后端的 `finalize()`）—— 按「结束录音」之后会后一个动作都不自动跑（R26），
    第 4 步 `post` 已经不再被任何钩子接住，它做完该做的（释放会中模型）就停在 `waiting`。
    积压量与段长仍由**假转写后端**报。

    **两件事一起断**：被 `_drain_wait()` 包住的那一步真的等满了它自己声明的时长
    （实测耗时确实抬到了拖长量之上），且这一趟收尾的总时长不超过预算函数算出的总上限
    （四步共享同一条 deadline，另加 `log` 步那条最小预算）。
    """
    plugin = fakes.write_slow_drain_plugin(
        meeting_env.home,
        capture_delay_seconds=DRAIN_SLOW_STEP_SECONDS + DRAIN_SLOW_POST_STEP_SECONDS,
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
    # 拖长的是 `flush` 一步（转码已摘出收尾序列，三步：flush / log / post）
    assert statuses["flush"] == "done", snapshot["ended"]
    assert statuses["post"] == "waiting", snapshot["ended"]
    assert statuses["log"] == "done", snapshot["ended"]
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
    # 第 4 步停在「等你动手」那一格（R26），其余步骤照契约落定
    assert statuses["post"] == "waiting", snapshot["ended"]
    assert set(statuses.values()) <= {"done", "skipped", "waiting"}

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
# 与拆分进各 mixin 模块（`runtime_facts` / `runtime_sessions` / `runtime_summary` /
# `runtime_drain`）读的 `time` 名字、连同事件循环（`asyncio.base_events`）一起前移 ——
# 谁读谁就得拨：上限由运行时那只秒表量，而 `wait_for` 到点靠事件循环自己的计时器，
# 漏拨的模块那一路越界不会发生。
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
    再返回，返回时写下 `DRAIN_OVERRUN_CLOCK_OFFSET_SECONDS` 的时钟偏移标记
    （原先写标记的是第 4 步的会后钩子，而按 R26 会后已经不再自动跑 —— 那一格不再被调用，
    标记因此改由收尾里第一个真实调用写下）；这一趟的预算仍是
    `compute_drain_budget(DRAIN_DEFAULT_BACKLOG, DRAIN_DEFAULT_SEGMENT_TIMEOUT_SECONDS)` = 下限
    30 秒（**用例按常量读它，不抄裸数**）。转码已摘出收尾序列，紧跟着的落点是 `log` 步 ——
    而它有 `DRAIN_LOG_MIN_BUDGET_SECONDS` 的保底，因此越界通常落在 `flush` 自己的实测耗时上
    （`finalize()` 的等待被包在剩余预算里；两档——越界落 `flush` 或落 `log`——都算验到）。

    四件事一起断，缺一件这一档就还是没验到：

      1. **真的越界了**：越界那一步（`transcode`）的 `error` 里有 §3.3 点名的「超出收尾预算」
         与**实测耗时**（`budget=` 与 `实测耗时 ` 两段都在），且这一步的 `status == "failed"`；
      2. **越界前面那一步照常落定**：`flush` 落 `done` —— 这一趟因此确实跑到了越界那一步之前，
         预算拦下的是它的调用；
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

    # ① 越界那一步：时钟偏移由 `flush` 那一步的 `finalize()` 写下，越界因此落在**它之后
    # 第一个被 `_drain_wait()` 包住的调用**上（`finalize()` 自己的实测耗时也有可能跟着被算成
    # 越界 —— 两档都由「越界 → `failed` + error 文案」这一条钉住，具体哪一格不写死）
    failed = [step for step in snapshot["ended"]["steps"] if step["status"] == "failed"]
    assert failed, evidence
    overran = failed[-1]
    error = overran["error"] or ""
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
    # ② 越界落在有真实调用的步骤里（`post` 那一格按 R26 停在 `waiting`，越界因此不会落在
    # 它身上；转码已摘出收尾序列，落点是 flush 自己的实测耗时或 log 步）
    assert overran["id"] in ("flush", "log"), evidence
    # ③ 盘上那一版：失败档的 `state`，`ended_at` 有值（收尾走到了终态）
    record = read_session_json(snapshot["session"]["dir"])
    assert record is not None, "盘上读不出 session.json"
    assert record["state"] == "failed", record
    assert record["ended_at"], record
    disk_steps = _step_statuses(_record_steps(record))
    assert disk_steps[overran["id"]] == "failed", record
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
    """收尾各步共享**同一条** deadline：越界那一步落 `failed`，后一步拿到的剩余预算比前一步小（C1）。

    转码已摘出收尾序列（三步：flush / log / post），越界现场随之改排在 `flush` 上：
    `finalize()` 的假件睡 `DRAIN_SHARED_TRANSCODE_SECONDS`（明显长于这一趟的小预算）。

    两件事一起断（缺一件「共享一条 deadline」就还是没验到）：

      1. **预算耗尽真的发生**：`flush` 那一步的假件睡过预算，它的 `status == "failed"`，
         `error` 是 §3.3 点名的「超出收尾预算」那一段；
      2. **共享 deadline 的直接证据**：各步无一例外都读到**同一条**截止时刻（逐值相等），
         而它们各自开跑时「离那条截止时刻还剩多久」**逐级单调不增**（每步各拿一份
         **完整**预算时，第 2 条当场红）。

    **把各处调用改成 `deadline=None` 时这一条必红**（变异自测见交付报告）：那时各步各自
    拿到一份**完整**的下限预算、读到的截止时刻互不相同，上面第 2 条的两半都不成立；
    `flush` 也不再落 `failed`。

    预算函数由 `monkeypatch` 换成一个固定的小值（`0.05` 秒）：这一条要验的是「各步怎么**用**
    这笔预算」，那笔预算**算多大**由 ④ / ⑤ / ⑤b2 各自负责。
    """
    from meeting.backends.registry import BackendResolution
    from meeting.service import runtime as runtime_module
    from meeting.state import begin_drain

    runtime = _inprocess_runtime(tmp_path, monkeypatch)

    # 采集假件在场：`finalize()` 睡 `DRAIN_SHARED_TRANSCODE_SECONDS`（明显长于这一趟的
    # 小预算 —— 越界现场在 `flush` 上；转码已不在收尾序列里）
    backend = fakes.FakeCaptureBackend(
        finalize_delay_seconds=DRAIN_SHARED_TRANSCODE_SECONDS,
    )
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
    # **各步都要真的经 `_drain_wait()`**（§3.1 第 3 条的「无一例外」）：`log` 步的
    # flush / 摘除自带真实实现；`post` 步只做一件事 —— 让会中模型退出显存
    # （R26：会后归两个按钮），那一次调用同样经 `_drain_wait()`，因此这里给一台
    # **带 `release_gpu()` 的假转写后端**（共享假件 `FakeTranscriberBackend`）。
    # **transcode 能力不给**：flush 越界落 `failed`，flush 落定自动起转码那一档不触发。
    transcriber = fakes.FakeTranscriberBackend()
    runtime.transcriber_backend = transcriber
    monkeypatch.setattr(runtime, "_resolve_backends", lambda: None)

    # 这一趟的总预算：换成一个固定的小值（预算**怎么算**由 ④ / ⑤ / ⑤b2 负责）
    calls = {"budget": 0}

    def tiny_budget(backlog, segment_timeout):
        del backlog, segment_timeout
        calls["budget"] += 1
        return DRAIN_SHARED_BUDGET_SECONDS

    # 补丁打在 **`_run_drain_steps()` 所在模块**的那个全局名上：`compute_drain_budget`
    # 的定义与唯一调用点拆分后都住在 `meeting.service.runtime_drain`，改
    # `meeting.state` 上的名字或只改聚合壳 `meeting.service.runtime` 的 re-export 绑定，
    # `_run_drain_steps()` 都看不见（「补丁打在哪一个命名空间」的问题）。
    from meeting.service import runtime_drain as runtime_drain_module

    monkeypatch.setattr(runtime_drain_module, "compute_drain_budget", tiny_budget)

    # 记下每一步**实际经 `_drain_wait()` 开跑**那一刻的剩余预算与截止时刻，以及这一步
    # 真跑完之后的剩余预算 —— 这一条的全部判据都从这里读。
    seen: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    collecting = True
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
        (seen if collecting else manual).append(record)
        try:
            return await real_wait(step_id, call, **kwargs)
        finally:
            if deadline is not None:
                record["after"] = max(0.0, float(deadline) - time.monotonic())

    monkeypatch.setattr(runtime, "_drain_wait", recording_wait)

    async def one_pass() -> None:
        nonlocal collecting
        runtime.start_session(dir=_new_workspace(tmp_path, "deadline-share"))
        begin_drain(runtime.snapshot, reason="user")
        await runtime.drain()
        # `post` 步那一趟单独再跑一遍：`flush` 越界已经把这一趟的 deadline 用光了（剩余
        # 预算 0），收尾编排里那一次 `_drain_wait()` 因此会在开跑之前就判越界。这一步本身
        # 只做一件事（让会中模型退出显存，R26），这里给它一条新的 deadline，
        # 量的是「它也经同一层等待」。
        collecting = False
        await runtime._drain_post(deadline=time.monotonic() + DRAIN_SHARED_BUDGET_SECONDS)

    asyncio.run(one_pass())

    # 各步无一例外都经 `_drain_wait()`（§3.1 第 3 条）
    assert [item["step"] for item in seen] == list(DRAIN_STEP_IDS), seen
    # ① 预算耗尽真的发生：超预算的那一步照 §3.3 点名的话落 `failed`
    statuses = {step.id: step.status for step in runtime.snapshot.ended.steps}
    # 变异（各处调用点改成 `deadline=None`）会让这一格读到 `None` —— 下面这句如实报出来，
    # 不靠格式化把信号盖掉
    assert seen[0]["remaining"] is not None, (
        f"第 1 步开跑时手上没有 deadline（各处调用点没把这一趟那条传下来）：{seen}"
    )
    assert statuses["flush"] == "failed", (
        f"{statuses}（这一步的假件睡了 {DRAIN_SHARED_TRANSCODE_SECONDS}s，"
        f"而它开跑时只剩 {seen[0]['remaining']:.4f}s）"
    )
    flush_step = next(step for step in runtime.snapshot.ended.steps if step.id == "flush")
    assert "超出收尾预算" in (flush_step.error or ""), flush_step.error
    # 收尾编排那一趟只算一次预算（单独再跑的 `post` 步不经过 `_run_drain_steps()`，不另算）
    assert calls["budget"] == 1, f"这一趟该只算一次预算，实测喊了 {calls['budget']} 次"
    # `post` 步做完它该做的（释放会中模型）就停在「等你动手」：三步里没有一步被漏掉
    assert statuses["post"] == "waiting", statuses
    # 释放被喊到（收尾编排那一趟 + 上面单独再跑的那一趟；两次都是真的经等待层交到工作线程）
    assert transcriber.released >= 1, transcriber.released
    # 那一次调用同样经 `_drain_wait()` 的等待层（给它的是新的一条 deadline）
    assert [item["step"] for item in manual] == ["post"], manual

    # ② 共享 deadline 的直接证据
    for item in seen:
        assert item["deadline"] is not None, (
            f"{item['step']} 那一步拿到的 deadline 是 None —— 每步各拿一份下限预算，"
            f"各步没有共享同一条 deadline：{seen}"
        )
    deadlines = {item["deadline"] for item in seen}
    assert len(deadlines) == 1, (
        f"各步该读到**同一条**截止时刻，实测有 {len(deadlines)} 条："
        f"{[(item['step'], item['deadline']) for item in seen]}"
    )
    # 每一步开跑时「这条 deadline 还剩多少」：同一趟收尾里逐级单调不增 —— 这正是「共享」的
    # 形状；每步各拿一份完整预算时这几格会一样大（甚至后一步比前一步大）
    remainings = [item["remaining"] for item in seen]
    assert all(
        later <= earlier for earlier, later in zip(remainings, remainings[1:])
    ), f"各步开跑时的剩余预算该逐级单调不增：{[(i['step'], i['remaining']) for i in seen]}"
    # 收尾期间那条 deadline 真的走到了头：越界那一步之后，剩余预算已经是 0
    assert seen[0]["after"] == 0.0, (
        f"flush 跨过预算之后，这条 deadline 的剩余预算该是 0，实测 {seen[0]['after']!r}：{seen}"
    )
    print(
        f"[C1 证据] 预算固定为 compute_drain_budget(){DRAIN_SHARED_BUDGET_SECONDS}s，"
        f"flush 的假件睡 {DRAIN_SHARED_TRANSCODE_SECONDS}s（明显长于预算）：\n"
        f"  每一步开跑时的剩余预算 / 截止时刻："
        f"{[(item['step'], round(item['remaining'], 4), round(item['deadline'], 4)) for item in seen]}\n"
        f"  超预算那一步：flush -> {statuses['flush']}"
        f"（error={flush_step.error!r}）\n"
        f"  各步读到同一条截止时刻：{len(deadlines) == 1}"
        f"（{list(deadlines)[0]:.4f}）；"
        f"剩余预算 {remainings[0]:.4f}s -> {remainings[-1]:.4f}s"
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


