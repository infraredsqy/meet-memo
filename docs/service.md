# meeting · 服务骨架

> 这份文档写**当前服务已经落地的那一块**：怎么装、怎么起、起了之后有什么、配置键在哪、log 在哪、退出码是什么、测试怎么跑。
>
> 一句话边界：现在起得来的是**一个有会话、能推、能问、能真录音、能边录边出转写临时稿、收尾会拉起会后离线转写的服务**。它会开会、会结束、会把状态推给你，开一场就把两路真声音边采边写进场次目录、把两路转写逐行写进会中临时稿，收尾转出 MP3，末尾那一步把会后离线转写拉起来（起线程就返回，产物稍后落进场次目录）。
>
> 读者两种：把它装起来用的人（读第 1–3、8 章），和往它里面插后端 / 加功能的下游开发者（读第 2、4–7 章）。
>
> 四态状态机、快照字段、`session.json`、SSE 帧形状、告警判定写在 `docs/state.md`；九条端点（七条 `/api/*` + 静态首页 + `/debug`）与动作信封写在 `docs/api.md`；会中总结那一层（窗口、提示词、配置键、动作与记录端点）写在 `docs/summary.md`。

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

包名 `meeting`，Python ≥ 3.13。运行时依赖两条主干 `starlette` / `uvicorn`，外加**音频四件套** `numpy` / `sounddevice` / `soundcard` / `pydub`（`dca4539b` §5 的裁定：录音是产品主体，放可选 extra 会让「没装 extra」退化成「一场会什么都没录」）。转写要的模型依赖在 `.[asr]` extra 里（`torch==2.9.1+cu128` / `transformers==5.15.0` / `webrtcvad-wheels==2.0.14`）；`torch` 那个是 CUDA 12.8 构建、PyPI 上没有这个 wheel，装的时候要带索引：`pip install --extra-index-url https://download.pytorch.org/whl/cu128 "meeting[asr]"`（CPU 档写 `torch==2.9.1`、不用那个索引即可）。**不装这一格照样能起服务**：重依赖全在函数体内惰性 import，转写那一路缺模型时如实记失败，录音一个字都不受影响。`capture` extra 有意留空。本机实测环境：Python 3.13.7 / starlette 1.0.0 / uvicorn 0.42.0 / pytest 9.0.3 / httpx 0.28.1 / numpy 2.4.4 / sounddevice 0.5.5 / soundcard 0.4.6 / pydub 0.25.1。

**外部前置条件：`ffmpeg` 要在 PATH 上**（`pydub` 靠它转 MP3；装机时一并装）。它缺席时服务照常起、照常录，只是收尾第 2 步 `transcode` 如实记 `failed`，**归档 PCM 原样留在场次目录里**（一个字节都不丢）。

命令面只有两个词：

| 命令 | 现在干什么 |
|---|---|
| `meeting web` | 起本地服务，前台跑到 `Ctrl+C` |
| `meeting post <场次目录>` | **已经接上**：只跑会后那一套（音频归档补齐 → 会后离线转写与说话人聚类 → 会后纪要），不开界面、不起服务，跑完打印每层的结论与产物绝对路径就退出 |

两种起法等价，选一种：

```powershell
meeting web                     # 装过包（console script，指向 meeting.cli:main）
python -m meeting web           # 不装包也行，在仓库根跑
meeting post <场次目录>          # 只跑会后那一套，跑完就退
python -m meeting post <场次目录>
```

- **不做可编辑安装也能跑**：在仓库根目录跑 `python -m meeting <动作词>` 即可（`python -m` 会把当前工作目录放进 `sys.path`）。
- **`meeting post` 认一个开关**：`--force`（重复给也照收）。它是本轮**唯一**会再次花钱的开关 —— 它无视两层的终态、把离线转写与纪要都重跑一遍；不给它时那一趟按幂等闸走：两层都已是终态且产物齐就一个引擎调用、一个子进程都不产生（终端打一句「无待办」），只差一层就只补那一层，任一层停在 `running` 就**拒跑**（退出码 `2`，提示里点名那一个状态文件并给出 `--force`）。
- **`meeting post` 要一个位置参数**：`<场次目录>` 给全（**不猜最新一场、不列目录内容、没有交互菜单**）。不给它、给多了、指到不存在的路径、或写了 `--force` 以外的开关，都会打印用法说明并以退出码 `2` 结束。
- **`--help` 这类开关没有**：`meeting` 不带参数、或写了 `web` / `post` 以外的词（例如 `meeting serve`），都会打印用法说明并以退出码 `2` 结束。
- **`meeting post` 读配置、也按生效配置重建服务级日志**（与 `web()` 同一套：配置损坏退出码 `3`、取值非法退出码 `2`，`log.dir` 那四格生效）。它的落点是命令行显式给的那一个场次目录。

起好之后终端会打印两样：`URL：…`、`log 路径：…`。要调端口用 `MEETING_PORT=8766 meeting web`；不想让它开浏览器就设 `MEETING_NO_BROWSER=1`（测试与无头场景用，浏览器打开失败只警告、不阻塞启动）。

想真开一场会，直接起服务按「开始录音」就行（默认 `capture.backend = auto` 解析到真采集后端、默认 `transcriber.backend = auto` 解析到真转写后端）。采集替身仍然在，但它只在显式点名时上场（`dev` 是**非产品路径**：合成电平 + 三档可注入故障，给开发与测试用）：

```powershell
$env:MEETING_CAPTURE="dev"; $env:MEETING_NO_BROWSER="1"; meeting web
```

`transcriber.backend = dev` 落的是同一个 `dev` 一档里的**转写替身**（合成转写行，不读盘、不加载模型）；它没有环境变量，要开就在配置文件里写。只想让界面上的字幕动起来、不想装模型依赖时用它。

## 2. 起服务时它按什么顺序做什么

顺序是定死的，逐步都能观察：

1. **先起 bootstrap 日志**：落在 `log.dir` 的默认位置（`~/.config/meeting/logs/meeting.log`），用默认轮转参数。这样后面的配置错误也留得下 log。
2. **读配置并做值级校验**：值非法 → 退出码 `2`；`config.json` 存在但读不出来或不是合法 JSON → 退出码 `3`。两档都在终端与 log 里写清原因。
3. **按生效配置重建服务级日志**：换掉 bootstrap 阶段的 handler；生效 `log.dir` 写不进去就是「主 log 不可写」→ 退出码 `3`，不再像 V1 那样静默降级。
4. **绑 `127.0.0.1:<port>`**（端口策略见 `docs/api.md` 第 5 章）：绑失败就先探一次占用者形状，两种情形都以退出码 `2` 结束。绑成功后从**这个监听 socket 的 `getsockname()`** 读回真实 host / port，往主 log 落一行 `绑定成功：<host>:<port>（getsockname 实测）`，并在终端打印 URL / log 路径。
5. **装信号处理函数**：`SIGINT` 与 `SIGBREAK` 挂**同一个**处理函数；服务器自己的信号捕获被屏蔽掉，所以运行期里生效的一直是本服务的处理函数。
6. **跑服务器**（已绑好并 `listen()` 的 socket 直接交给它，服务器不再自己绑端口）。框架生命周期的 startup 钩子在这之后依次做六步：取事件循环 → 写关停目标（语义：**先跑收尾、再请求关停**）→ 信号接管自检 → 解析三个后端 → 探测设备 → 组装初始快照 → 打开浏览器。每一步都往主 log 留话，例如 `信号接管自检通过：SIGINT 与 SIGBREAK 指向本服务的处理函数`。
   - 解析后端失败一律**回退替身 + 一条 warning**，服务照常起；探测设备在 `bind()` 之后调，结果取三态直取进快照。
   - **`transcript` 那几格的初值从转写后端读一次**（`meeting/service/runtime.py` 的 `_refresh_transcript_snapshot()`，位置在探测设备**之后** —— 转写后端是在那一步 `bind()` 上的）。没起过场次时快照里的 `transcript.health` 因此当场就是真实现报的 `stopped`，用不着等健康度节拍的第一拍。
   - **转写健康度节拍**（0.5 秒一拍）由 `start_session()` 起，只刷 `transcript` 那几格，并且只在 `health` 真迁移时 `commit()`；读路径（`/api/state`、SSE 首帧）保持纯读，谁读一次都不会涨 `revision`。
   - **不再扫未收尾场次**：启动不读任何别的场次目录、不挂重启提示、也不改写盘上的记录（`docs/design-intent.md` 第 4 条；「看住未收尾场次」由收编节点落在配置目录的工作区注册表里，界面经 `GET /api/sessions` 读）。
   - **绝不自动开始录音**。

**`Ctrl+C` 的两阶段语义**：`SIGINT` 与 `SIGBREAK` 挂在 `handle_shutdown_signal` 这**一个**处理函数上。函数体只做三件事：往主 log 落一行 `收到 SIGINT，进入关停`（或 `收到 SIGBREAK，进入关停`）、把按下次数加一、把关停目标排进事件循环。收尾状态、`interrupted` 与 `session.json` 的写值都不在它里面发生，写者始终是 `ServiceRuntime`。

按下之后落在哪一档，看的是**这一刻有没有收尾要管**：

| 按下的时刻 | 做什么 | 退出码 |
|---|---|---|
| 正在录（`recording` / `alert`） | 走与 `stop` 同一条的 `stop_session(reason="ctrl_c")` 起收尾，等它落定再关停；页面上的收尾进度照常推 | 收尾干净 `0`；有步骤 `failed` `1` |
| 刚起、还没开过任何一场（`idle`） | 直接退出 | `0` |
| `stop` 已经收尾干净走完之后 | 直接退出，盘上那一版（`state=done` + `ended_at`）逐字节不动 | `0` |
| 收尾还在跑，或收尾失败后停在 `ended·draining` | **等同第二次按下**：先落盘（`draining` + `interrupted: true`、原 `steps` 保留、不写 `ended_at`）→ 再 `cancel()` 收尾协程 → 关停 | `130` |
| 第二次按下 | 同上 | `130` |

被强制打断的场次，盘上留的是「收尾中 + 已打断」，没有 `ended_at` —— 重启与 `meeting post` 都还接得上。第二次按下时终端会打出行踪：档位、盘上那一格与 log 路径。

**收尾有步骤 `failed` 时服务不自杀**：进程继续活着、页面还能看、新的「开始录音」一律回 `409 draining`（带上失败那一步与 log 路径），出路是 `meeting post <场次目录>` 或看清现场后重启服务。收尾这一段本身的进度（四步、预算、耗时）见 `docs/state.md` 第 1 章。

**关停时会等转写收尾，事件循环在这段时间里被占住（有意的取舍）**：`shutdown()` 关掉转写这一档走的是 `TranscriberBackend.stop()`，它是**同步**等待 —— 流水线的收尾（关读线程、送 VAD 尾段、在预算内把队列里剩下的段做完）放进一条 daemon 线程，`stop()` 就在调用方线程里 `join` 它，而调用方正是事件循环上的那条 lifespan 协程。于是这一段里 `/api/heartbeat`、SSE、动作端点都不应答，进程也退不出去。取舍的理由是：此刻服务正在离场、录音已经收尾，等的就是「把临时稿的尾巴尽量做完」（不等就会丢尾巴，异步化要改整个关停编排）。

等的上界（`meeting/backends/transcribe.py` 的 `drain_join_budget()`）：

```
上界 = compute_drain_budget(积压段数 + 每路最多一段尾段, 单段超时)   # 下限 30s、封顶 300s
     + DRAIN_STOP_SLACK_SECONDS（1.0s）                        # 预算耗完那一刻 worker 手上那一段
     + 读线程收尾上界 × 本场路数                                # READER_JOIN_TIMEOUT_SECONDS（2.0s）/ 路
```

积压量那一项要**自己加上「每路最多一段尾段」**：内层是**先** `vad.flush()` 把两路手上那一段入队、**之后**才算自己的收尾预算，外层若只读「调内层之前那一刻的积压量」，最坏档（模型卡住 + 关停时两路都还压着尾巴）内层预算会比外层大 25 秒/段，外层先超时 → log 里那条 `转写收尾线程在 …s（收尾预算 + 余量）内没跑完` 就成了假的，`stop()` 还会在流水线仍在收尾时返回（末尾那句「还有 N 段没做完」因此没机会落 log）。尾段数按**本场真的在读的那几路**算。

空闲态（积压 0、两路，也就是刚起来还没开过场次就按 `Ctrl+C` 那一档）因此是 **30 + 1 + 4 = 35 秒**，最坏档是 **300 + 1 + 4 = 305 秒**；`mode=mic` 时读线程那一项按一路算（33 秒 / 303 秒）。这就是「`Ctrl+C` 之后可能要等一会儿才退出」的出处 —— 卡在上界那一刻，log 里会有一条 `转写收尾线程在 …s（收尾预算 + 余量）内没跑完`，它是真的没跑完（内层最坏档已经被外层余量盖住了）。

**同一笔同步收尾还有第二个调用点：换场。** 上面的口径只覆盖**关停**这一条路；服务里连开两场时，第二次「开始录音」也会在事件循环上同步等一次收尾 —— `TranscriberBackend.start()` 的第一句是 `_retire_previous()`，它直接调上一场流水线的 `stop()`（`stop` 动作只收采集、不碰转写，所以上一场通常还在）。触发时机与上界的差别是：**换场这一档没有外层余量**，等的就是流水线自己那笔预算 + 读线程收尾上界（最坏 300 + 4 = 304 秒），也就是「上一场积压很多、模型又卡着时，再点一次『开始录音』要等到那笔预算跑完才应答」（这一段里 `/api/heartbeat`、SSE、动作端点同样被占住）。理由同 `shutdown()` 那一档：换场必须先把上一场收干净，否则上一场的读线程会继续轮询上一场的归档路径。

## 3. 起了之后有什么

| 路径 | 现在给什么 |
|---|---|
| `GET /` | 构建产物（`web.dist_dir`，默认 `web/dist`）里的那份 React 界面。**三档**：目录有内容 → 产物；目录不存在 → 一页说明（`前端产物还没构建。跑 npm run build，或先用 /debug 自检页看服务状态`），同时在终端与 log 各留一行；**目录在但是空的 → 404**。服务照常起，不因为缺产物就起不来；目录存在性**按每请求判定**，产物一建好刷新即可见（不必重启服务） |
| `GET /debug` | 骨架自检页，四块都接通了：读原始快照、按开始 / 结束 / 静音、事件流面板（`EventSource`）、心跳原始应答面板（每 2 秒问一次）。零依赖、无构建，页面内显式标注**非产品界面、不承担任何告警职责**；产物目录不存在时照样能读 |
| `GET /api/state` | 完整状态快照，`200`。字段与取值域见 `docs/state.md` 第 2 章 |
| `GET /api/events` | SSE，四个事件（`snapshot` / `state` / `levels` / `transcript`）。首帧一定是 `snapshot`。帧形状见 `docs/state.md` 第 6 章 |
| `GET /api/heartbeat` | 六字段，任何状态下都 `200`。`service` 这个字面量是端口探测认亲用的，必须有 |
| `GET /api/summaries` | 本场按过的、成功产出的那几次会中总结（时刻 + 窗口 + 那几条），任何状态下都 `200`。记录活在内存里、不落文件，新的一场 `start` 清空 |
| `GET /api/sessions` | 工作区注册表 + `recent`（最近一场），任何状态下都 `200`，**纯读**。形状见 `docs/contract.md` 4.8 |
| `GET /api/dirs` | 目录选择器的一层现场（起点用户主目录，`?dir=` 换层），`200`，**纯读** |
| `POST /api/actions` | 十一个动作（`start` / `stop` / `mute_session` / `settings_update` / `reprobe` / `dev_inject` / `summarize` / `workspace_open` / `workspace_remove` / `post_offline` / `post_minutes`）。信封、动作表与 `code` 表见 `docs/api.md`；末尾两个是会后两个按钮（R26） |

`/api/*` 的应答一律带 `Cache-Control: no-store`。端口策略与三道本地最低防线（Host 校验 / 动作要自定义头 / 不开 CORS）见 `docs/api.md` 第 5 章。

`GET /api/summaries` 是本服务唯一一条「读内存里那点会话级数据」的端点（会中总结的记录表）：它**不落文件**，也没有配置键；七个 `summary.*` 键（三个端点键与四个预算 / 缺省档键）的取值与默认值见 `meeting/config.py` 与 `docs/api.md` 第 4 章 `settings_update` 那一张键表。

## 4. 配置

- **位置**：`~/.config/meeting/config.json`（Windows 上即 `%USERPROFILE%\.config\meeting\config.json`）。V1 的 `~/.config/meeting_notes/` **不迁移、不自动改写**。
- **形状**：点号扁平键名的单层映射，例如 `{"web.port": 8766}`。嵌套对象不合法；写了键表里没有的键名也不合法。
- **优先级**：**环境变量 > `config.json` > 代码默认值**。
- **文件不存在** → 用默认值照常起。
- **`settings_update` 会写这个文件**（原子写：同目录临时文件 + `os.replace`）。它**只写文件里已有的键 + 这次改动的键**，不会把代码里的默认值落盘。
- **配置目录里还住着一份非配置文件**：工作区注册表 `~/.config/meeting/workspaces.json`（`meeting/workspace.py` 读写，登记「我这些会」，按规范化路径去重、原子写）。它**不是配置键**、不在 `settings_update` 的键表里，归 `workspace_open` / `workspace_remove` 与两只读端点（`GET /api/sessions`）管，形状见 `docs/contract.md` 4.8。

### 键与默认值

| 键 | 默认值 | 现在谁在用 |
|---|---|---|
| `web.port` | `8765` | 服务（绑定端口） |
| `web.dist_dir` | `web/dist` | 服务（`/` 挂什么） |
| `log.level` | `INFO` | 日志 |
| `log.dir` | `~/.config/meeting/logs` | 日志 |
| `log.max_bytes` | `5 MiB` | 日志轮转上限 |
| `log.backup_count` | `3` | 日志轮转保留的备份份数 |
| `alert.silence_dbfs` | `-60` | 告警判定（低于此值算这一路在静默） |
| `alert.silence_seconds` | `180` | 告警判定（静默连续多久才起致命级） |
| `alert.recover_seconds` | `10` | 告警判定（连续有信号多久才算恢复） |
| `alert.alarm_suppress_seconds` | `60` | 告警判定（同一事件多久内不重复出声） |
| `alarm.sound` | `true` | 本场出不出声的持久化开关（**热读**：`settings_update` 写盘那一刻就改运行期行为） |
| `heartbeat.interval_seconds` | `10`（实现给的合法范围 `1–60` 秒） | **界面**发心跳的周期，进快照 `settings` |
| `capture.backend` | `auto` | 服务（选取采集后端；`auto` / `null` / `dev` / `module:Class`） |
| `devices.mic` | `null`（= 没配，按默认策略挑） | 服务（麦克风那一路用哪个输入端点；`dca4539b` 定名，写盘成功、**重启后生效**） |
| `devices.loopback` | `null`（= 没配，按默认策略挑） | 服务（loopback 那一路用哪个 render 端点，同上） |
| `transcriber.backend` | `auto` | 服务（选取转写后端；`auto` 解析到 `meeting.backends.transcribe:TranscriberBackend`，`dev` 落转写替身，缺席时按缺省替身） |
| `post.hook` | `auto` | 服务（选取会后钩子；`auto` 解析到 `meeting.backends.post:PostProcessHook`，`null` → 「生成会后精细稿」按钮 404 `action_unavailable`；收尾第 4 步不再拉起它，那一格停在 `waiting`） |
| `post.engine` | `auto` | 会后离线那一层（选取离线引擎；`auto` 解析到 `meeting.offline.engine:MossOfflineEngine`，`null` 在引擎解析时抛错） |
| `post.model` | `OpenMOSS-Team/MOSS-Transcribe-Diarize` | 会后离线那一层（离线引擎加载哪个模型，HF 仓库 id 或本地快照路径） |
| `post.dtype` | `bfloat16` | 会后离线那一层（引擎精度档位，认得的名字见 `docs/backend-protocols.md` 第 3 章） |
| `post.chunk_seconds` | `450`（合法范围 `0–86400`） | 会后离线那一层（每块音频的长度上限秒数，`0` = 关掉分块、整段一次跑；默认值理由见 `docs/backend-protocols.md` 第 3 章） |
| `minutes.hook` | `auto` | 会后纪要那一层（选取纪要钩子；`auto` 解析到 `meeting.backends.minutes:MinutesHook`，`null` → 「直接出纪要」按钮 404 `action_unavailable`，`meeting post` 补纪要那一路安静跳过） |
| `minutes.chunk_lines` | `100`（合法范围 `10–1000`） | 会后纪要那一层（每块行数，切块与行号区间表都用它） |
| `minutes.max_segments` | `8`（合法范围 `1–20`） | 会后纪要那一层（每条 source 的分段上限，契约校验与提示词都用它） |
| `minutes.timeout_seconds` | `14400`（合法范围 `60–86400`） | 会后纪要那一层（一次纪要生成的墙钟上限，默认四小时） |
| `minutes.dsh_url` | `http://127.0.0.1:3080` | 会后纪要那一层（`dsh web` 服务端的地址；拉起之前先探它，**在跑**就把这一场交付给它、终态记 `delivered`，没在跑才落回 headless 子进程。任意非空字符串，本层不校验 URL 形状） |
| `summary.base_url` | `https://api.deepseek.com/v1` | 会中总结那一层（OpenAI 兼容端点的地址；换厂商改这个键） |
| `summary.api_key` | `null`（= 没配，**可空键**） | 会中总结那一层（请求头那一格；也可以走环境变量 `MEETING_SUMMARY_API_KEY` 供一个不进配置文件的来源） |
| `summary.model` | `deepseek-chat` | 会中总结那一层（请求体里那个模型名） |
| `summary.extra_body_json` | `""`（= 不并） | 会中总结那一层（厂商特有字段，一段合法 JSON 对象的文本，原样并进请求体） |
| `summary.deadline_seconds` | `25.0`（合法范围 `1.0–3600.0`） | 会中总结那一层（一次总结的墙钟 deadline） |
| `summary.max_attempts` | `3`（合法范围 `1–10`） | 会中总结那一层（尝试次数上限；真正的刹车是 deadline） |
| `summary.default_window` | `15m`（取值 `5m` / `15m` / `30m` / `full`） | 会中总结那一层（`args.window` 省略时用的那一档） |
| `asr.max_segment_seconds` | `10` | 热读（每段重新读），进快照 `settings.asr` |
| `asr.timeout_seconds` | `20` | 热读；必须大于 `asr.max_segment_seconds` |
| `asr.max_new_tokens` | `400` | 热读 |
| `asr.repeat_max_unit` | `40` | 热读 |
| `asr.repeat_min_coverage` | `0.5` | 热读 |

两个前缀说的两件事：`alert.*` 是**判定阈值**（静默多久算告警、多久算恢复、多久内不重复出声）；`alarm.*` 只管出声。两个「心跳」也不许串线：`heartbeat.interval_seconds` 是**界面**发心跳的周期，服务侧的采集帧心跳窗口是 `max(2.0s, 3 × frame_interval)`。

`asr.*` 五个默认值沿用 V1 实测值（`docs/v1-lineage.md` 第 1 章）；`alarm.sound` 的默认值是 `true`（缺省出声）。四个 `post.*` 键为什么取这些值（块长标度曲线、实时率、精度档的对照情况），见 `docs/asr-backends.md`。

### 环境变量

一共四个，一个不多一个不少。其中三个改配置键的生效值，一个只是开关：

| 变量 | 作用 |
|---|---|
| `MEETING_PORT` | → `web.port` |
| `MEETING_LOG_DIR` | → `log.dir` |
| `MEETING_CAPTURE` | → `capture.backend`（`dev` 就是那个开发替身） |
| `MEETING_NO_BROWSER=1` | 关掉自动打开浏览器（不指向任何配置键） |

**另有一格落在这张表之外**：`MEETING_SUMMARY_API_KEY` → `summary.api_key`（会中总结那一格的密钥，`1987cb20` phase1 加进来）。它走的是与上面四个**同一条**通路（装载时求值、优先级一样），但**不改「哪几个键生效」这件事**，只是给密钥供一个不进配置文件的来源；`meeting/config.py` 的 `ENV_KEYS` 里记着它，测试的子进程环境剔除清单由那张表派生。

**环境变量一律在启动时求值**，改了不会热生效。配置里的热读键是 `asr.*` 那五个**加上 `alarm.sound`**（出声开关要在勾选那一刻生效，见 `docs/contract.md` 第 4.6 章），其余键改了要重启 —— 别把热读范围继续放大，会出现「我改了配置怎么没用」这类假 bug。

### 校验规则

- **值级非法** → 退出码 `2`：类型不对、超出范围、或违反键间规则。报错写清**键名、收到的值、合法范围**，并且在写生效值清单**之前**拦下。例如 `web.port` 不是整数或不在 `0–65535` 内；`asr.timeout_seconds` 必须大于 `asr.max_segment_seconds`。
- **文件级损坏** → 退出码 `3`：`config.json` 存在但读不出来（例如同名目录顶替）、或不是合法 JSON、或顶层不是 JSON 对象。报错写清**文件路径与出错的行**。
- **`settings_update` 的写前校验**（超时必须大于段长、键名必须在三分表里）也走同一条口径，违规回 `invalid_setting`(400)，**校验在写盘之前**。键态的两分与合法键名清单见 `docs/api.md` 第 4 章。

## 5. 日志

- **服务级主 log**：`~/.config/meeting/logs/meeting.log`（受 `log.dir` / `MEETING_LOG_DIR` 控制）。这是「服务挂了去哪儿查」的那一份。
- **轮转**：`RotatingFileHandler(max_bytes=log.max_bytes, backup_count=log.backup_count)` —— 默认 5 MiB 一份、留 3 份。**保留整份旧文件**（V1 那套「超限删前半、留后半」已换掉）。两个数都从配置取，改键即改行为。
- **场次级 log**：`<场次目录>/meeting.log`。`start` 成功、场次目录建好之后挂上（`attach_session_log()`）；收尾第 3 步 `log` 做三件事：**flush 场次 log → 写这一场的结束标记 → 摘掉 handler**（`write_session_end_marker()`）。摘掉之后这一场的消息只走服务级 log。
  - **结束标记是这一场的最后一行**，格式串是 `logging_setup.py` 里的具名常量 `SESSION_END_MARKER`：

    ```
    场次结束：state=<draining|done|failed>；收尾四步：<每步 status 概要>；服务级 log：<路径>
    ```

    场次 id 由格式串里的 `session=%(session)s` 带上（场次级 handler 的 `SessionFilter` 写的就是场次目录名）。
  - **写失败（例如磁盘满）不中断录音**，但如实上报：`drain.steps.log` 记 `failed`、状态里看得见、主 log 里留一条 —— 不许静默。`log` 这一格在任何构建里都有实现，所以它**永远不该出现 `skipped`**。
- **记录器**：名字 `meeting`，`propagate = False`（不污染 torch / transformers 的日志），初始化幂等（重复调用不重复加 handler）。
- **格式**（逐字）：

  ```
  %(asctime)s %(levelname)-7s [%(threadName)s] session=%(session)s %(name)s: %(message)s
  ```

  无场次时 `session=-`；`session` 字段由挂在 handler 上的 `SessionFilter` 注入（服务级 handler 恒 `-`，场次级 handler 是场次目录名）。

**现在会记下来的事**（两个 log 各有各的读者：服务级那一份回答「服务自己干了什么」，场次那一份回答「这一场里发生了什么」）：

- **服务级**：启动与选中的端口 / URL；配置来源、生效值清单与热读键清单；静态挂载与产物缺失回退；端口探测的判定结论；bootstrap 日志被换到哪个新路径；设备探测逐步结果（状态、设备名、detail）；信号接管自检与 `收到 <信号名>，进入关停`；收尾失败档 / 强制打断档退出时打给终端的那两套文案；未捕获异常带堆栈；退出原因与退出码（写常量名，不写裸数字）。
- **场次级**（`start` 到摘掉 handler 之间）：**场次开始与结束**（场次目录、`mode`、本场秒数）、**每一路的健康迁移**、**告警起止与出声 / 抑制**、**收尾每一步的开始 / 结束 / 耗时**、收尾这一趟的 `backlog` / `segment_timeout` / `budget`。
  - **每一路健康迁移**是单独一类事件，行里的四项固定：**是哪一路、从什么值到什么值、原因、阈值**。`health` 的取值域见 `docs/state.md` 第 2 章。行样子：

    ```
    每一路健康迁移：loopback capturing -> lost（原因：这一路起致命级告警；阈值：静默阈值 alert.silence_seconds=1s / silence_dbfs=-60 dBFS）
    ```

  - 健康迁移那行的「阈值：」后面有三种形态，读 log 时照各自的方式取：门限真的参与判定时写实测值（`恢复阈值 alert.recover_seconds=<生效值>s`、`静默阈值 alert.silence_seconds=<生效值>s / silence_dbfs=<生效值> dBFS`）；判据里本来就没有门限数时写一句「无阈值」并说明是哪个判据（设备探测三态、开场 `mode` 这一类）；收尾驱动的几条写「不看阈值」（进收尾、撤回空闲、收尾终态）。**没有可取的数时它不编一个数出来**。
  - 一处按时间顺序的实情：`场次开始：…` 那一行落在场次 log 挂上**之前**，所以它只出现在服务级 log 里；场次 log 里找得到的是同一场的 id、目录与 `场次结束：…`。查一场的完整经过，两个文件一起看。
- **两个 log 之外还有的**：后端解析结果与回退 warning、帧间隔读不到时的回退 warning、`settings_update` 写盘结果与被环境变量压住的 warning、SSE 慢消费者重同步（这几条走服务级那一份）。
- **启动扫描已退场**：启动不读、不改写任何别的场次目录，也就没有「启动扫描」这类 log（原「扫最近一场 → WARNING + 改写提示」随默认输出根一并拆除）。改写与"最近一场"的功能由收编节点落在注册表读写那边（`GET /api/sessions` / `workspace_open` / `workspace_remove`），不归启动序列。

## 6. 退出码

五个常量定义在 `meeting/cli.py` 模块级，取值一起冻结；实现与断言只引用常量名。**五档之外不自造。**

| 常量 | 取值 | 触发条件 | 现在能走到吗 |
|---|---|---|---|
| `EXIT_OK` | `0` | 正常跑完：空闲态按 `Ctrl+C`；收尾四步走到 `done` / `skipped` 之后关停 | 能 |
| `EXIT_FAILURE` | `1` | 收尾有步骤 `failed`；`meeting post` 那一趟没做成（音频转不动、离线那一层 `failed`、纪要落 `failed` 或等它落终态等过了 `minutes.timeout_seconds`）；`meeting web` 起步这一路（组装应用 / 构造服务器对象）出的意外 | 能 |
| `EXIT_REJECTED` | `2` | 四个触发面：**用法错误**（不带参数 / 动作词不在 `web` `post` 里 / `post` 的位置参数不对 / 未定义的开关）、**配置值非法**、**端口被占**（两种情形都算）、**`meeting post` 的幂等闸拒跑**（有状态文件停在 `running`） | 能 |
| `EXIT_STARTUP_UNMET` | `3` | 启动前提不成立：主 log 不可写、`config.json` 读不出来或不是合法 JSON | 能 |
| `EXIT_INTERRUPTED` | `130` | 收尾中途第二次 `Ctrl+C`；已经停在 `ended·draining` 时按下的那一次 | 能 |

**五档全部可达。** 五种关停情形各自落在哪一档，见第 2 章那张表。

退出码的出口一律是 `sys.exit(<常量>)`，异常不许冒到 uvicorn 或解释器那里变成解释器默认码。收尾失败档退出时，终端上会打 log 路径与**服务 log 的最后 20 行**；强制打断档退出时打的是档位、盘上留在哪一格与 log 路径。两套文案都按**现场**说话：现场真有 `failed` 步才说「收尾有步骤失败，服务不自杀」。

`SIGTERM` 不在本服务的接管范围内，本阶段不定义它的退出码。

## 7. 跑测试

**从仓库根跑**（`pythonpath = ["."]` 按 rootdir 解析，工作目录一变 `import meeting` 就不成立）：

```powershell
python -m pytest -q
python -m pytest -q -m "not slow"   # 快档：跳过慢档那十二个文件
python -m pytest -q -m slow         # 慢档：只跑那十二个文件
python -m pytest tests/test_state_machine.py tests/test_api_actions.py -q   # 只跑某几个文件
```

`tests/` 下现在有 **65 个测试文件、1328 个用例**（本机实测：`python -m pytest --collect-only -q` → `1328 tests collected`；`python -m pytest -q` → `1324 passed, 4 skipped in 155.60s`。那 4 条跳过的是 `tests/test_minutes_evidence.py` 里认 `.work` 证据脚本的用例——脚本不在盘上时按设计跳过，`-rs` 能看到路径与补救办法。口径是**pytest 会去收用例的文件数**：`tests/_helpers.py` / `tests/_service.py` / `tests/_drain_fakes.py` / `tests/_capture_faulty.py` / `tests/_dsh_web_fake.py` / `tests/_contract.py` 那几个测试支持模块不算，`tests/v1/test_repeat_filter.py` 算一个）。下面那张逐文件表走到今天这一步，中间隔着几笔增量，分开记：

- **`1987cb20` 那一步：49 行（行内用例数相加 976）→ 57 行 / 1133 条，+8 个文件、+157 条。** 六个 `tests/test_summary_*.py` 111 条（会中总结内核那一层，`1987cb20` phase1 —— `test_summary_client.py` 19 条、`test_summary_parse.py` 19 条、`test_summary_prompt.py` 12 条、`test_summary_runner.py` 23 条、`test_summary_settings.py` 21 条、`test_summary_window.py` 17 条）、`tests/test_summary_action.py` 31 条与 `tests/test_summary_buffer.py` 12 条（服务接线那一层，`1987cb20` phase2，含修复轮补的两条：四档之外的 `window` 取值一律 400、以及出口兜底那条自证），外加 `tests/test_minutes_contract.py` 60 → 63 条（会后纪要契约那几条跟上这一层）。
- **本轮那一步：57 → 58 个文件、1133 → 1147 条，+1 个文件、+14 条**（`796bbff` 与 `1c46c91` 这两笔一起算）。新文件 `tests/test_transcriber_release.py` 11 条（会中模型让出显存那四条语义与收尾接线的顺序），`tests/test_post_hook.py` 14 → 17 条（释放先于 `launch` 的顺序、释放抛错不拖垮整场、真后端确实走这条线）。
- **那张表里的行内用例数相加，与收集数已经对平**：58 行相加就是 `1147`，与 `1147 tests collected` 逐字相等。这一格先前算下来是 1144、差 3，差在 `tests/test_post_hook.py` 那一行的行内数（那一格写过 14，本机实收 17），改表时把那一格一并订正。
- **`65dd3e4c` 那一步：58 → 62 个文件、1147 → 1265 条，+4 个文件、+118 条**（三段一起算：phase1 的 `tests/test_cli_post.py`、phase2 的 `tests/test_post_idempotency.py`、phase3 的 `tests/test_cli_post_config.py`（18 条）与 `tests/test_config_isolation.py`（5 条），外加那个慢档文件 `tests/test_web_missing_dist_e2e.py`（1 条））。快档 1041 条、慢档 224 条，加起来就是 1265。
- **prune 审计第二轮那一步：1265 → 1263 条**（机主点名「检查 UI 文案是否 match 的那类测试完全没必要」之后做的一轮）。整条删掉两个用例函数（`tests/test_minutes_chunk.py` 的 `test_build_index_text_is_pure`、`tests/test_minutes_evidence.py` 的 `test_a_blank_env_var_falls_back_to_the_default`），另在七个存活用例里删掉若干条只把常量／自己刚写进去的值抄一遍的断言（`test_minutes_evidence.py` 的家目录回环、`test_cli_post.py` 的终态回读、`test_post_hook.py` 的线程实参回读、`web/tests/` 三处：恒真的 `frozenClockValue` 自比、恒等的 `formatNumber` 三元式、以及把 `无声明的 `连续 2 次` 抄进断言的整串比对）。**文件数不变**（62），因为两个用例函数所在的文件都还有大量承重内容。同轮把 `muteButtonLabel` 那三条逐字文案断言改写成「两档措辞不同 + 无告警按未静音那一档」的分支比对——它原本是该方法在 `web/tests` 里的唯一覆盖点，直接删会留下覆盖空洞。快档 1039 条、慢档 224 条，加起来就是 1263。
- **会后纪要的 web 分支那一步：62 → 64 个文件、1263 → 1295 条，+2 个文件、+32 条**（两个都快档：`tests/test_minutes_dsh_web.py`（19 条，交付层自己的契约：自签 cookie、存活探针、三步交付与失败分档、重试那一条线）与 `tests/test_minutes_delivered.py`（13 条，`delivered` 这一档的流转与读方那两道闸）。两个文件共用一件新的测试支持件 `tests/_dsh_web_fake.py`（不被 pytest 收集）；`tests/conftest.py` 同时加了第七道 autouse 兜底 `_no_real_dsh_web_call`。快档 1071 条、慢档 224 条，加起来就是 1295。
- **归档 PCM 解码修复那一步：1295 → 1297 条，+2 条**（`tests/test_offline_audio.py` 4 → 6：补两条用例钉住「无头裸 PCM 归档解得开」与「非 `.pcm` 的容器照旧走 PyAV 探测」，后者拿同一段样本的 wav 与裸 PCM 比对到逐样本相等）。修的是 `meeting/offline/audio.py`：`av.open()` 在无头裸流上必然探测失败（`InvalidDataError`），改为按后缀分流、对 `.pcm` 显式给出 `s16le` / 16k / 单声道——分流口径与 `runner.py` 的静音门、切块同一条。此前每一场「有真实音频的那一路」的会后转写都会栽在这里（环回那边靠数字静音门在解码之前被跳过，所以只有麦克风撞得上）。快档 1073 条、慢档 224 条，加起来就是 1297。**此后 `.work` 清理掉，`test_minutes_evidence.py` 里认那份证据脚本的 4 条转入跳过：收集数仍是 1297 条，全量变成 `1293 passed + 4 skipped`、快档变成 `1069 passed + 4 skipped`。**
- **工作台收编那一步（e2f6166b）：+1 个文件、+15 条**（`tests/test_workspace_registry.py` 新增 14 条 —— 注册表的登记 / 去重 / 原子写 / 损坏兜底、`classify_workspace_dir()` 的三档判读、两只读端点的现场形状、`workspace_open` / `workspace_remove` 逐档拒绝、`start` 之后刷新 `last_opened_at` 与 `recent`；另在 `tests/test_contract_fixtures.py` 补 `capture_workspace_group` 1 条，采集 `sessions/` 与收编两份动作样本，样本总数 28 → 32）。全量实测 `1294 passed, 4 skipped`；快慢分档实测：快档 1076 条（`1072 passed, 4 skipped, 222 deselected`）、慢档 222 条、共 1298。
- **会后不自动跑那一步（`05a48b6a`）：64 → 65 个文件、1298 → 1328 条，+1 个文件、+30 条。** 新文件 `tests/test_post_actions.py` 18 条（会后两个按钮：只按离线只跑离线链、只按纪要只拉纪要层、都不按收尾后什么都没起、显式 `dir` 的三档校验与省略语义、在飞 / 收尾未落定的 409、两个 hook 缺席的 404、纪要 claim 闸与线起不来时退回 claim、`GET /api/sessions` 的 `has_offline`）；其余 12 条按「两份稿平级对拍」与「收尾第 4 步停 `waiting`」的口径散在既有文件里增删（切块两份都落盘、提示词清单与说明段逐份写、契约校验的分母跟在场那份稿走、`test_state_machine.py` 钉「`waiting` 只许落在 `post` 那一格」等）。全量实测 `1324 passed, 4 skipped`；快慢分档实测：快档 1106 条（`1102 passed, 4 skipped, 222 deselected in 59.26s`）、慢档 222 条、共 1328。

文件数与用例数的口径写在 `docs/spec.md` 的代码现状表里。不需要先 `pip install`，也不需要可编辑安装。

| 文件 | 用例数 | 验什么 |
|---|---|---|
| `tests/test_config.py` | 63 | 默认值表、三层优先级、环境变量映射、六个热读键（`asr.*` 五个 + `alarm.sound`）、键间校验、配置损坏抛错、值非法 → 退出码 `2` |
| `tests/test_port_policy.py` | 22 | 端口与只监听回环、两种占用文案与退出码 `2`、探针 1 秒总超时、用法错误、`post` 那两个词面、空闲态 `CTRL_BREAK_EVENT` → 退出码 `0` 与两行信号 log、console script 入口面 |
| `tests/test_static.py` | 19 | `/` 的产物与说明页两条分支、**目录存在性按每请求判定**（同一个应用实例里「缺失 → 建出 → 又删掉」三步走完、中间不重启）、`/debug`、`/api/*` 的 `no-store`、Host 校验 `403`、动作头校验 `403`、完整版快照的键集与非空 `service.version`、`revision` 每次状态变更 +1、心跳六字段、空闲态盘上不留任何场次产物 |
| `tests/test_contract_fixtures.py` | 9 | 与 `contract/fixtures/` 的漂移比对：重走真实服务路径采集 32 个样本逐文件比对（默认模式）、掩码结构断言（顶层九键 / 十一键、两路次序、包络长度 15、`levels` 的 `ts is None` 与 `bar == envelope[-1]`、心跳六键、四帧 `revision` 自洽）、`--regen-contract` 的再生成与幂等 |
| `tests/test_logging_setup.py` | 15 | 服务级 log 位置与格式、bootstrap 重建、按两个配置键轮转、幂等、主 log 不可写与配置损坏 → 退出码 `3`、**启动不再读/改写别的场次目录**（扫描退场的三条新判据） |
| `tests/test_api_actions.py` | 30 | 除收编与会后那四个动作外其余七个动作的成功与拒绝路径、`args.dir` 的三档拒绝、`mode` 取值域、R17 的两个 409、`capture_unavailable`、`settings_update` 的两分（6 个热读键 / 32 个要重启的键）与写盘拓扑与 `persist_failed`、`alarm.sound` 当场生效、`muted` 的首发与重算、逐动作 × 逐态可用性、`dev_inject` 的三档可用性、动作名与 `code` 集合逐字冻结 |
| `tests/test_state_machine.py` | 21 | 四态取值域与全部合法迁移、非法迁移抛错且不涨 `revision`、三条铁律、`elapsed_seconds` 走秒与冻结、15 柱 × 2 秒包络、收尾闭环两个分支（成功档第 4 步停在 `waiting`、失败停留档）、**`waiting` 只许落在 `post` 那一格**、取消路径、探针越界取值回退 |
| `tests/test_alerts.py` | 26 | 逐帧累计（用帧时间戳）、连续性判据、抖动不来回告警、抑制窗口与「恢复后算新事件」、**首发那一帧的 `muted` 是 `false`**（且此后同一条不被重新放行、**本场静音期间过一遍标记条目仍是 `true`**）、`muted` 的重算、提示级的合并键与上限 3（**被挤掉的键从记账里回收**）、**两条对称的清除路径**、**新一场开场把提示级两份记账一起清**、阈值只从配置读 |
| `tests/test_dev_backend.py` | 15 | dev 替身的确定性与 100ms 节拍、三档故障的语义与到点恢复、重复注入替换前一次、`finalize()` 只记账、非 dev 后端回 404 |
| `tests/test_sse.py` | 11 | 首帧是 `snapshot` 且与 `GET /api/state` 同源、`state` 是全量且 `id` 对齐 `revision`、`levels` 的 4Hz 与形状、`transcript` 形状、`retry` 与 `: ping`、慢消费者重同步且不拖累别人 |
| `tests/test_heartbeat.py` | 11 | `idle` 下的空形态、`stall` 之后 `recording_alive == false` 而 HTTP 照常 200、窗口计算、两个心跳不串线、`SourceFacts` / `SourceLost` 不刷新帧时钟、帧间隔读不到时按 0.5 回退 |
| `tests/test_session_dir.py` | 13 | 工作区目录三档校验（必给 / 已存在 / 无另一场记录）、start 不建目录与不按日期命名、`session.json` 字段全貌与原子写（无 `.tmp` 残骸）、每个阶段变更都落盘（终态那一版里 `post` 停 `waiting`）、start 写盘失败清场文件并留痕 |
| `tests/test_workspace_registry.py` | 14 | 工作区注册表（e2f6166b：`meeting/workspace.py`）与收编动作：登记奏效且路径去重（含 Windows 大小写并一行）、再登记只刷新 `last_opened_at`、`classify_workspace_dir()` 三档判读与疑似档、注册表原子写且住配置目录、损坏兜底成空表、`workspace_open` / `workspace_remove` 的逐档拒绝与重复删除安全、`GET /api/sessions` / `GET /api/dirs` 的现场形状、`start` 开成一场后刷新 `last_opened_at` 与 `recent`、清单端点不涨 `revision` |
| `tests/test_multi_client.py` | 5 | 两个订阅者拿到同一个 `revision`、revision 序列逐项相同、断开一个不影响另一个与会话、重连首帧是 `snapshot` 且不重放、`/debug` 与产物无关地可达 |
| `tests/test_drain.py` | 32 | 收尾编排这一整块：四步顺序与终态、`done` 判据、`skipped` / `failed` 的边界、预算公式与五个边界档、一趟收尾的总时长随积压量变化且不越过预算、某一步超预算 → 该步 `failed`、四步共享一条 deadline、转写缺席时的缺省输入、`_drain_wait()` 包住每一个调用、`log` 步的最小预算、取消路径的记账、`log` 步与场次 log 的挂摘与结束标记、逐类日志事件可查、失败后服务不自杀且 `start` 回 409 |
| `tests/test_shutdown_signal.py` | 13 | `Ctrl+C` 的两阶段语义（真服务子进程 + `CTRL_BREAK_EVENT`）：空闲态 / 收尾干净走完之后各按一次 → `0` 且盘上不被改写；录音中按一次 → 收尾干净 → `0`；收尾中途第二次 / 已停在 `draining` 再按 / 同一进程连按两次 → `130`；收尾有步骤 `failed` → `1` 且终端打出 log 路径与 `meeting post`；`SIGINT` 与 `SIGBREAK` 共用同一个处理函数；退出码逐档与两套终端文案 |
| `tests/test_e2e_no_clients.py` | 1 | 关掉全部页面之后录音照跑（本条用 dev 真服务起一场），重连首帧是 `snapshot` 且与紧接着一次 `GET /api/state` 逐字段相等 |
| `tests/test_e2e_multi_client.py` | 2 | 两个订阅者同一 `revision`、同一 `muted`；下一场 `start` 之后会话级静音复位、`alarm.sound` 未变 |
| `tests/test_e2e_silence_alert.py` | 1 | 一路静默 → 致命级告警、另一路照常采、录音继续（`session.frozen` 为假）、结束必须由测试 `stop` |
| `tests/test_capture_probe.py` | 32 | 设备层与选择面（枚举与试开用替身）：`probe()` 两路各一条结论与三档取值域、`device_name` 是真端点名、试开真的读回样本、重复探测幂等、`list_input_endpoints()` 给系统默认输入端点打标记（枚举面用替身）、一条端点都挑不出来时 `start()` 抛 `CaptureUnavailable`、没有 `inject_fault`、`auto` 解析到真类、`settings.devices` 与 `reprobe` 动作、起服务那一场用假源走通 `start()` |
| `tests/test_capture_levels.py` | 13 | 电平换算（纯函数）：满量程锚点、10 倍幅度 = 20 dB、`-inf` 兜底成 `-120`、全零窗口落到静默阈值以下、正弦的均方根比峰值低约 3 dB、`bar = max(dbfs, peak)`、三个数都是有限值 |
| `tests/test_capture_archive.py` | 21 | PCM 归档与转码：`open_pcm()` 是追加模式且**无缓冲**（`write()` 一返回就在盘上）、写线程「取一块写一块」、`close()` 幂等且把队列里剩下的块写完、两路不共用队列、非空 PCM 转出 `ffmpeg` 读得回来的 MP3、空文件不产 MP3、转码失败原样抛错且 **PCM 字节不变**、`from_raw` 拿到的形状参数逐字是 V1 那一份、**转码一路一次都不 `unlink`** |
| `tests/test_capture_engine.py` | 27 | 会话引擎（注入假源）：字节数按 32000 字节/秒对账、两路各自长起来、单路读崩只报一条 `SourceLost` 而另一路继续写、死掉那一路的 PCM 可解、`mode` 过滤（未选中的那一路不读不发帧但文件在）、电平帧每 `frame_interval` 秒**无条件**发（静音时发 `-120`）、`SourceFacts` 跟着走、`finalize()` 停流 + join、`stop()` / `finalize()` 幂等 |
| `tests/test_capture_backend.py` | 14 | 真采集后端的起停与收尾（假源注入，一个真声卡入口都不碰）：两路都起来并写出归档、单路起不来不抛错、全起不来才抛 `CaptureUnavailable`（逐路缘由进 `message`）、`mode=mic` 只有 mic 算数、`finalize()` / `transcode()` / `stop()` 与幂等、没有 `inject_fault`，外加两条**端到端**（进程内真运行时 + `capture.backend = auto`）：收尾第 1 步记 `done`、两路各出一份 `.mp3` 且 PCM 留着；一路在运行期死掉时另一路的归档更多、死掉那一路留半份 |
| `tests/test_capture_e2e.py` | 3 | 端到端（进程内真运行时 + `_capture_faulty` 挂件）：一场正常会收尾四步到 `done`、两路归档与假源喂块账**逐字节一致**、样本数对墙钟偏差在一个采集块内、0.5 秒帧序号连续（有声/静音计数在案）、两个 MP3 经 `ffprobe` 时长与 PCM 相符；半路掉一路时另一路归档照常长、收尾照常 `done`；10 分钟合成 PCM 实测 `transcode()` 吞吐并线性外推两小时对 30 秒预算 |
| `tests/test_capture_accounting.py` | 10 | 对账函数本身（纯函数）与它的反例：完整序列全过、中段丢块按样本序号点名、等长换序照抓、奇残字节翻假、尾部不足一帧记 leftover、静音段照计数而不算丢块、空归档不炸、帧重放真的重算电平、缺帧序号点名、墙钟偏差一个块的判据 |
| `tests/test_real_capture_guard.py` | 1 | 「一碰真声卡就炸」那道看门**与导入次序无关**：在一个全新解释器里（库还没被 import）直接调四个真采集入口，逐条必须被拦下，且**每次调用之前先断言目标确实是桩**（桩不在时红在断言上，真调用一次都不发出去） |
| `tests/test_asr_glm.py` | 20 | `meeting/asr/glm.py` 与 `guards.py` 的契约（全程注入假模型，一条都不加载真模型）：四层防护真的挂上了（`no_repeat_ngram_size=8` / `logits_processor=[_LoopGuard]` / `stopping_criteria=StoppingCriteriaList([_DeadlineStop])` 逐条断言）、降级记忆（首次 `TypeError` 去掉该参数重跑一次，开关已是 `False` 时异常照抛）、噪声预筛命中即 `ok=True, reason="noise"` 并省掉那一次生成、**超时与判环都不落部分文本**、拿 V1 那条真实坏行当模型输出喂进去断言砍到 <60 字符、参数一律从 `config.json` 读出、**`import meeting.asr.glm` 在一个全新解释器里一条重依赖都不拉** |
| `tests/test_asr_pipeline.py` | 16 | 实时转写流水线（进程内、无真模型、无真声卡）：每一行的时间戳落在「那段音频写进归档的墙钟窗口」之内、两路各一个 `meeting_transcript_<source>.md` 且每行匹配 `^\[\d{2}:\d{2}\] `、段队列无界（`maxsize == 0`）且源码里没有 busy-drop、三档占位符的形状（**不含 `**`**）、噪声段连占位符都不写、收尾预算 `30 + n × (timeout + 5)`（下限 30 / 封顶 300）与预算耗完时的如实记账 |
| `tests/test_transcriber_backend.py` | 20 | 转写后端的协议实现与**解耦证明**：六个成员齐、模块顶层不 import 重依赖、`auto` 落到真实现（`has_implementation is True`）、`dev` 按 key 分流、三态与两个计数，以及把模型换成「睡很久」与「会抛错」两条之后 —— **采集归档的字节数照涨**、随后到来的行时间戳仍贴近采集时刻、`/api/heartbeat` 仍应答 |
| `tests/test_transcriber_release.py` | 11 | 会中模型**让出显存**的四条语义（没加载过就 no-op、幂等、有界等待到点放手、释放后才 `empty_cache()`）与收尾接线的顺序：释放排在 `launch()` 之前用**列表顺序**断言、替身后端走防御式取法是 no-op、释放抛异常时 `post` 仍落 `done`；无 `slow` 标记，单文件实测 0.30 s |
| `tests/test_transcript_hints.py` | 11 | 提示级接线落在 `_refresh_transcript_snapshot()` 上且**每次都调判定器**（时长跟着刷）、键集合变化提交一次而时长刷新不推 `revision`、上限挤掉也算变化、health 回 `normal` 与积压回落两条清除路径、0.5 秒节拍自己把提示带进快照、**空闲与结束档 `alerts.hints` 恒为空数组**（离开录音 / 告警态就清、下一场从零） |
| `tests/test_summary_window.py` | 17 | 会中总结的缓冲与窗口切片：`seq` / 副本 / 上限 / 清空 / 多线程、按时间戳切且同一 `ts` 的一批整批同进同出、`full` 收全部、锚点取末行 `ts`、装配文本与来源标签 |
| `tests/test_summary_prompt.py` | 12 | 两条消息的形状、七条硬规则逐条在 system 里、三条固定标签在 user 里、缺口那一句怎么讲、窗口有边界的说法、重试追加提示 |
| `tests/test_summary_parse.py` | 19 | 围栏容忍三步的四种样本（含混数字、含空串、`{"bullets": []}`）、三小段归位与「一条都认不出来」那一档 |
| `tests/test_summary_client.py` | 19 | 请求形状（地址 / 两个头 / payload 四格 / `extra_body` 并入 / `content` 取值）、四档错误、两种注入写法、默认走标准库 |
| `tests/test_summary_settings.py` | 21 | 四种配置来源、归一化（空串与 `None` 同解、认不出的窗口落缺省档）、`missing` 点名、七个 `summary.*` 键在配置层四处的登记、环境变量那一格 |
| `tests/test_summary_runner.py` | 23 | 成功 / 空数组 / 空窗口 / 缺键四档收场、解析重试与端点报错当场收工、deadline 是刹车（假钟量）、真钟收敛、每次超时取剩余预算 |
| `tests/test_summary_action.py` | 31 | `summarize` 动作与 `GET /api/summaries`：成功体八格、四档窗口、**四档之外的取值（含空串）一律 `invalid_setting`(400)**、五档失败逐档 `detail` 与判定次序（请求数为 0）、只读（快照九键与 `revision` 不动）、记录只记成功且按先后、`alert` 态可用、总结期间别的请求不被按住、**没注入假件时被 `tests/conftest.py` 的出口兜底拦下** |
| `tests/test_summary_buffer.py` | 12 | 转写行经**生产路径那个 sink** 进缓冲（`bind()` 调两次只装一层）、窗口行数对得上、重新 `bind()` 不清已有行、喂行不涨 `revision`、`start_session()` / `shutdown()` / `_clear_session_summary()` 一处清两笔 |
| `tests/test_offline_audio.py` | 6 | 归档解码：16k / 单声道 / float32 的形状与值域、PCM 与 MP3 两条来源解出同一段波形、**无头裸 PCM 归档（`meeting_audio_<source>.pcm`）解得开且换算仍是 `/32768.0`**、非 `.pcm` 的容器照旧走 PyAV 探测 |
| `tests/test_offline_segment.py` | 23 | 会后切段：30ms 帧、静默 2.0 秒收段、丢掉 <500ms 的尾段、单段上限、逐段区间与时间戳 |
| `tests/test_offline_engine.py` | 29 | 引擎包装：`post.engine`/`post.dtype` 的档位翻译、逐块调用与块内命名空间、失败如实上报 |
| `tests/test_offline_speakers.py` | 12 | 说话人编号：按本路首次出现顺序编 `S01`、跨路限定名不相交、`headcount.class_count` 的口径 |
| `tests/test_offline_artifacts.py` | 45 | 四份产物的字段契约：每路一份会后精细稿与对照表、运行报告、状态文件，字段名与行格式逐条钉住 |
| `tests/test_offline_runner.py` | 55 | 整条链的编排：逐路取归档、数字静音门（单趟与分块两条路）、分块秒数、产物落盘与状态文件终态 |
| `tests/test_post_hook.py` | 17 | 会后钩子：`auto` 解析到 `PostProcessHook`、`launch()` 立即返回（daemon 线程）、缺席档记 `skipped`、空场次目录抛 `ValueError`、能力表三项皆假 |
| `tests/test_cli_post.py` | 52 | `meeting post` 这条入口：位置参数与 `--force` 的用法档、终端文案与产物绝对路径、归档补齐的六档判据（非空才转、空 PCM 不产、比 PCM 新的不重转、转砸了 PCM 一个字节不动、一路砸不拖住另一路）、编排的注入点与 `run_session` 的模块属性调用、纪要那道闸的四档、等待的上界与落定、再入闸在产品路径上的安静跳过 |
| `tests/test_cli_post_config.py` | 18 | `meeting post` 接配置层：`post.engine` / `post.model` / `post.dtype` / `post.chunk_seconds` 与 `minutes.*` 逐个生效（不再退回默认值）、配置损坏与取值非法两档的退出码与终端说法、用法错误优先于配置、按生效 `log.*` 重建服务级日志（主 log 不可写那一档） |
| `tests/test_post_idempotency.py` | 39 | 幂等闸与两条入口一致性：连跑两遍时引擎与启动器的调用次数都为 0、PCM 与 MP3 不动、产物文件名集合相等、两层各缺一条判据的那几档、`running` 拒跑与 `--force` 的放行、`mark_running()` 在 `launch()` 返回之前落盘、两个启动器同时进来只拉起一次 |
| `tests/test_post_actions.py` | 18 | 会后两个按钮（`05a48b6a`，全程假件）：只按「生成会后精细稿」只跑离线链（`minutes=False` 那一趟、纪要一动不动）、只按「直接出纪要」只拉纪要层、**都不按就什么都没起**（第 4 步落 `waiting`）、显式 `dir` 的三档校验（不在注册表 / 目录不在原地 / 盘上还在录或收尾）与省略语义、有一趟在飞 → 409、收尾没落定 → 409、两个 hook 缺席 → 404、纪要 claim 闸同步拒与线起不来时把 claim 退回去、`GET /api/sessions` 每行的 `has_offline` |
| `tests/test_config_isolation.py` | 5 | 第六道兜底自己的用例：真入口读的是重定向后的家目录而不是机主那一份、下一条用例从「没有生效配置」开始、机主家目录被建出 / 改过时守卫当场判红并打出差 |
| `tests/test_web_missing_dist_e2e.py` | 1 | 真子进程端到端（**慢档**）：`web.dist_dir` 指到不存在的路径 → 真起 `meeting web` → `/` 是说明页、`/api/heartbeat` 与 `/api/state` 照常 `200`、那个目录仍不存在、`Ctrl+C` 后退出码 `0` |
| `tests/test_minutes_lines.py` | 11 | 两种行形一起吃（无标签的会中稿、带 `**S01**` 的会后稿、带秒的时间戳）、标题行 / 空行 / 注释行返回 `None`、说话人标签为空时落 `None`、识别失败占位符不被当成说话人、`count_lines()` 的空行口径 |
| `tests/test_minutes_chunk.py` | 23 | 按行切块与区间表：块文件里只有原始行（不加头）、`chunk_NN.md` 的两位数编号、闭区间与 `lines` 三者自洽、重跑先清本源旧块、只动 `chunks/` 里 `chunk_*.md` 这一族、`chunk_lines` 取值域、按名字找两条转写、逐 source 判档与两路合并后的公共档位、**两份稿各切一套落在 `working/<source>/<kind>/`、缺席那份不切（不伪造空块目录）** |
| `tests/test_minutes_contract.py` | 62 | 分段方案的契约校验：必填与可选字段的类型、连续性按 source 各判一遍（跨 source 不判）、段 id 重复、末段与真实行数对齐（**分母跟在场那份稿走：会中稿在按会中稿，只有会后稿按会后稿**）、`total_lines` 两种合法形状与各自的拒收档、`max_segments`、三种能分辨的读盘错误，以及**在全新解释器里 `import meeting.minutes` 一条重依赖都不拉** |
| `tests/test_minutes_prompt.py` | 71 | 提示词渲染：五处占位符缺一处就抛错、`{{PHASE_BODY}}` 可省（两块标记就地替换正文）、对不上号的占位符、路径一律正斜杠且相对场次目录、**清单与说明段逐份稿各写一条（两份都在出两行，谁也不叫"权威"）**、旧的单稿布局做兜底 / 双稿在场时拒收旧索引、逐 source 的档位说明段、模板缺席与空清单不渲染 |
| `tests/test_minutes_launch.py` | 41 | 启动器与产物四条判据：argv 只带短指针（正文落 `working/run_prompt.md`）、子进程环境那三格、`CREATE_NO_WINDOW`、四档失败的名字与消息、日志头三行、附录判据（表头看宽度不看第一格的字面值、只认附录那一节里的行） |
| `tests/test_minutes_evidence.py` | 6 | 那一次实测的证据脚本（`.work/nav/nodes/41465462-…/phase2/evidence/run_evidence.py`）**自己写 `evidence.log`**（stdout 全文、UTF-8）：证据目录与 `MINUTES_EVIDENCE_DIR` 的优先级、脚本缺席时带着路径与补救办法跳过、拒绝多余参数。脚本不在盘上（`.work` 被清理）时这几条**跳过**，跑不到不是红的 |
| `tests/test_minutes_dsh_web.py` | 19 | web 分支的交付层（`minutes/dsh_web.py`）：自签 cookie 的名字按 authority 算且签名能验回来、凭据读不到 / 密钥长度不对当场报错、存活探针只打一次只读的 `session/modelCatalog`（401 / 端口没人听 / 签不出 cookie 三档都交回「没在跑」且说明是实话）、交付三步的次序与信封（`payload.args.request`、会话只带 `workspaceId`、提示词带 `requestId` 与 `queue`）、两份盘上记录、五个失败档各归各的、四小时那个预算不许变成一次 HTTP 调用的超时、连接层抖动重试而 401 / 404 / `ok=false` 一次就交回 |
| `tests/test_minutes_delivered.py` | 13 | `delivered` 这一档的流转：探到 DSH web 服务端在跑就走交付（终态 `delivered`、`branch=web`、**不校验产物**，交付那一刻盘上已经是 `running`）、没在跑 / 探针炸了都落回 headless、交付失败如实记 `failed` 不改走另一条路、子会话闸连探针都不问、状态读得回、读方按产物判完成而按 `delivered` 判「已经交付过」（`_minutes_gate()` 与 `_announce_minutes()` 都不拉起第二个会话）、退出码那一档按成功算 |
| `tests/test_minutes_runner.py` | 32 | 一次运行的编排：再入闸当场返回 `skipped` 且不写状态文件、拉起之前先落 `running`、一条输入都没有时记 `failed` 且不启动 DSH、**两份稿各切一套各落各的目录（缺席那份不伪造）**、切块与渲染失败各记 `failed`、成功档覆盖成 `done`、超时与退出码非 0 的转发（启动器全程是假实现，一条都不真调系统上的 `dsh`） |
| `tests/test_minutes_registry.py` | 11 | `resolve_minutes_hook()`：`auto` 落 `MinutesHook`、配置里写 JSON `null` 落缺席档（不退回 `auto`）、写字符串 `"null"` 同解、`module:Class` 显式取值、解析失败回退替身且 `has_implementation = false` |
| `tests/test_capture_devices.py` | 33 | 设备模型与挑选策略（纯逻辑、注入假端点表）：有波形 / 全零 / 开不了三档、`CABLE Output` 抓取型端点挡拦（含 WDM-KS 的 `VB-Audio Point` 写法）、host API 排序、MME 截断名的前缀兜底与**精确名优先**、收尾两档收敛（一条都没打开 → `probe_failed`）、没配名字时种子取**系统默认输入端点**（跟着 `is_default` 走）、loopback 三级（两个子串判据逐字照 V1 的 `in`） |
| `tests/v1/test_repeat_filter.py` | 10 | V1 那套重复循环过滤的回归（`tests/v1/` 是**整份副本**，`fixtures_real_loops.py` 逐字照抄、本文件只改 import）：两条真实故障行（周期 7 与 5）必须被砍到 <60 字符且只留一组、循环之前的前导正文保留、周期上限调回 4 就立刻红、正常行与合法口语重复一行都不许动、token 级判环与「判环后把 EOS 抬到最高分」 |

除这些，`tests/` 下还有**测试支持**（不被 `pytest` 收集）：`tests/_helpers.py`（真服务子进程与假浏览器等工具）、`tests/_service.py`（真服务子进程驱动件 + SSE 读帧与等待助手）、`tests/_drain_fakes.py`（收尾与关停用例共用的假后端 / 假钩子，与工厂辅助一起挂进子进程）、`tests/_capture_faulty.py`（端到端专用的「问题采集后端」挂件：真采集后端 + 假端点表 + 内容带块号的假源，旋钮走环境变量，经配置键 `capture.backend = "tests._capture_faulty:FaultyCaptureBackend"` 挂进进程内真运行时）、`tests/_dsh_web_fake.py`（假 DSH web 端点：一件真 HTTP 服务起在回环的临时端口上，照抄本层用到的那三条端点的形状、逐条记下请求，`session/modelCatalog` 那个只读探针也在登记表里）、`tests/_contract.py`（`contract/fixtures/` 的采集、掩码与落盘比对；它按需写出一个假转写后端，经 `PYTHONPATH` + `transcriber.backend` 的 `module:Class` 挂进子进程）、`tests/__init__.py`（`tests/` 的包标记，跨文件取用走包路径 import）、`tests/conftest.py`（**只放 fixture**，其中**七道** autouse 的兜底：一道把 `webbrowser.open` 换成只记录不开窗的假替身，一道 `_no_real_audio_capture` 把真采集入口打成「一碰就炸」的桩 —— `sounddevice.InputStream` / `rec` / `check_input_settings` / `soundcard.get_microphone`，异常类 `RealCaptureForbidden` 不继承 `Exception` 因此不会被生产代码的 `except Exception` 吞掉，只读枚举不禁；第三道 `_no_real_minutes_session` 把 `MEETING_MINUTES_CHILD=1` 立起来，让离线链收尾处那次 `minutes.hook` 通知当场走再入闸 —— 本机 PATH 上真的有 `dsh`，不立这一格，每一场跑完整条离线链的用例都会真拉起一个 headless 子会话去跑五阶段；第四道 `_no_real_model_call` 盯**会中总结那条 HTTP 出口**（`meeting/summary/client.py` 的 `_http_post()`）：`urllib.request.urlopen` 按目标地址判，本机回环放行、其余当场抛 `RealModelCallForbidden` —— `summary.base_url` 的配置默认值就是真地址，忘了注入假客户端就会真花机主的 token；**第五道是 PATH 守卫**（会话级的 `_no_real_dsh_on_path` 与用例级的 `_no_real_dsh_launch` 两条夹具）：在临时目录里造一件假的 `dsh.cmd`（只往账本追加一行）并前置到测试进程 `PATH` 的最前面，于是漏网的用例命中的是它、真会话起不来，用例级那一条再拿账本把「这一趟真的叫了它」当场判红（要故意走到可执行文件解析那一步的用例，显式请 opt-out 夹具 `allow_real_dsh_resolution`）；**第六道 `_no_writes_to_the_real_home`** 开工记下机主真实家目录下那个配置目录（`~/.config/meeting`）的文件集合与逐个 sha256，每一条用例收工与整场收场各复查一次，被建出或改过就当场判红并打出差（含收工时那份文件的内容）；**第七道 `_no_real_dsh_web_call`** 把 `meeting/minutes/dsh_web.py` 唯一那个 HTTP 出口 `_post_json()` 包起来 —— 打向用例自己起的那些假端点（本机回环、端口由系统挑）原样放行，打向 3080 那台真服务的一律当场抛 `RealDshWebCallForbidden`：`minutes.dsh_url` 的配置默认值就是那个真地址，少了这道网，一条忘了注入假件的用例会在机主开着 DSH GUI 时往那台服务上建真工作区、真会话、发真提示词；第四道 `_no_real_model_call` 盯**会中总结那条 HTTP 出口**（`meeting/summary/client.py` 的 `_http_post()`）：`urllib.request.urlopen` 按目标地址判，本机回环放行、其余当场抛 `RealModelCallForbidden` —— `summary.base_url` 的配置默认值就是真地址，忘了注入假客户端就会真花机主的 token；另注册 `--regen-contract` 开关）、`tests/_fake_webbrowser/`（替掉标准库 `webbrowser` 的假模块）、`tests/v1/`（V1 那套回归的整份副本：`fixtures_real_loops.py` 逐字照抄，`test_repeat_filter.py` 只改 import，那两条真实故障行因此不依赖会议目录就能复现）。

**契约 samples 的重生成开关**：`python -m pytest tests/test_contract_fixtures.py --regen-contract -q` 重写 `contract/fixtures/`，不给开关就是逐文件比对（改了服务行为就重跑它并复核差异）。规则与出处见 `contract/README.md`。

**每一条用例都有一道自动看门**：`tests/conftest.py` 汇总 pytest 自己报的 setup / call / teardown 三段耗时，**合计超过 5 秒就把这条用例判红**（红在它自己的名字下，报出三段各自的耗时）。看门只读 pytest 的 report，不额外计时、不加 fixture。

这条红线牵着测试侧的等待窗口：静默阈值、收尾那一步的阻塞时长、端到端的观察窗口都按它收口过（例：一条用例的静默阈值取 1 秒、把某一步钉在 `running` 的阻塞取 0.6 秒、端到端的观察窗口取 1 秒）。**被测的语义没有跟着改** —— 收尾预算仍是 30 秒起步、告警默认阈值仍是 180 秒，改的只是测试自己等多久。

测试的隔离手段：**重定向家目录**（`HOME` / `USERPROFILE` 都指向临时目录），配置与日志因此整套换落点；**不新增 `MEETING_*` 变量**。真服务一律用 `[sys.executable, "-m", "meeting", "web"]` 起子进程，地址从 `MEETING_PORT=0` 加子进程打印的 URL 行解析出来（**不猜端口**）。采集后端由用例自己的 `config.json` 的 `capture.backend` 表达，子进程 env 里不塞 `MEETING_CAPTURE`（它的优先级更高，会静默压住那条配置键）。ASGI 层用 `with TestClient(app, base_url="http://127.0.0.1") as c:`（`base_url` 把回环 `Host` 带上，只有 `with` 会触发应用的生命周期钩子）。

### 7.1 快慢分档（`slow` 标记）

全量一趟约 **156 秒**，其中九成时间花在真子进程与真墙钟等待上。因此按**文件粒度**分成两档，
标记名 `slow`，注册在 `pyproject.toml` 的 `[tool.pytest.ini_options]`：

```toml
markers = [
    "slow: 需要真子进程或真实墙钟等待；未改动核心代码时可跳过",
]
addopts = "--strict-markers"
```

`--strict-markers` 是有意加的：`slow` 标记名必须注册，改个名字又忘了同步注册时 pytest 会对
标记本身报 unknown mark。命令行的 `-m` 表达式另有 pytest 自己的兜底 —— `-m sloww` 选不中
任何用例，以退出码 **5（no tests collected）** 收场，不会被读成绿。

**一条已知的残余**（本机 pytest 9.0.3 实测）：`-m "not sloww"` 这种**否定式**拼错会让
pytest 把「sloww」当成一个谁都不带的标记，表达式于是恒真、1328 条全被选上 —— 结果是悄悄跑了
全量（慢，但不假绿）。正向拼错由退出码 5 兜住，否定式拼错由这道 `--strict-markers` 兜不住。

**归位判据**（一条用例满足任一条就进慢档）：

1. 起**真服务子进程**（`[sys.executable, "-m", "meeting", "web"]`）；
2. 依赖**真实墙钟等待**才能得出结论（收尾预算、100ms 节拍、抑制窗口、恢复窗口）；
3. 注入**「慢模型」**（睡很久 / 会抛错）来证明解耦或存活；
4. 单条用例实测超过 **2.0 秒**。

**只能落在文件粒度**：`tests/conftest.py` 那道看门（`CASE_WALL_CLOCK_LIMIT_SECONDS = 5.0`）管的是
**逐条用例**的 `setup + call + teardown` 之和压在 5 秒之内，最慢的一条也才
3.40 秒（`tests/test_drain.py::test_all_log_event_categories_findable_before_end_marker`），
「慢」来自文件层面的真子进程与真墙钟等待。**慢档一共十二个文件**，各自在文件头加一行
`pytestmark = pytest.mark.slow`（判据是 `Select-String -Path tests\test_*.py -Pattern 'pytestmark = pytest\.mark\.slow'`，实测 12 个；**路径要写成 `tests\test_*.py`**：不加这一层的时候 `tests\conftest.py` 的文档串里同名的那句话也会被数进来、读出 13 个。`-m "not slow"` 与 `-m slow` 两条收集口径分别是 `1106/1328` 与 `222/1328`，加得起来）：

| 慢档文件 | 单文件实测 | 慢在哪一档判据 |
|---|---|---|
| `tests/test_drain.py` | 15.56 s | ② 真墙钟等待 + 预算边界 |
| `tests/test_dev_backend.py` | 15.18 s | ② 100ms 真节拍 × 三档故障窗口 |
| `tests/test_transcriber_backend.py` | 12.36 s | ③ 「睡很久」的模型 |
| `tests/test_shutdown_signal.py` | 8.70 s | ① 真服务子进程 + `CTRL_BREAK_EVENT` |
| `tests/test_asr_pipeline.py` | 7.89 s | ② 真归档 + 真线程 + 收尾预算 |
| `tests/test_port_policy.py` | 7.73 s | ① 真服务子进程 + 端口探针 |
| `tests/test_capture_engine.py` | 6.71 s | ② 真读写线程 + 打点线程 |
| `tests/test_api_actions.py` | 6.65 s | ② 进程内真运行时（30 条各起一次真应用 + lifespan） |
| `tests/test_summary_action.py` | 5.99 s | ② 进程内真运行时（31 条里 30 条各起一次真应用 + lifespan；单条最长 1.22 s） |
| `tests/test_capture_e2e.py` | 4.86 s | ① 真运行时 + `ffprobe` + 10 分钟合成 PCM |
| `tests/test_web_missing_dist_e2e.py` | 3.49 s | ① 真服务子进程 + 真墙钟（`Ctrl+C` 之后等它退出） |
| `tests/test_summary_buffer.py` | 1.40 s | ② 进程内真运行时 |

**`tests/test_transcript_hints.py` 不在这张表里**，它也没有 `slow` 标记（实测：这个文件里搜不到 `pytestmark`，它 11 条用例全在快档里跑，单文件 1.84 s）——它满足判据 ②，但那一条端到端用例只等 0.5 秒节拍，落在快档不拖累谁。**碰了转写提示级那一路时仍然要跑它**（`python -m pytest tests/test_transcript_hints.py -q`），别因为「表里没有」就跳过。

上表那些单文件读数是各自单独跑一次量的（含解释器冷启动与真应用启动的固定开销），**不随某个文件被挪进挪出慢档而变**；改完代码之后要刷新哪几个数，就按同一口径单跑一遍那个文件。

**三条命令与实测耗时**（本机，收集数 `1328` 条；`05a48b6a` 收尾重测的那一版 —— 快档与全量是那一趟的实测，慢档那格的单跑读数沿用上一轮，本轮未单独重跑）：

| 命令 | 用例 | 实测 |
|---|---|---|
| `python -m pytest -q -m "not slow"` | **1102 passed, 4 skipped, 222 deselected** | **59.26 s** |
| `python -m pytest -q -m slow` | **222 passed, 1106 deselected** | 上轮单跑 89.83 s（本轮未重测） |
| `python -m pytest -q` | **1324 passed, 4 skipped** | **155.60 s** |

**不加 `-m` 时跑全量**，这一点是有意的：新写的用例默认落在快档，只有被显式标 `slow` 才跳过。
漏跑的风险因此压在「新写的慢用例拖慢快档」这一侧，而不在「忘了标记 → 慢用例从没跑过」那一侧。
快档那 59 秒里包含了全部承重的离线契约用例：`test_offline_artifacts.py`（45 条）、
`test_offline_engine.py`（29 条）、`test_offline_runner.py`（55 条）、`test_offline_segment.py`
（23 条）、`test_offline_speakers.py`（12 条）、`test_offline_audio.py`（6 条）、
`test_post_hook.py`（17 条）合计 187 条，在共享运行里约 1.9 秒；`test_contract_fixtures.py`
的 32 个 golden 样本漂移比对也在快档。

**将来某个快档文件里出现一条真子进程用例**：那一条单独加 `@pytest.mark.slow` 即可，不必把
整个文件挪进慢档。

## 8. 现在能做什么 / 还接不上的部分

**能**：起停、跑完启动序列、交 `/`（产物或说明页）、**接通了的 `/debug` 自检页**、`/api/state` 的完整快照、`/api/events` 的三通道 SSE、`/api/heartbeat` 的真值、`POST /api/actions` 的十一个动作、`GET /api/summaries` 的本场总结记录、**工作台收编（e2f6166b）**：工作区注册表（`meeting/workspace.py`，住配置目录 `workspaces.json`）、`workspace_open` / `workspace_remove` 两动作与 `GET /api/sessions` / `GET /api/dirs` 两只纯读端点、`start` 开成一场后刷新注册表的 `last_opened_at` 与 `recent`、空闲态的工作区首页（`web/src/home/`，左侧清单 + 目录选择器 + 非空收编 / 删除确认弹窗）、四态与全部迁移、场次目录与原子写 `session.json`、`stop` 的四步收尾跑到终态（含自适应预算、超时如实上报、失败后两条出路）、`Ctrl+C` 的两阶段语义与五档退出码、场次 log 的挂上与摘下、告警判定与静音、`settings_update` 的原子写、**设备枚举与试开（`devices.mic` / `devices.loopback` 两个键位与 `reprobe` 动作）**、**真实双路采集与归档**（`capture.backend` 默认值 `auto`：两路各自独立读写、边采边落盘 16k / 单声道 / int16 裸 PCM、每 0.5 秒一帧 dBFS 电平事实、收尾 `finalize()` 停流落盘 + `transcode()` 出 64k MP3）、**真实转写与会中临时稿**（`transcriber.backend` 默认值 `auto`：两路各一本 `meeting_transcript_<source>.md`、一行 `[HH:MM] 正文`）、**会后离线转写与说话人聚类**（`post.hook` 默认值 `auto`，由「生成会后精细稿」按钮按需拉起：两路各一份 `meeting_offline_transcript_<source>.md` 与 `meeting_offline_speakers_<source>.json`，外加一份运行报告与一份状态文件，都落进场次目录）、**会后纪要生成**（`minutes.hook` 默认值 `auto`，由「直接出纪要」按钮按需拉起：拉起一个 headless DSH 子会话按五个阶段出一份 `meeting_final.md`，正文之外带说话人附录）、dev 替身（采集合成电平与三档故障注入 / 转写合成行，按 key 分流）、仓库根的简版 `README.md`、**契约看守**（`python -m pytest tests/test_contract_fixtures.py -q` 比对 `contract/fixtures/` 的 32 个 golden 样本，`--regen-contract` 重新生成；同一批样本在 `web/tests/` 里由 `npx vitest run` 再验一遍类型层）。

**下面这几条是已经接通的细节；其余按「能力还没进来」理解，服务本身照常跑**：

- **真实采集已经接通**（`dca4539b`）：`capture.backend` 的默认值 `auto` 解析到 `meeting.backends.capture:CaptureBackend`，它做三件事 —— 枚举端点并以 16k 试开、读回样本（两路各一条 `ready` / `probe_failed`）；按 `mode` 起两路**各自独立**的采集（一路出事另一路照跑，起不来的那一路发一条 `SourceLost`，本场该采的路全起不来才回 `503 capture_unavailable`）；把 16k / 单声道 / int16 的裸 PCM 边采边写进场次目录的 `meeting_audio_<source>.pcm`，每 0.5 秒往服务报一帧 dBFS 电平事实。收尾第 1 步 `finalize()` 停流 + join + 关文件 + `fsync`，第 2 步 `transcode()` 把两份 PCM 转成 `meeting_audio_<source>.mp3`（`pydub` + `ffmpeg`，64k）。**转码成功之后 PCM 照样留着**（`session.json` 的 `sources[*].archive` 声明的是它）。
- **真实转写已经接通**（`9e5db89c`）：`transcriber.backend` 的默认值 `auto` 解析到 `meeting.backends.transcribe:TranscriberBackend`，它把 `meeting/asr/pipeline.py` 那条流水线包成协议实现 —— 两路各一条读线程**游标尾读**场次目录里的 `meeting_audio_<source>.pcm`（只看新字节、不碰采集层的对象）→ VAD 切段（16k / 单声道 / int16，帧长 30ms、静默 2.0 秒出段、单段硬上限 `asr.max_segment_seconds`）→ **入队即盖采集时刻** → **一个** worker 顺序识别（`meeting/asr/glm.py` 的 GLM-ASR-Nano，带四层退化防护：token 级判环、墙钟超时、噪声预筛、文本级剥离）→ 每路一本会中临时稿 + 一条 SSE `transcript` 行。落盘格式与占位符见 `docs/state.md` 第 3 章。**转写坏了不影响录音**：起步失败只落 `health = stopped`，采集、电平、告警照跑；`shutdown()` 与换场都会同步等一次它的收尾（上界见第 2 章那段）。
- 上面这条流水线在本机实测过的几个数（口径逐一写在括号里）：
  - **双跑对照**（V1 那场事故会议的 GLM-ASR-Nano 素材、10 秒段）：关掉防护裸跑，三个真实触发点（`P4-vac-1836` 的 seg3 / seg7、`C1-A-normal` 的 seg23）分别产出 **2670 / 3996 / 2021 字**、耗时 **36.1–36.9 秒**、顶满 2000 token、循环覆盖率 0.977–0.988；四层防护全开之后同样三段落到 **87 / 85 / 59 字**、**0.75–1.09 秒**。裸跑那三段与 V1 当年记录的三行**逐字节相同**（sha256 前 16 位三对相同）。
  - **文本级过滤器**在事故会议全 **411 条**正文上命中 **2 行**、误杀 **0 行**（那两行的真实循环覆盖率 0.9954 / 0.9931，第 3 名按只算可剥离行是 0.1130 —— 与 V1 记录值一致）。
  - **实时率**：本机 10 秒段单段耗时中位 **0.58 秒**、RTF 中位 **0.058×**（这一档是 10 秒段的口径；V1 记录的 `0.39×` 出自**生产路径探针** —— 整场重放、10 秒段正常识别 3.1–4.2 秒，两个数口径不同，不能直接相减）。
  - **时间戳**：把 530 秒素材按 1× 真实时间轴喂进整条流水线，**53 行全部配对**，偏差中位 **0.0 分钟**、最大 **1.0 分钟**；压力档（积压峰值 29–30 段、排队延迟最大 80 秒）偏差仍是中位 0.0 / 最大 1.0 —— **滞后不随积压增长**。
- **会后那一套现在由人按**（`05a48b6a`，R26 / `docs/design-intent.md` 第 10 条）：按「结束录音」之后**一个会后动作都不自动跑** —— 收尾第 4 步 `post` 只做一件事（让会中模型退出显存），做完就如实停在 `waiting`（＝界面上那句"等你动手"），`ended.phase` 仍判 `done`（前三步落定、无失败步）。会后两个按钮是 `POST /api/actions` 的 `post_offline`（**生成会后精细稿**：只跑离线那一条链，`run_session(..., minutes=False)`，纪要一动不动）与 `post_minutes`（**直接出纪要**：把这一场手上有的稿子交给纪要那一层，**不补跑离线**）。两条各自起一条 daemon 线程、起完即回，跑成什么样由场次目录里的状态文件回答。`post.hook` / `minutes.hook` 仍是那两个实现的解析键（`auto` / `null` / `dev` / `module:Class`）：解析不出实现时对应的按钮当场回 404 `action_unavailable`。产物（每路一份会后稿与对照表、一份运行报告、一份状态文件）见 `docs/state.md` 第 3 章。收尾第 2 步的闸门只看**采集侧**（`meeting/service/runtime.py` 的 `_drain_transcode()` 看 `capture_resolution` 与 `capture_capabilities["transcode"]`）：transcode 是采集后端的能力，转写后端在不在场与它无关 —— 真采集那一场两份 MP3 在收尾里自动出现，`null` / `dev` 替身那一场这一格记 `skipped`。
- **会后纪要那一层**（`41465462`；由「直接出纪要」按钮（`post_minutes`）或 `meeting post` 按需拉起，见下文「会后由谁拉起」）：`minutes.hook` 的默认值 `auto` 解析到 `meeting.backends.minutes:MinutesHook`，它起一条 daemon 线程、返回即算启动成功，跑多久都不占收尾预算。这一层炸了不带走进度更早的产物（`meeting_offline_status.json` 与那三份离线产物一个字节都不改，只记主 log 一条）；`minutes.hook = null` 与环境里的 `MEETING_MINUTES_CHILD=1`（DSH 子会话自己）两档都安静跳过。产物是场次目录根的 `meeting_final.md`（纪要正文 + 说话人附录）与 `meeting_minutes_status.json`（`running` / `done` / `failed` / `delivered` + 档位与实际走的启动分支），中间产物在 `working/` 下。**收尾四步没有增加第五步**。它吃什么、经哪几步出一份纪要、产物四条判据与三个手动子命令（`python -m meeting.minutes check` / `chunk` / `prompt`）见 `docs/minutes.md`。
- **纪要这一层的两条分支**：拉起之前先探一次 `minutes.dsh_url` 指的那台 DSH web 服务端（`meeting/minutes/dsh_web.py` 的 `probe()`：先 TCP，再发一次只读、无参数的 RPC 确认协议与鉴权）。**在跑**走 web 分支：经 `/api` 三步（`workspace/create` → `session/create`（只带 `workspaceId`）→ `session/prompt` 送提示词全文）把这一场交付给服务端，**投递 accepted 即算交付完成**，状态文件的终态记 `delivered`；**没在跑**才落回本进程自己起的 headless 子进程（等它退出并按四条判据校验产物，终态 `done` / `failed`）。V1 那套 web 路径在 DSH 0.1.5-rc.2 上的几处实测口径（端点写斜杠形式、一元方法 `workspace.list` 不存在、信封要包一层 `args`、`/api` 只认 HMAC 签名的 Cookie）这一层都按实测绕过去了，坑的实测口径写在 `docs/minutes.md` 第 4 节；`branch` 那一格记的是**实际走的**那一条（拉起方建线程之前落的那一版 `running` 记配置里写的那个词）。
- **会中总结也已经接通**（`1987cb20`）：动作 `summarize` 把内存里那份实时临时稿按行首时间戳切一段窗口（四档 `5m` / `15m` / `30m` / `full`，缺省 `15m`）交给一个 OpenAI 兼容端点，回三小段（刚才在讲什么 / 讲到哪儿了 / 丢掉了什么），十到十五秒出结果。结果走**应答体**（一过性，不进快照、不落文件），本场按过的那几次由 `GET /api/summaries` 从内存里读回来。端点由七个 `summary.*` 键配（都归 `pending_restart`，密钥那一格可以走 `MEETING_SUMMARY_API_KEY`）；没配齐就当场回 `502 summary_failed` 并点名缺哪几个键，转写没在跑则回 `503 transcript_unavailable`。这一层怎么切窗口、提示词与模型输出契约、deadline 与重试的预算见 `docs/summary.md`。
- **端口被占时那句文案里的「已录多久」**：占用者是本服务时，括号里眼下是**字面写死的「当前空闲」**（`meeting/service/app.py` 的端口探测那一处还没取用会话时长这个值）。`README.md` 与 `docs/api.md` 都照这个现状写。
- **真机真开一场会**：设备枚举与真实电平只有真开一场才见分晓，这一条是**手工验收**，四条端到端脚本与它无关。自动化边界归下游 `46ae4a08`。
- **`meeting post` 已经接通**（`65dd3e4c`）：`meeting post <场次目录> [--force]` 只跑会后那一趟，不开界面、不起服务。一场跑下来做三件事，各层的结论与产物的绝对路径都打到终端：

  - **音频归档补齐**（`sync_archive()`）：两路各自独立判、独立转，判据与收尾第 2 步一致 —— 该路 PCM 非空且（MP3 缺席 或 MP3 比 PCM 旧）才转，空 PCM 不产 MP3、已比 PCM 新的 MP3 不重转；转码失败如实记 `failed`、**PCM 一个字节都不动**；转码实现只有一份（`meeting/capture/archive.py` 的 `convert_to_mp3`）。
  - **会后离线那一趟**（`meeting/offline/runner.py::run_session()`）：产物与状态文件的终态见 `docs/state.md` 第 3 章。
  - **等纪要那一层落终态**：纪要挂在离线链的尾巴上（那条入口起线程就返回），`meeting post` 拉起之后要等它落定才报结论 —— 上界取现成的 `minutes.timeout_seconds`（默认四小时），事件驱动的读盘等待，落定即返回。**拉起 ≠ 落定**：到上界还没落定时如实报 `timeout`（既不谎报成功也不写成 `failed`）。

  **幂等闸落在花钱之前**：两层各有「已完成」判据（状态文件终态 + 产物齐，缺一条就不算完成），据此分四档 —— 两层都到终态且音频已归档 → 打一句「无待办」、一个引擎调用与一个子进程都不产生；只差一层 → 只补那一层；任一层停在 `running` → 拒跑（`EXIT_REJECTED`，点名状态文件 + 给出 `--force`）；`--force` → 两层都重跑。
- **会后由谁拉起**：收尾第 4 步什么都不拉起（那一格停在 `waiting`）；「生成会后精细稿」（`post_offline`）只跑离线那条链（`run_session(..., minutes=False)`），「直接出纪要」（`post_minutes`）只拉起纪要那一层；`meeting post` 仍是命令行那两条路，调 `meeting/cli.py` 的 `run_post()`。
- **反复重跑不再拉起第二个子会话**：纪要那一层的启动器在建线程**之前**先把「这一层在飞」落到盘上（`meeting/minutes/status.py` 的 `mark_running()`，那一版 `state = "running"` 在 `launch()` 返回之前就落定），盘上已经停着 `running` 时当场抛 `AlreadyRunningError`、这一趟不起线程；离线那一趟的尾巴也会先过一道闸（`_minutes_gate()`）：纪要已是 `done` 且 `meeting_final.md` 过得了那四条判据时**不调启动器**。`--force` 是唯一会再次花钱的开关。
- **前端**：`web/` 已经落地（React + TypeScript 严格模式 + Vite 6 + vitest 2 + zod），产物落 `web/dist`。**会中主视图已经接上**（`070b31e0` phase2：状态胶囊与时长、两路电平表、每路健康、告警区（致命横幅 + 提示级）、心跳四级本地态与「采集已停」处置、实时转写区、告警出声开关；`1987cb20` phase3 接上总结区：四档窗口 + 主按钮 + 进行中那一句 + 结果 / 失败面板 + 本场总结记录回看；数据全部来自服务推送，界面不造派生数据）。还没进来的：设置抽屉、转写历史回看（契约章 7 的保留登记，界面上只有位置与禁用态）。**工作区首页已经接上**（e2f6166b：`web/src/home/` 的 `WorkspaceHome` —— 左侧清单读 `GET /api/sessions`、空态 logo、目录选择器弹窗（起点 `~/`、非空父目录「进这一层」）、非空收编与删除确认弹窗、Windows 大小写并一行、选中那一场详情里的**会后两个按钮**（`post_offline` / `post_minutes`，按钮上那一格按 `has_offline` 渲染）。`HomeGate` 的分流判据在 `home/gate.ts`：`idle` 且没有场次、或 `ended` 且收尾四步都落定（`post` 停 `waiting` 算落定）→ 工作台；其余给会中主视图 —— 于是收尾落定那一刻页面自己回到工作台，会后按钮就在那一场的详情里）。工程自己的说明在 `web/README.md`（视图结构、`src/live/` 各管什么、两份样式的来源与抽取口径、两条铁律），这里只点两条链路：
  - **改界面**：`cd web; npm run dev`，vite 起在 5173、`/api` 转发到 `http://127.0.0.1:${MEETING_PORT ?? 8765}`（服务照常跑着，改文案走 HMR，不必刷新）。
  - **开会**：`cd web; npm run build`（内部先跑 `npm run typecheck`）产出 `web/dist`，再 `meeting web` **由服务直接发这份静态产物** —— 从服务端口（如 `http://127.0.0.1:8765`）打开就是真界面，**运行时不需要 Node**。
  - 自检是 `npm test`（vitest run，14 个测试文件 360 个用例）与 `npm run typecheck`。
  - 产物缺失时 `/` 给说明页、`/api/*` 与 `/debug` 照常，产物建好刷新即可见（见第 3 章那张表）。
- **`settings.devices`**：两个子键 `mic` / `loopback`（`dca4539b` 定名），取值是设备名或 `null`（没配）。设备挑选在 `meeting/capture/devices.py`（骨架搬自 V1 `mnotes_core/recorder.py` 的 `_resolve_mic_device` / `_resolve_loopback_speaker`，多加一步：试开时读回一小段样本判有没有波形）；真实枚举、试开与持续采集在 `meeting/capture/sources.py`，会话引擎（两路读侧 / 写侧 / 0.5 秒电平刻度）在 `meeting/capture/engine.py`。**配置键是「用户配的意图」，快照 `sources[*].device_name` 是「探测实际落到的那个设备」**：配了却没命中时按「精确名 → CABLE Input（不含 16ch）→ 系统默认输出」这条回退顺序走，主 log 一行 `WARNING`，不报错（契约陈述见 `docs/contract.md` 第 2 章 `settings.devices` 那一段）。

## 9. 相关文档

- 需求锚：`docs/requirements.md`（R1 / R2 / R5 / R15 / R16 / R17）
- 状态、场次与推送：`docs/state.md`
- 会后纪要生成那一层：`docs/minutes.md`
- 会中总结那一层（窗口、提示词、七个配置键、动作与记录端点）：`docs/summary.md`
- HTTP 面（端点、动作、`code` 表）：`docs/api.md`
- 界面：`docs/ui-design.md`（四态与迁移、告警分级、心跳判据）
- V1 迁移边界（配置契约、日志三处必改、设置项语义与键间规则、导入顺序）：`docs/v1-lineage.md` 第 2 章
- 后端协议：`docs/backend-protocols.md`
- 会中与会后两套模型的选型、四个 `post.*` 键的取值依据与本机实测：`docs/asr-backends.md`
- 索引：`docs/spec.md`
