"""`Ctrl+C` 两阶段语义的进程级用例（设计口径 / §3.8；契约点名的六个函数逐字在位）。

**用例计数（照实登记，数目可核对）**

  - **契约点名的六个函数**（§4 落点表那行逐条列出）：`test_idle_sigbreak_exits_zero` /
    `test_recording_sigbreak_drains_and_exits_zero` / `test_second_sigbreak_during_drain_exits_130` /
    `test_sigbreak_while_already_draining_exits_130` / `test_failed_drain_exits_one_and_prints_log_path` /
    `test_sigint_and_sigbreak_share_one_handler` —— 六个一个不少、各自独立；
  - **审查修复追加的 3 个函数**（名字与上面六个不相撞、也不顶替其中任何一条；出处写在各自的
    docstring 里）：`test_clean_drain_then_sigbreak_exits_zero_without_rewriting_disk`
    （**C1 的另一格**：`stop` 已收尾干净走完之后按下 → 空闲退出档、盘上逐字节不变、
    重启后能直接开新场）、`test_web_startup_failure_exits_failure_and_says_so`
    （M11：`server.run()` 之外那一路的意外同样以 `EXIT_FAILURE` 说话）、
    `test_drain_exit_code_matrix_including_the_server_crash_fallback`（M4：退出码逐档写死，
    含「收尾干净 + `server.run()` 抛异常」这个组合；它不起服务、直接调 `meeting/cli.py` 的
    `_drain_exit_code()`，因此不占墙钟）；
  - **第六轮审查再追加的 2 个函数**（同样不与上面任何一条相撞）：原文那一句「追加的 3 个函数」
    的实际条数是 4（上面已逐条列全），这里只登记本轮新增的两条 ——
    `test_second_press_in_the_same_process_carries_force_true`（**C1**：同一个真服务子进程
    连按两次信号，第二次带 `force=True`，落进 log 的是「本次是第 2 次按下」）、
    `test_drain_report_speaks_for_the_real_face_not_the_exit_code`（**M1**：终端那套「收尾有
    步骤失败」的文案只在现场真有 `failed` 步时出现；进程内纯函数级、不占墙钟）。
    **合计 6 + 3 + 2 = 11 个测试函数**（本文件里没有参数化；`notice.restart_unfinished`
    那一条用例随启动扫描一并退场，见上一节）。
  - **第七轮审查再追加的 2 个函数**（同样不与上面任何一条相撞、各自独立）：
    `test_degraded_state_read_still_raises_when_the_service_answers_badly`
    （**m7b**：`_state_while_alive()` 只把「进程已收场」与「连接被断」这两种真时序读成
    `None`，服务答了个非 200 时照样抛出去 —— 于是「退出前读到的是运行期快照」这件事有活的
    判据，不会被一句 `except Exception: return None` 静默吞掉；进程内纯函数级、不占墙钟）、
    `test_interrupted_report_speaks_for_the_forced_interrupt_not_the_failed_face`
    （**M11**：强制打断档 + 现场前面有 `failed` 步这一格的终端文案；进程内纯函数级、不占墙钟）。
    **本文件当前实测收集数**：`python -m pytest tests/test_shutdown_signal.py --collect-only -q`
    = **13 条**（契约点名的六个一个不少、各自独立；其余为历次审查修复追加的条目，均不带参数化）。

**「重启提示态」那一格已随默认输出根退场**（`docs/design-intent.md` 第 4 条）：启动不再扫
最近一场、不再挂 `notice.restart_unfinished`、也不再把盘上的未收尾场次改写成
`draining + interrupted`。原先盯那一格的
`test_restart_notice_first_sigbreak_exits_zero_without_rewriting_disk` 因此整条删除；
「相关行为归下一节点的注册表收编」（节点 spec 上下游一节）。

**C1 的第二格（同一处判据的另一面）**：`stop` 已经把四步全部收尾干净（`ended.phase == "done"`、
盘上 `state="done"` + `ended_at`）之后按一次 `Ctrl+C`，同样落在「当场没有收尾要管」这一格 ——
退出码 `EXIT_OK`、`session.json` **逐字节未变**（不写 `interrupted`、不删 `ended_at`）、
盘上那份场次不会被重启扫描认领（`start` 因此能直接开新场）。这一格由
`test_clean_drain_then_sigbreak_exits_zero_without_rewriting_disk` 钉住（进程级）。

**顶部注释（契约要求写在这里）**：`SIGINT` 与 `SIGBREAK` **共用同一个处理函数**
（`meeting/service/app.py` 的 `handle_shutdown_signal`，安装器 `install_shutdown_signal_handlers()`
在服务器运行入口之前把它挂到两个信号上）—— 两个信号走的是**同一条代码路径**，
因此下面每一条用例都可用 `CTRL_BREAK_EVENT`（Windows 的子进程只能这样发信号）触发，
第 ⑥ 条再用 `CTRL_C_EVENT` 复证两者结果一致。

**真服务子进程**：起停、发中断信号、读退出码、取 `base_url` 四项能力都在
`tests/_service.py` 的 `ServiceProcess` 上（设计的夹具能力），**本文件不另起一套子进程管理**。
子进程的环境变量与 `service_process` 夹具那一支**同一套**：家目录重定向（`HOME` +
`USERPROFILE`）、`MEETING_PORT` / `MEETING_LOG_DIR` / `MEETING_NO_BROWSER`
三项、`PYTHONPATH`；假件经临时目录 + `PYTHONPATH` + 既有配置键 `post.hook` 挂上（**本文件只
import `tests/_drain_fakes.py` 里的假件，不自行定义、不各写一份**）。**采集后端一律由本用例
`config.json` 的 `capture.backend` 表达，子进程 env 里不写 `MEETING_CAPTURE`** —— 这正是
`tests/test_drain.py` 那一支的同一口径（两条子进程路径的采集后端因此同源、也看得见），
理由见 `_spawn()` 的 docstring：环境变量会静默压住那条配置键。

**③ 与 ④ 的确定同步点（不许靠抢时间窗口）**：骨架构建下四步几乎瞬间跑完，靠抢时间窗口去
「收尾中途再按一次」会 flaky。本文件用**采集假件的 `finalize()`**（收尾第 1 步 `flush`）
把同步点钉死，两条各用一个子进程、各自一份临时 home 与临时假件目录：

  - ③ 用 `FakeCaptureBackend` 的 `finalize_delay_seconds`（= `SHUTDOWN_BLOCKING_STEP_SECONDS`）：
    它经运行时的**收尾专用 daemon 执行器**跑在工作线程里，事件循环空着，`/api/state`
    在整个阻塞期间照常应答 → `POST stop` 之后轮询 `/api/state` 直到 `flush` 那一步
    `status == "running"`，再发 `CTRL_BREAK_EVENT`；
  - ④ 用同一个假件的 `finalize_error`（`finalize()` 抛 `OSError`）先把收尾失败现场落定 →
    轮询到 `ended.phase == "draining"` 且 `flush` 那一步 `status == "failed"`，再发一次
    `CTRL_BREAK_EVENT`。

**这两条原先钉的是第 4 步 `post`**（一个 2 秒不返回的假会后钩子、一个抛错的假会后钩子）。
按「结束录音」之后会后一个动作都不自动跑（R26）之后，第 4 步只做「让会中模型退出显存」
就落 `waiting`，它既不是「被阻塞钉住」的同步点、也造不出失败步 —— 同步点因此落到采集件上
（`tests/test_drain.py` 的 ③ / ⑤ / ⑩ 用的是同一套手法）。「哪一步被钉住」变了，
被断言的那几件事（收尾中途按下、失败现场、退出码、盘上那一版）一个字没改。

**⑥ 里 `SIGINT` 那一路怎么真的送到（写清手法与实测结论）**：本机（Windows 11 + Python 3.13.7）
实测 `GenerateConsoleCtrlEvent(CTRL_C_EVENT, ...)` 与 `os.kill(pid, CTRL_C_EVENT)` 都**送不到**
子进程（两条都返回成功、子进程一行 log 都不落），`CTRL_BREAK_EVENT` 则照常送达 —— 所以
「①`SIGBREAK` 按一次、②`SIGINT` 按一次，比对两边的结果字段与退出码」这件事没法靠控制台事件
收口。本文件改用**子进程自己给自己发**：包装脚本用 `python -c` 起（**包装脚本只存在于命令行
里，仓库里与仓库外都不落任何探针文件**），仍然跑真实的 `meeting.cli.main(["web"])`，另起一条
线程等 `SELF_SIGNAL_DELAY_SECONDS` 后调 `signal.raise_signal(signal.SIGINT)` —— 收到的是服务
自己装的 `handle_shutdown_signal`，走的是 `Ctrl+C` 那条真实路径（不是模拟、不是进程内调用），
两路于是可以逐字段比对。`raise_signal` 由主线程执行（CPython 的信号处理只认主线程），
所以它排在 `main()` 跑起来之后。
"""
