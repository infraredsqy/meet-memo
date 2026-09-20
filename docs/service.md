# meeting · 服务骨架

> 这份文档写**当前服务已经落地的那一块**：怎么装、怎么起、起了之后有什么、配置键在哪、log 在哪、退出码是什么、测试怎么跑。
>
> 一句话边界：现在起得来的是**一个有会话、能推、能问的服务骨架**。它会开会、会结束、会把状态推给你，但**不会录进任何声音** —— 采集与转写后端还没接进来。
>
> 读者两种：把它装起来用的人（读第 1–3、8 章），和往它里面插后端 / 加功能的下游开发者（读第 2、4–7 章）。
>
> 四态状态机、快照字段、`session.json`、SSE 帧形状、告警判定写在 `docs/state.md`；五个端点与动作信封写在 `docs/api.md`。

## 章节目录

| 章 | 标题 |
|---|---|
| 1 | 怎么装、怎么起 |
| 2 | 起服务时它按什么顺序做什么 |
| 3 | 起了之后有什么 |
| 4 | 配置 |
| 5 | 日志 |
| 6 | 退出码 |
| 7 | 跑测试 |
| 8 | 现在能做什么 / 还接不上的部分 |
| 9 | 相关文档 |

## 1. 怎么装、怎么起

包名 `meeting`，Python ≥ 3.13，运行时依赖只有 `starlette` 与 `uvicorn`（模型与音频依赖留成 `.[capture]` / `.[asr]` 两个空 extra，由各自的后端节点去填）。本机实测环境：Python 3.13.7 / starlette 1.0.0 / uvicorn 0.42.0 / pytest 9.0.3 / httpx 0.28.1。

命令面只有两个词：

| 命令 | 现在干什么 |
|---|---|
| `meeting web` | 起本地服务，前台跑到 `Ctrl+C` |
| `meeting post` | **还没接入**：打印「会后流程还没接入（65dd3e4c）」并以退出码 `1` 结束 |

两种起法等价，选一种：

```powershell
meeting web                     # 装过包（console script，指向 meeting.cli:main）
python -m meeting web           # 不装包也行，在仓库根跑
python -m meeting post
```

- **不做可编辑安装也能跑**：在仓库根目录跑 `python -m meeting <动作词>` 即可（`python -m` 会把当前工作目录放进 `sys.path`）。
- **`--help` 这类开关没有**：`meeting` 不带参数、或写了 `web` / `post` 以外的词（例如 `meeting serve`），都会打印用法说明并以退出码 `2` 结束。

起好之后终端会打印三样：`URL：…`、`输出根：…`、`log 路径：…`。要调端口用 `MEETING_PORT=8766 meeting web`；不想让它开浏览器就设 `MEETING_NO_BROWSER=1`（测试与无头场景用，浏览器打开失败只警告、不阻塞启动）。

想真开一场会，得先把采集后端换成开发替身：

```powershell
$env:MEETING_CAPTURE="dev"; $env:MEETING_NO_BROWSER="1"; meeting web
```

## 2. 起服务时它按什么顺序做什么

顺序是定死的，逐步都能观察：

1. **先起 bootstrap 日志**：落在 `log.dir` 的默认位置（`~/.config/meeting/logs/meeting.log`），用默认轮转参数。这样后面的配置错误也留得下 log。
2. **读配置并做值级校验**：值非法 → 退出码 `2`；`config.json` 存在但读不出来或不是合法 JSON → 退出码 `3`。两档都在终端与 log 里写清原因。
3. **按生效配置重建服务级日志**：换掉 bootstrap 阶段的 handler；生效 `log.dir` 写不进去就是「主 log 不可写」→ 退出码 `3`，不再像 V1 那样静默降级。
4. **绑 `127.0.0.1:<port>`**（端口策略见 `docs/api.md` 第 5 章）：绑失败就先探一次占用者形状，两种情形都以退出码 `2` 结束。绑成功后从**这个监听 socket 的 `getsockname()`** 读回真实 host / port，往主 log 落一行 `绑定成功：<host>:<port>（getsockname 实测）`，并在终端打印 URL / 输出根 / log 路径。
5. **装信号处理函数**：`SIGINT` 与 `SIGBREAK` 挂**同一个**处理函数；服务器自己的信号捕获被屏蔽掉，所以运行期里生效的一直是本服务的处理函数。
6. **跑服务器**（已绑好并 `listen()` 的 socket 直接交给它，服务器不再自己绑端口）。框架生命周期的 startup 钩子在这之后依次做七步：取事件循环 → 写关停目标（语义：立即请求关停）→ 信号接管自检 → 解析三个后端 → 探测设备 → 组装初始快照 → 扫未收尾场次 → 打开浏览器。每一步都往主 log 留话，例如 `信号接管自检通过：SIGINT 与 SIGBREAK 指向本服务的处理函数`。
   - 解析后端失败一律**回退替身 + 一条 warning**，服务照常起；探测设备在 `bind()` 之后调，结果取三态直取进快照。
   - **扫未收尾场次**：只取输出根下最新的一场，`state` 是 `recording` / `draining` 或 `interrupted: true` 时挂 `notice.restart_unfinished`，并把那份记录改写成「收尾中 + 已打断」（原步骤表原样保留），同时**拦住新场次**。绝不自动续录。
   - **绝不自动开始录音**。

**`Ctrl+C`**：处理函数往主 log 落一行 `收到 SIGBREAK，进入关停`（或 `收到 SIGINT，进入关停`），然后请求关停，进程以退出码 `0` 结束。本阶段只有「空闲态触发 → 关停」这一半语义；收尾编排与「第二次 `Ctrl+C` 强制中断」还没有。

## 3. 起了之后有什么

| 路径 | 现在给什么 |
|---|---|
| `GET /` | 构建产物（`web.dist_dir`，默认 `web/dist`）存在就是那份 React 应用；**不存在就给一页说明**：`前端产物还没构建。跑 npm run build，或先用 /debug 自检页看服务状态`，同时在终端与 log 各留一行。服务照常起，不因为缺产物就起不来 |
| `GET /debug` | 骨架自检页，四块都接通了：读原始快照、按开始 / 结束 / 静音、事件流面板（`EventSource`）、心跳原始应答面板（每 2 秒问一次）。零依赖、无构建，页面内显式标注**非产品界面、不承担任何告警职责**；产物目录不存在时照样能读 |
| `GET /api/state` | 完整状态快照，`200`。字段与取值域见 `docs/state.md` 第 2 章 |
| `GET /api/events` | SSE，三通道。首帧一定是 `snapshot`。帧形状见 `docs/state.md` 第 6 章 |
| `GET /api/heartbeat` | 六字段，任何状态下都 `200`。`service` 这个字面量是端口探测认亲用的，必须有 |
| `POST /api/actions` | 五个动作（`start` / `stop` / `mute_session` / `settings_update` / `dev_inject`）。信封、动作表与 `code` 表见 `docs/api.md` |

`/api/*` 的应答一律带 `Cache-Control: no-store`。端口策略与三道本地最低防线（Host 校验 / 动作要自定义头 / 不开 CORS）见 `docs/api.md` 第 5 章。

## 4. 配置

- **位置**：`~/.config/meeting/config.json`（Windows 上即 `%USERPROFILE%\.config\meeting\config.json`）。V1 的 `~/.config/meeting_notes/` **不迁移、不自动改写**。
- **形状**：点号扁平键名的单层映射，例如 `{"web.port": 8766}`。嵌套对象不合法；写了键表里没有的键名也不合法。
- **优先级**：**环境变量 > `config.json` > 代码默认值**。
- **文件不存在** → 用默认值照常起。
- **`settings_update` 会写这个文件**（原子写：同目录临时文件 + `os.replace`）。它**只写文件里已有的键 + 这次改动的键**，不会把代码里的默认值落盘。

### 键与默认值

| 键 | 默认值 | 现在谁在用 |
|---|---|---|
| `web.port` | `8765` | 服务（绑定端口） |
| `web.dist_dir` | `web/dist` | 服务（`/` 挂什么） |
| `output.root` | `%USERPROFILE%\Documents\meeting\out` | 服务（场次目录建在它下面；**根不存在时在 `start` 那一刻建**） |
| `log.level` | `INFO` | 日志 |
| `log.dir` | `~/.config/meeting/logs` | 日志 |
| `log.max_bytes` | `5 MiB` | 日志轮转上限 |
| `log.backup_count` | `3` | 日志轮转保留的备份份数 |
| `alert.silence_dbfs` | `-60` | 告警判定（低于此值算这一路在静默） |
| `alert.silence_seconds` | `180` | 告警判定（静默连续多久才起致命级） |
| `alert.recover_seconds` | `10` | 告警判定（连续有信号多久才算恢复） |
| `alert.alarm_suppress_seconds` | `60` | 告警判定（同一事件多久内不重复出声） |
| `alarm.sound` | `true` | 本场出不出声的持久化开关（运行期取启动时的生效值） |
| `heartbeat.interval_seconds` | `10`（实现给的合法范围 `1–60` 秒） | **界面**发心跳的周期，进快照 `settings` |
| `capture.backend` | `auto` | 服务（选取采集后端；`auto` / `null` / `dev` / `module:Class`） |
| `transcriber.backend` | `auto` | 服务（选取转写后端，缺席时按缺省替身） |
| `post.hook` | `auto` | 服务（选取会后钩子，缺席 → 收尾第 4 步 `skipped`） |
| `asr.max_segment_seconds` | `10` | 热读（每段重新读），进快照 `settings.asr` |
| `asr.timeout_seconds` | `20` | 热读；必须大于 `asr.max_segment_seconds` |
| `asr.max_new_tokens` | `400` | 热读 |
| `asr.repeat_max_unit` | `40` | 热读 |
| `asr.repeat_min_coverage` | `0.5` | 热读 |

两个前缀说的两件事：`alert.*` 是**判定阈值**（静默多久算告警、多久算恢复、多久内不重复出声）；`alarm.*` 只管出声。两个「心跳」也不许串线：`heartbeat.interval_seconds` 是**界面**发心跳的周期，服务侧的采集帧心跳窗口是 `max(2.0s, 3 × frame_interval)`。

`asr.*` 五个默认值沿用 V1 实测值（`docs/legacy-assets.md` 的 B1）。

### 环境变量

一共五个，一个不多一个不少。其中四个改配置键的生效值，一个只是开关：

| 变量 | 作用 |
|---|---|
| `MEETING_PORT` | → `web.port` |
| `MEETING_OUT_ROOT` | → `output.root` |
| `MEETING_LOG_DIR` | → `log.dir` |
| `MEETING_CAPTURE` | → `capture.backend`（`dev` 就是那个开发替身） |
| `MEETING_NO_BROWSER=1` | 关掉自动打开浏览器（不指向任何配置键） |

**环境变量一律在启动时求值**，改了不会热生效。配置里也只有 `asr.*` 那五个键是「每段重新读」的，其余键改了要重启 —— 别把热读范围放大，会出现「我改了配置怎么没用」这类假 bug。

### 校验规则

- **值级非法** → 退出码 `2`：类型不对、超出范围、或违反键间规则。报错写清**键名、收到的值、合法范围**，并且在写生效值清单**之前**拦下。例如 `web.port` 不是整数或不在 `0–65535` 内；`asr.timeout_seconds` 必须大于 `asr.max_segment_seconds`。
- **文件级损坏** → 退出码 `3`：`config.json` 存在但读不出来（例如同名目录顶替）、或不是合法 JSON、或顶层不是 JSON 对象。报错写清**文件路径与出错的行**。
- **`settings_update` 的写前校验**（超时必须大于段长、键名必须在三分表里）也走同一条口径，违规回 `invalid_setting`(400)，**校验在写盘之前**。键态的两分与合法键名清单见 `docs/api.md` 第 4 章。

## 5. 日志

- **服务级主 log**：`~/.config/meeting/logs/meeting.log`（受 `log.dir` / `MEETING_LOG_DIR` 控制）。这是「服务挂了去哪儿查」的那一份。
- **轮转**：`RotatingFileHandler(max_bytes=log.max_bytes, backup_count=log.backup_count)` —— 默认 5 MiB 一份、留 3 份。**保留整份旧文件**（V1 那套「超限删前半、留后半」已换掉）。两个数都从配置取，改键即改行为。
- **场次级 log**：`<场次目录>/meeting.log`。`start` 成功之后挂上（`attach_session_log()`）；**摘掉与结束标记归后续节点**（`detach_session_log()` 目前没有调用者）。`detach_session_log()` 写失败时抛 `OSError`，不静默吞。
- **记录器**：名字 `meeting`，`propagate = False`（不污染 torch / transformers 的日志），初始化幂等（重复调用不重复加 handler）。
- **格式**（逐字）：

  ```
  %(asctime)s %(levelname)-7s [%(threadName)s] session=%(session)s %(name)s: %(message)s
  ```

  无场次时 `session=-`；`session` 字段由挂在 handler 上的 `SessionFilter` 注入（服务级 handler 恒 `-`，场次级 handler 是场次目录名）。

**现在会记下来的事**：

- 启动这一路（phase1 已落）：启动与选中的端口 / URL / 输出根；配置来源、生效值清单与热读键清单；静态挂载与产物缺失回退；端口探测的判定结论；bootstrap 日志被换到哪个新路径；设备探测逐步结果（状态、设备名、detail）；信号接管自检与 `收到 <信号名>，进入关停`；未捕获异常带堆栈；退出原因与退出码（写常量名，不写裸数字）。
- 场次这一路：**每一路的健康迁移**（含原因与阈值）、**告警起止与出声 / 抑制**（含 `muted` 与 `alarm.sound` 的生效值）、**场次开始与结束**（场次目录、`mode`、本场秒数）、**收尾每一步的开始与结束**（该步的 `status` 与说明）、后端解析结果与回退 warning、帧间隔读不到时的回退 warning、`settings_update` 写盘结果与被环境变量压住的 warning、SSE 慢消费者重同步。
- **启动扫描**：主 log 落**一行** `WARNING`，含场次目录路径与扫到的 `state` / `interrupted` 取值（每次启动最多一行）；命中时再落一行说明「已改写、这一场拦住新场次」。它不自动录音、只改那一份 `session.json`、不新建任何目录。

## 6. 退出码

五个常量定义在 `meeting/cli.py` 模块级，取值一起冻结；实现与断言只引用常量名。**五档之外不自造。**

| 常量 | 取值 | 触发条件 | 现在能走到吗 |
|---|---|---|---|
| `EXIT_OK` | `0` | 正常跑完：空闲态 `Ctrl+C` | 能（空闲态 `Ctrl+C` 这一支；收尾四步全落定之后关停那一支还没有） |
| `EXIT_FAILURE` | `1` | 收尾有步骤 `failed`；`meeting post` 眼下是桩 | 能（`post` 桩这一支；收尾失败那一支还没有） |
| `EXIT_REJECTED` | `2` | 三个触发面：**用法错误**（不带参数 / 动作词不在 `web` `post` 里）、**配置值非法**、**端口被占**（两种情形都算） | 能 |
| `EXIT_STARTUP_UNMET` | `3` | 启动前提不成立：主 log 不可写、`config.json` 读不出来或不是合法 JSON | 能 |
| `EXIT_INTERRUPTED` | `130` | 收尾中途第二次 `Ctrl+C` | **还不能**（收尾编排未落） |

`SIGTERM` 不在本服务的接管范围内，本阶段不定义它的退出码。

## 7. 跑测试

**从仓库根跑**（`pythonpath = ["."]` 按 rootdir 解析，工作目录一变 `import meeting` 就不成立）：

```powershell
python -m pytest -q
python -m pytest tests/test_state_machine.py tests/test_api_actions.py -q   # 只跑某几个文件
```

`tests/` 下现在有 **13 个测试文件、211 个用例**（`python -m pytest -q` 实测：`211 passed in 177.48s`）。不需要先 `pip install`，也不需要可编辑安装。

| 文件 | 用例数 | 验什么 |
|---|---|---|
| `tests/test_config.py` | 29 | 默认值表、三层优先级、环境变量映射、五个热读键、键间校验、配置损坏抛错、值非法 → 退出码 `2` |
| `tests/test_port_policy.py` | 22 | 端口与只监听回环、两种占用文案与退出码 `2`、探针 1 秒总超时、用法错误、`post` 桩、空闲态 `CTRL_BREAK_EVENT` → 退出码 `0` 与两行信号 log、console script 入口面 |
| `tests/test_static.py` | 18 | `/` 的产物与说明页两条分支、`/debug`、`/api/*` 的 `no-store`、Host 校验 `403`、动作头校验 `403`、完整版快照的键集与非空 `service.version`、`revision` 每次状态变更 +1、心跳六字段、空闲态盘上不留任何场次产物 |
| `tests/test_logging_setup.py` | 17 | 服务级 log 位置与格式、bootstrap 重建、按两个配置键轮转、幂等、主 log 不可写与配置损坏 → 退出码 `3`、扫未收尾场次只报最新一场（那行 `WARNING` 照旧；改写现场的断言归下面的重启用例） |
| `tests/test_api_actions.py` | 27 | 五个动作的成功与拒绝路径、`mode` 取值域、R17 的两个 409、`capture_unavailable`、`settings_update` 的两分与写盘拓扑与 `persist_failed`、逐动作 × 逐态可用性、`dev_inject` 的三档可用性、信封与 `code` 集合 |
| `tests/test_state_machine.py` | 20 | 四态取值域与全部合法迁移、非法迁移抛错且不涨 `revision`、三条铁律、`elapsed_seconds` 走秒与冻结、15 柱 × 2 秒包络、收尾闭环两个分支（成功档与失败停留档）、取消路径、探针越界取值回退 |
| `tests/test_alerts.py` | 18 | 逐帧累计（用帧时间戳）、连续性判据、抖动不来回告警、抑制窗口与「恢复后算新事件」、`muted` 的重算、提示级的合并键与上限 3、阈值只从配置读 |
| `tests/test_dev_backend.py` | 15 | dev 替身的确定性与 100ms 节拍、三档故障的语义与到点恢复、重复注入替换前一次、`finalize()` 只记账、非 dev 后端回 404 |
| `tests/test_sse.py` | 11 | 首帧是 `snapshot` 且与 `GET /api/state` 同源、`state` 是全量且 `id` 对齐 `revision`、`levels` 的 4Hz 与形状、`transcript` 形状、`retry` 与 `: ping`、慢消费者重同步且不拖累别人 |
| `tests/test_heartbeat.py` | 11 | `idle` 下的空形态、`stall` 之后 `recording_alive == false` 而 HTTP 照常 200、窗口计算、两个心跳不串线、`SourceFacts` / `SourceLost` 不刷新帧时钟、帧间隔读不到时按 0.5 回退 |
| `tests/test_session_dir.py` | 11 | 输出根只在 `start` 建、`preview_next_dir()` 只算不建、`YYMMDD` 与 `-2` / `-3` 让位、`session.json` 字段全貌与原子写（无 `.tmp` 残骸）、每个阶段变更都落盘、`start` 写盘失败清理目录并留痕 |
| `tests/test_restart_consistency.py` | 7 | 未收尾场次 → `notice` + 改写现场 + 拦住新场次、再重启读到同一副面孔、`interrupted: true` 单独算未收尾、已收尾的不动、输出根不存在时不建目录 |
| `tests/test_multi_client.py` | 5 | 两个订阅者拿到同一个 `revision`、revision 序列逐项相同、断开一个不影响另一个与会话、重连首帧是 `snapshot` 且不重放、`/debug` 与产物无关地可达 |

除这些，`tests/` 下还有**测试支持**：`tests/_helpers.py`（真服务子进程与假浏览器等工具）、`tests/_service.py`（真服务子进程驱动件 + SSE 读帧与等待助手）、`tests/conftest.py`（**只放 fixture**，五个夹具，其中一个 autouse 的兜底把 `webbrowser.open` 换成只记录不开窗的假替身）、`tests/_fake_webbrowser/`（替掉标准库 `webbrowser` 的假模块）。这些不被 `pytest` 收集。

测试的隔离手段：**重定向家目录**（`HOME` / `USERPROFILE` 都指向临时目录），配置与日志因此整套换落点；**不新增 `MEETING_*` 变量**。真服务一律用 `[sys.executable, "-m", "meeting", "web"]` 起子进程，地址从 `MEETING_PORT=0` 加子进程打印的 URL 行解析出来（**不猜端口**）。ASGI 层用 `with TestClient(app, base_url="http://127.0.0.1") as c:`（`base_url` 把回环 `Host` 带上，只有 `with` 会触发应用的生命周期钩子）。

## 8. 现在能做什么 / 还接不上的部分

**能**：起停、跑完启动序列、交 `/`（产物或说明页）、**接通了的 `/debug` 自检页**、`/api/state` 的完整快照、`/api/events` 的三通道 SSE、`/api/heartbeat` 的真值、`POST /api/actions` 的五个动作、四态与全部迁移、场次目录与原子写 `session.json`、`stop` 的四步收尾到终态、重启自洽闭环、告警判定与静音、`settings_update` 的原子写、dev 采集替身与三档故障注入。

**还接不上**（都按「能力还没进来」理解，服务本身照常跑）：

- **真实采集与转写**：默认后端是 `null` 替身，如实报「两路都未探测」；`start` 会回 `503 capture_unavailable`（「采集后端未接入：当前是 null 替身，不会真的录音」），盘上不留任何场次目录。**四步收尾里的第 1、2 步和第 4 步眼下都记 `skipped`** —— 没有真实音频可落盘、可转码，也没有会后钩子。
- **收尾的自适应预算**：收尾会按顺序真的跑完四步，但**每一步都不带超时等待**，也没有「预算内等不完就说话」这类提示。`compute_drain_budget()` 的名字与算法已经落在代码里（`30 + 积压段数 ×（单段超时 + 5）`，下限 30、封顶 300），**收尾路径眼下还没调它**。
- **收尾失败之后的完整出路**：失败档会把现场留在 `ended·draining`、服务不自杀、新的 `start` 回 `draining`(409)，但「出路说明」的文案（`meeting post <场次目录>` 或重启）还没有。
- **`Ctrl+C` 的收尾语义**：现在只有「空闲态触发 → 关停」这一半；第一次 `Ctrl+C` 跑收尾、第二次强制中断、`EXIT_INTERRUPTED` 都还没有。
- **场次级 log 的收尾**：`start` 已经把它挂上，**摘掉与结束标记还没有调用者**；收尾第 3 步 `log` 现在只写 `session.json` 的结束标记与服务级主日志的结束标记（不写 log 内容明细、不记耗时、不写预算与积压）。
- **四条端到端脚本与 README**：都还没有。
- **`meeting post`**：只有桩。
- **前端**：`web/` 与构建产物还没有；产物一建好，刷新 `/` 就是真界面。
- **`settings.devices`**：现在是空对象，子键等设备节点与双端契约定名。

## 9. 相关文档

- 需求锚：`docs/requirements.md`（R1 / R2 / R5 / R15 / R16 / R17）
- 状态、场次与推送：`docs/state.md`
- HTTP 面（端点、动作、`code` 表）：`docs/api.md`
- 界面：`docs/ui-design.md`（四态与迁移、告警分级、心跳判据）
- V1 迁移边界：`docs/legacy-assets.md`（B11 配置契约、D4 日志三处必改、D6 键间规则、C10 导入顺序）
- 后端协议：`docs/backend-protocols.md`
- 索引：`docs/spec.md`
