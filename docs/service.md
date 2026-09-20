# meeting · 服务骨架

> 这份文档写**当前服务已经落地的那一块**：怎么装、怎么起、起了之后有什么、配置键在哪、log 在哪、退出码是什么、测试怎么跑。
>
> 一句话边界：现在起得来的是**一个能起能停、能自证的服务骨架**。它会告诉你自己活着，但**不会录任何东西** —— 采集与转写后端还没接进来。
>
> 读者两种：把它装起来用的人（读第 1–3 章），和往它里面插后端 / 加功能的下游开发者（读第 4–7 章）。

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

## 2. 起服务时它按什么顺序做什么

顺序是定死的，逐步都能观察：

1. **先起 bootstrap 日志**：落在 `log.dir` 的默认位置（`~/.config/meeting/logs/meeting.log`），用默认轮转参数。这样后面的配置错误也留得下 log。
2. **读配置并做值级校验**：值非法 → 退出码 `2`；`config.json` 存在但读不出来或不是合法 JSON → 退出码 `3`。两档都在终端与 log 里写清原因。
3. **按生效配置重建服务级日志**：换掉 bootstrap 阶段的 handler；生效 `log.dir` 写不进去就是「主 log 不可写」→ 退出码 `3`，不再像 V1 那样静默降级。
4. **绑 `127.0.0.1:<port>`**（端口策略见第 3 章）：绑失败就先探一次占用者形状，两种情形都以退出码 `2` 结束。绑成功后从**这个监听 socket 的 `getsockname()`** 读回真实 host / port，往主 log 落一行 `绑定成功：<host>:<port>（getsockname 实测）`，并在终端打印 URL / 输出根 / log 路径。
5. **装信号处理函数**：`SIGINT` 与 `SIGBREAK` 挂**同一个**处理函数；服务器自己的信号捕获被屏蔽掉，所以运行期里生效的一直是本服务的处理函数。
6. **跑服务器**（已绑好并 `listen()` 的 socket 直接交给它，服务器不再自己绑端口）。框架生命周期的 startup 钩子在这之后依次做：取事件循环 → 写关停目标（语义：立即请求关停）→ 信号接管自检 → 探测设备 → 扫未收尾场次 → 打开浏览器。钩子里每一步都往主 log 留话，例如 `信号接管自检通过：SIGINT 与 SIGBREAK 指向本服务的处理函数`。

**`Ctrl+C`**：处理函数往主 log 落一行 `收到 SIGBREAK，进入关停`（或 `收到 SIGINT，进入关停`），然后请求关停，进程以退出码 `0` 结束。本阶段只有「空闲态触发 → 关停」这一半语义；收尾编排与「第二次 `Ctrl+C` 强制中断」还没有。

## 3. 起了之后有什么

| 路径 | 现在给什么 |
|---|---|
| `GET /` | 构建产物（`web.dist_dir`，默认 `web/dist`）存在就是那份 React 应用；**不存在就给一页说明**：`前端产物还没构建。跑 npm run build，或先用 /debug 自检页看服务状态`，同时在终端与 log 各留一行。服务照常起，不因为缺产物就起不来 |
| `GET /debug` | 骨架自检页。零依赖、无构建，页面内显式标注**非产品界面、不承担任何告警职责**；现在接通「读一次 `GET /api/state` 的原始快照」与「服务信息（版本 / 端口 / URL / 输出根 / log 路径）」，按开始 / 按结束与事件流面板是**可见的「未接入」占位** |
| `GET /api/state` | 状态快照（形状见下），`200` |
| `GET /api/heartbeat` | `{"service": "meeting", "server_time": …, "revision": N, "top_state": …, "recording_alive": null, "seconds_since_last_tick": null}`，`200`。`service` 这个字面量是端口探测认亲用的，必须有 |
| `POST /api/actions` | **端点本体还没接**。三道防线里的头部校验已经生效：缺 `X-Meeting-Service: 1` 或缺 `Content-Type: application/json` → `403`；头齐了 → `404` |

`/api/*` 的应答一律带 `Cache-Control: no-store`。

**状态快照的形状**（`GET /api/state` 的 body，顶层十键；现在取值全是空形态）：

| 顶层键 | 现在的取值 |
|---|---|
| `revision` | int，当前恒 `1`（还没有变更源） |
| `service` | 六个字段齐：`started_at` / `version` / `port` / `url` / `log_path` / `out_root`。`port` 与 `url` 由监听 socket 的 `getsockname()` 读回值产生，**不是**配置值或字面量拼接 |
| `top_state` | 恒 `"idle"` |
| `session` | `null` |
| `sources` | 恰好两项（`mic` / `loopback`），每项 `health = "unprobed"`、`device_name` 为 `null`、电平类字段全 `null`、`envelope = []` |
| `alerts` | `{"critical": null, "hints": []}` |
| `transcript` | `{"health": "normal", "last_line_at": null, "buffered_lines": 0, "failed_segments": 0}` |
| `ended` | `null` |
| `settings` | 顶层四键 `alarm_sound` / `heartbeat_interval_seconds` / `asr` / `devices`；前两个取生效配置值，后两个现在是空对象 `{}` |
| `notice` | `null` |

### 端口策略

- 默认 **8765**，只监听 **`127.0.0.1`** —— 这一条**不可配**，没有 `web.host` 这类键。
- `web.port` 或 `MEETING_PORT` 改端口；测试与无头场景可以用 **`0`** 让系统挑一个空闲端口，选中的端口照常打印并落 log（`web.port = 0` 时不做占用探测）。
- **端口被占不猜、不静默换端口**，两种情形都以退出码 `2` 结束：
  - 占用者应答出 meeting 形状（`GET /api/heartbeat` 带 `"service": "meeting"`）→「已经有一个 meeting 服务在跑：http://127.0.0.1:8765（当前空闲）」。括号里现在恒为「当前空闲」——还没有会话，取不到已录时长。
  - 占用者是别的程序 → 写明端口被谁占着、怎么改：`端口 8766 被别的程序占着，没法起服务。换一个端口再来，例如：MEETING_PORT=8766 meeting web`。
- **探针**：标准库 `socket` 手写一次 `GET /api/heartbeat`，**总超时 1 秒**（`time.monotonic()` 的 deadline 兜底）。超时、连接被拒、状态码非 200、body 里没有 `"service": "meeting"`，一律按「别的程序占着」处理。运行时代码不 import `httpx`。

### 三道本地最低防线

只监听回环挡的是外网，挡不住本机浏览器里的恶意页面往 `127.0.0.1:8765` 发请求。三条一起生效：

1. **Host 校验**：`Host` 不是 `127.0.0.1[:port]` / `localhost[:port]` 就 `403`（挡 DNS rebinding）。
2. **动作要自定义头**：`POST /api/actions` 必须同时带 `Content-Type: application/json` 与 `X-Meeting-Service: 1`，否则 `403`（挡表单式简单请求）。校验先于路由匹配生效。
3. **不开 CORS**：不发 `Access-Control-Allow-Origin`，前端由本服务同源发出。

## 4. 配置

- **位置**：`~/.config/meeting/config.json`（Windows 上即 `%USERPROFILE%\.config\meeting\config.json`）。V1 的 `~/.config/meeting_notes/` **不迁移、不自动改写**。
- **形状**：点号扁平键名的单层映射，例如 `{"web.port": 8766}`。嵌套对象不合法；写了键表里没有的键名也不合法。
- **优先级**：**环境变量 > `config.json` > 代码默认值**。
- **文件不存在** → 用默认值照常起。

### 键与默认值

| 键 | 默认值 | 现在谁在用 |
|---|---|---|
| `web.port` | `8765` | 服务（绑定端口） |
| `web.dist_dir` | `web/dist` | 服务（`/` 挂什么） |
| `output.root` | `%USERPROFILE%\Documents\meeting\out` | 服务（只解析成路径字符串，**不建目录**） |
| `log.level` | `INFO` | 日志 |
| `log.dir` | `~/.config/meeting/logs` | 日志 |
| `log.max_bytes` | `5 MiB` | 日志轮转上限 |
| `log.backup_count` | `3` | 日志轮转保留的备份份数 |
| `alert.silence_dbfs` | `-60` | 还没有消费方 |
| `alert.silence_seconds` | `180` | 还没有消费方 |
| `alert.recover_seconds` | `10` | 还没有消费方 |
| `alert.alarm_suppress_seconds` | `60` | 还没有消费方 |
| `alarm.sound` | `true` | 还没有消费方 |
| `heartbeat.interval_seconds` | `10`（实现给的合法范围 `1–60` 秒） | 还没有消费方 |
| `capture.backend` | `auto` | 服务（选取采集后端） |
| `transcriber.backend` | `auto` | 服务（选取转写后端） |
| `post.hook` | `auto` | 还没有消费方 |
| `asr.max_segment_seconds` | `10` | 还没有消费方 |
| `asr.timeout_seconds` | `20` | 还没有消费方 |
| `asr.max_new_tokens` | `400` | 还没有消费方 |
| `asr.repeat_max_unit` | `40` | 还没有消费方 |
| `asr.repeat_min_coverage` | `0.5` | 还没有消费方 |

两个前缀说的两件事：`alert.*` 是**判定阈值**（静默多久算告警、多久算恢复、多久内不重复出声）；`alarm.*` 只管出声。两个「心跳」也不许串线：`heartbeat.interval_seconds` 是**界面**发心跳的周期，服务侧的采集帧心跳窗口是 `max(2.0s, 3 × frame_interval)`。

`asr.*` 五个默认值沿用 V1 实测值（`docs/legacy-assets.md` 的 B1）。

### 环境变量

一共五个，一个不多一个不少：

| 变量 | 作用 |
|---|---|
| `MEETING_PORT` | → `web.port` |
| `MEETING_OUT_ROOT` | → `output.root` |
| `MEETING_LOG_DIR` | → `log.dir` |
| `MEETING_NO_BROWSER=1` | 关掉自动打开浏览器 |
| `MEETING_CAPTURE` | → `capture.backend` |

**环境变量一律在启动时求值**，改了不会热生效。配置里也只有 `asr.*` 那五个键是「每段重新读」的，其余键改了要重启 —— 别把热读范围放大，会出现「我改了配置怎么没用」这类假 bug。

### 校验规则

- **值级非法** → 退出码 `2`：类型不对、超出范围、或违反键间规则。报错写清**键名、收到的值、合法范围**，并且在写生效值清单**之前**拦下。例如 `web.port` 不是整数或不在 `0–65535` 内；`asr.timeout_seconds` 必须大于 `asr.max_segment_seconds`。
- **文件级损坏** → 退出码 `3`：`config.json` 存在但读不出来（例如同名目录顶替）、或不是合法 JSON、或顶层不是 JSON 对象。报错写清**文件路径与出错的行**。

## 5. 日志

- **服务级主 log**：`~/.config/meeting/logs/meeting.log`（受 `log.dir` / `MEETING_LOG_DIR` 控制）。这是「服务挂了去哪儿查」的那一份。
- **轮转**：`RotatingFileHandler(max_bytes=log.max_bytes, backup_count=log.backup_count)` —— 默认 5 MiB 一份、留 3 份。**保留整份旧文件**（V1 那套「超限删前半、留后半」已换掉：最早、最接近起因的那些行恰恰最先被删）。两个数都从配置取，改键即改行为。
- **场次级 log**：`<场次目录>/meeting.log`。挂 / 摘机制（`attach_session_log()` / `detach_session_log()`）已经就位，**但还没有调用者** —— 挂上归开会那一步，摘掉归收尾。`detach_session_log()` 写失败时抛 `OSError`，不静默吞。
- **记录器**：名字 `meeting`，`propagate = False`（不污染 torch / transformers 的日志），初始化幂等（重复调用不重复加 handler）。
- **格式**（逐字）：

  ```
  %(asctime)s %(levelname)-7s [%(threadName)s] session=%(session)s %(name)s: %(message)s
  ```

  无场次时 `session=-`；`session` 字段由挂在 handler 上的 `SessionFilter` 注入（服务级 handler 恒 `-`，场次级 handler 是场次目录名）。

**现在会记下来的事**：启动与选中的端口 / URL / 输出根；配置来源、生效值清单与热读键清单；静态挂载与产物缺失回退；端口探测的判定结论；bootstrap 日志被换到哪个新路径；设备探测结果；信号接管自检与 `收到 <信号名>，进入关停`；未捕获异常带堆栈；退出原因与退出码（写常量名，不写裸数字）。

**扫描未收尾场次**：启动时扫一次 `output.root`，**只取最新的一场**（场次目录名字典序最大者），它的 `session.json` 里 `state` 是 `recording` / `draining`、或 `interrupted: true` 时，往主 log 落**一行** `WARNING`（含场次目录路径与扫到的取值），**每次启动最多一行**。它不自动录音、不改写那份记录、不建目录、也不建输出根。

## 6. 退出码

五个常量定义在 `meeting/cli.py` 模块级，取值一起冻结；实现与断言只引用常量名。**五档之外不自造。**

| 常量 | 取值 | 触发条件 | 现在能走到吗 |
|---|---|---|---|
| `EXIT_OK` | `0` | 正常跑完：空闲态 `Ctrl+C`，或收尾四步全落定之后关停 | 能（空闲态 `Ctrl+C` 这一支） |
| `EXIT_FAILURE` | `1` | 收尾有步骤 `failed`；`meeting post` 眼下是桩 | 能（`post` 桩这一支；收尾失败那一支还没有） |
| `EXIT_REJECTED` | `2` | 三个触发面：**用法错误**（不带参数 / 动作词不在 `web` `post` 里）、**配置值非法**、**端口被占**（两种情形都算） | 能 |
| `EXIT_STARTUP_UNMET` | `3` | 启动前提不成立：主 log 不可写、`config.json` 读不出来或不是合法 JSON | 能 |
| `EXIT_INTERRUPTED` | `130` | 收尾中途第二次 `Ctrl+C` | **还不能**（收尾编排未落） |

`SIGTERM` 不在本服务的接管范围内，本阶段不定义它的退出码。

## 7. 跑测试

**从仓库根跑**（`pythonpath = ["."]` 按 rootdir 解析，工作目录一变 `import meeting` 就不成立）：

```powershell
python -m pytest tests/test_config.py tests/test_logging_setup.py tests/test_port_policy.py tests/test_static.py -q
python -m pytest -q          # 等价形态，跑全部
```

四个测试文件、85 个用例（实测）。不需要先 `pip install`，也不需要可编辑安装。

| 文件 | 验什么 |
|---|---|
| `tests/test_config.py` | 默认值表、三层优先级、环境变量映射、五个热读键、键间校验、配置损坏抛错、值非法 → 退出码 `2` |
| `tests/test_logging_setup.py` | 服务级 log 位置与格式、bootstrap 重建、按两个配置键轮转（备份份数与整份保留）、幂等、主 log 不可写与配置损坏 → 退出码 `3`、扫未收尾场次只报最新一场 |
| `tests/test_port_policy.py` | 端口与只监听回环、两种占用文案与退出码 `2`、探针 1 秒总超时、用法错误、`post` 桩、空闲态 `CTRL_BREAK_EVENT` → 退出码 `0` 与两行信号 log、console script 入口面 |
| `tests/test_static.py` | `/` 的产物与说明页两条分支、`/debug`、`/api/*` 的 `no-store`、Host 校验 `403`、动作头校验 `403` 与头齐 `404`、最小版快照形状 |

除这四个文件，`tests/` 下还有两个**测试支持模块**：`tests/_helpers.py`（真服务子进程、假浏览器、假 meeting 应答者、占用端口的工具）与 `tests/_fake_webbrowser/`（替掉标准库 `webbrowser` 的假模块）。pytest 只收集上面那四个测试文件。

测试的隔离手段：**重定向家目录**（`HOME` / `USERPROFILE` 指向临时目录），配置与日志因此整套换落点；**不新增环境变量**。起真服务一律用 `[sys.executable, "-m", "meeting", <动作词>]` 且 `cwd` 给仓库根。ASGI 层用 `with TestClient(app, base_url="http://127.0.0.1") as c:` —— `base_url` 把回环 `Host` 带上，且只有 `with` 会触发应用的生命周期钩子。

## 8. 现在能做什么 / 还接不上的部分

**能**：起停、跑完启动序列、交 `/`（产物或说明页）、`/debug` 自检页、`/api/state` 与 `/api/heartbeat` 的最小版、三道本地防线、五档退出码常量（`EXIT_OK` / `EXIT_FAILURE`（`post` 桩）/ `EXIT_REJECTED` / `EXIT_STARTUP_UNMET` 四支可断言）、`Ctrl+C` 关停。

**还接不上**（都按「能力还没进来」理解，服务本身照常跑）：

- **采集与转写**：默认后端是 `null` 替身，如实报「两路都未探测」；调它的 `start()` 会抛 `capture_unavailable`（「采集后端未接入：当前是 null 替身，不会真的录音」）。**四态状态机、场次目录与 `session.json` 写入都还没有**，所以「开始录音」这件事现在整条路走不通 —— `/debug` 上以**可见的「未接入」占位**呈现，不留静默失败的死按钮。
- **动作端点**：`POST /api/actions` 的端点本体还没有（头齐的请求现在拿 `404`），它的两道头部校验已经生效。
- **实时推送**：SSE（`GET /api/events`）还没有，`/debug` 只能拉一次 `GET /api/state`。
- **`meeting post`**：只有桩。
- **前端**：`web/` 与构建产物还没有；产物一建好，刷新 `/` 就是真界面。
- **告警判定**：四个阈值的默认值已在配置表里，`alerts.py` 还没有；界面侧那个 10 秒心跳的消费方也还没有。
- **收尾编排**：四步收尾、预算、第二次 `Ctrl+C` 强制中断、`EXIT_INTERRUPTED` 都还没有。
- **场次级 log 的挂 / 摘调用点**：机制在位，调用者还没有。
- **`recording_alive` / `seconds_since_last_tick`**：恒 `null`（没有后端、也一帧事实都没收到过，与实际一致）。
- **设备探测结果**：只进 log，`/debug` 现在显示的是服务信息与原始快照，探测明细还没有面板。

## 9. 相关文档

- 需求锚：`docs/requirements.md`（R5 心跳 / R15 两个词 / R16 必须留 log / R17 只录一场）
- 界面：`docs/ui-design.md`（四态与迁移、告警分级、心跳判据）
- V1 迁移边界：`docs/legacy-assets.md`（B11 配置契约、D4 日志三处必改、D6 键间规则、C10 导入顺序）
- 后端协议：`docs/backend-protocols.md`
- 索引：`docs/spec.md`
