"""收尾三步、自适应预算与失败出路的黑盒用例（phase3 §5.1 / §3.8 的 12 条 + 审查修复追加的几条）。

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
    M2 的取消登记、M3 的缺省预算输入），那些断言不起服务、直接调用被断言的对象。

**「单条用例 ≤ 5 秒」怎么落（用户硬要求）**：契约点名的 12 个函数各自独立，每个只做自己那一遍
必需的现场操作（一次 `start` + `stop` 的建场-收尾路径、读一次 log 文件），自造的等待常量贴着
下界取（见下面每个常量上的说明），看门由 `tests/conftest.py` 的耗时 hook 自动判红。
**⑤b 那两条**把「超预算 → `DrainLimitExceeded`」与「阻塞型同步步不钉住事件循环」搬到
**进程内可注入的小 deadline** 上（0.02 秒），于是墙钟与断言语义两不误。

**等到某条件一律带显式超时**：所有「等到某条件」都走 `tests/_helpers.py` 的 `wait_until()`，
超时值取本文件下面的具名常量，**不留裸秒数**。

**每一路健康迁移与告警起止由谁造出来**：由 `MEETING_CAPTURE=dev` 的两路合成电平与
`dev_inject` 三档故障在**真服务子进程**里造出来（⑫ 条据此断言）；「收尾每一步的开始 / 结束 /
耗时与 `budget` / `backlog`」由 phase3 的编排在同一个子进程里落；③ 的「有实现却抛错」与
⑤ 的「某一步可控拖长」由注入子进程的共享假件（`FakeCaptureBackend` / `FakePostHook`）
造出来。**「服务级 log」与「场次 log」是两个不同的文件**：⑫ 条断的是**场次 log**
（`<场次目录>/meeting.log`）；服务级 log 那半边由 `tests/test_shutdown_signal.py` 第 ⑤ 条负责。
"""
