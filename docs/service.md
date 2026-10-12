# meeting · 服务骨架

> 这份文档写**当前服务已经落地的那一块**：怎么装、怎么起、起了之后有什么、配置键在哪、log 在哪、退出码是什么、测试怎么跑。
>
> 一句话边界：现在起得来的是**一个有会话、能推、能问、能真录音、能边录边出转写临时稿、收尾会拉起会后离线转写的服务**。它会开会、会结束、会把状态推给你，开一场就把两路真声音边采边写进场次目录、把两路转写逐行写进会中临时稿，收尾转出 MP3，末尾那一步把会后离线转写拉起来（起线程就返回，产物稍后落进场次目录）。
>
> 读者两种：把它装起来用的人（读第 1–3、8 章），和往它里面插后端 / 加功能的下游开发者（读第 2、4–7 章）。
>
> 四态状态机、快照字段、`session.json`、SSE 帧形状、告警判定写在 `docs/state.md`；十五条端点（十三条 `/api/*` + 静态首页 + `/debug`）与动作信封写在 `docs/api.md`；会中总结那一层（窗口、提示词、配置键、动作与记录端点）写在 `docs/summary.md`。

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

包名 `meeting`，Python ≥ 3.13。运行时依赖三条主干 `starlette` / `uvicorn` / `anyio`（routes.py 直接 import），外加**音频四件套** `numpy` / `sounddevice` / `soundcard` / `pydub`（`dca4539b` §5 的裁定：录音是产品主体，放可选 extra 会让「没装 extra」退化成「一场会什么都没录」）。转写要的模型依赖在 `.[asr]` extra 里（`torch==2.9.1+cu128` / `transformers==5.15.0` / `webrtcvad-wheels==2.0.14`）；`torch` 那个是 CUDA 12.8 构建、PyPI 上没有这个 wheel，装的时候要带索引：`pip install --extra-index-url https://download.pytorch.org/whl/cu128 "meeting[asr]"`（CPU 档写 `torch==2.9.1`、不用那个索引即可）。**不装这一格照样能起服务**：重依赖全在函数体内惰性 import，转写那一路缺模型时如实记失败，录音一个字都不受影响。`capture` extra 有意留空。本机实测环境：Python 3.13.7 / starlette 1.0.0 / uvicorn 0.42.0 / pytest 9.0.3 / httpx 0.28.1 / numpy 2.4.4 / sounddevice 0.5.5 / soundcard 0.4.6 / pydub 0.25.1。

**外部前置条件：`ffmpeg` 要在 PATH 上**（`pydub` 靠它转 MP3；装机时一并装）。它缺席时服务照常起、照常录，只是收尾第 2 步 `transcode` 如实记 `failed`，**归档 PCM 原样留在场次目录里**（一个字节都不丢）。

命令面三个词：

| 命令 | 现在干什么 |
|---|---|
| `meeting web` | 起本地服务，前台跑到 `Ctrl+C` |
| `meeting post <场次目录>` | **已经接上**：只跑会后那一套（音频归档补齐 → 会后离线转写与说话人聚类 → 会后纪要），不开界面、不起服务，跑完打印每层的结论与产物绝对路径就退出 |
| `meeting replay <场次目录>` | **已经接上**（`a3b684bd`）：重放验证——把那一场的素材截断复制进 `--out` 指定的目录，在拷贝上全速重跑会后离线转写链；只验证、不拉纪要，跑完打印实时率、每块耗时与失败原因分布就退出 |

两种起法等价，选一种：

```powershell
meeting web                     # 装过包（console script，指向 meeting.cli:main）
python -m meeting web           # 不装包也行，在仓库根跑
meeting post <场次目录>          # 只跑会后那一套，跑完就退
python -m meeting post <场次目录>
meeting replay <场次目录>        # 重放验证：只读素材，在拷贝上重跑离线链
python -m meeting replay <场次目录>
```

- **不做可编辑安装也能跑**：在仓库根目录跑 `python -m meeting <动作词>` 即可（`python -m` 会把当前工作目录放进 `sys.path`）。
- **`meeting post` 认一个开关**：`--force`（重复给也照收）。它是本轮**唯一**会再次花钱的开关 —— 它无视两层的终态、把离线转写与纪要都重跑一遍；不给它时那一趟按幂等闸走：两层都已完成（状态文件落 `done` 且产物齐）就一个引擎调用、一个子进程都不产生（终端打一句「无待办」），只差一层就只补那一层，任一层停在 `running` 就**拒跑**（退出码 `2`，提示里点名那一个状态文件并给出 `--force`）；被打断的那一场（`interrupted`）是终态但不算完成，下一趟把离线段**从头再跑**，不需要 `--force`。
- **`meeting post` 要一个位置参数**：`<场次目录>` 给全（**不猜最新一场、不列目录内容、没有交互菜单**）。不给它、给多了、指到不存在的路径、或写了 `--force` 以外的开关，都会打印用法说明并以退出码 `2` 结束。
- **`--help` 这类开关没有**：`meeting` 不带参数、或写了 `web` / `post` / `replay` 以外的词（例如 `meeting serve`），都会打印用法说明并以退出码 `2` 结束。
- **`meeting post` 读配置、也按生效配置重建服务级日志**（与 `web()` 同一套：配置损坏退出码 `3`、取值非法退出码 `2`，`log.dir` 那四格生效）。它的落点是命令行显式给的那一个场次目录。
- **`meeting replay <场次目录>` 认三个开关**：`--seconds N` / `--chunks N` / `--out 目录`（`a3b684bd`）。上限两种表达给其一：`--seconds N` 只重放前 N 秒；`--chunks N` 只重放**每路**前 N 块，按生效的 `post.chunk_seconds` 换算成秒（该值 `<= 0` 时块语义塌缩，这一档如实拒跑）；都不给就是整场，同给两个拒绝。`--out` 指定拷贝与产物的落点（缺省落系统临时目录，每遍给一个新目录）。它**只验证离线链、绝不拉纪要**：素材截断复制进 `--out` 的落地，在那份拷贝上直调 `run_session(..., minutes=False)`——生产链零改动，那一场的产物一个字节不动（用例拿 digest 树断言）。跑完打印实时率（墙钟 ÷ 盘上实测音频时长）、每块耗时与失败原因分布（`transcribed` / `silent` / `failed` / `unjudged` 四档）。

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
   - **`transcript` 那几格的初值从转写后端读一次**（`meeting/service/runtime_facts.py` 的 `_refresh_transcript_snapshot()`，位置在探测设备**之后** —— 转写后端是在那一步 `bind()` 上的）。没起过场次时快照里的 `transcript.health` 因此当场就是真实现报的 `stopped`，用不着等健康度节拍的第一拍。
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
| `GET /api/heartbeat` | 七字段，任何状态下都 `200`。`service` 这个字面量是端口探测认亲用的，必须有 |
| `GET /api/summaries` | 本场按过的、成功产出的那几次会中总结（时刻 + 窗口 + 那几条），任何状态下都 `200`。记录活在内存里、不落文件，新的一场 `start` 清空 |
| `GET /api/sessions` | 工作区注册表 + `recent`（最近一场）+ 顶层 `post_job`（此刻在飞的那趟会后，全局「显卡忙不忙」的会后一半读数），任何状态下都 `200`，**纯读**。形状见 `docs/contract.md` 4.8 |
| `GET /api/sessions/{id}` | 单场状态：这一场此刻跑到哪一档、各段时长与时间、会后离线跑到第几块 / 共几块、正在跑哪一路、已经跑了多久，`200`，**纯读**。`{id}` 是这一场的目录路径（URL 编码的绝对路径）。形状见 `docs/contract.md` 4.9 |
| `GET /api/dirs` | 目录选择器的一层现场（起点用户主目录，`?dir=` 换层），`200`，**纯读** |
| `GET /api/devices` | 两路各自的候选设备端点表（`{mic, loopback}` 每项六格），`200`，**纯读、只枚举、不开设备**（设置抽屉的两个下拉读它） |
| `POST /api/actions` | 十六个动作（`start` / `stop` / `mute_session` / `settings_update` / `reprobe` / `dev_inject` / `summarize` / `workspace_open` / `workspace_remove` / `post_offline` / `post_minutes` / `post_stop` / `pause` / `resume` / `set_source` / `speaker_rename`）。信封、动作表与 `code` 表见 `docs/api.md`；`post_offline` / `post_minutes` 是会后两个按钮（R26），`post_stop` 停掉正在跑的精细稿（R28，`b1c6a58e`），`pause` / `resume` / `set_source` 是会中的暂停、继续与逐路开关（`10770b41`），`speaker_rename` 把一个说话人编号的名字写进这一场目录里的人名表（R10 第四条，`91b6752e`） |
| `GET /api/sessions/{id}/materials` | 会议资料的清单（`{dir, materials_dir, items:[{name, size_bytes, added_at}]}`），`200`，**纯读**；目录就是清单，`meeting_materials/` 还不在时 `items` 是空数组、目录也不建。资料层口径见 `docs/api.md` 第 3 章末与 `docs/contract.md` 4.12 |
| `POST /api/sessions/{id}/materials?name=<文件名>` | 上传：请求体就是文件的原样字节，**流式写盘**；`name` 不可信（服务侧清洗），重名自动加序号后缀。成功体 = 通用信封 + `material` |
| `GET /api/sessions/{id}/materials/{name}` | 取原文件：原始字节从盘上流式读出，`Content-Type` 按扩展名判 |
| `DELETE /api/sessions/{id}/materials/{name}` | 删除：只删那一个文件。成功体 = 通用信封 + `material: {name}` |

`/api/*` 的应答一律带 `Cache-Control: no-store`。端口策略与三道本地最低防线（Host 校验 / 写语义端点要自定义头——动作之外，会议资料的两条写端点也要 / 不开 CORS）见 `docs/api.md` 第 5 章。

`GET /api/summaries` 是本服务唯一一条「读内存里那点会话级数据」的端点（会中总结的记录表）：它**不落文件**，也没有配置键；九个 `summary.*` 键（三个端点键、预设两键与四个预算 / 缺省档键）的取值与默认值见 `meeting/config.py` 与 `docs/api.md` 第 4 章 `settings_update` 那一张键表。

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
| `heartbeat.interval_seconds` | `10`（实现给的合法范围 `1–60` 秒） | **界面**发心跳的周期，进快照 `settings`（**热读**：写盘那一刻快照就换新值，`311f84f5`） |
| `capture.backend` | `auto` | 服务（选取采集后端；`auto` / `null` / `dev` / `module:Class`） |
| `devices.mic` | `null`（= 没配，按默认策略挑） | 服务（麦克风那一路用哪个输入端点；`dca4539b` 定名。**热读**（`311f84f5`）：写盘成功、内存生效值当场换掉，空闲态打一次 `reprobe` 即按新值挑；会中仍不换设备） |
| `devices.loopback` | `null`（= 没配，按默认策略挑） | 服务（loopback 那一路用哪个 render 端点，同上） |
| `transcriber.backend` | `auto` | 服务（选取转写后端；`auto` 解析到 `meeting.backends.transcribe:TranscriberBackend`，`dev` 落转写替身，缺席时按缺省替身） |
| `post.hook` | `auto` | 服务（选取会后钩子；`auto` 解析到 `meeting.backends.post:PostProcessHook`，`null` → 「生成会后精细稿」按钮 404 `action_unavailable`；收尾第 4 步不再拉起它，那一格停在 `waiting`） |
| `post.engine` | `auto` | 会后离线那一层（选取离线引擎；`auto` 解析到 `meeting.offline.engine:MossOfflineEngine`，`null` 在引擎解析时抛错） |
| `post.model` | `OpenMOSS-Team/MOSS-Transcribe-Diarize` | 会后离线那一层（离线引擎加载哪个模型，HF 仓库 id 或本地快照路径） |
| `post.dtype` | `bfloat16` | 会后离线那一层（引擎精度档位，认得的名字见 `docs/backend-protocols.md` 第 3 章） |
| `post.chunk_seconds` | `450`（合法范围 `0–86400`） | 会后离线那一层（每块音频的长度上限秒数，`0` = 关掉分块、整段一次跑；默认值理由见 `docs/backend-protocols.md` 第 3 章） |
| `minutes.hook` | `auto` | 会后纪要那一层（选取纪要钩子；`auto` 解析到 `meeting.backends.minutes:MinutesHook`，`null` → 「生成纪要」按钮 404 `action_unavailable`，`meeting post` 补纪要那一路安静跳过） |
| `minutes.chunk_lines` | `100`（合法范围 `10–1000`） | 会后纪要那一层（每块行数，切块与行号区间表都用它） |
| `minutes.max_segments` | `8`（合法范围 `1–20`） | 会后纪要那一层（每条 source 的分段上限，契约校验与提示词都用它） |
| `minutes.timeout_seconds` | `14400`（合法范围 `60–86400`） | 会后纪要那一层（一次纪要生成的墙钟上限，默认四小时） |
| `minutes.dsh_url` | `http://127.0.0.1:3080` | 会后纪要那一层（`dsh web` 服务端的地址；拉起之前先探它，**在跑**就把这一场交付给它、终态记 `delivered`，没在跑才落回 headless 子进程。任意非空字符串，本层不校验 URL 形状） |
| `summary.base_url` | `https://api.deepseek.com/v1` | 会中总结那一层（OpenAI 兼容端点的地址；换厂商改这个键） |
| `summary.api_key` | `null`（= 没配，**可空键**） | 会中总结那一层（请求头那一格；也可以走环境变量 `MEETING_SUMMARY_API_KEY` 供一个不进配置文件的来源） |
| `summary.model` | `deepseek-chat` | 会中总结那一层（请求体里那个模型名） |
| `summary.extra_body_json` | `""`（= 不并） | 会中总结那一层（厂商特有字段，一段合法 JSON 对象的文本，原样并进请求体） |
| `summary.presets_json` | `""`（= 没配，回落平铺四键） | 会中总结那一层（**热读**：写盘那一刻生效值就换。多套端点预设整装一个键：一段 JSON 数组的文本，每套一个对象，`name` 必填、非空白、不重名，`base_url` / `api_key` / `model` / `extra_body_json` 可省；预设里的密钥不进快照、不在界面回显） |
| `summary.active_preset` | `""`（= 不选预设，用平铺四键） | 会中总结那一层（**热读**：写盘那一刻生效值就换，会中切模型不用重启。当前用哪一套的指针：空串或某套预设的 `name`，名字不在清单里被 `settings_update` 的写前校验当场拒） |
| `summary.deadline_seconds` | `25.0`（合法范围 `1.0–3600.0`） | 会中总结那一层（一次总结的墙钟 deadline） |
| `summary.max_attempts` | `3`（合法范围 `1–10`） | 会中总结那一层（尝试次数上限；真正的刹车是 deadline） |
| `summary.default_window` | `15m`（取值 `5m` / `15m` / `30m` / `full`） | 会中总结那一层（`args.window` 省略时用的那一档；**热读**：写盘那一刻快照 `settings.summary_default_window` 就换新值，界面默认预选跟着走，`311f84f5`） |
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

**另有一格落在这张表之外**：`MEETING_SUMMARY_API_KEY` → `summary.api_key`（会中总结那一格的密钥，`1987cb20` phase1 加进来）。它走的是与上面四个**同一条**通路（装载时求值、优先级一样），但**不改「哪几个键生效」这件事**，只是给密钥供一个不进配置文件的来源；`meeting/config.py` 的 `ENV_KEYS` 里记着它，测试的子进程环境剔除清单由那张表派生。它压的是**平铺那一格**：指针指到某套预设时，密钥取预设里那一格，平铺键（含这一格环境变量）不参与。

**环境变量一律在启动时求值**，改了不会热生效。配置里的热读键是 `asr.*` 那五个、`alarm.sound`（出声开关要在勾选那一刻生效，见 `docs/contract.md` 第 4.6 章），加上设置抽屉那四项（`311f84f5`：两个设备键位、`heartbeat.interval_seconds`、`summary.default_window`，写盘那一刻生效值视图就提交），再加上会中切模型两键（`summary.presets_json` / `summary.active_preset`：消费方零持有 —— 每次总结都是现读配置、现造客户端，写盘那一刻生效值视图就提交），其余键改了要重启 —— 热读键的单子以 `meeting/config.py` 的 `HOT_KEYS` 一处为准，别在天上多加会出现「我改了配置怎么没用」这类假 bug。

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
| `EXIT_FAILURE` | `1` | 收尾有步骤 `failed`；`meeting post` 那一趟没做成（音频转不动、离线那一层 `failed`、纪要落 `failed` 或等它落终态等过了 `minutes.timeout_seconds`）；`meeting replay` 那一趟没做成（整场落在 `failed`，或重放路上抛异常）；`meeting web` 起步这一路（组装应用 / 构造服务器对象）出的意外 | 能 |
| `EXIT_REJECTED` | `2` | 四个触发面：**用法错误**（不带参数 / 动作词不在 `web` `post` `replay` 里 / `post` 或 `replay` 的位置参数不对 / 未定义的开关；`meeting replay` 的上限换算不出——两开关同给、`--chunks` 撞上 `post.chunk_seconds <= 0`——与素材目录指不对也落这一档）、**配置值非法**、**端口被占**（两种情形都算）、**`meeting post` 的幂等闸拒跑**（有状态文件停在 `running`） | 能 |
| `EXIT_STARTUP_UNMET` | `3` | 启动前提不成立：主 log 不可写、`config.json` 读不出来或不是合法 JSON | 能 |
| `EXIT_INTERRUPTED` | `130` | 收尾中途第二次 `Ctrl+C`；已经停在 `ended·draining` 时按下的那一次 | 能 |

**五档全部可达。** 五种关停情形各自落在哪一档，见第 2 章那张表。

退出码的出口一律是 `sys.exit(<常量>)`，异常不许冒到 uvicorn 或解释器那里变成解释器默认码。收尾失败档退出时，终端上会打 log 路径与**服务 log 的最后 20 行**；强制打断档退出时打的是档位、盘上留在哪一格与 log 路径。两套文案都按**现场**说话：现场真有 `failed` 步才说「收尾有步骤失败，服务不自杀」。

`SIGTERM` 不在本服务的接管范围内，本阶段不定义它的退出码。

## 7. 跑测试

**从仓库根跑**（`pythonpath = ["."]` 按 rootdir 解析，工作目录一变 `import meeting` 就不成立）：

```powershell
python -m pytest -q
python -m pytest -q -m "not slow"   # 快档：每次迭代圈跑
python -m pytest -q -m slow         # 慢档：收尾/大改时跑（不受时长限制）
python -m pytest tests/test_state_machine.py tests/test_api_actions.py -q   # 只跑某几个文件

# 并行档（pytest-xdist 3.8.0 + execnet 2.1.2，进 test extra；运行时依赖零增加）。
# 并行参数只走命令行、不进 addopts：串行档命令与行为与从前逐字节一致。
$env:MEETING_CASE_WALL_CLOCK_LIMIT_SECONDS = "8"    # 并行档放宽逐用例看门（缺省 5.0 不动）
python -m pytest -q -m "not slow" -n auto --dist loadfile   # 快档并行（每圈这条；本轮实测 7.66 s）
python -m pytest -q -n 12 --dist loadfile                   # 全量并行（收尾这条；本轮收尾跑的是串行全量，
                                                            # 并行档未单跑。全量用 12 worker——16 个会把
                                                            # shutdown 的真子进程信号用例饿到超时）
```

**快档红线（机主定，`68927c27` 收尾轮起生效）**

为什么要压得这么死：**这些测试没有 CI**——不存在一条独立运行的流水线在旁边替谁跑测试、
出报警；测试就跑在机主本机上、跑在 agent 的循环里。execute 跑一遍、review 跑一遍、
fix 每改一点再跑一遍、发现一个错修完还要再跑一遍——跑测试的时间要**按圈记账**，每跑一圈
记一笔：快档每慢一秒，整个开发循环就慢「圈数 × 一秒」，而圈数没有上限。
快档压得越死，agent 的迭代越快；这条与健壮性无关，是循环结构本身的要求。据此：

- 快档的计价是**每圈并行 ≤ 10 秒（红线 8–10 s）**：「8–10 秒买 60–70% 的正确率，好过 15 秒
  买 90%」——迭代速度优先于健壮。
- **之后新加的任何测试，必须满足「无 sleep、无真子进程、无真墙钟等待、毫秒级纯逻辑或结构
  断言」才许进快档；达不到的一律进慢档（慢档不受时长限制，收尾 / 大改时跑一次）。**

`tests/` 下现在有 **82 个测试文件、1554 个用例**（本机实测：`python -m pytest --collect-only -q` → `1554 tests collected`；快档并行（`python -m pytest -q -m "not slow" -n auto --dist loadfile`）→ `1114 passed, 4 skipped in 8.94s`；全量串行（`python -m pytest -q`）→ `1550 passed, 4 skipped`（墙钟 156 秒）（`e8fe7da9` 轮实测）；两条收集口径 `-m "not slow"` → `1118/1554`、`-m slow` → `436/1554`，加得起来。**`68927c27` 收尾轮按上面的红线做了一轮精简与归档**：整删 `test_pause_actions.py` 5 条（pause/resume 覆盖属实：引擎字节语义在 `test_capture_engine`、动作接线折一条进 `test_api_actions`；另有两 set_source API 行为测试一并删净且全仓零替代覆盖——400 `invalid_setting` 参数校验 / 503 `capture_unavailable` 重开被占设备，改坏这套校验或失败映射套件不会红，机主裁决不恢复）；`test_dev_backend` 15 → 3（三档故障的 3 秒窗口时钟戏删，12 条）；`test_transcriber_backend` 20 → 14（解耦证明换成「模型线程 id ≠ 主线程」的结构断言）；`test_drain` 32 → 29（删观察等待行为的 3 条）；`test_asr_pipeline` 删源码形状钉扎 1 条；`test_port_policy` 22 → 17（删浏览器三连 / `av` 清单核对 / 与 shutdown 重复的信号断言，5 条）；`test_capture_e2e` 3 → 2（删 10 分钟 PCM 吞吐外推）。**挪进慢档的**：e2e 三兄弟、`test_contract_fixtures`、`test_sse`、`test_multi_client`、`test_capture_backend`、`test_post_idempotency`、`test_cli_post`、`test_offline_runner` 十个文件整体（文件级 slow 标记账目：HEAD 14 个 − 整删的 `test_pause_actions` + 新获标记 10 个 = 23；续接节点 `670d72b5` 再 +1（`tests/test_resume.py`），现在 24，与下文「24 个文件级标记」对平），加上 shutdown / port / heartbeat / state_machine / config_isolation / minutes_dsh_web 里「真子进程 / 真阈值等待」的用例（按用例级标记）——挪的都是「真子进程、真流式、真节拍、等阈值」的时钟戏，快档留下的是纯逻辑表与结构断言，逐条清单与理由见节点报告。**例外如实记：`test_post_idempotency` 走的也是「挪档 + 删 8 条」两笔账**——39 条里 31 条随文件进慢档，8 条直接删除（机主裁决不恢复，逐名清单见 `68927c27` 节点报告）：在飞标记时序守卫、双线程 claim 原子性（Barrier）两条并发守卫，加 force×在飞组合两条、注入路径等终态 / 超时不撒谎、minutes 不落定如实报在跑、refill 二跑落定四条。上一版口径：`a3b684bd` 那一轮 75 个文件、1458 条（快档收集 1049、慢档 409，全量 passed 数那一轮没刷新）；更早 `91b6752e` 是 74/1440，再往前 72/1428，71/1407 与进场实收 1408 的那 1 条漂移已在当时订正。那 4 条跳过的是 `tests/test_minutes_evidence.py` 里认 `.work` 证据脚本的用例——脚本不在盘上时按设计跳过，`-rs` 能看到路径与补救办法。口径是**pytest 会去收用例的文件数**：`tests/_helpers.py` / `tests/_service.py` / `tests/_drain_fakes.py` / `tests/_capture_faulty.py` / `tests/_dsh_web_fake.py` / `tests/_contract.py` 那几个测试支持模块不算，`tests/v1/test_repeat_filter.py` 算一个）。下面那张逐文件表走到今天这一步，中间隔着几笔增量，分开记：

- **`1987cb20` 那一步：49 行（行内用例数相加 976）→ 57 行 / 1133 条，+8 个文件、+157 条。** 六个 `tests/test_summary_*.py` 111 条（会中总结内核那一层，`1987cb20` phase1 —— `test_summary_client.py` 19 条、`test_summary_parse.py` 19 条、`test_summary_prompt.py` 12 条、`test_summary_runner.py` 23 条、`test_summary_settings.py` 21 条、`test_summary_window.py` 17 条）、`tests/test_summary_action.py` 31 条与 `tests/test_summary_buffer.py` 12 条（服务接线那一层，`1987cb20` phase2，含修复轮补的两条：四档之外的 `window` 取值一律 400、以及出口兜底那条自证），外加 `tests/test_minutes_contract.py` 60 → 63 条（会后纪要契约那几条跟上这一层）。
- **本轮那一步：57 → 58 个文件、1133 → 1147 条，+1 个文件、+14 条**（`796bbff` 与 `1c46c91` 这两笔一起算）。新文件 `tests/test_transcriber_release.py` 11 条（会中模型让出显存那四条语义与收尾接线的顺序），`tests/test_post_hook.py` 14 → 17 条（释放先于 `launch` 的顺序、释放抛错不拖垮整场、真后端确实走这条线）。
- **那张表里的行内用例数相加，与收集数已经对平**：58 行相加就是 `1147`，与 `1147 tests collected` 逐字相等。这一格先前算下来是 1144、差 3，差在 `tests/test_post_hook.py` 那一行的行内数（那一格写过 14，本机实收 17），改表时把那一格一并订正。
- **`65dd3e4c` 那一步：58 → 62 个文件、1147 → 1265 条，+4 个文件、+118 条**（三段一起算：phase1 的 `tests/test_cli_post.py`、phase2 的 `tests/test_post_idempotency.py`、phase3 的 `tests/test_cli_post_config.py`（18 条）与 `tests/test_config_isolation.py`（5 条），外加那个慢档文件 `tests/test_web_missing_dist_e2e.py`（1 条））。快档 1041 条、慢档 224 条，加起来就是 1265。
- **prune 审计第二轮那一步：1265 → 1263 条**（机主点名「检查 UI 文案是否 match 的那类测试完全没必要」之后做的一轮）。整条删掉两个用例函数（`tests/test_minutes_chunk.py` 的 `test_build_index_text_is_pure`、`tests/test_minutes_evidence.py` 的 `test_a_blank_env_var_falls_back_to_the_default`），另在七个存活用例里删掉若干条只把常量／自己刚写进去的值抄一遍的断言（`test_minutes_evidence.py` 的家目录回环、`test_cli_post.py` 的终态回读、`test_post_hook.py` 的线程实参回读、`web/tests/` 三处：恒真的 `frozenClockValue` 自比、恒等的 `formatNumber` 三元式、以及把 `无声明的 `连续 2 次` 抄进断言的整串比对）。**文件数不变**（62），因为两个用例函数所在的文件都还有大量承重内容。同轮把 `muteButtonLabel` 那三条逐字文案断言改写成「两档措辞不同 + 无告警按未静音那一档」的分支比对——它原本是该方法在 `web/tests` 里的唯一覆盖点，直接删会留下覆盖空洞。快档 1039 条、慢档 224 条，加起来就是 1263。
- **会后纪要的 web 分支那一步：62 → 64 个文件、1263 → 1295 条，+2 个文件、+32 条**（两个都快档：`tests/test_minutes_dsh_web.py`（19 条，交付层自己的契约：自签 cookie、存活探针、三步交付与失败分档、重试那一条线）与 `tests/test_minutes_delivered.py`（13 条，`delivered` 这一档的流转与读方那两道闸）。两个文件共用一件新的测试支持件 `tests/_dsh_web_fake.py`（不被 pytest 收集）；`tests/conftest.py` 同时加了第七道 autouse 兜底 `_no_real_dsh_web_call`。快档 1071 条、慢档 224 条，加起来就是 1295。
- **归档 PCM 解码修复那一步：1295 → 1297 条，+2 条**（`tests/test_offline_audio.py` 4 → 6：补两条用例钉住「无头裸 PCM 归档解得开」与「非 `.pcm` 的容器照旧走 PyAV 探测」，后者拿同一段样本的 wav 与裸 PCM 比对到逐样本相等）。修的是 `meeting/offline/audio.py`：`av.open()` 在无头裸流上必然探测失败（`InvalidDataError`），改为按后缀分流、对 `.pcm` 显式给出 `s16le` / 16k / 单声道——分流口径与 `runner.py` 的静音门、切块同一条。此前每一场「有真实音频的那一路」的会后转写都会栽在这里（环回那边靠数字静音门在解码之前被跳过，所以只有麦克风撞得上）。快档 1073 条、慢档 224 条，加起来就是 1297。**此后 `.work` 清理掉，`test_minutes_evidence.py` 里认那份证据脚本的 4 条转入跳过：收集数仍是 1297 条，全量变成 `1293 passed + 4 skipped`、快档变成 `1069 passed + 4 skipped`。**
- **工作台收编那一步（e2f6166b）：+1 个文件、+15 条**（`tests/test_workspace_registry.py` 新增 14 条 —— 注册表的登记 / 去重 / 原子写 / 损坏兜底、`classify_workspace_dir()` 的三档判读、两只读端点的现场形状、`workspace_open` / `workspace_remove` 逐档拒绝、`start` 之后刷新 `last_opened_at` 与 `recent`；另在 `tests/test_contract_fixtures.py` 补 `capture_workspace_group` 1 条，采集 `sessions/` 与收编两份动作样本，样本总数 28 → 32）。全量实测 `1294 passed, 4 skipped`；快慢分档实测：快档 1076 条（`1072 passed, 4 skipped, 222 deselected`）、慢档 222 条、共 1298。
- **会后不自动跑那一步（`05a48b6a`）：64 → 65 个文件、1298 → 1328 条，+1 个文件、+30 条。** 新文件 `tests/test_post_actions.py` 18 条（会后两个按钮：只按离线只跑离线链、只按纪要只拉纪要层、都不按收尾后什么都没起、显式 `dir` 的三档校验与省略语义、在飞 / 收尾未落定的 409、两个 hook 缺席的 404、纪要 claim 闸与线起不来时退回 claim、`GET /api/sessions` 的 `has_offline`）；其余 12 条按「两份稿平级对拍」与「收尾第 4 步停 `waiting`」的口径散在既有文件里增删（切块两份都落盘、提示词清单与说明段逐份写、契约校验的分母跟在场那份稿走、`test_state_machine.py` 钉「`waiting` 只许落在 `post` 那一格」等）。全量实测 `1324 passed, 4 skipped`；快慢分档实测：快档 1106 条（`1102 passed, 4 skipped, 222 deselected in 59.26s`）、慢档 222 条、共 1328。
- **会后进度读数那一步（`a9affaa8`）：65 → 67 个文件、1328 → 1349 条，+2 个文件、+21 条。** 新文件 `tests/test_offline_progress.py` 13 条（逐块进度账：一块跑完那一块的字落盘、跑到一半的现场读数与盘上那份账对得上、静音与失败都算跑过一块、注入缝与落盘失败两条边界、读方对半行 / 空文件 / 缺文件的容忍、写入的原子与幂等）与 `tests/test_session_status.py` 7 条（`GET /api/sessions/{id}`：跑到一半读得到、重启后退回读盘、两档 404、空闲档、快照与列表两个字不动）；另在 `tests/test_contract_fixtures.py` 补 `capture_session_status_group` 1 条，采集 `sessions/status-after-run.json` 那一份，样本总数 32 → 33。
- **同一节点的修复轮（`fix-1`）：1349 → 1354 条，+5 条**（`tests/test_session_status.py` 7 → 10 条：前七格与列表同一行同源、崩在半路那场照盘上那份如实答、盘上有账而状态文件缺席那一档；`tests/test_cli_post.py` 52 → 54 条：命令行那条入口跑完把逐块账落在盘上、`run_post()` 收得下注入的那份账）。本轮实测：`python -m pytest -q -m "not slow"` → `1128 passed, 4 skipped, 222 deselected in 58.36s`、`python -m pytest -q` → `1350 passed, 4 skipped in 146.18s`（上一版记的是 1345，那 5 条正是本修复轮补的）；快慢分档收集口径 `1132/1354` 与 `222/1354`。慢档那格的单跑读数沿用上一步，本修复轮未重跑。
- **录音分段落盘那一步（`6ea32d8d`）：67 → 68 个文件、1354 → 1370 条，+1 个文件、+16 条。** 新文件 `tests/test_capture_segments.py` 2 条（**慢档**，验收两场戏：假采集喂真素材、中间带一次暂停 —— 序号命名的段、零窗、两路时间对齐、段账与权威时长对得上；把墙钟与段戳改成胡话 —— 程序的读法与结论一个字不变）。既有文件的增删：`test_capture_engine.py` 27 → 32（暂停写静音不录内容、暂停幂等与越界 no-op、关一路写静音不报 `SourceLost`、未选一路只跑静音节拍器、收尾前的段账形状、逐段转码各一 MP3；「未选一路没有线程」那条旧判据照新行为改写），`test_session_dir.py` 13 → 16（段序号命名、段文件按序枚举、`build_session_record()` 带段账与 `archives` 表），`test_offline_runner.py` 55 → 58（多段拼成一条时间轴、单趟多段拼临时 wav、墙钟改错不改会后结论），`test_asr_pipeline.py` 16 → 18（尾读跨段接续、暂停窗的数字静音不产转写行），`test_capture_archive.py` 21 → 22（`open_pcm()` 改截断写并钉住「同名残留被截掉」、静音块写数字零且 `content_bytes` / `silence_bytes` 账分两格；删「`open_pcm()` 是追加模式」那条旧判据），`test_capture_backend.py` / `test_capture_e2e.py` / `test_transcriber_backend.py` 条数不变、判据跟着段口径走。全量实测 `1366 passed, 4 skipped in 150.4s`；快慢分档实测：快档 `1135 passed, 4 skipped, 231 deselected`（61.07s）、慢档 231 条（其中 9 条新增：`test_capture_segments` 2 + `test_capture_engine` 5 + `test_asr_pipeline` 2）、共 1370。

- **暂停与会中开关某一路那一步（`10770b41`）：69 → 70 个文件、1376 → 1385 条，+1 个文件、+9 条。** 新文件 `tests/test_pause_actions.py` 5 条（**慢档**，三条 spec 读数全部经 HTTP 动作 `pause` / `resume` / `set_source` 驱动：暂停窗逐样本为零且继续之后内容逐字节照旧、段账恒一段、权威时长不含暂停；关一路那一路改写静音而另一路照常、重新打开之后又出内容；重开时设备打不开如实回 `capture_unavailable`(503) 带 `{source, error}`；安静场景里暂停跨过 `alert.silence_seconds` 也不冒致命级、继续之后那条照常到点出现，顺带钉住「暂停期间电平帧照发、`recording_alive` 仍为真」；`set_source` 那三档取值校验）。`tests/test_alerts.py` 26 → 30 条（暂停区间不进静默 / 恢复那两条累计、继续之后从零起算、新一场开场清掉暂停）。`tests/_capture_faulty.py` 加了 `MEETING_TEST_CAPTURE_REOPEN_ERROR` 这一个旋钮（点名的那几路在**重新打开**时开不起来）。全量实测 `1391 passed, 4 skipped in 159.35s`（收集数 1395：1391 + 4 = 1395；这一读数是含 `fix-3` 那 10 条的盘面，它之后的会后三轮修复再 +12 条，收集数因此走到 1407）；快档实测 `1145 passed, 4 skipped, 236 deselected`，本节点碰过的慢档文件单跑 `218 passed in 54.23s`。

- **同一节点的第三轮（`fix-3`）：70 → 71 个文件、1385 → 1395 条，+1 个文件、+10 条。** 新文件 `tests/test_pause_model_lifecycle.py` 10 条（**快档**，0.35s；全程注入假识别器 + 假 `torch` 模块，不读权重、不碰卡）：`acquire_gpu()` 的四条语义（没释放过就安静 no-op、释放之后经**同一个 factory** 重造一份、工厂造不回来时「继续」这个动作照常、既不碰在跑的流水线也不碰 `health()`）；暂停窗跨过一次释放与一次重拉 —— 那 1.2 秒连续零整截都进不了识别器，继续之后的素材照常出段、且由新造的那一份识别；`pause_session()` / `resume_session()` 两次提交按按下顺序落到 release / acquire（幂等档在提交之前就返回，按两次不做两遍）；替身把两次调用各按住 3 秒时两个动作仍 < 0.5 秒返回（没把事件循环钉住）；`transcriber.backend = auto` 那一档两个方法确实被喊到；替身后端缺这两格照样暂停与继续；模型调用抛错不让按钮失败。快档实测 `1155 passed, 4 skipped, 236 deselected in 60.84s`。

- **会后两个 bug 那个节点的第 1 轮修复（`fix-1`）：1395 → 1404 条，+9 条**，全落在既有文件里：`tests/test_offline_engine.py` 29 → **30**、`tests/test_offline_runner.py` 58 → **61**（加载失手那一档、逃逸出来的异常在盘上落一份写清逐路的兜底状态文件、只读顶层 `state` 一格就能把三档分开）、`tests/test_session_status.py` 10 → **12**（`GET /api/sessions/{id}` 的 `offline.state` 照盘上那份答 `partial`）、`tests/test_post_hook.py` 17 → **18**、`tests/test_cli_post.py` 54 → **56**（调用方补账不把 runner 当场写下的原因整句盖掉、也不把盘上的 `partial` 翻成 `failed`）；文件数不变（71）。

- **同一节点的第 2 轮修复（`fix-2`）：1404 → 1407 条，+3 条**：`tests/test_offline_runner.py` 61 → **63**（整路都是数字静音时模型一次都不被拉起来、真有块要送引擎时加载失手照旧独立成档）、`tests/test_offline_progress.py` 13 → **14**（加载失手那一场账上留一行、读方看到的 `total` 仍是这一路的块数）；文件数不变（71）。

- **同一节点的第 3 轮修复（`fix-3`）：1407 → 1407 条，+0 条。** 唯一动过的用例是 `tests/test_offline_runner.py` 里既有的 `test_an_exception_escaping_run_session_still_leaves_a_failed_status` —— 它多钉了「一路都没跑到的那一格取 `not_run`、`error` 为 `null`」这一档，条数与文件数都不动（71 个文件、1407 条）。

- **设置抽屉那一步（`311f84f5`）：68 → 69 个文件、1370 → 1376 条，+1 个文件、+6 条。** 新文件 `tests/test_devices_endpoint.py` 5 条（快档：`GET /api/devices` 的形状与纯读、枚举失败的 503、心跳与总结缺省档写后即时可见、心跳越界写盘前拒绝、`reprobe` 按刚写进去的设备值挑），`tests/test_contract_fixtures.py` 10 → 11 条（settings 五键的键集断言 + `devices/all.json` 那一份采集，样本总数 33 → 34）。既有文件的判据跟上键档迁移（热读六键 → 十键）：`test_api_actions.py` 30 条（`applied`/`pending_restart` 两份名单、快照 settings 五键）、`test_capture_probe.py` 32 条（设备键位归 `applied`、写后当场可见）、`test_summary_settings.py` 21 条（`summary.default_window` 一键改热读）、`test_config.py` 63 条（热读键清单十个）、`test_state_machine.py` 21 条与 `test_static.py` 19 条（快照 settings 键集）。全量那档本步未跑（收尾由指挥者安排）；快档实测 `1141 passed, 4 skipped, 231 deselected in 58.12s`。

- **续接那一步（`670d72b5`）：71 → 72 个文件、1388 → 1393 条，+1 个文件、+5 条。** 新文件 `tests/test_resume.py` 1 条（**慢档**，真引擎 + 假采集 `tests._capture_faulty` 的端到端验收：录一段、结束、再对同一工作区接着录一段 —— 旧段 PCM 逐字节原样、段账两段、权威时长 = 两段之和、两段各产 MP3、`/api/sessions` 两只端点都看得到两段）。既有文件：`test_session_dir.py` 16 → 17（`next_segment_number()` 段号计器用例；原「已有 `session.json` 即拒」两条翻案为「放行 = 续接」—— 身份三格沿用、收尾四步重起），`test_asr_pipeline.py` 17 → 19（续接开场三用例：记号行落两段正文之间、`kind="gap"` 行推给出口、旧段一个字节不重读；开场无断口行），`test_offline_runner.py` 63 → 64（两段归档的会后链：每路两段按序拼成一份 wav 喂引擎、状态文件与报告的秒数 = 两段之和），`test_session_status.py` 条数不变、判据跟上（行键数断言 11 → 12），`test_contract_fixtures.py` 条数不变、`sessions/` 两份样本补 `segments` 格。全量实测 `1389 passed, 4 skipped`；快档实测 `980 passed, 4 skipped, 409 deselected`。

- **人名表那一步（`91b6752e`）：73 → 74 个文件、+29 条（本节点自己的账）。** 新文件 `tests/test_speaker_names.py` 20 条（人名表的写读往返与原子落盘、键形状与空名字的写侧拦截、坏表 / 未来主版本的读侧宽容与写侧拒写、身份行三档来源——有编号带次数读数、无编号给本地 / 远端）；`test_minutes_prompt.py` +8（`{{SPEAKER_NAMES}}` 的渲染三档、占位符名单六必写 + 可省 PHASE_BODY）；`test_api_actions.py` +1（`speaker_rename` 动作面）；`test_summary_action.py` / `test_post_actions.py` 判据跟上动作名冻结清单（15 只）；契约样本 `sessions/` 两份补 `speakers` 格、`actions/fail-invalid-setting-unknown-action.json` 跟上动作面。本轮收尾实测：快档并行 `1027 passed, 4 skipped in 7.66s`、全量串行 `1436 passed, 4 skipped in 128.49s`。表头 74 / 1440 是与并行落地的隔离闸节点（`tests/test_minutes_gate.py`，账目归它自己的报告）合并后的盘面。
- **打断会后那一步（`b1c6a58e`）：75 → 76 个文件、1458 → 1480 条，+1 个文件、+22 条（快档 13 / 慢档 9）。** 新文件 `tests/test_post_reconcile.py` 5 条（**快档**：服务启动对账——在飞记录 + 盘上 `running` → offline 补成 `interrupted` 并清记录、盘上不是 `running` 只清记录不写盘、minutes 档只清 + log、坏记录按空表起不阻塞启动）。既有文件：`test_offline_runner.py` 64 → **70**（旗标在块边界命中整场落 `interrupted` + 缺口注记、起跑前就立旗标一路都不跑、打断不通知纪要、`write_interrupted_status()` 对账写点不翻已落定的终态）、`test_post_actions.py` 19 → **23**（`post_stop` 四档：受理回执、没有在飞拒、纪要在飞如实拒、`dir` 指到另一场拒）、`test_offline_engine.py` 30 → **32**（`release_engine()`：丢引用报真、空手与裸假件报假）、`test_session_status.py` 13 → **14**（盘上 `interrupted` 照盘答、不折回 `running`）、`test_post_hook.py` 18 → **19**（补账不把 `interrupted` 盖成 `failed`）、`test_cli_post.py` 56 → **57**（同一条补账判据在 CLI 侧）、`test_post_idempotency.py` 31 → **32**（`interrupted` 免 `--force` 从头再跑）、`test_contract_fixtures.py` 11 → **12**（`sessions/status-interrupted.json` 那一份，样本总数 34 → 35）。本轮实测：快档并行 `1058 passed, 4 skipped in 7.91s`、全量串行 `1476 passed, 4 skipped in 131.57s`。

- **开场误报「采集已停止」修复那一步（`de899128`）：76 个文件不变、1490 → 1493 条，+3 条。** `tests/test_heartbeat.py` 11 → 13 条（**「还没有第一帧」报未知**：自本场 `start` 起算的开场窗口（`max(2.0s, 3 × frame_interval)`）内一帧没到时报 `None`、超窗仍无首帧报 `False`，`stall` 全程挡帧、窗口由用例拨过去，两个分支在同一场里先后各验一遍；再连开三场，开场窗口里 `recording_alive` 只许 `None` 或 `True`）。`tests/test_contract_fixtures.py` 12 → 13 条（`heartbeat/no-first-frame.json` 那一份采集：`stall` 先挂再 `start`，开场窗口内的那一读正是「还没有第一帧」档，样本总数 35 → 36）。本轮实测：快档并行 `1068 passed, 4 skipped in 7.91s`、全量串行 `1489 passed, 4 skipped in 138.48s`。

- **帧空洞留痕那一步（gil-stall-hole）：76 → 77 个文件、1493 → 1506 条，+1 个文件、+13 条（快 10 / 慢 3）。** 新文件 `tests/test_capture_gaps.py` 11 条（快 10 / 慢 1：检测器时钟注入、引擎刻度线接线、运行时账合并与有界、dev 档 silence / lost 不算而 stall 算；慢档那条是 stall 端到端），`test_contract_fixtures.py` 13 → 14 条（`heartbeat/capture-gap.json` 那一份采集，样本总数 36 → 37），`test_capture_engine.py` +1（暂停期间节拍照发、帧空洞账一拍不缺）。

- **gil-stall-hole 与资料节点之间的几个节点（运行时拆 mixin 与若干 web / 采集修缮）：77 → 81 个文件、1506 → 1532 条，+4 个文件、+26 条。** 新文件 `tests/test_dev_level_sequence.py` 3 条、`tests/test_importlock.py` 3 条、`tests/test_minutes_config_defaults.py` 5 条、`tests/test_set_source_alerts.py` 5 条，其余 10 条散在既有文件里；这一段当时未逐节点记账，本轮收尾补一笔总数。

- **会议资料那一步（`285bf58c`）：81 → 82 个文件、1532 → 1553 条，+1 个文件、+21 条（快 20 / 慢 1）。** 新文件 `tests/test_materials.py` 20 条（**全快档**，验什么见下面那张表的行），`tests/test_contract_fixtures.py` 14 → 15 条（`materials/` 四份样本那一组采集，契约样本总数 37 → 41，随文件在慢档）。本轮实测：全量串行 `1549 passed, 4 skipped in 156.68s`、快档并行 `1113 passed, 4 skipped in 8.42s`。

- **纪要提示词吃会议资料那一步（`e8fe7da9`）：82 个文件不变、1553 → 1554 条，+1 条（快 1 / 慢 0）。** `tests/test_minutes_prompt.py` 80 → 81 条（`test_the_real_template_carries_the_three_materials_rules`：真模板带着三处资料口径且各落各的阶段——逐段总结读资料、合并扫一眼资料、只读检查对资料核；正文形状零变化，固定骨架逐格在、没有「参考资料」一节；纯提示词改动，渲染器与占位符零变化）。本轮实测：全量串行 `1550 passed, 4 skipped`（墙钟 156 秒）、快档并行 `1114 passed, 4 skipped in 8.94s`。

文件数与用例数的口径写在 `docs/spec.md` 的代码现状表里。不需要先 `pip install`，也不需要可编辑安装。

| 文件 | 用例数 | 验什么 |
|---|---|---|
| `tests/test_config.py` | 64 | 默认值表、三层优先级、环境变量映射、十二个热读键（`asr.*` 五个 + `alarm.sound` + 设置抽屉那四项（`311f84f5`）+ 会中切模型两键）、键间校验（预设指针的键间规则在内）、配置损坏抛错、值非法 → 退出码 `2` |
| `tests/test_port_policy.py` | 17 | 端口与只监听回环、两种占用文案与退出码 `2`、探针 1 秒总超时、用法错误、console script 入口面（`68927c27` 收尾轮：删浏览器三连 / `av` 清单核对 / 与 shutdown 重复的信号断言；子进程用例改按用例级标记进慢档） |
| `tests/test_static.py` | 19 | `/` 的产物与说明页两条分支、**目录存在性按每请求判定**（同一个应用实例里「缺失 → 建出 → 又删掉」三步走完、中间不重启）、`/debug`、`/api/*` 的 `no-store`、Host 校验 `403`、动作头校验 `403`、完整版快照的键集与非空 `service.version`、`revision` 每次状态变更 +1、心跳七字段、空闲态盘上不留任何场次产物 |
| `tests/test_contract_fixtures.py` | 15 | 与 `contract/fixtures/` 的漂移比对：重走真实服务路径采集 41 个样本逐文件比对（默认模式）、掩码结构断言（顶层九键 / 十一键、settings 七键、两路次序、包络长度 15、`levels` 的 `ts is None` 与 `bar == envelope[-1]`、心跳七键、四帧 `revision` 自洽）、`--regen-contract` 的再生成与幂等 |
| `tests/test_logging_setup.py` | 15 | 服务级 log 位置与格式、bootstrap 重建、按两个配置键轮转、幂等、主 log 不可写与配置损坏 → 退出码 `3`、**启动不再读/改写别的场次目录**（扫描退场的三条新判据） |
| `tests/test_api_actions.py` | 32 | 除收编与会后那四个动作外其余七个动作的成功与拒绝路径、`mode` 取值域、R17 的两个 409、`capture_unavailable`、`settings_update` 的两分（12 个热读键 / 28 个要重启的键）与写盘拓扑与 `persist_failed`、`alarm.sound` 当场生效、`muted` 的首发与重算、逐动作 × 逐态可用性、`dev_inject` 的三档可用性、动作名与 `code` 集合逐字冻结、`pause`/`resume` 动作接线（`68927c27` 自 test_pause_actions 收编；start 落点校验与续接放行的用例在 `test_session_dir.py`） |
| `tests/test_state_machine.py` | 21 | 四态取值域与全部合法迁移、非法迁移抛错且不涨 `revision`、三条铁律、`elapsed_seconds` 走秒与冻结、15 柱 × 2 秒包络、收尾闭环两个分支（成功档第 4 步停在 `waiting`、失败停留档）、**`waiting` 只许落在 `post` 那一格**、取消路径、探针越界取值回退 |
| `tests/test_alerts.py` | 30 | 逐帧累计（用帧时间戳）、连续性判据、抖动不来回告警、抑制窗口与「恢复后算新事件」、**首发那一帧的 `muted` 是 `false`**（且此后同一条不被重新放行、**本场静音期间过一遍标记条目仍是 `true`**）、`muted` 的重算、提示级的合并键与上限 3（**被挤掉的键从记账里回收**）、**两条对称的清除路径**、**新一场开场把提示级两份记账一起清**、阈值只从配置读、**暂停区间不进那两条累计**（`pause_session()` / `resume_session()`，`10770b41`） |
| `tests/test_dev_backend.py` | 3 | dev 替身的基本盘（`68927c27` 收尾轮 15 → 3：解析出的确定性序列逐帧相同、`finalize()` 只记账、注入参数校验；三档故障的到点生效/恢复那批 3 秒窗口时钟戏按敏捷口径删） |
| `tests/test_sse.py` | 11 | 首帧是 `snapshot` 且与 `GET /api/state` 同源、`state` 是全量且 `id` 对齐 `revision`、`levels` 的 4Hz 与形状、`transcript` 形状、`retry` 与 `: ping`、慢消费者重同步且不拖累别人 |
| `tests/test_heartbeat.py` | 13 | `idle` 下的空形态（含空洞账零账）、`stall` 之后 `recording_alive == false` 而 HTTP 照常 200、窗口计算、两个心跳不串线、`SourceFacts` / `SourceLost` 不刷新帧时钟、帧间隔读不到时按 0.5 回退、**「还没有第一帧」报未知**（`de899128`：开场窗口内 `null`、超窗仍无首帧 `false`；连开几场开场不落 `false`） |
| `tests/test_session_dir.py` | 17 | 工作区目录校验（必给 / 已存在；目录里已有 `session.json` 的那一档放行 = **续接**，身份三格沿用、收尾四步重起）、start 不建目录与不按日期命名、`session.json` 字段全貌与原子写（无 `.tmp` 残骸）、每个阶段变更都落盘（终态那一版里 `post` 停 `waiting`）、start 写盘失败清场文件并留痕、**段账（`6ea32d8d`）**：段序号命名的三道口径（三位零填充、从 1 起、认名规则）、`segment_archive_paths()` 按序枚举、`build_session_record()` 带上 `segments` / `audio_duration_seconds` / `sources[*].archives`、**续接（`670d72b5`）**：`next_segment_number()` 段号计器（盘上实有最大段号 + 1，两路共一个计器） |
| `tests/test_workspace_registry.py` | 14 | 工作区注册表（e2f6166b：`meeting/workspace.py`）与收编动作：登记奏效且路径去重（含 Windows 大小写并一行）、再登记只刷新 `last_opened_at`、`classify_workspace_dir()` 三档判读与疑似档、注册表原子写且住配置目录、损坏兜底成空表、`workspace_open` / `workspace_remove` 的逐档拒绝与重复删除安全、`GET /api/sessions` / `GET /api/dirs` 的现场形状、`start` 开成一场后刷新 `last_opened_at` 与 `recent`、清单端点不涨 `revision` |
| `tests/test_devices_endpoint.py` | 5 | 设置抽屉的服务面（`311f84f5`，快档；设备枚举走假件，不碰真声卡）：`GET /api/devices` 两路各一份六格清单且纯读不涨 `revision`（录音态照 200）、枚举失败如实 503 `capture_unavailable`、`heartbeat.interval_seconds` 与 `summary.default_window` 写盘那一刻快照就换新值、心跳 1–60 之外写盘前拒绝、`reprobe` 重新 bind 时读到的就是刚写进去的设备值 |
| `tests/test_materials.py` | 20 | 会议资料的四条端点（`285bf58c`，全快档）：四条路由注册在单场状态那只之前、未登记 / 目录不在原地的拒收（拒收路径盘上零落笔）、上传原样字节落盘且贴上时刻 = mtime、重名序号后缀、文件名清洗七例（穿越 / 非法字符 / 保留设备名 / 结尾点与空格 / 干净名原样）、`name` 必给、录制中的活跃场次照收（被摘出清单仍可传）、清单三格与空档不建目录、取原文件的字节原样与两档 `Content-Type`、缺失与穿越名 404 `material_missing`、删除只删一个且重复删 404、几 MB 流式大文件逐字节一致、四条端点不涨 `revision` 快照九键不动、写请求缺头 403 而读不拦 |
| `tests/test_multi_client.py` | 5 | 两个订阅者拿到同一个 `revision`、revision 序列逐项相同、断开一个不影响另一个与会话、重连首帧是 `snapshot` 且不重放、`/debug` 与产物无关地可达 |
| `tests/test_drain.py` | 29 | 收尾编排这一整块：四步顺序与终态、`done` 判据、`skipped` / `failed` 的边界、预算公式与五个边界档、一趟收尾的总时长随积压量变化且不越过预算、某一步超预算 → 该步 `failed`、四步共享一条 deadline、转写缺席时的缺省输入、`_drain_wait()` 包住每一个调用、`log` 步的最小预算、取消路径的记账、`log` 步与场次 log 的挂摘与结束标记、逐类日志事件可查、失败后服务不自杀且 `start` 回 409（`68927c27` 收尾轮删 3 条「观察等待行为」的时钟戏：settle wait×2、不钉事件循环） |
| `tests/test_shutdown_signal.py` | 13 | `Ctrl+C` 的两阶段语义（真服务子进程 + `CTRL_BREAK_EVENT`；`68927c27` 收尾轮起**子进程用例按用例级标记进慢档**，纯函数级的退出码矩阵与终端文案留在快档）：空闲态 / 收尾干净走完之后各按一次 → `0` 且盘上不被改写；录音中按一次 → 收尾干净 → `0`；收尾中途第二次 / 已停在 `draining` 再按 / 同一进程连按两次 → `130`；收尾有步骤 `failed` → `1` 且终端打出 log 路径与 `meeting post`；`SIGINT` 与 `SIGBREAK` 共用同一个处理函数；退出码逐档与两套终端文案 |
| `tests/test_e2e_no_clients.py` | 1 | **慢档（`68927c27` 收尾轮挪入）**：关掉全部页面之后录音照跑（本条用 dev 真服务起一场），重连首帧是 `snapshot` 且与紧接着一次 `GET /api/state` 逐字段相等 |
| `tests/test_e2e_multi_client.py` | 2 | **慢档（`68927c27` 收尾轮挪入）**：两个订阅者同一 `revision`、同一 `muted`；下一场 `start` 之后会话级静音复位、`alarm.sound` 未变 |
| `tests/test_e2e_silence_alert.py` | 1 | **慢档（`68927c27` 收尾轮挪入）**：一路静默 → 致命级告警、另一路照常采、录音继续（`session.frozen` 为假）、结束必须由测试 `stop` |
| `tests/test_capture_probe.py` | 32 | 设备层与选择面（枚举与试开用替身）：`probe()` 两路各一条结论与三档取值域、`device_name` 是真端点名、试开真的读回样本、重复探测幂等、`list_input_endpoints()` 给系统默认输入端点打标记（枚举面用替身）、一条端点都挑不出来时 `start()` 抛 `CaptureUnavailable`、没有 `inject_fault`、`auto` 解析到真类、`settings.devices` 与 `reprobe` 动作、起服务那一场用假源走通 `start()` |
| `tests/test_capture_levels.py` | 13 | 电平换算（纯函数）：满量程锚点、10 倍幅度 = 20 dB、`-inf` 兜底成 `-120`、全零窗口落到静默阈值以下、正弦的均方根比峰值低约 3 dB、`bar = max(dbfs, peak)`、三个数都是有限值 |
| `tests/test_capture_archive.py` | 22 | PCM 归档与转码：`open_pcm()` 是**截断写**且**无缓冲**（`write()` 一返回就在盘上；同名残留被截掉，不接上一场的半截文件）、写线程「取一块写一块」、**静音块写等长数字零且账分两格**（`content_bytes` / `silence_bytes`，`bytes_written` 仍是落盘总数）、`close()` 幂等且把队列里剩下的块写完、两路不共用队列、非空 PCM 转出 `ffmpeg` 读得回来的 MP3、空文件不产 MP3、转码失败原样抛错且 **PCM 字节不变**、`from_raw` 拿到的形状参数逐字是 V1 那一份、**转码一路一次都不 `unlink`** |
| `tests/test_capture_engine.py` | 33 | 会话引擎（注入假源）：字节数按 32000 字节/秒对账、两路各自长起来、单路读崩只报一条 `SourceLost` 而另一路继续写、死掉那一路的 PCM 可解、`mode` 过滤（未选中的那一路**只跑静音节拍器**，整段写静音填充）、电平帧每 `frame_interval` 秒**无条件**发（静音时发 `-120`）、`SourceFacts` 跟着走、`finalize()` 停流 + join、`stop()` / `finalize()` 幂等、**段账（`6ea32d8d`）**：暂停写等长数字零且不录内容（暂停 / 续跑幂等、没起或收尾后 no-op）、**暂停期间节拍照发、帧空洞账一拍不缺**（gil-stall-hole：暂停不构成空洞）、关一路写静音且**不报 `SourceLost`**、收尾前的段账没有终点戳、逐段转码写满静音的段也出 MP3 |
| `tests/test_capture_gaps.py` | 11 | 帧空洞的「留痕」链（gil-stall-hole，快档 10 + 慢档 1）：检测器纯逻辑（时钟注入：首帧前不记、窗内不记、超窗报一次、同一回合不连记、`reset()`）、引擎刻度线接线（先报 `FrameGap` 再发电平帧）、运行时账（两路同回合合并为一条、秒数取区间并集、最近列表有界 10 条而累计数不设界、恢复后 `recording_alive == true` 且空洞仍在、新一场清账、录音态之外不记账）、dev 档的区分（`silence` 与单路 `lost` 不算空洞；`stall` 端到端在慢档：真实停发 3.5s 超窗 → 恢复后账上一条） |
| `tests/test_capture_backend.py` | 14 | 真采集后端的起停与收尾（假源注入，一个真声卡入口都不碰）：两路都起来并写出归档、单路起不来不抛错、全起不来才抛 `CaptureUnavailable`（逐路缘由进 `message`）、`mode=mic` 只有 mic 算数、`finalize()` / `transcode()` / `stop()` 与幂等、没有 `inject_fault`，外加两条**端到端**（进程内真运行时 + `capture.backend = auto`）：收尾第 1 步记 `done`、两路各出一份 `.mp3` 且 PCM 留着；一路在运行期死掉时另一路的归档更多、死掉那一路留半份 |
| `tests/test_capture_e2e.py` | 2 | 端到端（进程内真运行时 + `_capture_faulty` 挂件）：一场正常会收尾四步到 `done`、两路归档与假源喂块账**逐字节一致**、样本数对墙钟偏差在一个采集块内、0.5 秒帧序号连续（有声/静音计数在案）、两个 MP3 经 `ffprobe` 时长与 PCM 相符；半路掉一路时另一路归档照常长、收尾照常 `done`（`68927c27` 收尾轮删 10 分钟 PCM 吞吐外推那条性能断言） |
| `tests/test_capture_segments.py` | 2 | **分段落盘的验收两场戏（`6ea32d8d`，慢档）**：假采集（真后端 + 假源剧本）喂真素材、中间一次暂停 —— 按序号命名的段、暂停窗逐样本为零而时间轴照涨、两路段起止一致、`session.json` 的段列表 / 暂停区间 / 权威时长与盘上事实对得上；把墙钟字段与段戳改成胡话 —— 程序的读法与结论一个字不变（元信息除权威时长外一律不当判据） |
| `tests/test_resume.py` | 1 | **续接的端到端验收（`670d72b5`，慢档）**：进程内真运行时 + 假采集（`tests._capture_faulty`），录一段、结束、再对同一个工作区接着录一段 —— 旧段 PCM 逐字节原样、`session.json` 段账两段且按序号升序、权威时长 = 两段之和、各路段文件名表两段都在、收尾转码两段各产一份 MP3、`GET /api/sessions` 与 `GET /api/sessions/{id}` 看得到两段的时长与时间 |
| `tests/test_web_missing_dist_e2e.py` | 1 | 真子进程端到端（**慢档**）：`web.dist_dir` 指到不存在的路径 → 真起 `meeting web` → `/` 是说明页、`/api/heartbeat` 与 `/api/state` 照常 `200`、那个目录仍不存在、`Ctrl+C` 后退出码 `0` |
| `tests/test_capture_accounting.py` | 10 | 对账函数本身（纯函数）与它的反例：完整序列全过、中段丢块按样本序号点名、等长换序照抓、奇残字节翻假、尾部不足一帧记 leftover、静音段照计数而不算丢块、空归档不炸、帧重放真的重算电平、缺帧序号点名、墙钟偏差一个块的判据 |
| `tests/test_real_capture_guard.py` | 1 | 「一碰真声卡就炸」那道看门**与导入次序无关**：在一个全新解释器里（库还没被 import）直接调四个真采集入口，逐条必须被拦下，且**每次调用之前先断言目标确实是桩**（桩不在时红在断言上，真调用一次都不发出去） |
| `tests/test_asr_glm.py` | 20 | `meeting/asr/glm.py` 与 `guards.py` 的契约（全程注入假模型，一条都不加载真模型）：四层防护真的挂上了（`no_repeat_ngram_size=8` / `logits_processor=[_LoopGuard]` / `stopping_criteria=StoppingCriteriaList([_DeadlineStop])` 逐条断言）、降级记忆（首次 `TypeError` 去掉该参数重跑一次，开关已是 `False` 时异常照抛）、噪声预筛命中即 `ok=True, reason="noise"` 并省掉那一次生成、**超时与判环都不落部分文本**、拿 V1 那条真实坏行当模型输出喂进去断言砍到 <60 字符、参数一律从 `config.json` 读出、**`import meeting.asr.glm` 在一个全新解释器里一条重依赖都不拉** |
| `tests/test_asr_pipeline.py` | 19 | 实时转写流水线（进程内、无真模型、无真声卡）：每一行的时间戳落在「那段音频写进归档的墙钟窗口」之内、两路各一个 `meeting_transcript_<source>.md` 且每行匹配 `^\[\d{2}:\d{2}\] `、三档占位符的形状（**不含 `**`**）、噪声段连占位符都不写、收尾预算 `30 + n × (timeout + 5)`（下限 30 / 封顶 300）与预算耗完时的如实记账、**段口径（`6ea32d8d`）**：尾读到 EOF 且下一段已在盘上时按序接过去、暂停窗写下的数字静音一行转写都不产、**续接（`670d72b5`）**：开场记号行 `<!-- … -->` 落在两段正文之间、`kind="gap"` 行推给出口、读头从盘上最大段号接起旧段不重读，开场（无临时稿）无断口行（`68927c27` 收尾轮删「翻源码钉语法树」那条形状测试） |
| `tests/test_transcriber_backend.py` | 14 | 转写后端的协议实现与解耦边界：六个成员齐、模块顶层不 import 重依赖、`auto` 落到真实现（`has_implementation is True`）、`dev` 按 key 分流、三态与两个计数，以及**模型在 worker 线程上被调（线程 id ≠ 主线程）的结构断言**（`68927c27` 收尾轮 20 → 14：按住模型若干秒看归档照涨那批时钟戏收走，结构事实一次调用钉死） |
| `tests/test_transcriber_release.py` | 11 | 会中模型**让出显存**的四条语义（没加载过就 no-op、幂等、有界等待到点放手、释放后才 `empty_cache()`）与收尾接线的顺序：释放排在 `launch()` 之前用**列表顺序**断言、替身后端走防御式取法是 no-op、释放抛异常时 `post` 仍落 `done`；无 `slow` 标记，单文件实测 0.30 s |
| `tests/test_transcript_hints.py` | 11 | 提示级接线落在 `_refresh_transcript_snapshot()` 上且**每次都调判定器**（时长跟着刷）、键集合变化提交一次而时长刷新不推 `revision`、上限挤掉也算变化、health 回 `normal` 与积压回落两条清除路径、0.5 秒节拍自己把提示带进快照、**空闲与结束档 `alerts.hints` 恒为空数组**（离开录音 / 告警态就清、下一场从零） |
| `tests/test_summary_window.py` | 17 | 会中总结的缓冲与窗口切片：`seq` / 副本 / 上限 / 清空 / 多线程、按时间戳切且同一 `ts` 的一批整批同进同出、`full` 收全部、锚点取末行 `ts`、装配文本与来源标签 |
| `tests/test_summary_prompt.py` | 12 | 两条消息的形状、七条硬规则逐条在 system 里、三条固定标签在 user 里、缺口那一句怎么讲、窗口有边界的说法、重试追加提示 |
| `tests/test_summary_parse.py` | 19 | 围栏容忍三步的四种样本（含混数字、含空串、`{"bullets": []}`）、三小段归位与「一条都认不出来」那一档 |
| `tests/test_summary_client.py` | 19 | 请求形状（地址 / 两个头 / payload 四格 / `extra_body` 并入 / `content` 取值）、四档错误、两种注入写法、默认走标准库 |
| `tests/test_summary_settings.py` | 28 | 四种配置来源、归一化（空串与 `None` 同解、认不出的窗口落缺省档）、`missing` 点名（预设模式报 `summary.presets_json[名字].格`）、九个 `summary.*` 键在配置层四处的登记、环境变量那一格、预设清单的取值次序与回落 |
| `tests/test_summary_runner.py` | 23 | 成功 / 空数组 / 空窗口 / 缺键四档收场、解析重试与端点报错当场收工、deadline 是刹车（假钟量）、真钟收敛、每次超时取剩余预算 |
| `tests/test_summary_action.py` | 32 | `summarize` 动作与 `GET /api/summaries`：成功体八格、四档窗口、**四档之外的取值（含空串）一律 `invalid_setting`(400)**、五档失败逐档 `detail` 与判定次序（请求数为 0）、只读（快照九键与 `revision` 不动）、记录只记成功且按先后、`alert` 态可用、总结期间别的请求不被按住、**没注入假件时被 `tests/conftest.py` 的出口兜底拦下**、会中切预设（`settings_update` 两键、快照生效值跟着换、换一套当场走新的） |
| `tests/test_summary_buffer.py` | 12 | 转写行经**生产路径那个 sink** 进缓冲（`bind()` 调两次只装一层）、窗口行数对得上、重新 `bind()` 不清已有行、喂行不涨 `revision`、`start_session()` / `shutdown()` / `_clear_session_summary()` 一处清两笔 |
| `tests/test_offline_audio.py` | 6 | 归档解码：16k / 单声道 / float32 的形状与值域、PCM 与 MP3 两条来源解出同一段波形、**无头裸 PCM 归档（`meeting_audio_<source>_<NNN>.pcm`，一段一个文件）解得开且换算仍是 `/32768.0`**、非 `.pcm` 的容器照旧走 PyAV 探测 |
| `tests/test_offline_segment.py` | 23 | 会后切段：30ms 帧、静默 2.0 秒收段、丢掉 <500ms 的尾段、单段上限、逐段区间与时间戳 |
| `tests/test_offline_engine.py` | 32 | 引擎包装：`post.engine`/`post.dtype` 的档位翻译、逐块调用与块内命名空间、失败如实上报、`release_engine()` 丢引用还显存（`b1c6a58e`） |
| `tests/test_offline_speakers.py` | 12 | 说话人编号：按本路首次出现顺序编 `S01`、跨路限定名不相交、`headcount.class_count` 的口径 |
| `tests/test_offline_artifacts.py` | 45 | 四份产物的字段契约：每路一份会后精细稿与对照表、运行报告、状态文件，字段名与行格式逐条钉住（逐块进度账那一份是另一个文件，见下面 `test_offline_progress.py`） |
| `tests/test_offline_runner.py` | 70 | 整条链的编排：逐路取归档（**段列表**）、数字静音门（单趟与分块两条路）、分块秒数、产物落盘与状态文件终态、**段口径（`6ea32d8d`）**：多段按序拼成一条时间轴、单趟多段拼临时 wav 喂引擎、墙钟改错不改会后结论、**续接（`670d72b5`）**：两段归档的会后链验收 —— 每路两段按序拼成一份 wav 喂（假）引擎、状态文件与报告的秒数 = 两段之和；**打断（`b1c6a58e`）**：旗标在块边界命中整场落 `interrupted` + 缺口注记、起跑前立旗标一路都不跑、打断不通知纪要、`write_interrupted_status()` 对账写点不翻已落定的终态 |
| `tests/test_offline_replay.py` | 18 | `meeting replay` 重放验证（`a3b684bd`，**慢档**）：`--seconds` / `--chunks` 的上限换算与拒绝档、素材截断复制进自己的落地（原目录 digest 树不动）、跑完的三样读数、入口的用法错与退出码映射 |
| `tests/test_offline_progress.py` | 14 | **逐块进度账**（`a9affaa8`）：一块跑完那一块的字与落点就在盘上（含块内 `cNN:` 标签与结果档）、跑到一半时盘上那份账与现场读数对得上、静音跳过的块与失败的块都算跑过了一块而分母不因此增减、不给 `progress` 时一个字节都不落、落盘写不下去也不带走这一趟、半行 / 空文件 / 不存在的文件读得出来、连写两次字节逐字相同且不留 `.tmp` |
| `tests/test_session_status.py` | 14 | `GET /api/sessions/{id}`（`a9affaa8`）：路由注册点、**跑到一半读得到读数**（第 N 块 / 共 M 块、当前哪一路、已跑多久，且不涨 `revision`）、服务重启之后退回读盘那份账（`elapsed_seconds` 如实为 `null`）、目录不在原地 404 `session_dir_missing`、没登记过 404 `invalid_setting`、一次都没跑过时读数如实为空、读它之后快照九键与列表十二键一个字不动；**列表顶层第三格 `post_job`**（`52b24392`）：没在飞如实 `null`、在飞带 `{kind, dir, started_at}` 三格且每行照旧十二键、跑完翻回 `null`；**行里的 `segments` 段读数（`670d72b5`）**：行键数 11 → 12、清单与单场两只端点都带盘上段账原文；**被打断档照盘答**（`b1c6a58e`）：盘上 `interrupted` 的那一场这一格答 `interrupted`，不折回 `running` |
| `tests/test_post_hook.py` | 10 | 会后钩子的解析落点与收尾第 4 步的接线：`auto` 解析到 `PostProcessHook`（本类不带任何钩子方法）、`null` 缺席档记 `skipped`、能力表三项皆假、第 4 步停 `waiting` 且一个会后动作都不启动（`auto` 与 `null` 两档）、释放显存照做（释放抛错如实记 log、真后端确实走这条线）、惰性导入（起线程跑会后链的钩子机器已删，19 → 10） |
| `tests/test_cli_post.py` | 54 | `meeting post` 这条入口：位置参数与 `--force` 的用法档、终端文案与产物绝对路径、归档补齐的六档判据（非空才转、空 PCM 不产、比 PCM 新的不重转、转砸了 PCM 一个字节不动、一路砸不拖住另一路）、编排的注入点与 `run_session` 的模块属性调用、纪要那道闸的四档、等待的上界与落定、再入闸在产品路径上的安静跳过 |
| `tests/test_cli_post_config.py` | 18 | `meeting post` 接配置层：`post.engine` / `post.model` / `post.dtype` / `post.chunk_seconds` 与 `minutes.*` 逐个生效（不再退回默认值）、配置损坏与取值非法两档的退出码与终端说法、用法错误优先于配置、按生效 `log.*` 重建服务级日志（主 log 不可写那一档） |
| `tests/test_post_idempotency.py` | 30 | 幂等闸：连跑两遍时引擎与启动器的调用次数都为 0、PCM 与 MP3 不动、两层各缺一条判据的那几档、`running` 拒跑与 `--force` 的放行、`mark_running()` 在 `launch()` 返回之前落盘（`68927c27` 收尾轮 39 → 31：文件挪慢档，其中 8 条系**删除**非挪——在飞标记时序、双线程 claim 原子性两条并发守卫加其余 6 条等终态/超时观察类，机主裁决不恢复、逐名清单见节点报告；门闸判定走盘上状态组合的表驱动。「两条入口一致性」那 2 条随钩子机器删除，32 → 30） |
| `tests/test_post_actions.py` | 23 | 会后两个按钮（`05a48b6a`，全程假件）：只按「生成会后精细稿」只跑离线链（`minutes=False` 那一趟、纪要一动不动）、只按「生成纪要」只拉纪要层、**都不按就什么都没起**（第 4 步落 `waiting`）、显式 `dir` 的三档校验（不在注册表 / 目录不在原地 / 盘上还在录或收尾）与省略语义、有一趟在飞 → 409、收尾没落定 → 409、两个 hook 缺席 → 404、纪要 claim 闸同步拒与线起不来时把 claim 退回去、`GET /api/sessions` 每行的 `has_offline`、**`post_stop` 四档**（`b1c6a58e`：受理回执带 `stopping`、没有在飞拒、纪要在飞如实拒、`dir` 指到另一场拒） |
| `tests/test_post_reconcile.py` | 5 | 服务启动对账（`b1c6a58e`，**快档**）：在飞记录 + 盘上 `running` → offline 补成 `interrupted`（缺口注记进 `error`）并清记录、盘上不是 `running` 只清记录一个字节不写、minutes 档只清 + log、没有记录不动任何东西、坏记录按空表起不阻塞启动 |
| `tests/test_config_isolation.py` | 5 | 第六道兜底自己的用例：真入口读的是重定向后的家目录而不是机主那一份、下一条用例从「没有生效配置」开始、机主家目录被建出 / 改过时守卫当场判红并打出差 |
| `tests/test_pause_model_lifecycle.py` | 10 | **暂停当场结束会中模型、继续再把它拉起来（`10770b41` / `fix-3`，快档）**：`acquire_gpu()` 四条语义逐条钉住（已经在手就安静 no-op 且不重造第二份权重、释放之后经同一个 factory 造一份新的、造不出来只留一条 warning 而继续这个动作照常、不碰在跑的流水线与 `health()`）；暂停窗跨一次释放与一次重拉，那 1.2 秒连续零一段都进不了识别器，继续之后经新造的那一份照常出段；两次动作的提交按按下顺序落到 release / acquire、幂等档不重复交；替身把两次调用各按住 3 秒而两个动作仍 < 0.5 秒返回；`transcriber.backend = auto` 那一档两个方法确实被喊到；替身后端缺这两格照样暂停与继续；模型调用抛错不让按钮失败 |
| `tests/test_minutes_chunk.py` | 23 | 按行切块与区间表：块文件里只有原始行（不加头）、`chunk_NN.md` 的两位数编号、闭区间与 `lines` 三者自洽、重跑先清本源旧块、只动 `chunks/` 里 `chunk_*.md` 这一族、`chunk_lines` 取值域、按名字找两条转写、逐 source 判档与两路合并后的公共档位、**两份稿各切一套落在 `working/<source>/<kind>/`、缺席那份不切（不伪造空块目录）** |
| `tests/test_minutes_contract.py` | 62 | 分段方案的契约校验：必填与可选字段的类型、连续性按 source 各判一遍（跨 source 不判）、段 id 重复、末段与真实行数对齐（**分母跟在场那份稿走：会中稿在按会中稿，只有会后稿按会后稿**）、`total_lines` 两种合法形状与各自的拒收档、`max_segments`、三种能分辨的读盘错误，以及**在全新解释器里 `import meeting.minutes` 一条重依赖都不拉** |
| `tests/test_minutes_prompt.py` | 81 | 提示词渲染：占位符六处必写缺一处就抛错、`{{PHASE_BODY}}` 可省（两块标记就地替换正文）、对不上号的占位符、路径一律正斜杠且相对场次目录、**清单与说明段逐份稿各写一条（两份都在出两行，谁也不叫"权威"）**、旧的单稿布局做兜底 / 双稿在场时拒收旧索引、逐 source 的档位说明段、模板缺席与空清单不渲染、**`{{SPEAKER_NAMES}}` 的渲染三档（`91b6752e`：确认过的名字拼表照抄、没有确认过的如实渲染空态正文，人名表缺席 / 坏表都不挡渲染）**；**模板正文带着三处会议资料口径**（`e8fe7da9`：逐段总结把相关资料读进总结、合并扫一眼资料补漏、只读检查对资料核——对真模板的在场检查，有料与没料的场次同一份模板、没有「参考资料」一节） |
| `tests/test_minutes_launch.py` | 41 | 启动器与产物四条判据：argv 只带短指针（正文落 `working/run_prompt.md`）、子进程环境那三格、`CREATE_NO_WINDOW`、四档失败的名字与消息、日志头三行、附录判据（表头看宽度不看第一格的字面值、只认附录那一节里的行） |
| `tests/test_minutes_evidence.py` | 6 | 那一次实测的证据脚本（`.work/nav/nodes/41465462-…/phase2/evidence/run_evidence.py`）**自己写 `evidence.log`**（stdout 全文、UTF-8）：证据目录与 `MINUTES_EVIDENCE_DIR` 的优先级、脚本缺席时带着路径与补救办法跳过、拒绝多余参数。脚本不在盘上（`.work` 被清理）时这几条**跳过**，跑不到不是红的 |
| `tests/test_minutes_dsh_web.py` | 19 | web 分支的交付层（`minutes/dsh_web.py`）：自签 cookie 的名字按 authority 算且签名能验回来、凭据读不到 / 密钥长度不对当场报错、存活探针只打一次只读的 `session/modelCatalog`（401 / 端口没人听 / 签不出 cookie 三档都交回「没在跑」且说明是实话）、交付三步的次序与信封（`payload.args.request`、会话只带 `workspaceId`、提示词带 `requestId` 与 `queue`）、两份盘上记录、五个失败档各归各的、四小时那个预算不许变成一次 HTTP 调用的超时、连接层抖动重试而 401 / 404 / `ok=false` 一次就交回 |
| `tests/test_minutes_delivered.py` | 13 | `delivered` 这一档的流转：探到 DSH web 服务端在跑就走交付（终态 `delivered`、`branch=web`、**不校验产物**，交付那一刻盘上已经是 `running`）、没在跑 / 探针炸了都落回 headless、交付失败如实记 `failed` 不改走另一条路、子会话闸连探针都不问、状态读得回、读方按产物判完成而按 `delivered` 判「已经交付过」（`_minutes_gate()` 与 `_announce_minutes()` 都不拉起第二个会话）、退出码那一档按成功算 |
| `tests/test_minutes_runner.py` | 32 | 一次运行的编排：再入闸当场返回 `skipped` 且不写状态文件、拉起之前先落 `running`、一条输入都没有时记 `failed` 且不启动 DSH、**两份稿各切一套各落各的目录（缺席那份不伪造）**、切块与渲染失败各记 `failed`、成功档覆盖成 `done`、超时与退出码非 0 的转发（启动器全程是假实现，一条都不真调系统上的 `dsh`） |
| `tests/test_speaker_names.py` | 20 | 人名表（`meeting/speaker_names.py`，快档）：写读往返与原子落盘（不留 `.tmp`）、键形状与空名字的写侧拦截（两路不撞、跳过 = 不落任何状态）、坏表 / 未来主版本的读侧宽容（交回「没有」不挡下游）与写侧拒写（表在盘上但读不出时不许整份覆盖）、`identity_rows()` 身份行三档来源——有编号带次数读数、无编号给本地 / 远端 |
| `tests/test_minutes_registry.py` | 11 | `resolve_minutes_hook()`：`auto` 落 `MinutesHook`、配置里写 JSON `null` 落缺席档（不退回 `auto`）、写字符串 `"null"` 同解、`module:Class` 显式取值、解析失败回退替身且 `has_implementation = false` |
| `tests/test_capture_devices.py` | 33 | 设备模型与挑选策略（纯逻辑、注入假端点表）：有波形 / 全零 / 开不了三档、`CABLE Output` 抓取型端点挡拦（含 WDM-KS 的 `VB-Audio Point` 写法）、host API 排序、MME 截断名的前缀兜底与**精确名优先**、收尾两档收敛（一条都没打开 → `probe_failed`）、没配名字时种子取**系统默认输入端点**（跟着 `is_default` 走）、loopback 三级（两个子串判据逐字照 V1 的 `in`） |
| `tests/v1/test_repeat_filter.py` | 10 | V1 那套重复循环过滤的回归（`tests/v1/` 是**整份副本**，`fixtures_real_loops.py` 逐字照抄、本文件只改 import）：两条真实故障行（周期 7 与 5）必须被砍到 <60 字符且只留一组、循环之前的前导正文保留、周期上限调回 4 就立刻红、正常行与合法口语重复一行都不许动、token 级判环与「判环后把 EOS 抬到最高分」 |
| `tests/test_parallel_safety_nets.py` | 20 | 并行档两道安全网的适配（`68927c27`，快档）：看门阈值的环境变量覆盖口径（不设 = 缺省 5.0、正数生效、垃圾值从严退回、两个钩子一红一放行各一条）、xdist 收场复查搬运（worker 把两道守卫的红账写成 run 级上报文件、controller 汇总打红线并把整趟判红、无上报时安静、串行趟搬运口零动作）、`_xdist_dist_active()` 判据三态（没装 xdist / 带并行参数 / 串行参数） |

除这些，`tests/` 下还有**测试支持**（不被 `pytest` 收集）：`tests/_helpers.py`（真服务子进程与假浏览器等工具）、`tests/_service.py`（真服务子进程驱动件 + SSE 读帧与等待助手）、`tests/_drain_fakes.py`（收尾与关停用例共用的假后端 / 假钩子，与工厂辅助一起挂进子进程）、`tests/_capture_faulty.py`（端到端专用的「问题采集后端」挂件：真采集后端 + 假端点表 + 内容带块号的假源，旋钮走环境变量，经配置键 `capture.backend = "tests._capture_faulty:FaultyCaptureBackend"` 挂进进程内真运行时）、`tests/_dsh_web_fake.py`（假 DSH web 端点：一件真 HTTP 服务起在回环的临时端口上，照抄本层用到的那三条端点的形状、逐条记下请求，`session/modelCatalog` 那个只读探针也在登记表里）、`tests/_contract.py`（`contract/fixtures/` 的采集、掩码与落盘比对；它按需写出一个假转写后端，经 `PYTHONPATH` + `transcriber.backend` 的 `module:Class` 挂进子进程）、`tests/__init__.py`（`tests/` 的包标记，跨文件取用走包路径 import）、`tests/conftest.py`（**只放 fixture**，其中**七道** autouse 的兜底：一道把 `webbrowser.open` 换成只记录不开窗的假替身，一道 `_no_real_audio_capture` 把真采集入口打成「一碰就炸」的桩 —— `sounddevice.InputStream` / `rec` / `check_input_settings` / `soundcard.get_microphone`，异常类 `RealCaptureForbidden` 不继承 `Exception` 因此不会被生产代码的 `except Exception` 吞掉，只读枚举不禁；第三道 `_no_real_minutes_session` 把 `MEETING_MINUTES_CHILD=1` 立起来，让离线链收尾处那次 `minutes.hook` 通知当场走再入闸 —— 本机 PATH 上真的有 `dsh`，不立这一格，每一场跑完整条离线链的用例都会真拉起一个 headless 子会话去跑五阶段；第四道 `_no_real_model_call` 盯**会中总结那条 HTTP 出口**（`meeting/summary/client.py` 的 `_http_post()`）：`urllib.request.urlopen` 按目标地址判，本机回环放行、其余当场抛 `RealModelCallForbidden` —— `summary.base_url` 的配置默认值就是真地址，忘了注入假客户端就会真花机主的 token；**第五道是 PATH 守卫**（会话级的 `_no_real_dsh_on_path` 与用例级的 `_no_real_dsh_launch` 两条夹具）：在临时目录里造一件假的 `dsh.cmd`（只往账本追加一行）并前置到测试进程 `PATH` 的最前面，于是漏网的用例命中的是它、真会话起不来，用例级那一条再拿账本把「这一趟真的叫了它」当场判红（要故意走到可执行文件解析那一步的用例，显式请 opt-out 夹具 `allow_real_dsh_resolution`）；**第六道 `_no_writes_to_the_real_home`** 开工记下机主真实家目录下那个配置目录（`~/.config/meeting`）的文件集合与逐个 sha256，每一条用例收工与整场收场各复查一次，被建出或改过就当场判红并打出差（含收工时那份文件的内容）；**第七道 `_no_real_dsh_web_call`** 把 `meeting/minutes/dsh_web.py` 唯一那个 HTTP 出口 `_post_json()` 包起来 —— 打向用例自己起的那些假端点（本机回环、端口由系统挑）原样放行，打向 3080 那台真服务的一律当场抛 `RealDshWebCallForbidden`：`minutes.dsh_url` 的配置默认值就是那个真地址，少了这道网，一条忘了注入假件的用例会在机主开着 DSH GUI 时往那台服务上建真工作区、真会话、发真提示词；第四道 `_no_real_model_call` 盯**会中总结那条 HTTP 出口**（`meeting/summary/client.py` 的 `_http_post()`）：`urllib.request.urlopen` 按目标地址判，本机回环放行、其余当场抛 `RealModelCallForbidden` —— `summary.base_url` 的配置默认值就是真地址，忘了注入假客户端就会真花机主的 token；另注册 `--regen-contract` 开关）、`tests/_fake_webbrowser/`（替掉标准库 `webbrowser` 的假模块）、`tests/v1/`（V1 那套回归的整份副本：`fixtures_real_loops.py` 逐字照抄，`test_repeat_filter.py` 只改 import，那两条真实故障行因此不依赖会议目录就能复现）。

**契约 samples 的重生成开关**：`python -m pytest tests/test_contract_fixtures.py --regen-contract -q` 重写 `contract/fixtures/`，不给开关就是逐文件比对（改了服务行为就重跑它并复核差异）。规则与出处见 `contract/README.md`。

**每一条用例都有一道自动看门**：`tests/conftest.py` 汇总 pytest 自己报的 setup / call / teardown 三段耗时，**合计超过本趟阈值就把这条用例判红**（红在它自己的名字下，报出三段各自的耗时）。阈值缺省 **5 秒**（`CASE_WALL_CLOCK_LIMIT_SECONDS`）；并行档（`pytest-xdist`）经环境变量 `MEETING_CASE_WALL_CLOCK_LIMIT_SECONDS` 放宽（本机并行趟实测最忙一条合计 2.98 s，文档口径取 8.0 留约 2.7 倍余量），**不设变量 = 缺省 5.0，串行趟与从前逐字节一致**；变量读不出来（非数字 / ≤ 0 / 空串）一律从严退回 5.0。看门只读 pytest 的 report，不额外计时、不加 fixture。

这条红线牵着测试侧的等待窗口：静默阈值、收尾那一步的阻塞时长、端到端的观察窗口都按它收口过（例：一条用例的静默阈值取 1 秒、把某一步钉在 `running` 的阻塞取 0.6 秒、端到端的观察窗口取 1 秒）。**被测的语义没有跟着改** —— 收尾预算仍是 30 秒起步、告警默认阈值仍是 180 秒，改的只是测试自己等多久。

测试的隔离手段：**重定向家目录**（`HOME` / `USERPROFILE` 都指向临时目录），配置与日志因此整套换落点；**不新增 `MEETING_*` 变量**。真服务一律用 `[sys.executable, "-m", "meeting", "web"]` 起子进程，地址从 `MEETING_PORT=0` 加子进程打印的 URL 行解析出来（**不猜端口**）。采集后端由用例自己的 `config.json` 的 `capture.backend` 表达，子进程 env 里不塞 `MEETING_CAPTURE`（它的优先级更高，会静默压住那条配置键）。ASGI 层用 `with TestClient(app, base_url="http://127.0.0.1") as c:`（`base_url` 把回环 `Host` 带上，只有 `with` 会触发应用的生命周期钩子）。

### 7.1 快慢分档（`slow` 标记）

全量一趟约 **130 秒**（串行实测 128.49 s），其中九成时间花在真子进程与真墙钟等待上。因此按**文件粒度**分成两档，
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
pytest 把「sloww」当成一个谁都不带的标记，表达式于是恒真、1440 条全被选上 —— 结果是悄悄跑了
全量（慢，但不假绿）。正向拼错由退出码 5 兜住，否定式拼错由这道 `--strict-markers` 兜不住。

**归位判据**（一条用例满足任一条就进慢档）：

1. 起**真服务子进程**（`[sys.executable, "-m", "meeting", "web"]`）；
2. 依赖**真实墙钟等待**才能得出结论（收尾预算、100ms 节拍、抑制窗口、恢复窗口）；
3. 注入**「慢模型」**（睡很久 / 会抛错）来证明解耦或存活；
4. 单条用例实测超过 **2.0 秒**。

**只能落在文件粒度**：`tests/conftest.py` 那道看门（`CASE_WALL_CLOCK_LIMIT_SECONDS = 5.0`）管的是
**逐条用例**的 `setup + call + teardown` 之和压在 5 秒之内，最慢的一条也才
3.40 秒（`tests/test_drain.py::test_all_log_event_categories_findable_before_end_marker`），
「慢」来自文件层面的真子进程与真墙钟等待。**慢档现在是 24 个文件级标记 + 一批用例级标记**（账目：收尾轮 14 个 − 整删的 `test_pause_actions` + 新获文件级标记 10 个 = 23；续接节点 `670d72b5` 加 `tests/test_resume.py`，24）
（`68927c27` 收尾轮起：shutdown / port / heartbeat / state_machine / config_isolation /
minutes_dsh_web 里「真子进程 / 真阈值等待」的用例改按用例级 `@pytest.mark.slow` 进慢档，
文件本身不再整体带标记）。文件级判据是 `Select-String -Path tests\test_*.py -Pattern 'pytestmark = pytest\.mark\.slow'`，
实测 24 个；**路径要写成 `tests\test_*.py`**：不加这一层的时候 `tests\conftest.py` 的文档串里同名的那句话也会被数进来、读出 25 个。`-m "not slow"` 与 `-m slow` 两条收集口径分别是 `1031/1440` 与 `409/1440`，加得起来）：

| 慢档文件 | 单文件实测 | 慢在哪一档判据 |
|---|---|---|
| `tests/test_drain.py` | 15.56 s | ② 真墙钟等待 + 预算边界（收尾轮 32 → 29 条；历史读数，本轮未单跑） |
| `tests/test_offline_runner.py` | ~15 s | ② 真归档链 × 63 条（`68927c27` 收尾轮整文件挪入慢档：`meeting post` 的承重契约归收尾验收） |
| `tests/test_cli_post.py` | ~15 s | ① `meeting post` 真入口 × 54 条（`68927c27` 收尾轮整文件挪入慢档） |
| `tests/test_dev_backend.py` | 0.39 s | 收尾轮 15 → 3 条后的实测（基本盘：确定性 / finalize 记账 / 注入校验；3 秒故障窗口时钟戏删） |
| `tests/test_transcriber_backend.py` | 5.70 s | ③ 模型注入与协议（收尾轮 20 → 14 条后实测；时钟戏换成「模型线程 id ≠ 主线程」结构断言） |
| `tests/test_shutdown_signal.py` | 8.70 s | ① 真服务子进程 + `CTRL_BREAK_EVENT`（子进程用例现按用例级标记；历史读数） |
| `tests/test_asr_pipeline.py` | 10.19 s | ② 真归档 + 真线程 + 收尾预算（收尾轮删 1 条形状测试；历史读数） |
| `tests/test_port_policy.py` | 7.73 s | ① 真服务子进程 + 端口探针（子进程用例现按用例级标记；历史读数） |
| `tests/test_capture_engine.py` | 6.77 s | ② 真读写线程 + 打点线程 |
| `tests/test_api_actions.py` | 6.65 s | ② 进程内真运行时（31 条各起一次真应用 + lifespan，含自 test_pause_actions 收编的动作接线 1 条） |
| `tests/test_summary_action.py` | 5.99 s | ② 进程内真运行时（31 条里 30 条各起一次真应用 + lifespan；单条最长 1.22 s） |
| `tests/test_capture_e2e.py` | 3.88 s | ① 真运行时 + `ffprobe`（收尾轮 3 → 2 条后实测：删 10 分钟 PCM 吞吐外推） |
| `tests/test_capture_segments.py` | 3.93 s | ② 真墙钟等待（假源按 0.1 秒一块的节奏喂）+ 进程内真运行时 |
| `tests/test_resume.py` | 1.2 s | ② 真墙钟等待（假源喂块）+ 进程内真运行时 + 真收尾 ffmpeg 转码（续接验收，`670d72b5`） |
| `tests/test_web_missing_dist_e2e.py` | 3.49 s | ① 真服务子进程 + 真墙钟（`Ctrl+C` 之后等它退出） |
| `tests/test_summary_buffer.py` | 1.40 s | ② 进程内真运行时 |
| `tests/test_sse.py` / `test_multi_client.py` / `test_capture_backend.py` | 3.94 / 3.55 / 3.23 s（并行 busy） | ①② 真流式 / 真后端（`68927c27` 收尾轮整文件挪入慢档；busy 读数来自并行档 `--durations=0` 聚合） |
| `tests/test_e2e_no_clients.py` / `test_e2e_multi_client.py` / `test_e2e_silence_alert.py` / `test_contract_fixtures.py` | ≈8.6 / 2.63 s（并行 busy） | ① 真服务 + 真订阅者、37 golden 样本采集（`68927c27` 收尾轮挪入慢档；样本数现按本轮口径） |
| `tests/test_post_idempotency.py` | ~5 s | ① `meeting post` 幂等闸与真入口（收尾轮挪入慢档；其中 8 条删除非挪，见逐文件表） |

**`tests/test_transcript_hints.py` 不在这张表里**，它也没有 `slow` 标记（实测：这个文件里搜不到 `pytestmark`，它 11 条用例全在快档里跑，单文件 1.84 s）——它满足判据 ②，但那一条端到端用例只等 0.5 秒节拍，落在快档不拖累谁。**碰了转写提示级那一路时仍然要跑它**（`python -m pytest tests/test_transcript_hints.py -q`），别因为「表里没有」就跳过。

上表那些单文件读数是各自单独跑一次量的（含解释器冷启动与真应用启动的固定开销），**不随某个文件被挪进挪出慢档而变**；改完代码之后要刷新哪几个数，就按同一口径单跑一遍那个文件。

**命令与实测耗时**（现行一组数，`91b6752e` 收尾轮量得；收集口径 `1440` 条，与本章开头对得上）：

| 命令 | 用例 | 实测 |
|---|---|---|
| `python -m pytest -q -m "not slow" -n auto --dist loadfile`（快档并行，每圈这条；先把看门放宽那格 env 立起来） | **1027 passed, 4 skipped** | **7.66 s**（红线 ≤10 s 内） |
| `python -m pytest -q`（全量串行，收尾这条） | **1436 passed, 4 skipped** | **128.49 s** |
| `python -m pytest -q -m slow` | **409**（收集口径） | 慢档未单跑墙钟（随全量那一趟覆盖） |

全量并行（`-n 12 --dist loadfile`）本轮未单跑；worker 钉死 12 的道理（16 个会把 shutdown 的真子进程信号用例饿到超时）写在上面命令块的注释里。

**不加 `-m` 时跑全量**，这一点是有意的：新写的用例默认落在快档，只有被显式标 `slow` 才跳过。
配合上面的**快档红线**（新用例无 sleep / 无真子进程 / 毫秒级纯逻辑才许进快档），漏跑的风险两侧都被压住：
快档不会被慢用例拖慢（进不来），慢用例也逃不过收尾那一趟全量。离线契约那一族
（`test_offline_artifacts.py` / `test_offline_engine.py` / `test_offline_runner.py` /
`test_offline_segment.py` / `test_offline_speakers.py` / `test_offline_audio.py` /
`test_post_hook.py`）与 `test_contract_fixtures.py` 的 41 个 golden 样本漂移比对、
`test_cli_post.py` / `test_post_idempotency.py` 两条 `meeting post` 入口，在 `68927c27`
收尾轮一并归入慢档——它们是收尾功能的验收，跟着收尾那一趟全量跑。

**将来某个快档文件里出现一条真子进程用例**：那一条单独加 `@pytest.mark.slow` 即可，不必把
整个文件挪进慢档。

## 8. 现在能做什么 / 还接不上的部分

**能**：起停、跑完启动序列、交 `/`（产物或说明页）、**接通了的 `/debug` 自检页**、`/api/state` 的完整快照、`/api/events` 的三通道 SSE、`/api/heartbeat` 的真值、`POST /api/actions` 的十六个动作（含停掉正在跑的精细稿那一只 `post_stop`，b1c6a58e）、`GET /api/summaries` 的本场总结记录、**工作台收编（e2f6166b）**：工作区注册表（`meeting/workspace.py`，住配置目录 `workspaces.json`）、`workspace_open` / `workspace_remove` 两动作与 `GET /api/sessions` / `GET /api/dirs` 两只纯读端点、`start` 开成一场后刷新注册表的 `last_opened_at` 与 `recent`、空闲态的工作区首页（`web/src/home/`，左侧清单（行四态）+ 目录选择器 + 非空收编 / 删除确认弹窗 + 选中那一场的会后两个按钮与会后进度区（轮询 `GET /api/sessions/{id}`，可展开收缩、逐块行往下刷、每路一条进度条）+ 两处顶栏的「显卡忙不忙」一格，`52b24392`；对录过的行顶栏按钮叫「接着录」、详情里摆各段的时长与时间（`670d72b5`））、四态与全部迁移、场次目录与原子写 `session.json`、**续接（`670d72b5`）**：对录过的工作区按 start = 同一场会再录一段 —— 旧段归档与临时稿一个字节不碰、段账与权威时长合并着写盘、转写开场各路落记号行并推 `gap` 行、续接失败旧记录原样写回、`GET /api/sessions` 两只端点的行带段读数、`stop` 的四步收尾跑到终态（含自适应预算、超时如实上报、失败后两条出路）、`Ctrl+C` 的两阶段语义与五档退出码、场次 log 的挂上与摘下、告警判定与静音、`settings_update` 的原子写、**设置抽屉（`311f84f5`）**：`GET /api/devices` 纯枚举设备清单、抽屉六行（两设备键位 / 告警出声 / 心跳频率 / 总结缺省档 / 总结模型预设）就地写进 config（都收 `HOT_KEYS` 热读档，写盘那一刻快照就换新值，设备改完 `reprobe` 按新值挑，换一套模型预设会中当场生效）、**设备枚举与试开（`devices.mic` / `devices.loopback` 两个键位与 `reprobe` 动作）**、**真实双路采集与归档**（`capture.backend` 默认值 `auto`：两路各自独立读写、边采边落盘 16k / 单声道 / int16 裸 PCM（**按段落盘**：`meeting_audio_<source>_<NNN>.pcm`，一段 = 一次录制）、每 0.5 秒一帧 dBFS 电平事实、**引擎层的暂停/继续与「关一路」**（`6ea32d8d`：暂停写静音不录内容、不切段；关一路写静音、不报 `SourceLost` —— HTTP 动作入口是 `pause` / `resume` / `set_source` 三个动作与界面上那两个位置，`10770b41`；这两个动作还各让会中模型退出显存 / 回到手上，`fix-3`）、收尾 `finalize()` 停流落盘 + `transcode()` 逐段出 64k MP3）、**真实转写与会中临时稿**（`transcriber.backend` 默认值 `auto`：两路各一本 `meeting_transcript_<source>.md`、一行 `[HH:MM] 正文`）、**会后离线转写与说话人聚类**（`post.hook` 默认值 `auto`，由「生成会后精细稿」按钮按需拉起：两路各一份 `meeting_offline_transcript_<source>.md` 与 `meeting_offline_speakers_<source>.json`，外加一份运行报告与一份状态文件，都落进场次目录；**每跑完一块往 `meeting_offline_progress.jsonl` 补一行**，跑到哪一档由 `GET /api/sessions/{id}` 回答）、**会后纪要生成**（`minutes.hook` 默认值 `auto`，由「生成纪要」按钮按需拉起：拉起一个 headless DSH 子会话按五个阶段出一份 `meeting_final.md`，正文之外带说话人附录）、dev 替身（采集合成电平与三档故障注入 / 转写合成行，按 key 分流）、仓库根的简版 `README.md`、**契约看守**（`python -m pytest tests/test_contract_fixtures.py -q` 比对 `contract/fixtures/` 的 41 个 golden 样本，`--regen-contract` 重新生成；同一批样本在 `web/tests/` 里由 `npx vitest run` 再验一遍类型层）。

**下面这几条是已经接通的细节；其余按「能力还没进来」理解，服务本身照常跑**：

- **真实采集已经接通**（`dca4539b`；分段与静音机制是 `6ea32d8d`）：`capture.backend` 的默认值 `auto` 解析到 `meeting.backends.capture:CaptureBackend`，它做三件事 —— 枚举端点并以 16k 试开、读回样本（两路各一条 `ready` / `probe_failed`）；按 `mode` 起两路**各自独立**的采集（一路出事另一路照跑，起不来的那一路发一条 `SourceLost`，本场该采的路全起不来才回 `503 capture_unavailable`）；把 16k / 单声道 / int16 的裸 PCM 边采边写进场次目录的 `meeting_audio_<source>_<NNN>.pcm`（NNN 三位零填充、从 1 起；**一段 = 一次录制**，段号 = 各路盘上实有最大段号 + 1，续接接着已有段号编新文件、旧段一个字节不碰），每 0.5 秒往服务报一帧 dBFS 电平事实。**暂停与「关一路」的引擎能力**（`6ea32d8d`）：`pause()` / `resume()` 暂停期间把真音频在写侧**等长替换成数字零**（不录内容；不切段，暂停只进段账的 `pauses` 区间，暂停窗不产生转写行），`deactivate_source()` 让被关的那一路改写静音、**不报 `SourceLost`、不进告警**；`mode` 没选中的那一路整段写静音填充，两路时间轴始终等长。这些能力做在引擎与服务层；HTTP 动作入口（`pause` / `resume` / `set_source`）与界面上的那两个位置由 `10770b41` 接上。收尾第 1 步 `finalize()` 停流 + join + 关文件 + `fsync`（顺手从写侧的内容字节结出**权威时长**），第 2 步 `transcode()` 把各段 PCM 逐段转成 `meeting_audio_<source>_<NNN>.mp3`（`pydub` + `ffmpeg`，64k）。**转码成功之后 PCM 照样留着**（`session.json` 的 `sources[*].archives` 段文件名表声明的是它们）。
- **真实转写已经接通**（`9e5db89c`；按段尾读是 `6ea32d8d`）：`transcriber.backend` 的默认值 `auto` 解析到 `meeting.backends.transcribe:TranscriberBackend`，它把 `meeting/asr/pipeline.py` 那条流水线包成协议实现 —— 两路各一条读线程**按段序号游标尾读**场次目录里的 `meeting_audio_<source>_<NNN>.pcm`（新场从段 1、续接从盘上实有最大段号接起，逐段接续，拼起来就是连续时间轴；旧段一个字节不重读，只看新字节、不碰采集层的对象）→ VAD 切段（16k / 单声道 / int16，帧长 30ms、静默 2.0 秒出段、单段硬上限 `asr.max_segment_seconds`）→ **入队即盖采集时刻** → **一个** worker 顺序识别（`meeting/asr/glm.py` 的 GLM-ASR-Nano，带四层退化防护：token 级判环、墙钟超时、噪声预筛、文本级剥离）→ 每路一本会中临时稿 + 一条 SSE `transcript` 行。落盘格式与占位符见 `docs/state.md` 第 3 章。**转写坏了不影响录音**：起步失败只落 `health = stopped`，采集、电平、告警照跑；`shutdown()` 与换场都会同步等一次它的收尾（上界见第 2 章那段）。
- 上面这条流水线在本机实测过的几个数（口径逐一写在括号里）：
  - **双跑对照**（V1 那场事故会议的 GLM-ASR-Nano 素材、10 秒段）：关掉防护裸跑，三个真实触发点（`P4-vac-1836` 的 seg3 / seg7、`C1-A-normal` 的 seg23）分别产出 **2670 / 3996 / 2021 字**、耗时 **36.1–36.9 秒**、顶满 2000 token、循环覆盖率 0.977–0.988；四层防护全开之后同样三段落到 **87 / 85 / 59 字**、**0.75–1.09 秒**。裸跑那三段与 V1 当年记录的三行**逐字节相同**（sha256 前 16 位三对相同）。
  - **文本级过滤器**在事故会议全 **411 条**正文上命中 **2 行**、误杀 **0 行**（那两行的真实循环覆盖率 0.9954 / 0.9931，第 3 名按只算可剥离行是 0.1130 —— 与 V1 记录值一致）。
  - **实时率**：本机 10 秒段单段耗时中位 **0.58 秒**、RTF 中位 **0.058×**（这一档是 10 秒段的口径；V1 记录的 `0.39×` 出自**生产路径探针** —— 整场重放、10 秒段正常识别 3.1–4.2 秒，两个数口径不同，不能直接相减）。
  - **时间戳**：把 530 秒素材按 1× 真实时间轴喂进整条流水线，**53 行全部配对**，偏差中位 **0.0 分钟**、最大 **1.0 分钟**；压力档（积压峰值 29–30 段、排队延迟最大 80 秒）偏差仍是中位 0.0 / 最大 1.0 —— **滞后不随积压增长**。
- **会后那一套现在由人按**（`05a48b6a`，R26 / `docs/design-intent.md` 第 10 条）：按「结束录音」之后**一个会后动作都不自动跑** —— 收尾第 4 步 `post` 只做一件事（让会中模型退出显存），做完就如实停在 `waiting`（＝界面上那句"等你动手"），`ended.phase` 仍判 `done`（前三步落定、无失败步）。会后两个按钮是 `POST /api/actions` 的 `post_offline`（**生成会后精细稿**：只跑离线那一条链，`run_session(..., minutes=False)`，纪要一动不动）与 `post_minutes`（**生成纪要**：把这一场手上有的稿子交给纪要那一层，**不补跑离线**）。两条各自起一条 daemon 线程、起完即回，跑成什么样由场次目录里的状态文件回答，**跑到哪一档**（第几块 / 共几块、正在跑哪一路、已经跑了多久）由 `GET /api/sessions/{id}` 回答。**正在跑的精细稿能停**（`b1c6a58e`，R28）：`post_stop` 立起取消旗标即回，线程在块边界收场、盘上状态写成 `interrupted`，重按按钮从头再跑；服务启动时按在飞记录（`post_jobs.json`）把「进程死了还停着 running」的那一场对账成 `interrupted`（`meeting/service/postjobs.py`）。`post.hook` / `minutes.hook` 仍是那两个实现的解析键（`auto` / `null` / `dev` / `module:Class`）：解析不出实现时对应的按钮当场回 404 `action_unavailable`。产物（每路一份会后稿与对照表、一份运行报告、一份状态文件、一份逐块进度账）见 `docs/state.md` 第 3 章。收尾第 2 步的闸门只看**采集侧**（`meeting/service/runtime_drain.py` 的 `_drain_transcode()` 看 `capture_resolution` 与 `capture_capabilities["transcode"]`）：transcode 是采集后端的能力，转写后端在不在场与它无关 —— 真采集那一场两份 MP3 在收尾里自动出现，`null` / `dev` 替身那一场这一格记 `skipped`。
- **会后纪要那一层**（`41465462`；由「生成纪要」按钮（`post_minutes`）或 `meeting post` 按需拉起，见下文「会后由谁拉起」）：`minutes.hook` 的默认值 `auto` 解析到 `meeting.backends.minutes:MinutesHook`，它起一条 daemon 线程、返回即算启动成功，跑多久都不占收尾预算。这一层炸了不带走进度更早的产物（`meeting_offline_status.json` 与那四份离线产物一个字节都不改，只记主 log 一条）；`minutes.hook = null` 与环境里的 `MEETING_MINUTES_CHILD=1`（DSH 子会话自己）两档都安静跳过。产物是场次目录根的 `meeting_final.md`（纪要正文 + 说话人附录）与 `meeting_minutes_status.json`（`running` / `done` / `failed` / `delivered` + 档位与实际走的启动分支），中间产物在 `working/` 下。**收尾四步没有增加第五步**。它吃什么、经哪几步出一份纪要、产物四条判据与三个手动子命令（`python -m meeting.minutes check` / `chunk` / `prompt`）见 `docs/minutes.md`。
- **纪要这一层的两条分支**：拉起之前先探一次 `minutes.dsh_url` 指的那台 DSH web 服务端（`meeting/minutes/dsh_web.py` 的 `probe()`：先 TCP，再发一次只读、无参数的 RPC 确认协议与鉴权）。**在跑**走 web 分支：经 `/api` 三步（`workspace/create` → `session/create`（只带 `workspaceId`）→ `session/prompt` 送提示词全文）把这一场交付给服务端，**投递 accepted 即算交付完成**，状态文件的终态记 `delivered`；**没在跑**才落回本进程自己起的 headless 子进程（等它退出并按四条判据校验产物，终态 `done` / `failed`）。V1 那套 web 路径在 DSH 0.1.5-rc.2 上的几处实测口径（端点写斜杠形式、一元方法 `workspace.list` 不存在、信封要包一层 `args`、`/api` 只认 HMAC 签名的 Cookie）这一层都按实测绕过去了，坑的实测口径写在 `docs/minutes.md` 第 4 节；`branch` 那一格记的是**实际走的**那一条（拉起方建线程之前落的那一版 `running` 记配置里写的那个词）。
- **会中总结也已经接通**（`1987cb20`）：动作 `summarize` 把内存里那份实时临时稿按行首时间戳切一段窗口（四档 `5m` / `15m` / `30m` / `full`，缺省 `15m`）交给一个 OpenAI 兼容端点，回三小段（刚才在讲什么 / 讲到哪儿了 / 丢掉了什么），十到十五秒出结果。结果走**应答体**（一过性，不进快照、不落文件），本场按过的那几次由 `GET /api/summaries` 从内存里读回来。端点由九个 `summary.*` 键配（缺省档 `summary.default_window` 与预设两键 `summary.presets_json` / `summary.active_preset` 收热读档，写盘一刻快照就换 —— `311f84f5` 与会中切模型节点；其余六键归 `pending_restart`，密钥那一格可以走 `MEETING_SUMMARY_API_KEY`）；没配齐就当场回 `502 summary_failed` 并点名缺哪几个键，转写没在跑则回 `503 transcript_unavailable`。这一层怎么切窗口、提示词与模型输出契约、deadline 与重试的预算见 `docs/summary.md`。
- **端口被占时那句文案里的「已录多久」**：占用者是本服务时，括号里眼下是**字面写死的「当前空闲」**（`meeting/service/app.py` 的端口探测那一处还没取用会话时长这个值）。`README.md` 与 `docs/api.md` 都照这个现状写。
- **真机真开一场会**：设备枚举与真实电平只有真开一场才见分晓，这一条是**手工验收**，四条端到端脚本与它无关。自动化边界归下游 `46ae4a08`。
- **`meeting post` 已经接通**（`65dd3e4c`）：`meeting post <场次目录> [--force]` 只跑会后那一趟，不开界面、不起服务。一场跑下来做三件事，各层的结论与产物的绝对路径都打到终端：

  - **音频归档补齐**（`sync_archive()`）：两路各自独立判、独立转，判据与收尾第 2 步一致 —— 该路 PCM 非空且（MP3 缺席 或 MP3 比 PCM 旧）才转，空 PCM 不产 MP3、已比 PCM 新的 MP3 不重转；转码失败如实记 `failed`、**PCM 一个字节都不动**；转码实现只有一份（`meeting/capture/archive.py` 的 `convert_to_mp3`）。
  - **会后离线那一趟**（`meeting/offline/runner.py::run_session()`）：产物与状态文件的终态见 `docs/state.md` 第 3 章。
  - **等纪要那一层落终态**：纪要挂在离线链的尾巴上（那条入口起线程就返回），`meeting post` 拉起之后要等它落定才报结论 —— 上界取现成的 `minutes.timeout_seconds`（默认四小时），事件驱动的读盘等待，落定即返回。**拉起 ≠ 落定**：到上界还没落定时如实报 `timeout`（既不谎报成功也不写成 `failed`）。

  **幂等闸落在花钱之前**：两层各有「已完成」判据（状态文件落 `done` + 产物齐，缺一条就不算完成），据此分四档 —— 两层都已完成且音频已归档 → 打一句「无待办」、一个引擎调用与一个子进程都不产生；只差一层 → 只补那一层；任一层停在 `running` → 拒跑（`EXIT_REJECTED`，点名状态文件 + 给出 `--force`）；被打断的那一场（`interrupted`）是终态但不算完成、不拦闸，下一趟把离线段从头再跑；`--force` → 两层都重跑。
- **会后由谁拉起**：收尾第 4 步什么都不拉起（那一格停在 `waiting`）；「生成会后精细稿」（`post_offline`）只跑离线那条链（`run_session(..., minutes=False)`），「生成纪要」（`post_minutes`）只拉起纪要那一层；`meeting post` 仍是命令行那两条路，调 `meeting/cli.py` 的 `run_post()`。命令行上的另一个入口是重放验证 `meeting replay <场次目录>`（`a3b684bd`）：素材截断复制进它自己的 `--out` 落地，在拷贝上直调 `run_session(..., minutes=False)`——不经 `run_post()`、不碰纪要层，那一场的产物一个字节不动，只拿来验证参数与改动。**会后跑到哪一档看哪一只**：`GET /api/sessions/{id}`（`a9affaa8`）给「第几块 / 共几块、正在跑哪一路、已经跑了多久」，成不成仍看场次目录里的状态文件与那几份产物。
- **反复重跑不再拉起第二个子会话**：纪要那一层的启动器在建线程**之前**先把「这一层在飞」落到盘上（`meeting/minutes/status.py` 的 `mark_running()`，那一版 `state = "running"` 在 `launch()` 返回之前就落定），盘上已经停着 `running` 时当场抛 `AlreadyRunningError`、这一趟不起线程；离线那一趟的尾巴也会先过一道闸（`_minutes_gate()`）：纪要已是 `done` 且 `meeting_final.md` 过得了那四条判据时**不调启动器**。`--force` 是唯一会再次花钱的开关。
- **前端**：`web/` 已经落地（React + TypeScript 严格模式 + Vite 6 + vitest 2 + zod），产物落 `web/dist`。**会中主视图已经接上**（`070b31e0` phase2：状态胶囊与时长、两路电平表、每路健康、告警区（致命横幅 + 提示级）、心跳四级本地态与「采集已停」处置、实时转写区、告警出声开关；`1987cb20` phase3 接上总结区：四档窗口 + 主按钮 + 进行中那一句 + 结果 / 失败面板 + 本场总结记录回看；数据全部来自服务推送，界面不造派生数据）。还没进来的：设置抽屉、转写历史回看（契约章 7 的保留登记，界面上只有位置与禁用态）。**工作区首页已经接上**（e2f6166b：`web/src/home/` 的 `WorkspaceHome` —— 左侧清单读 `GET /api/sessions`、空态 logo、目录选择器弹窗（起点 `~/`，单击选中、双击进下层，「选定这个目录」确认后才登记）、非空收编与删除确认弹窗、Windows 大小写并一行、选中那一场详情里的**会后两个按钮**（`post_offline` / `post_minutes`，按钮上那一格按 `has_offline` 渲染）。`HomeGate` 的分流判据在 `home/gate.ts`：`idle` 且没有场次、或 `ended` 且收尾四步都落定（`post` 停 `waiting` 算落定）→ 工作台；其余给会中主视图 —— 于是收尾落定那一刻页面自己回到工作台，会后按钮就在那一场的详情里）。**续接的界面面**（`670d72b5`）：对录过的行顶栏动作钮改名「接着录」（没录过的照旧「开始录音」），详情区在日期下面摆「录音 共 N 段 · 合计」与每段的时长、起止时刻（合计只数写完的段，还在录的那段如实写「没写完」）。工程自己的说明在 `web/README.md`（视图结构、`src/live/` 各管什么、两份样式的来源与抽取口径、两条铁律），这里只点两条链路：
  - **改界面**：`cd web; npm run dev`，vite 起在 5173、`/api` 转发到 `http://127.0.0.1:${MEETING_PORT ?? 8765}`（服务照常跑着，改文案走 HMR，不必刷新）。
  - **开会**：`cd web; npm run build`（内部先跑 `npm run typecheck`）产出 `web/dist`，再 `meeting web` **由服务直接发这份静态产物** —— 从服务端口（如 `http://127.0.0.1:8765`）打开就是真界面，**运行时不需要 Node**。
  - 自检是 `npm test`（vitest run，21 个测试文件 511 个用例）与 `npm run typecheck`。
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
