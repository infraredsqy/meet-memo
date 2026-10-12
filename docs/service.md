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
python -m pytest -q -m "not slow" -n auto --dist loadfile   # 快档并行（每圈这条）
python -m pytest -q -n 12 --dist loadfile                   # 全量并行（收尾这条）。
                                                            # 全量用 12 worker——16 个会把 shutdown 的
                                                            # 真子进程信号用例饿到超时
```

**快档红线（机主定，`68927c27` 收尾轮起生效）**

为什么要压得这么死：**这些测试没有 CI**——不存在一条独立运行的流水线在旁边替谁跑测试、
出报警；测试就跑在机主本机上、跑在 agent 的循环里。execute 跑一遍、review 跑一遍、
fix 每改一点再跑一遍、发现一个错修完还要再跑一遍——跑测试的时间要**按圈登记**，每跑一圈
记一笔：快档每慢一秒，整个开发循环就慢「圈数 × 一秒」，而圈数没有上限。
快档压得越死，agent 的迭代越快；这条与健壮性无关，是循环结构本身的要求。据此：

- 快档的计价是**每圈并行 ≤ 10 秒（红线 8–10 s）**：「8–10 秒买 60–70% 的正确率，好过 15 秒
  买 90%」——迭代速度优先于健壮。
- **之后新加的任何测试，必须满足「无 sleep、无真子进程、无真墙钟等待、毫秒级纯逻辑或结构
  断言」才许进快档；达不到的一律进慢档（慢档不受时长限制，收尾 / 大改时跑一次）。**

`tests/` 下的文件数与用例数**不进文档存数**，用 `python -m pytest -q --collect-only` 现算（文件数取输出里出现过 `tests/….py::` 的文件个数；`tests/_helpers.py` 这类测试支持模块不被收集，`tests/v1/test_repeat_filter.py` 算一个）。不需要先 `pip install`，也不需要可编辑安装。

- 用例一律**注入假件**：一条真实模型端点、真实声卡、真实 DSH 子会话 / web 服务的请求都不发；出口另有几道 autouse 兜底，见下面「七道 autouse 兜底」。
- **分档**：`pytestmark = pytest.mark.slow` 的文件与带用例级 `@pytest.mark.slow` 的用例归慢档，其余走快档；归位判据见 7.1。
- `tests/v1/` 是 V1 那套重复循环回归的**整份副本**（`fixtures_real_loops.py` 逐字照抄、`test_repeat_filter.py` 只改 import），那两条真实故障行因此不依赖会议目录就能复现。

**测试支持**（不被 `pytest` 收集）：`tests/_helpers.py`（真服务子进程与假浏览器等工具）、`tests/_service.py`（真服务子进程驱动件 + SSE 读帧与等待助手）、`tests/_drain_fakes.py`（收尾与关停用例共用的假后端 / 假钩子，与工厂辅助一起挂进子进程）、`tests/_capture_faulty.py`（端到端专用的「问题采集后端」挂件：真采集后端 + 假端点表 + 内容带块号的假源，旋钮走环境变量，经配置键 `capture.backend = "tests._capture_faulty:FaultyCaptureBackend"` 挂进进程内真运行时）、`tests/_dsh_web_fake.py`（假 DSH web 端点：一件真 HTTP 服务起在回环的临时端口上，照抄本层用到的那三条端点的形状、逐条记下请求）、`tests/_contract.py`（`contract/fixtures/` 的采集、掩码与落盘比对；它按需写出一个假转写后端，经 `PYTHONPATH` + `transcriber.backend` 的 `module:Class` 挂进子进程）、`tests/__init__.py`（`tests/` 的包标记，跨文件取用走包路径 import）、`tests/_fake_webbrowser/`（替掉标准库 `webbrowser` 的假模块）、`tests/conftest.py`（**只放 fixture**，注册 `--regen-contract` 开关，另带**七道 autouse 安全兜底**，见下）。

**七道 autouse 兜底**（`tests/conftest.py`，挡的都是「真花钱 / 真动静」的出口）：

1. `webbrowser.open` 换成只记录不开窗的假替身（`tests/_fake_webbrowser/`）；
2. `_no_real_audio_capture`：把真采集入口打成「一碰就炸」的桩 —— `sounddevice.InputStream` / `rec` / `check_input_settings` / `soundcard.get_microphone`；异常类 `RealCaptureForbidden` 不继承 `Exception`，不会被生产代码的 `except Exception` 吞掉；只读枚举不禁；
3. `_no_real_minutes_session`：立起 `MEETING_MINUTES_CHILD=1`，让离线链收尾处那次 `minutes.hook` 通知当场走再入闸 —— 本机 PATH 上真的有 `dsh`，不立这一格，每场跑完整条离线链的用例都会真拉起一个 headless 子会话去跑五阶段；
4. `_no_real_model_call`：盯**会中总结那条 HTTP 出口**（`meeting/summary/client.py` 的 `_http_post()`），`urllib.request.urlopen` 按目标地址判，本机回环放行、其余当场抛 `RealModelCallForbidden` —— `summary.base_url` 的配置默认值就是真地址，忘了注入假客户端就会真花机主的 token；
5. **PATH 守卫**（会话级 `_no_real_dsh_on_path` 与用例级 `_no_real_dsh_launch` 两条夹具）：在临时目录里造一件假的 `dsh.cmd`（只往记录追加一行）并前置到测试进程 `PATH` 的最前面，漏网的用例命中的是它、真会话起不来；用例级那一条再拿记录把「这一趟真的叫了它」当场判红（要故意走到可执行文件解析那一步的用例，显式请 opt-out 夹具 `allow_real_dsh_resolution`）；
6. `_no_writes_to_the_real_home`：开工记下机主真实家目录下那个配置目录（`~/.config/meeting`）的文件集合与逐个 sha256，每一条用例收工与整场收场各复查一次，被建出或改过就当场判红并打出差（含收工时那份文件的内容）；
7. `_no_real_dsh_web_call`：包住 `meeting/minutes/dsh_web.py` 唯一那个 HTTP 出口 `_post_json()` —— 打向用例自己起的那些假端点（本机回环、端口由系统挑）原样放行，打向 3080 那台真服务的一律当场抛 `RealDshWebCallForbidden`：`minutes.dsh_url` 的配置默认值就是那个真地址，少了这道网，一条忘了注入假件的用例会在机主开着 DSH GUI 时往那台服务上建真工作区、真会话、发真提示词。

**契约 samples 的重生成开关**：`python -m pytest tests/test_contract_fixtures.py --regen-contract -q` 重写 `contract/fixtures/`，不给开关就是逐文件比对（改了服务行为就重跑它并复核差异）。规则与出处见 `contract/README.md`。

**每一条用例都有一道自动看门**：`tests/conftest.py` 汇总 pytest 自己报的 setup / call / teardown 三段耗时，**合计超过本趟阈值就把这条用例判红**（红在它自己的名字下，报出三段各自的耗时）。阈值缺省 **5 秒**（`CASE_WALL_CLOCK_LIMIT_SECONDS`）；并行档（`pytest-xdist`）经环境变量 `MEETING_CASE_WALL_CLOCK_LIMIT_SECONDS` 放宽（文档口径取 8.0，给并行趟留余量），**不设变量 = 缺省 5.0，串行趟与从前逐字节一致**；变量读不出来（非数字 / ≤ 0 / 空串）一律从严退回 5.0。看门只读 pytest 的 report，不额外计时、不加 fixture。

这条红线牵着测试侧的等待窗口：静默阈值、收尾那一步的阻塞时长、端到端的观察窗口都按它收口过（例：一条用例的静默阈值取 1 秒、把某一步钉在 `running` 的阻塞取 0.6 秒、端到端的观察窗口取 1 秒）。**被测的语义没有跟着改** —— 收尾预算仍是 30 秒起步、告警默认阈值仍是 180 秒，改的只是测试自己等多久。

测试的隔离手段：**重定向家目录**（`HOME` / `USERPROFILE` 都指向临时目录），配置与日志因此整套换落点；**不新增 `MEETING_*` 变量**。真服务一律用 `[sys.executable, "-m", "meeting", "web"]` 起子进程，地址从 `MEETING_PORT=0` 加子进程打印的 URL 行解析出来（**不猜端口**）。采集后端由用例自己的 `config.json` 的 `capture.backend` 表达，子进程 env 里不塞 `MEETING_CAPTURE`（它的优先级更高，会静默压住那条配置键）。ASGI 层用 `with TestClient(app, base_url="http://127.0.0.1") as c:`（`base_url` 把回环 `Host` 带上，只有 `with` 会触发应用的生命周期钩子）。

### 7.1 快慢分档（`slow` 标记）

全量一趟约两分钟上下，九成时间花在真子进程与真墙钟等待上。因此按**文件粒度**分成两档，
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
pytest 把「sloww」当成一个谁都不带的标记，表达式于是恒真、全部用例都被选上 —— 结果是悄悄跑了
全量（慢，但不假绿）。正向拼错由退出码 5 兜住，否定式拼错由这道 `--strict-markers` 兜不住。

**归位判据**（一条用例满足任一条就进慢档）：

1. 起**真服务子进程**（`[sys.executable, "-m", "meeting", "web"]`）；
2. 依赖**真实墙钟等待**才能得出结论（收尾预算、100ms 节拍、抑制窗口、恢复窗口）；
3. 注入**「慢模型」**（睡很久 / 会抛错）来证明解耦或存活；
4. 单条用例实测超过 **2.0 秒**。

**只能落在文件粒度**：`tests/conftest.py` 那道看门（`CASE_WALL_CLOCK_LIMIT_SECONDS = 5.0`）管的是
**逐条用例**的 `setup + call + teardown` 之和压在 5 秒之内；「慢」来自文件层面的真子进程与真墙钟等待，
看门够不着它们。慢档现在是**一批文件级标记 + 一批用例级标记**：shutdown / port / heartbeat /
state_machine / config_isolation / minutes_dsh_web 里「真子进程 / 真阈值等待」的用例按用例级
`@pytest.mark.slow` 进慢档，文件本身不再整体带标记。文件级清单现算：

    Select-String -Path tests\test_*.py -Pattern 'pytestmark = pytest\.mark\.slow'

**路径要写成 `tests\test_*.py`**：不加这一层时 `tests\conftest.py` 的文档串里同名的那句话也会被数进去。

**一个例外判例**：`tests/test_transcript_hints.py` 满足判据 ②（它那条端到端用例只等 0.5 秒节拍），
但**不带 `slow` 标记**、落在快档 —— 不拖累谁。**碰了转写提示级那一路时仍然要单跑它**
（`python -m pytest tests/test_transcript_hints.py -q`），别因为它不在慢档清单里就跳过。

**不加 `-m` 时跑全量**，这一点是有意的：新写的用例默认落在快档，只有被显式标 `slow` 才跳过。
配合上面的**快档红线**（新用例无 sleep / 无真子进程 / 毫秒级纯逻辑才许进快档），漏跑的风险两侧都被压住：
快档不会被慢用例拖慢（进不来），慢用例也逃不过收尾那一趟全量。会后契约那一族与 `meeting post`
入口的那些文件归入慢档 —— 它们是收尾功能的验收，跟着收尾那一趟全量跑。

**将来某个快档文件里出现一条真子进程用例**：那一条单独加 `@pytest.mark.slow` 即可，不必把
整个文件挪进慢档。

## 8. 现在能做什么 / 还接不上的部分

**能**（细节一律看对应章节，这里不复述实现）：

- 服务本体：起停、启动序列、五档退出码、`Ctrl+C` 两阶段、收尾四步跑到终态（自适应预算、超时如实上报、失败后服务不自杀）、场次 log 的挂摘、配置原子写与热读键、端口与三道本地防线（第 1–6 章）；
- 端点与动作：`/`（产物或说明页）、`/debug` 自检页、`/api/state`、四事件 SSE、心跳（含帧空洞记录 `capture_gaps`）、十六个动作、`GET /api/summaries`、工作区注册表两只读端点与收编两动作、单场状态（第 3 章那张表）；
- 真采集：两路各自独立、边采边落盘按段命名的裸 PCM、0.5 秒电平帧、对录过的目录续接（同一场再录一段，旧段一个字节不碰）、暂停与「会中关一路」（动作 `pause` / `resume` / `set_source`）、收尾逐段转 64k MP3（PCM 保留）；
- 真转写：两路各一本会中临时稿（`[HH:MM] 正文`），四层退化防护；防护在本机对 V1 事故素材实测过（读数与口径见下面「防护实测」）；
- 会后离线：按钮或 `meeting post` 按需拉起，两路各出会后稿与说话人对照表，逐块进度落盘、`GET /api/sessions/{id}` 答跑到哪一块，数字静音门拦全零段，`meeting replay` 重放验证；
- 会后纪要：按钮或 `meeting post` 拉起，先探 `minutes.dsh_url` —— 在跑就交付（终态 `delivered`）、没在跑才 headless 子会话（`done` / `failed`），产物 `meeting_final.md` 带说话人附录，人名可改、随下一次纪要带上；
- 会中总结：动作 `summarize` 十到十五秒回三小段，四档窗口，本场记录可回看；
- 前端：工作区首页（清单 / 目录选择器 / 会后两个按钮 / 会后进度区）与会中主视图，`web/dist` 由服务直发（`web/README.md`）；
- 契约看守：`python -m pytest tests/test_contract_fixtures.py -q` 比对 `contract/fixtures/` 的 golden 样本（`--regen-contract` 重新生成），同一批样本在 `web/tests/` 里再验一遍类型层。

其余按「能力还没进来」理解，服务本身照常跑。两件如实记：真机真开一场会的设备枚举与真实电平是**手工验收**（自动化不替代它）；端口被占文案括号里的「已录多久」还是字面写死的「当前空闲」（`README.md` 第 1 节）。

**防护实测**（口径写在括号里；V1 出处见 `docs/v1-lineage.md` 第 1 章）：

- **双跑对照**（V1 那场事故会议的 GLM-ASR-Nano 素材、10 秒段）：关掉防护裸跑，三个真实触发点（`P4-vac-1836` 的 seg3 / seg7、`C1-A-normal` 的 seg23）分别产出 **2670 / 3996 / 2021 字**、耗时 **36.1–36.9 秒**、顶满 2000 token、循环覆盖率 0.977–0.988；四层防护全开之后同样三段落到 **87 / 85 / 59 字**、**0.75–1.09 秒**。裸跑那三段与 V1 当年记录的三行**逐字节相同**（sha256 前 16 位三对相同）。
- **文本级过滤器**在事故会议全 **411 条**正文上命中 **2 行**、误杀 **0 行**（那两行的真实循环覆盖率 0.9954 / 0.9931，第 3 名按只算可剥离行是 0.1130 —— 与 V1 记录值一致）。
- **实时率**：本机 10 秒段单段耗时中位 **0.58 秒**、RTF 中位 **0.058×**（这一档是 10 秒段的口径；V1 记录的 `0.39×` 出自**生产路径探针** —— 整场重放、10 秒段正常识别 3.1–4.2 秒，两个数口径不同，不能直接相减）。
- **时间戳**：把 530 秒素材按 1× 真实时间轴喂进整条流水线，**53 行全部配对**，偏差中位 **0.0 分钟**、最大 **1.0 分钟**；压力档（积压峰值 29–30 段、排队延迟最大 80 秒）偏差仍是中位 0.0 / 最大 1.0 —— **滞后不随积压增长**。

各能力的正文落点：采集与转写协议 `docs/backend-protocols.md`；场次目录与产物 `docs/state.md`；纪要 `docs/minutes.md`；会中总结 `docs/summary.md`；前端 `web/README.md`。

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
