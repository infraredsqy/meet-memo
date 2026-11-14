"""收尾三步、自适应预算与失败出路的黑盒用例。

**测试层级（本文件写死的那一种）**：

  - 凡**通过服务可观测**的判据统一走**真服务子进程**，一律复用
    设计的 `service_process` 夹具与 `tests/_service.py` 的 `ServiceProcess` ——
    **本文件不另起一套子进程管理、不自带子进程起停**；
  - **假件一律从 `tests/_drain_fakes.py` 取，本文件只 import、不自行定义、不各写一份**：
    「有实现却抛错」、「某一步可控拖长」、
    「启动即完成」、「某一步钉在 `running`」全部由那五个共享假件配出来 ——
    行为参数经**环境变量旋钮**交进子进程（`FINALIZE_ERROR_ENV` / `FINALIZE_DELAY_SECONDS_ENV` /
    `LAUNCH_DELAY_SECONDS_ENV` / `LAUNCH_THREAD_SECONDS_ENV` / `BLOCK_SECONDS_ENV` /
    `BACKLOG_DEPTH_ENV` / `SEGMENT_TIMEOUT_SECONDS_ENV`；旋钮的说明在 `_drain_fakes.py` 里），
    选哪个假件经**既有配置键** `capture.backend` / `post.hook` / `transcriber.backend`。
    **本文件里没有一个本地假件类**，也没有第四份假件源码；
  - 假件**写进临时目录、经 `PYTHONPATH`（= 仓库根 + 该临时目录）**配进子进程，子进程侧的断言
    一律读**临时目录里的日志文件**（`<场次目录>/meeting.log` 与服务级 `meeting.log`）——
    **本文件不使用 `caplog`**（`caplog` 只抓当前进程的日志，与真服务子进程的日志不是同一份，
    二者混用会让断言读不到东西）；
  - 只有**纯函数口径**的断言留在进程内，那些断言不起服务、直接调用被断言的对象。

**「单条用例 ≤ 5 秒」怎么落**：每个只做自己那一遍
必需的现场操作（一次 `start` + `stop` 的建场-收尾路径、读一次 log 文件），自造的等待常量贴着
下界取（见下面每个常量上的说明），看门由 `tests/conftest.py` 的耗时 hook 自动判红。

**等到某条件一律带显式超时**：所有「等到某条件」都走 `tests/_helpers.py` 的 `wait_until()`，
超时值取本文件下面的具名常量，**不留裸秒数**。

**每一路健康迁移与告警起止由谁造出来**：由 `MEETING_CAPTURE=dev` 的两路合成电平与
`dev_inject` 三档故障在**真服务子进程**里造出来；「收尾每一步的开始 / 结束 /
耗时与 `budget` / `backlog`」由设计的编排在同一个子进程里落；「有实现却抛错」与
「某一步可控拖长」由注入子进程的共享假件（`FakeCaptureBackend` / `FakePostHook`）
造出来。**「服务级 log」与「场次 log」是两个不同的文件**：断的是**场次 log**
（`<场次目录>/meeting.log`）；服务级 log 那半边由 `tests/test_shutdown_signal.py` 负责。
"""
