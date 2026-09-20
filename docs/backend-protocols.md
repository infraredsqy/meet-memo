# meeting · 后端协议

> 这份文档写服务与后端之间的接缝：三个协议的成员、数据结构、缺省值，以及后端怎么被找到、怎么被注入。
>
> 现状：**协议本身已经定死，真实实现一个都还没有。** 服务里现在生效的是两个 `null` 替身（默认），外加一个只给开发与测试用的 dev 采集替身（默认关闭）。真实的采集、转写、会后钩子由各自的节点落地，落地时按这份协议接进来，服务这一侧不需要改代码。
>
> 协议与数据结构住在 `meeting/backends/base.py`（**只放协议与数据结构**，实现全在下游自己的模块里），替身住在 `meeting/backends/null.py` 与 `meeting/backends/dev.py`，解析的唯一入口是 `meeting/backends/registry.py`。

## 1. 三个协议

### `CaptureBackend` —— 采集

| 成员 | 语义 |
|---|---|
| `frame_interval` | **类属性**（秒）：这个后端每帧事实的间隔。协议默认 `0.5`，dev 替身写 `0.1`。runtime 在 `bind()` 之后读实例上的这个属性算存活窗口；读不到或取值不是正数就按 `DEFAULT_FRAME_INTERVAL_SECONDS = 0.5` 算 |
| `bind(emit, *, config)` | `probe()` 之前调一次。`emit` 是**事实出口**，`config` 是只读配置视图 |
| `probe() -> list[SourceProbe]` | 枚举并试开设备，**两路各一条**（`mic` / `loopback`） |
| `start(mode, session_dir)` | 按开场前定下的 `mode` 起采集，开始往自己的归档里写。两路各自独立，一路起不来别拖死另一路 —— 起不来的那路如实抛错 |
| `finalize()` / `transcode()` / `stop()` | 收尾的落盘与转码两步（各自可抛错），以及停采集 |

后端往里送的事实（**只报原始事实，不判告警、不带阈值语义**）：

| 事实 | 字段 |
|---|---|
| `LevelFrame` | `source` / `ts` / `dbfs` / `peak` / `bar`。`ts` 由后端给（采集线程看到那一刻的时间），服务只拿它做窗口比较与静默累计 |
| `SourceFacts` | `source` / `stream_alive` |
| `SourceLost` | `source` / `reason` |

设备就绪三态是 `SourceProbe.status`，取值 `ready`（能打开）/ `probe_failed`（能打开但没信号）/ `unprobed`（未探测）；`detail` 是写一句给界面看的人话。**`probe()` 只许报这三档** —— 快照里另外四档（`capturing` / `lost` / `restored` / `stopped`）由状态迁移写值。后端报了取值域之外的第四档，runtime 记一条 warning 并把该路落 `probe_failed`，服务照常起。

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

这是**默认**行为：什么都没配时服务照常起、照常发页面，只是不会有任何东西被录下来。此时 `start` 回 `503 capture_unavailable`，盘上不留任何场次目录。

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
| `dev` | 用开发替身（**只有 `capture.backend` 这一档有东西可解析**：`meeting.backends.dev:DevCaptureBackend`） |
| `module:Class` | 显式指定 |

取到的是**类就无参实例化**（后端构造函数不许有必填参数，运行期依赖一律走 `bind()`）。导入失败、找不到、取到的对象不可调用、无参实例化失败 → **回退替身 + 一条 warning**，服务不会因为后端缺席或写坏而起不来。

### 解析结果怎么喂给收尾

解析函数返回一个 `BackendResolution`，它分开回答两个问题：

| 字段 | 取值 | 说的是什么 |
|---|---|---|
| `has_implementation` | `true` / `false` | 这个构建里**有没有**可执行实现。`true` = 解析到了实现（`null` / `dev` 替身也算）；`false` = 缺席（转写后端缺席、会后钩子缺席、显式取值解析失败） |
| `run_ok` | `null` / `true` / `false` | 那个实现**跑起来成不成**。被调用之前是 `null`，调用走完是 `true`，调用抛异常或超时是 `false` |

判定规则：`has_implementation == false` → 该步记 **`skipped`**；`has_implementation == true` 且 `run_ok == false` → 该步记 **`failed`**。**缺席记「跳过」，在场却抛错记「失败」** —— `has_implementation` 只由解析结果回答，调用阶段的异常只许写进 `run_ok`。

`capabilities_of(implementation)` 另外回答采集实现有没有 `finalize` / `transcode` / `dev_inject` 三项能力：`null` / `dev` 这类不写真实音频的替身没有前两项，于是收尾第 1、2 步记 `skipped`；`dev_inject` 这一项只有 dev 替身在。

## 4. 注入与构造规则

- **类就无参实例化**：后端构造函数不许有必填参数，运行期依赖一律走 `bind()`。
- **解析失败就回退**：见第 3 章。服务不会因为后端缺席或写坏而起不来。
- **`emit()` 与 `line_sink()` 可以从任何线程调**：服务在 `bind()` 里包一层 `loop.call_soon_threadsafe(...)` 投递，后端不必知道事件循环在哪，也不许自己碰状态。
- **`config` 是只读视图**（`ReadOnlyConfig`）：`get(key)` 按点号路径取值，`as_dict()` 拿整份生效值。后端读配置，不能改配置。

## 5. dev 采集替身

`meeting/backends/dev.py` 里的 `DevCaptureBackend` 是**只给开发与测试用的非产品路径，默认关闭**：只有把 `capture.backend` 配成 `dev`（或 `MEETING_CAPTURE=dev`）才会被解析到。

它报两路 `ready`（`device_name` 是 `dev-mic` / `dev-loopback`），每 100ms 发一帧合成电平，另外接受三档故障注入（`silence` / `stall` / `lost`）。它**不写真实音频**，`finalize()` / `transcode()` 只记账。

用法、三档故障的语义与恢复口径写在 `docs/state.md` 第 7 章。

## 6. 相关文档

- 配置键与默认值：`docs/service.md` 第 4 章
- 服务侧现状与不可用清单：`docs/service.md` 第 8 章
- 四态、快照、收尾四步、dev 替身怎么用：`docs/state.md`
- V1 里可搬的转写退化防护逻辑与参数标定：`docs/legacy-assets.md`（1.A / 1.B）
- 索引：`docs/spec.md`
