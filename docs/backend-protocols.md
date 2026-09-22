# meeting · 后端协议

> 这份文档写服务与后端之间的接缝：三个协议的成员、数据结构、缺省值，以及后端怎么被找到、怎么被注入。
>
> 现状：**协议本身已经定死，三个位置里真采集与真转写都已经落地。** `capture.backend` 的默认值 `auto` 解析到真采集后端（`meeting.backends.capture:CaptureBackend`，真探测、真起两路、真落盘与真转码）；`transcriber.backend` 的默认值 `auto` 解析到真转写后端（`meeting.backends.transcribe:TranscriberBackend`，尾读归档 → VAD 切段 → 单 worker 顺序识别 → 每路一份会中临时稿 + 行出口）。**会后钩子眼下仍是缺席**（第 4 步 `post` 记 `skipped`，归 `65dd3e4c`；落地时按这份协议接进来，服务这一侧不需要改代码）。另有一个只给开发与测试用的 dev 替身（默认关闭，**按 key 分流**：`capture.backend = dev` 落采集替身、`transcriber.backend = dev` 落转写替身）。
>
> 协议与数据结构住在 `meeting/backends/base.py`（**只放协议与数据结构**，实现全在下游自己的模块里），替身住在 `meeting/backends/null.py` 与 `meeting/backends/dev.py`，解析的唯一入口是 `meeting/backends/registry.py`。

## 1. 三个协议

### `CaptureBackend` —— 采集

| 成员 | 语义 |
|---|---|
| `frame_interval` | **类属性**（秒）：这个后端每帧事实的间隔。协议默认 `0.5`，dev 替身写 `0.1`。runtime 在 `bind()` 之后读实例上的这个属性算存活窗口；读不到或取值不是正数就按 `DEFAULT_FRAME_INTERVAL_SECONDS = 0.5` 算 |
| `bind(emit, *, config)` | `probe()` 之前调一次。`emit` 是**事实出口**，`config` 是只读配置视图 |
| `probe() -> list[SourceProbe]` | 枚举并试开设备，**两路各一条**（`mic` / `loopback`） |
| `start(mode, session_dir)` | 按开场前定下的 `mode` 起采集，开始往 `session_dir` 里的归档写。**单路起不来不抛错**：只给那一路发一条 `SourceLost`（`reason` 写人话），另一路照跑；**本场该采的路全部起不来**才抛 `CaptureUnavailable`（`mode=mic` 时只有 mic 起不来才算全败）。抛错之前自己拉起来的线程要自己收干净（`meeting/service/runtime.py` 的 `_abort_session()` 那条路不调 `stop()`） |
| `finalize()` | 收尾第 1 步：停流 + join + 关文件 + `fsync`。**幂等** |
| `transcode()` | 收尾第 2 步：把归档转成 MP3。**可抛错**（该步据此记 `failed`，原始归档原样留下） |
| `stop()` | 停采集（进程关停时调）。**幂等**，没落过盘就先 `finalize()` |

真采集后端（`dca4539b` 的 `meeting.backends.capture:CaptureBackend`，`capture.backend` 的默认值 `auto` 解析到它）把上面四条都落到了实处：

- `probe()` 用 `sounddevice` 枚举输入端点、用 `soundcard` 枚举 render 端点，按配置与策略挑选，**以 16k 试开并读回一小段样本**（「能打开 ≠ 有波形」那一档就是这一步加出来的），结果如实报成两路 `ready` / `probe_failed`；
- `start()` 起两路**各自独立**的采集：mic 走 `sd.InputStream` 回调、loopback 走 `soundcard` 的 recorder（专用线程 + `CoInitializeEx`），把 16k / 单声道 / int16 的裸 PCM 写进 `meeting_audio_<source>.pcm`（`buffering=0` 的追加写，强杀最多丢最后一个块的尾巴），每 `frame_interval` 秒**无条件**发一帧 `LevelFrame`（静音时发 `-120 dBFS` 的那一帧）+ 一条 `SourceFacts`；
- 两路各有一条写线程（一路卡在磁盘上不会把另一路拖住）；一路的源读崩时该路报一条 `SourceLost`，另一路照常写；
- `finalize()` / `transcode()` / `stop()` 见上表；转码成功之后**保留 PCM**（`session.json` 的 `sources[*].archive` 声明的是它）。

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

行事实是 `TranscriptLine`：`ts` / `source` / `text` / `kind`（`line` 或 `gap`）。

方向只有一条：**服务把 sink 交给后端，后端往里送**；后端不反过来读服务的状态。

真转写后端（`9e5db89c` 的 `meeting.backends.transcribe:TranscriberBackend`，`transcriber.backend` 的默认值 `auto` 解析到它）把上面这几条都落到了实处：

- `bind(line_sink, *, config)` 记下出口与只读配置视图。runtime 在 `probe()` 之前先 `bind()` 一次、会话起来之后再 `bind()` 一次，两处都接得住：出口经一层间接层**现取**，于是「绑定时刻」与「产出时刻」谁先谁后都不影响那一行被送出去；
- `start(mode, session_dir)` 起 `meeting/asr/pipeline.py` 那条流水线：两路各一条读线程**游标尾读** `session_dir/meeting_audio_<source>.pcm`（只看新字节、不碰采集层的对象、不要求采集给它开旁路出口）、VAD 按 16k / 单声道 / int16 切段（帧长 30ms、静默 2.0 秒出段、单段硬上限 `asr.max_segment_seconds`）、段**入队时就盖上采集时刻**、**一个** worker 顺序消费两路的段（显存只有一份）。**起步失败不阻塞录音**：异常收成一条 error 日志、`health()` 落 `stopped`，采集照跑；
- `stop()` 关读线程、把 VAD 手上的尾段送进队列，再在**收尾预算**内把队列里剩下的段尽量做完（预算按积压量自适应，公式见 `docs/state.md` 第 1 章）；
- `health()` 三取值：已经 `stop()` 过、worker 没起来 / 已经停了、模型加载或识别出事 → `stopped`；积压段数压过 `BACKLOG_BEHIND_THRESHOLD`（20 段）→ `behind`；读线程出事、这一段再也不会长出新段也报 `behind`；其余情况 `normal`。**没起过场次时报 `stopped`** —— 快照空闲态那一格就是从它读来的真值；
- `backlog_depth()` 是段队列里还没处理的段数（队列**无界**，不做 busy-drop），`segment_timeout()` 是配置里的 `asr.timeout_seconds`（默认 `20.0`）。

每一段落一份产物：`session_dir/meeting_transcript_<source>.md`，一行 `[HH:MM] 正文`（分钟级时间戳取**采集时刻**，追加写、逐行 flush）。识别失败的段落照落一行，正文是**占位符**（`[[识别失败: 超时 20s]]` 这一形状：双层方括号、裸文本、不带时间戳、不含 `**`）；真后端产出的 `kind` 一律是 `line`，失败段落也是 `line`，靠正文认出来。文件位置、行格式与占位符的几档见 `docs/state.md` 第 3 章，SSE 侧那一行见 `docs/contract.md` 第 3.3 节。

### `PostProcessHook` —— 会后钩子

| 成员 | 语义 |
|---|---|
| `launch(session_dir)` | 收尾第 4 步调它，**返回即算启动成功**（不等它跑完）。没接后端时该步记 `skipped` |

## 2. 缺省值：缺席时是什么样

后端没接进来时，服务不假装有它。`meeting/backends/null.py` 给两个替身：

| 替身 | 行为 |
|---|---|
| `NullCaptureBackend` | **两路都 `unprobed`**、`device_name` 为 `null`；`start()` 抛 `CaptureUnavailable`（`code = capture_unavailable`，带一句人话「采集后端未接入：当前是 null 替身，不会真的录音」）；`finalize()` / `transcode()` / `stop()` 是安全空实现 |
| `NullTranscriberBackend` | 恒 `health = normal`、`backlog_depth() = 0`、`segment_timeout() = 20.0`；起停是安全空实现 |

替身那一档**只在显式配置时出现**（`capture.backend = null`，或解析失败回退）：`start` 回 `503 capture_unavailable`，盘上不留任何场次目录。现在的**默认取值是 `auto`**，它解析到真采集后端（`meeting.backends.capture:CaptureBackend`）：设备探测与试开在 `probe()`，`start` 真的开一场、真的会把声音录下来；只有设备全起不来时才回 `503 capture_unavailable`（`detail.reason` 写明哪一路为什么）。

转写这一档现在有两层：**默认取值 `auto`** 解析到真转写后端（`meeting.backends.transcribe:TranscriberBackend`，见第 1 章那一节）；`transcriber.backend = null`（或解析失败回退）才是**缺席**那一档 —— `NullTranscriberBackend` 恒 `health = normal`、`backlog_depth() = 0`、`segment_timeout() = 20.0`，收尾预算的输入因此取 `0` 段 / `20.0` 秒这两个缺省值，转写这一侧一行都不产。**缺席档的 `normal` 与真实现空闲态的 `stopped` 各说各的事**：`normal` 说的是「这个构建里没有转写这件事」，`stopped` 说的是「有转写、这一档没在跑」（真实现没起过场次时报的就是它）。

`null` 替身**不长**合成电平或故障注入那类测试逻辑 —— 那是开发替身（`backends/dev.py`）的事。

## 3. 后端怎么被找到

`meeting/backends/registry.py` 是**解析的唯一入口**。三个解析函数：

| 函数 | 解析谁 | 约定路径（`auto` 按它找） |
|---|---|---|
| `resolve_capture_backend()` | `capture.backend` | `meeting.backends.capture:CaptureBackend` |
| `resolve_transcriber_backend()` | `transcriber.backend` | `meeting.backends.transcribe:TranscriberBackend` |
| `resolve_post_hook()` | `post.hook` | `meeting.backends.post:PostProcessHook` |

取值口径：

| 取值 | 含义 |
|---|---|
| `auto`（默认） | 按上面的约定路径找 |
| `null` | 直接用 `null` 替身 |
| `dev` | 用开发替身，**按 key 分流**：`capture.backend` 落 `meeting.backends.dev:DevCaptureBackend`、`transcriber.backend` 落 `meeting.backends.dev:DevTranscriberBackend`（会后钩子这一档没有 dev 替身） |
| `module:Class` | 显式指定 |

取到的是**类就无参实例化**（后端构造函数不许有必填参数，运行期依赖一律走 `bind()`）。导入失败、找不到、取到的对象不可调用、无参实例化失败 → **回退替身 + 一条 warning**，服务不会因为后端缺席或写坏而起不来。

### 解析结果怎么喂给收尾

解析函数返回一个 `BackendResolution`，它分开回答两个问题：

| 字段 | 取值 | 说的是什么 |
|---|---|---|
| `has_implementation` | `true` / `false` | 这个构建里**有没有**可执行实现。`true` = 解析到了实现（`null` / `dev` 替身也算）；`false` = 缺席（转写后端缺席、会后钩子缺席、显式取值解析失败） |
| `run_ok` | `null` / `true` / `false` | 那个实现**跑起来成不成**。被调用之前是 `null`，调用走完是 `true`，调用抛异常或超时是 `false` |

判定规则：`has_implementation == false` → 该步记 **`skipped`**；`has_implementation == true` 且 `run_ok == false` → 该步记 **`failed`**。**缺席记「跳过」，在场却抛错记「失败」** —— `has_implementation` 只由解析结果回答，调用阶段的异常只许写进 `run_ok`。

`capabilities_of(implementation)` 另外回答采集实现有没有 `finalize` / `transcode` / `dev_inject` 三项能力：`null` / `dev` 这类不写真实音频的替身没有前两项，于是收尾第 1、2 步记 `skipped`；真采集后端（`meeting.backends.capture:CaptureBackend`）前两项都是 `True`，收尾第 1、2 步因此会真的调它（这两步的闸门**只看采集侧**：解析结论与 `capture_capabilities["finalize"]` / `["transcode"]`，transcode 是采集后端的能力，转写后端在不在场与这道闸门无关 —— 见 `docs/state.md` 的「收尾四步与终态」）；`dev_inject` 这一项只有 dev 替身在 —— 真后端**有意不加** `inject_fault`，给真后端加上它会把开发替身的非产品动作在真后端上打开。

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
- V1 里可搬的转写退化防护逻辑与参数标定：`docs/legacy-assets.md`（1.A / 1.B）
- 索引：`docs/spec.md`
