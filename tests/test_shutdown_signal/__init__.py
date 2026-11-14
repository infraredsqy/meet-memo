"""`Ctrl+C` 两阶段语义的进程级用例。

**顶部注释**：`SIGINT` 与 `SIGBREAK` **共用同一个处理函数**
（`meeting/service/app.py` 的 `handle_shutdown_signal`，安装器 `install_shutdown_signal_handlers()`
在服务器运行入口之前把它挂到两个信号上）—— 两个信号走的是**同一条代码路径**，
因此下面每一条用例都可用 `CTRL_BREAK_EVENT`（Windows 的子进程只能这样发信号）触发。

**真服务子进程**：起停、发中断信号、读退出码、取 `base_url` 四项能力都在
`tests/_service.py` 的 `ServiceProcess` 上，**本文件不另起一套子进程管理**。
子进程的环境变量与 `service_process` 夹具那一支**同一套**：家目录重定向（`HOME` +
`USERPROFILE`）、`MEETING_PORT` / `MEETING_LOG_DIR` / `MEETING_NO_BROWSER`
三项、`PYTHONPATH`；假件经临时目录 + `PYTHONPATH` + 既有配置键 `post.hook` 挂上（**本文件只
import `tests/_drain_fakes.py` 里的假件，不自行定义、不各写一份**）。**采集后端一律由本用例
`config.json` 的 `capture.backend` 表达，子进程 env 里不写 `MEETING_CAPTURE`** —— 这正是
`tests/test_drain.py` 那一支的同一口径（两条子进程路径的采集后端因此同源、也看得见），
理由见 `_spawn()` 的 docstring：环境变量会静默压住那条配置键。

**确定同步点（不许靠抢时间窗口）**：骨架构建下四步几乎瞬间跑完，靠抢时间窗口去
「收尾中途再按一次」会 flaky。本文件用**采集假件的 `finalize()`**（收尾第 1 步 `flush`）
把同步点钉死，两条各用一个子进程、各自一份临时 home 与临时假件目录：

  - 用 `FakeCaptureBackend` 的 `finalize_delay_seconds`（= `SHUTDOWN_BLOCKING_STEP_SECONDS`）：
    它经运行时的**收尾专用 daemon 执行器**跑在工作线程里，事件循环空着，`/api/state`
    在整个阻塞期间照常应答 → `POST stop` 之后轮询 `/api/state` 直到 `flush` 那一步
    `status == "running"`，再发 `CTRL_BREAK_EVENT`；
  - 用同一个假件的 `finalize_error`（`finalize()` 抛 `OSError`）先把收尾失败现场落定 →
    轮询到 `ended.phase == "draining"` 且 `flush` 那一步 `status == "failed"`，再发一次
    `CTRL_BREAK_EVENT`。

**`SIGINT` 那一路怎么真的送到**：本机（Windows 11 + Python 3.13.7）
实测 `GenerateConsoleCtrlEvent(CTRL_C_EVENT, ...)` 与 `os.kill(pid, CTRL_C_EVENT)` 都**送不到**
子进程（两条都返回成功、子进程一行 log 都不落），`CTRL_BREAK_EVENT` 则照常送达 —— 所以
「`SIGBREAK` 按一次、`SIGINT` 按一次，比对两边的结果字段与退出码」这件事没法靠控制台事件
收口。本文件改用**子进程自己给自己发**：包装脚本用 `python -c` 起（**包装脚本只存在于命令行
里，仓库里与仓库外都不落任何探针文件**），仍然跑真实的 `meeting.cli.main(["web"])`，另起一条
线程等 `SELF_SIGNAL_DELAY_SECONDS` 后调 `signal.raise_signal(signal.SIGINT)` —— 收到的是服务
自己装的 `handle_shutdown_signal`，走的是 `Ctrl+C` 那条真实路径（不是模拟、不是进程内调用），
两路于是可以逐字段比对。`raise_signal` 由主线程执行（CPython 的信号处理只认主线程），
所以它排在 `main()` 跑起来之后。
"""
