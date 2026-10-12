# meeting · 后端协议

> 这份文档写服务与后端之间的接缝：四个协议的成员、数据结构、缺省值，以及后端怎么被找到、怎么被注入。
>
> 现状：**协议本身已经定死，四个位置现在都有实现。** `capture.backend` 的默认值 `auto` 解析到真采集后端（`meeting.backends.capture:CaptureBackend`，真探测、真起两路、真落盘与真转码）；`transcriber.backend` 的默认值 `auto` 解析到真转写后端（`meeting.backends.transcribe:TranscriberBackend`，尾读归档 → VAD 切段 → 单 worker 顺序识别 → 每路一份会中稿 + 行出口）；`post.hook` 的默认值 `auto` 解析到 `meeting.backends.post:PostProcessHook`（**解析落点**：本实现不带任何钩子方法，只答「这一层接没接」），`minutes.hook` 的默认值 `auto` 解析到 `meeting.backends.minutes:MinutesHook` —— 这两个键现在**由人按按钮时才被用到**（R26）：会后两个动作 `post_offline` / `post_minutes` 各起一条后台线程（后者经 `MinutesHook.launch()`），起完即回，跑成什么样由场次目录里的状态文件回答。**收尾第 4 步 `post` 一个会后动作都不自动跑**：它做完该做的（让会中模型退出显存）就停在 `waiting`（＝等你动手），`ended.phase` 仍判 `done`。`post.hook = null` / `minutes.hook = null` 两档是缺席：对应的按钮当场回 404 `action_unavailable`（如实说这台机器上没接这一层）。`meeting post` 已经接通（`65dd3e4c`）：它是**命令行那条入口**，两层都跑（R15 的两词命令形照守）。命令行另有一个重放验证入口 `meeting replay`（`a3b684bd`）：素材截断复制进工具自己的落地、直调 `run_session(..., minutes=False)`，只验证离线链——不经这两个钩子、不拉纪要，那一场的产物一个字节不动。会后那条链自己的四个开关是 `post.engine` / `post.model` / `post.dtype` / `post.chunk_seconds`（见第 3 章末），会后纪要那一层的五个开关是 `minutes.hook` / `minutes.chunk_lines` / `minutes.max_segments` / `minutes.timeout_seconds` / `minutes.dsh_url`（见第 3 章末那一节）。另有一个只给开发与测试用的 dev 替身（默认关闭，**按 key 分流**：`capture.backend = dev` 落采集替身、`transcriber.backend = dev` 落转写替身）。
>
> 协议与数据结构住在 `meeting/backends/base.py`（**只放协议与数据结构**，实现全在下游自己的模块里），替身住在 `meeting/backends/null.py` 与 `meeting/backends/dev.py`，解析的唯一入口是 `meeting/backends/registry.py`。

## 1. 四个协议

### `CaptureBackend` —— 采集

| 成员 | 语义 |
|---|---|
| `frame_interval` | **类属性**（秒）：这个后端每帧事实的间隔。协议默认 `0.5`，dev 替身写 `0.1`。runtime 在 `bind()` 之后读实例上的这个属性算存活窗口；读不到或取值不是正数就按 `DEFAULT_FRAME_INTERVAL_SECONDS = 0.5` 算 |
| `bind(emit, *, config)` | `probe()` 之前调一次。`emit` 是**事实出口**，`config` 是只读配置视图 |
| `probe() -> list[SourceProbe]` | 枚举并试开设备，**两路各一条**（`mic` / `loopback`） |
| `start(mode, session_dir)` | 按开场前定下的 `mode` 起采集，开始往 `session_dir` 里的归档写。**单路起不来不抛错**：只给那一路发一条 `SourceLost`（`reason` 写人话），另一路照跑；**本场该采的路全部起不来**才抛 `CaptureUnavailable`（`mode=mic` 时只有 mic 起不来才算全败）。抛错之前自己拉起来的线程要自己收干净（`meeting/service/runtime_sessions.py` 的 `_abort_session()` 那条路不调 `stop()`） |
| `finalize()` | 收尾第 1 步：停流 + join + 关文件 + `fsync`。**幂等** |
| `transcode()` | 收尾第 2 步：把归档转成 MP3。**可抛错**（该步据此记 `failed`，原始归档原样留下） |
| `stop()` | 停采集（进程关停时调）。**幂等**，没落过盘就先 `finalize()` |

真采集后端（`dca4539b` 的 `meeting.backends.capture:CaptureBackend`，`capture.backend` 的默认值 `auto` 解析到它）把上面四条都落到了实处：

- `probe()` 用 `sounddevice` 枚举输入端点、用 `soundcard` 枚举 render 端点，按配置与策略挑选，**以 16k 试开并读回一小段样本**（「能打开 ≠ 有波形」那一档就是这一步加出来的），结果如实报成两路 `ready` / `probe_failed`；
- `start()` 起两路**各自独立**的采集：mic 走 `sd.InputStream` 回调、loopback 走 `soundcard` 的 recorder（专用线程 + `CoInitializeEx`），把 16k / 单声道 / int16 的裸 PCM 写进 `meeting_audio_<source>_<NNN>.pcm`（NNN 三位零填充、从 1 起；**一段 = 一次录制**，段号 = 各路盘上实有最大段号 + 1，续接接着已有段号编新文件、旧段一个字节不碰；`buffering=0` 的截断写，强杀最多丢最后一个块的尾巴），每 `frame_interval` 秒**无条件**发一帧 `LevelFrame`（静音时发 `-120 dBFS` 的那一帧）+ 一条 `SourceFacts`；
- 两路各有一条写线程（一路卡在磁盘上不会把另一路拖住）；一路的源读崩时该路报一条 `SourceLost`，另一路照常写；
- 暂停与「关一路」的服务层出口（`6ea32d8d`；不在上面那张协议表里 —— HTTP 动作入口是 `pause` / `resume` / `set_source`，`10770b41`）：`pause()` / `resume()` 让所有路从这一刻起写数字静音（**不录内容**，真音频在写侧被等长替换成数字零），暂停不切段、只记进段账的 `pauses` 区间；`deactivate_source()` 是用户主动关掉某一路 —— 那一路改写静音，**不报 `SourceLost`、不进告警**（与「这一路真死了」是两回事）；`mode` 没选中的那一路整段写静音填充。三种静音都单独记进写侧的 `silence_bytes` 账，两路时间轴始终等长；**同一对动作在转写那一侧还牵一件事** —— 暂停把会中模型放出显存、继续再把它拉回来（可选成员 `acquire_gpu()`，见下面转写那一节 `release_gpu()` 那一条，`10770b41`）；
- `finalize()` / `transcode()` / `stop()` 见上表；转码成功之后**保留 PCM**（`session.json` 的 `sources[*].archives` 段文件名表声明的是它们）。

后端往里送的事实（**只报原始事实，不判告警、不带阈值语义**）：

| 事实 | 字段 |
|---|---|
| `LevelFrame` | `source` / `ts` / `dbfs` / `peak` / `bar`。`ts` 由后端给（采集线程看到那一刻的时间），服务只拿它做窗口比较与静默累计 |
| `SourceFacts` | `source` / `stream_alive` |
| `SourceLost` | `source` / `reason` |

设备就绪三态是 `SourceProbe.status`，取值 `ready`（这一路**挑到了端点**，也就是能打开）/ `probe_failed`（这一路**一个能打开的端点都没有**：没候选、或候选全开不了）/ `unprobed`（未探测）；`detail` 是写一句给界面看的人话。**「能打开但试开读回的是全零」落在 `ready`**，那一档只把峰值写进 `detail` —— 试开只看得见零点几秒，房间当时安静、或远端当时没在出声，读到的就是全零，拿它报故障等于每次安静都亮一次故障灯。**`probe()` 只许报这三档** —— 快照里另外四档（`capturing` / `lost` / `restored` / `stopped`）由状态迁移写值。后端报了取值域之外的第四档，runtime 记一条 warning 并把该路落 `probe_failed`，服务照常起。

**静默秒数不在这里**：服务侧按 `alert.silence_dbfs` 逐帧累计 `LevelFrame` 的帧时间戳来判，协议里没有「静默了多少秒」这个字段。

**导入顺序（C10，模型节点落地时成立）**：先 `import transformers`，再 `import speechbrain`；反过来会让 speechbrain 抢在 transformers 之前做依赖探测，出现难以定位的版本不匹配。这条约束记在 `base.py` 的模块注释里。

### `TranscriberBackend` —— 转写

| 成员 | 语义 |
|---|---|
| `bind(line_sink, *, config)` | `start()` 之前调一次。`line_sink` **可从任何线程调**，后端每出一行就调它一次 |
| `start(mode, session_dir)` / `stop()` | 起停转写。起步失败**不阻塞录音**（转写坏了可以重跑） |
| `health()` | `normal` / `behind` / `stopped`，直接进快照的 `transcript.health` |
| `backlog_depth()` / `segment_timeout()` | 积压段数与单段超时（秒），供收尾预算与提示级用；数值本身只进 log 与 `/debug` |
| `release_gpu()` / `acquire_gpu()` | **可选成员**（两个都不在协议强制那一份里）：把会中模型移出显存 / 再把它拉回来。调用方（收尾第 4 步、暂停 / 继续两个动作）用**防御式取法**（`getattr(..., None)` + 可调用才调），替身后端省掉这两格照样跑得通 —— 这条宽松是有意留的 |

行事实是 `TranscriptLine`：`ts` / `source` / `text` / `kind`（`line` 或 `gap`）。

方向只有一条：**服务把 sink 交给后端，后端往里送**；后端不反过来读服务的状态。

真转写后端（`9e5db89c` 的 `meeting.backends.transcribe:TranscriberBackend`，`transcriber.backend` 的默认值 `auto` 解析到它）把上面这几条都落到了实处：

- `bind(line_sink, *, config)` 记下出口与只读配置视图。runtime 在 `probe()` 之前先 `bind()` 一次、会话起来之后再 `bind()` 一次，两处都接得住：出口经一层间接层**现取**，于是「绑定时刻」与「产出时刻」谁先谁后都不影响那一行被送出去；
- `start(mode, session_dir)` 起 `meeting/asr/pipeline.py` 那条流水线：两路各一条读线程**按段序号尾读** `session_dir/meeting_audio_<source>_<NNN>.pcm`（段 1、段 2、…逐段接续，按序号拼起来 = 连续时间轴；只看新字节、不碰采集层的对象、不要求采集给它开旁路出口）、VAD 按 16k / 单声道 / int16 切段（帧长 30ms、静默 2.0 秒出段、单段硬上限 `asr.max_segment_seconds`）、段**入队时就盖上采集时刻**、**一个** worker 顺序消费两路的段（显存只有一份）。**起步失败不阻塞录音**：异常收成一条 error 日志、`health()` 落 `stopped`，采集照跑；
- `stop()` 关读线程、把 VAD 手上的尾段送进队列，再在**收尾预算**内把队列里剩下的段尽量做完（预算按积压量自适应，公式见 `docs/state.md` 第 1 章）；
- `health()` 三取值：已经 `stop()` 过、worker 没起来 / 已经停了、模型加载或识别出事 → `stopped`；积压段数压过 `BACKLOG_BEHIND_THRESHOLD`（20 段）→ `behind`；读线程出事、这一段再也不会长出新段也报 `behind`；其余情况 `normal`。**没起过场次时报 `stopped`** —— 快照空闲态那一格就是从它读来的真值；
- `backlog_depth()` 是段队列里还没处理的段数（队列**无界**，不做 busy-drop），`segment_timeout()` 是配置里的 `asr.timeout_seconds`（默认 `20.0`）；
- `release_gpu()` 由收尾第 4 步调一次（那一步只做「让会中模型退出显存」这一件事，之后那一格停在 `waiting`；会后离线链与纪要层都由「生成会后精细稿」/「生成纪要」按钮或 `meeting post` 另行拉起），耗时算在 `post` 这一步的记账里。语义四条：**没加载过就 no-op**（一条日志都不写）、**幂等**、等在飞推理有 **5 秒上界**（后端自己的在飞计数 + `Condition`，事件驱动；到点照旧丢引用并记一条 warning）、**丢引用之后**才尽力清一次缓存。它抛错不拖垮整场 —— 调用方如实记一条 log，`post` 那一格照常落 `waiting`。**配对那一半是 `acquire_gpu()`**（`10770b41`）：由动作 `pause` / `resume` 经服务层提交，走**同一条拉起路径**（`_transcriber_or_create()`，同一个 factory、同一把 `_model_lock`）。语义四条：**已经在手就安静 no-op**（不重造第二份权重）、释放之后经同一个 factory 造一份新的并复位释放标记（下一轮释放照样管得着）、**造不出来只记一条 warning** 且这次动作照常（R1 的立场：转写坏了不挡录音）、不碰 `_stopped` 与 `health()`。**权重真正的加载仍在继续之后的第一段转写时**（`GlmAsrTranscriber` 自己那条加载路径）—— 「继续」这一刻拿回来的是识别器。这一处交接的来由与本机数字见 `docs/asr-backends.md`。

每一段落一份产物：`session_dir/meeting_transcript_<source>.md`，一行 `[HH:MM] 正文`（分钟级时间戳取**采集时刻**，追加写、逐行 flush）。识别失败的段落照落一行，正文是**占位符**（`[[识别失败: 超时 20s]]` 这一形状：双层方括号、裸文本、不带时间戳、不含 `**`）；真后端产出的 `kind` 一律是 `line`，失败段落也是 `line`，靠正文认出来。文件位置、行格式与占位符的几档见 `docs/state.md` 第 3 章，SSE 侧那一行见 `docs/contract.md` 第 3.3 节。

### `PostProcessHook` —— 会后钩子（解析落点）

真钩子住在 `meeting/backends/post.py`（`7c6952e5` 已落地，`post.hook` 的默认值 `auto` 解析到它）。**本类只是 `post.hook` 的解析落点，不带任何钩子方法**（起 daemon 线程跑会后链的那套 `launch` / `_run` / `_mark_failed` 没有任何产品调用方，已随收尾第 4 步的退场一并删除）：收尾第 4 步不拉起它（那一格做完释放显存就停在 `waiting`），「生成会后精细稿」按钮（`post_offline`）走 `run_session(..., minutes=False)` 那条直路、不经它，`post.hook` 在那一侧只答「这一层接没接」（`null` → 按钮 404 `action_unavailable`）。「跑会后一套」（音频归档补齐 → 离线那一趟 → 等纪要终态 → 汇总）的产品入口只剩 `meeting post` 这一条（`meeting.cli.run_post()`）；会后离线那条链由按钮按需直接跑，跑成什么样由场次目录的 `meeting_offline_status.json` 与逐块账回答（产物契约见 `docs/state.md` 第 3 章）。

- `registry.capabilities_of()` 靠类的 `__module__` 把它当本实现、不当替身；它**不带 `finalize` / `transcode`**：那两项是采集后端的能力，这两个键上的闸门不看它。

### `MinutesHook` —— 会后纪要钩子

| 成员 | 语义 |
|---|---|
| `claim(session_dir)` | **可选成员**：**同步**把「这一层在飞」落到盘上（真实现里是 `meeting/minutes/status.py` 的 `mark_running()`），盘上已经停着一版 `running` 时当场抛 `AlreadyRunningError`。`post_minutes` 按钮那趟在起线程之前调它同步认账（runtime 用 `getattr` 找它，取不到就跳过同步认账这一步、照旧走线程体那条路） |
| `launch(session_dir)` | `meeting post` 的离线尾巴与 `post_minutes` 按钮调它，**返回即算启动成功**（不等它跑完）。`minutes.hook = null` 那一档不调它（按钮当场 404，`meeting post` 补纪要那一路安静跳过）。真实现另收两格 keyword-only：`force`（`meeting post --force` 那一趟）—— 盘上已经停着一版 `running` 时**建线程之前**就抛 `AlreadyRunningError`（当场拒跑），带 `force` 时放行；`claimed` —— 按钮那趟在 `claim()` 认过账时带 `claimed=True` 调它（实现若接受 `claim()` 就应接受这个格，缺省 `False`）：这里不再重复认账，重复一次会读到自己刚落下的 `running`、把这一趟自己拒掉 |

真钩子住在 `meeting/backends/minutes.py`（`41465462` 已落地，`minutes.hook` 的默认值 `auto` 解析到它）。它的形状：

- `launch(session_dir)` 起一条 daemon 线程（线程名 `minutes-final`）跑 `meeting.minutes.runner.run(session_dir)`，随即返回 —— 它跑多久都不占收尾预算，也不占离线那一趟的墙钟；
- **返回之前先把「这一层在飞」落到盘上**：`meeting/minutes/status.py` 的 `mark_running()` 同步写一版 `state = "running"` 的 `meeting_minutes_status.json`，盘上已经停着一版 `running` 时抛 `AlreadyRunningError`（带 `force=True` 时放行）—— 「同一场被拉起两个子会话」这件事因此在**跨进程**下也读得出来（用户随后手敲的 `meeting post` 是另一个进程），判据与那把瞬态 `meeting_minutes_status.lock` 见 `docs/minutes.md` 第 1.1 节；
- 线程里的异常落进场次目录的 `meeting_minutes_status.json`（补成 `failed` 并写上原因），**不外传**；`session_dir` 是空值时当场抛错；
- `meeting_offline_status.json` 与离线的四份产物，这条线程一个字节都不碰；
- **重依赖在函数体内 import**：模块顶层只有标准库，`import meeting.backends.minutes` 保持轻量。

它调用的那一层做什么、产物有哪几份、怎么手动跑，见 `docs/minutes.md`。

## 2. 缺省值：缺席时是什么样

后端没接进来时，服务不假装有它。`meeting/backends/null.py` 给两个替身：

| 替身 | 行为 |
|---|---|
| `NullCaptureBackend` | **两路都 `unprobed`**、`device_name` 为 `null`；`start()` 抛 `CaptureUnavailable`（`code = capture_unavailable`，带一句人话「采集后端未接入：当前是 null 替身，不会真的录音」）；`finalize()` / `transcode()` / `stop()` 是安全空实现 |
| `NullTranscriberBackend` | 恒 `health = normal`、`backlog_depth() = 0`、`segment_timeout() = 20.0`；起停是安全空实现 |

替身那一档**只在显式配置时出现**（`capture.backend = null`，或解析失败回退）：`start` 回 `503 capture_unavailable`，盘上不留任何场次目录。现在的**默认取值是 `auto`**，它解析到真采集后端（`meeting.backends.capture:CaptureBackend`）：设备探测与试开在 `probe()`，`start` 真的开一场、真的会把声音录下来；只有设备全起不来时才回 `503 capture_unavailable`（`detail.reason` 写明哪一路为什么）。

转写这一档现在有两层：**默认取值 `auto`** 解析到真转写后端（`meeting.backends.transcribe:TranscriberBackend`，见第 1 章那一节）；`transcriber.backend = null`（或解析失败回退）才是**缺席**那一档 —— `NullTranscriberBackend` 恒 `health = normal`、`backlog_depth() = 0`、`segment_timeout() = 20.0`，收尾预算的输入因此取 `0` 段 / `20.0` 秒这两个缺省值，转写这一侧一行都不产。**缺席档的 `normal` 与真实现空闲态的 `stopped` 各说各的事**：`normal` 说的是「这个构建里没有转写这件事」，`stopped` 说的是「有转写、这一档没在跑」（真实现没起过场次时报的就是它）。

`null` 替身**不长**合成电平或故障注入那类测试逻辑 —— 那是开发替身（`backends/dev.py`）的事。两个会后钩子的缺席占位各有一个：`registry.py` 的 `_AbsentPostHook`（`post.hook = null`）与 `_AbsentMinutesHook`（`minutes.hook = null`），两者的 `launch()` 都抛错，而缺席那一档根本不会被调用（调用方先问 `has_implementation`，是 `false` 就安静跳过：界面上对应的按钮回 404 `action_unavailable`，`meeting post` 补纪要的那一路按缺席档记账）。

## 3. 后端怎么被找到

`meeting/backends/registry.py` 是**解析的唯一入口**。四个解析函数：

| 函数 | 解析谁 | 约定路径（`auto` 按它找） |
|---|---|---|
| `resolve_capture_backend()` | `capture.backend` | `meeting.backends.capture:CaptureBackend` |
| `resolve_transcriber_backend()` | `transcriber.backend` | `meeting.backends.transcribe:TranscriberBackend` |
| `resolve_post_hook()` | `post.hook` | `meeting.backends.post:PostProcessHook` |
| `resolve_minutes_hook()` | `minutes.hook` | `meeting.backends.minutes:MinutesHook` |

取值口径：

| 取值 | 含义 |
|---|---|
| `auto`（默认） | 按上面的约定路径找 |
| `null` | 采集这一档用 `null` 替身；两个钩子没有替身可用，这一档就是**缺席** |
| `dev` | 用开发替身，**按 key 分流**：`capture.backend` 落 `meeting.backends.dev:DevCaptureBackend`、`transcriber.backend` 落 `meeting.backends.dev:DevTranscriberBackend`。这条分流只认采集与转写两个键：`post.hook = dev` 或 `minutes.hook = dev` 都会落到采集替身上，那个对象没有 `launch()`，离线那一趟的尾巴如实记失败；两个钩子键没有「可关的自动链」，各自答「对应那个按钮由哪个实现接」：`post.hook = null` → 「生成会后精细稿」按钮 404（`meeting post` 的离线段不受它管），`minutes.hook = null` → 「生成纪要」按钮 404、`meeting post` 补纪要那一路安静跳过 |
| `module:Class` | 显式指定 |

`minutes.hook` 那一格多一层：**配置里写 JSON 的 `null` 也在缺席档**（不是「回退 `auto`」），它在 `resolve_minutes_hook()` 里提前返回，落 `has_implementation = false`、`selector = "null"`。写字符串 `"null"` 或不写这个键，走的仍是上面那张表。

取到的是**类就无参实例化**（后端构造函数不许有必填参数，运行期依赖一律走 `bind()`）。导入失败、找不到、取到的对象不可调用、无参实例化失败 → **回退替身 + 一条 warning**，服务不会因为后端缺席或写坏而起不来。

### 解析结果怎么喂给收尾

解析函数返回一个 `BackendResolution`，它分开回答两个问题：

| 字段 | 取值 | 说的是什么 |
|---|---|---|
| `has_implementation` | `true` / `false` | 这个构建里**有没有**可执行实现。`true` = 解析到了实现（`null` / `dev` 替身也算）；`false` = 缺席（转写后端缺席、会后钩子缺席、显式取值解析失败） |
| `run_ok` | `null` / `true` / `false` | 那个实现**跑起来成不成**。被调用之前是 `null`，调用走完是 `true`，调用抛异常或超时是 `false` |

判定规则：`has_implementation == false` → 该步记 **`skipped`**；`has_implementation == true` 且 `run_ok == false` → 该步记 **`failed`**。**缺席记「跳过」，在场却抛错记「失败」** —— `has_implementation` 只由解析结果回答，调用阶段的异常只许写进 `run_ok`。

`capabilities_of(implementation)` 另外回答采集实现有没有 `finalize` / `transcode` / `dev_inject` 三项能力：`null` / `dev` 这类不写真实音频的替身没有前两项，于是收尾第 1、2 步记 `skipped`；真采集后端（`meeting.backends.capture:CaptureBackend`）前两项都是 `True`，收尾第 1、2 步因此会真的调它（这两步的闸门**只看采集侧**：解析结论与 `capture_capabilities["finalize"]` / `["transcode"]`，transcode 是采集后端的能力，转写后端在不在场与这道闸门无关 —— 见 `docs/state.md` 的「收尾四步与终态」）；`dev_inject` 这一项只有 dev 替身在 —— 真后端**有意不加** `inject_fault`，给真后端加上它会把开发替身的非产品动作在真后端上打开。

### 会后离线那一层的四个开关

`post.hook` 管这一层在不在，另外四个键管它跑起来什么样，都在启动时读一次（非热读，改了要重启）：

| 键 | 默认值 | 谁在读、怎么读 |
|---|---|---|
| `post.engine` | `auto` | `meeting/offline/runner.py` 的 `resolve_engine()`。取值口径与 `capture.backend` / `transcriber.backend` / `post.hook` 同形：`auto` 按约定路径落 `meeting.offline.engine:MossOfflineEngine`，`module:Class` 按它导入，`null` 当场抛错（这一层没有替身引擎）。**与会后钩子分开解析**：换引擎时 `post.hook` 那一层一个字不动 |
| `post.model` | `OpenMOSS-Team/MOSS-Transcribe-Diarize` | 离线引擎构造：HF 仓库 id 或本地快照绝对路径。默认值是本机已有本地快照的那一份 |
| `post.dtype` | `bfloat16` | 离线引擎构造的精度档位。只认 `meeting/offline/engine.py` 认得的那几个名字（`bfloat16` / `bf16` / `float16` / `fp16` / `float32` / `fp32`），写错档位在启动时就拦下 |
| `post.chunk_seconds` | `450` | 分块的长度上限（秒），合法范围 `0–86400`：音频长过它就切成连续不重叠的块，逐块调引擎；`0` 显式关掉分块、整段一次跑。默认取 450 是本机实测的安全档（块长标度曲线与越界判据见 `docs/asr-backends.md` 第 4 章）。短于 450 秒的音频走的就是整段一次跑（块数为 1 时不切块） |

这四格之外，会后这条链还有一道**不带配置键的闸**：数字静音门（判据、留痕与四档终态的区分见 `docs/state.md` 第 3 章的「没声的那几段」）。它没有开关 —— 峰值够不着半格的那一段一律不送引擎，**单趟跑与分块跑都过这道闸**（归档 PCM 上按字节扫那一段、别的容器用解好的波形，判不了就现解一次）。跳过的那几段记在状态文件的 `silent` / `silent_spans`、对照表的 `audio.silent_spans` 与转写第一行那条注释上；**门判不了**那几段时（音频解不开、要的那一段读不全）照常送引擎，同时把「没判成」记在状态文件与对照表的 `gate`（`applied: false` + `unjudged_spans`）以及逐块那一行的 `gate: "unjudged"` 上 —— 一次没门可用的运行因此在产物里与一次健康的运行分得开。逐块那一行另带一个**结论词** `reason`（`digital_silence` / `engine_error` / `no_segments` / `zero_span_segments` / `transcribed` / `gate_unjudged`）与 `gate_method`（门凭什么判的），下游按它们读「这一段为什么没有字」与「这一段有没有人判过」，不必从整场那几个字段反推。`gate_method` 的取值是 **`pcm_bytes`**（在归档 PCM 上按字节扫）、**`waveform`**（在已经解好的波形上切片判）、**`decode_at_plan`**（量长度那一步就没解开，结论落「判不了」）与 **`decode_now`**（门现解一次音频文件，只对直接调用 `measure_span()` 的调用方存在，产品路径到不了它），另加一个 **`unjudged`**（这一段门没做成判断，与逐块 `gate` 那一格同名的那个词）。逐路 `audio.gate.method` 与状态文件 `artifacts.<source>.gate.method` 只由**判过的块**构成（判过的块按各自那把尺子拼起来，多把写成 `a/b`），一块都没判成、或这一路没有可判的音频时它是 `null`；逐块那一行的 `gate_method` 记的是这一块**原本打算用**的那把尺子，`gate` 为 `unjudged` 的块同样带着它（解不开那一档因此读得到 `decode_at_plan`），「没判成」这件事由 `gate` 与 `gate_detail` 说。**单趟跑的 MP3 记的是 `waveform`**：`_plan_for_source()` 为了量长度已经解出整份波形，门切片用掉的就是那一段，门口不再现解第二遍。

`post.dtype` 与 `post.chunk_seconds` 这两格的取值依据（模型自带的那份精度、官方 helper 在 CUDA 上的选择、块长标度曲线与越界判据）写在 `docs/asr-backends.md`，连同那台 3060 6GB 机器上还没量过的几项。

### 会后纪要那一层的五个开关

`minutes.hook` 管这一层在不在（`auto` 落 `meeting.backends.minutes:MinutesHook`，`null` 是缺席：「生成纪要」按钮当场 404 `action_unavailable`，`meeting post` 补纪要那一路安静跳过），另外四个键管它跑起来什么样，都在启动时读一次（非热读，改了要重启）：

| 键 | 默认值 | 谁在读、怎么读 |
|---|---|---|
| `minutes.hook` | `auto` | `meeting/backends/registry.py` 的 `resolve_minutes_hook()`（`meeting post` 的离线尾巴与 `post_minutes` 按钮经它拿实现；缺席档按钮当场 404、`meeting post` 补纪要那一路安静跳过）。取值口径与 `post.hook` 同形，另加「配置里写 JSON `null` 也落缺席档」这一条 |
| `minutes.chunk_lines` | `100` | `meeting/minutes/runner.py` 的 `chunk_lines_of()` → `chunk.chunk_session()`，合法范围 `10–1000`（与 `chunk.MIN_CHUNK_LINES` / `MAX_CHUNK_LINES` 同值） |
| `minutes.max_segments` | `8` | `runner.py` 的 `max_segments_of()` → 契约校验与提示词渲染各用一次，合法范围 `1–20`（默认值与 `contract.DEFAULT_MAX_SEGMENTS` 同值） |
| `minutes.timeout_seconds` | `14400` | `runner.py` 的 `_timeout_seconds()` → 启动器那一次子进程的墙钟上限，合法范围 `60–86400` |
| `minutes.dsh_url` | `http://127.0.0.1:3080` | `meeting/minutes/dsh_web.py` 的 `probe()` 在拉起之前探的那台 DSH web 服务端（非空字符串，本层不校验 URL 形状；探不通就落回 headless 分支）。默认值就是 `dsh web` 的默认监听地址 |

**`minutes.hook` 是 `config.py` 里唯一一个「`null` 是取值、原样留着」的可空键**（`NULL_SENTINEL_KEYS`）：配置里写 `null` 就是「关掉这一步」，归一成空串或默认值都会把这件事吃掉，因此它不进两个设备键那一档（`NULLABLE_KEYS`）。两档合起来是 `ALL_NULLABLE_KEYS`。

## 4. 注入与构造规则

- **类就无参实例化**：后端构造函数不许有必填参数，运行期依赖一律走 `bind()`。
- **解析失败就回退**：见第 3 章。服务不会因为后端缺席或写坏而起不来。
- **`emit()` 与 `line_sink()` 可以从任何线程调**：服务在 `bind()` 里包一层 `loop.call_soon_threadsafe(...)` 投递，后端不必知道事件循环在哪，也不许自己碰状态。
- **`config` 是只读视图**（`ReadOnlyConfig`）：`get(key)` 按点号路径取值，`as_dict()` 拿整份生效值。后端读配置，不能改配置。

## 5. dev 替身（采集与转写）

`meeting/backends/dev.py` 是**只给开发与测试用的非产品路径，默认关闭**。同一条取值 `dev` 在两个后端键上**按 key 分流**：`capture.backend = dev`（或 `MEETING_CAPTURE=dev`）落 `DevCaptureBackend`，`transcriber.backend = dev` 落 `DevTranscriberBackend`。

`DevCaptureBackend` 报两路 `ready`（`device_name` 是 `dev-mic` / `dev-loopback`），每 100ms 发一帧合成电平，另外接受三档故障注入（`silence` / `stall` / `lost`）。它**不写真实音频**，`finalize()` / `transcode()` 只记账。

`DevTranscriberBackend` 是一条「会中能看见字幕在动」的替身：起一条线程，开场 2.0 秒后首发、之后每 0.5 秒经 `line_sink` 产一行两路轮换的合成转写（正文形如 `dev 替身合成的一行转写（mic #1）`）。它不读归档、不加载模型、**不写产物文件**，`health()` 恒 `normal`、`backlog_depth()` 恒 `0`、`segment_timeout()` 报 `20.0`；`stop()` 之后线程收队、一行都不再产。**它没有对应的环境变量** —— `MEETING_CAPTURE` 只映射 `capture.backend`，要开转写替身就写配置文件里的 `"transcriber.backend": "dev"`。

用法、三档故障的语义与恢复口径写在 `docs/state.md` 第 7 章。

## 6. 相关文档

- 配置键与默认值：`docs/service.md` 第 4 章
- 服务侧现状与不可用清单：`docs/service.md` 第 8 章
- 四态、快照、收尾四步、dev 替身怎么用：`docs/state.md`
- 会后纪要那一层吃什么、经哪几步、怎么手动跑：`docs/minutes.md`
- 会中与会后两套模型的选型、四个 `post.*` 键的取值依据与本机实测：`docs/asr-backends.md`
- V1 里可搬的转写退化防护逻辑与参数标定：`docs/v1-lineage.md` 第 1 章
- 索引：`docs/spec.md`
