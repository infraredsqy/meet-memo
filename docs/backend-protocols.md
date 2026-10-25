# meeting · 后端协议

> 服务与后端之间的接口规格：四个协议的成员与语义、缺席档、解析与注入规则。给要实现或替换一个后端的人读。
>
> 协议与数据结构在 `meeting/backends/base.py`，替身在 `null.py` / `dev.py`，解析的唯一入口是 `registry.py`。四个配置键的当前值与默认值正本在 `docs/service.md`。

## 1. 协议成员与语义

### `CaptureBackend` —— 采集

| 成员 | 语义 |
|---|---|
| `frame_interval` | **类属性**（秒）：每帧事实的间隔。协议默认 `0.5`，dev 替身写 `0.1`；runtime 读不到或非正数按 0.5 算 |
| `bind(emit, *, config)` | `probe()` 之前调一次。`emit` 是事实出口，`config` 是只读配置视图 |
| `probe() -> list[SourceProbe]` | 枚举并试开设备，两路各一条（`mic` / `loopback`） |
| `start(mode, session_dir)` | 按定下的 `mode` 起采集、开始写归档。单路起不来不抛错：只给那一路发一条 `SourceLost`，另一路照跑；该采的路全部起不来才抛 `CaptureUnavailable`。抛错前自己拉起的线程自己收干净 |
| `finalize()` | 收尾第 1 步：停流 + join + 关文件 + `fsync`。幂等 |
| `transcode()` | 收尾第 2 步：归档转 MP3。可抛错（该步据此记 `failed`，原始归档原样留下） |
| `stop()` | 停采集（进程关停时调）。幂等，没落过盘就先 `finalize()` |

`SourceProbe.status` 只许报这三档（快照里另外四档由状态迁移写值）；报了取值域之外的档，runtime 记 warning 并落 `probe_failed`，服务照常起：

| 档 | 语义 |
|---|---|
| `ready` | 挑到了端点、能打开并试开读回过样本。读回全零也落这档——房间安静或远端没出声读到的就是零，不是故障 |
| `probe_failed` | 一个能打开的端点都没有 |
| `unprobed` | 未探测 |

后端只报原始事实，不判告警、不带阈值语义（静默秒数不在协议里——服务侧按 `alert.silence_dbfs` 逐帧累计判，判据正本 `docs/state.md`）：

| 事实 | 字段 |
|---|---|
| `LevelFrame` | `source` / `ts`（后端给）/ `dbfs` / `peak` / `bar` |
| `SourceFacts` | `source` / `stream_alive` |
| `SourceLost` | `source` / `reason` |

导入顺序（C10，出处 `docs/v1-lineage.md`）：先 `import transformers`、再 `import speechbrain`；反过来 speechbrain 会抢做依赖探测，出现难定位的版本不匹配。`base.py` 模块注释同文。

### `TranscriberBackend` —— 转写

| 成员 | 语义 |
|---|---|
| `bind(line_sink, *, config)` | `start()` 之前调一次。`line_sink` 可从任何线程调，后端每出一行调它一次 |
| `start(mode, session_dir)` / `stop()` | 起停转写。起步失败不阻塞录音（转写坏了可以重跑，R1） |
| `health()` | `normal` / `behind` / `stopped`，直接进快照的 `transcript.health`。已停 / 没起过场次 / 模型加载或识别出事 → `stopped`；积压压过 20 段或读线程出事 → `behind`；其余 `normal` |
| `backlog_depth()` / `segment_timeout()` | 积压段数与单段超时（秒），供收尾预算与提示级用。队列无界、不做 busy-drop（R2 的语义）；单段超时缺省 20.0 |
| `release_gpu()` | **可选成员**：把会中模型移出显存。幂等；没加载过就 no-op；等在飞推理至多 5 秒（到点照旧丢引用并记 warning）；丢引用之后才清一次缓存。抛错不拖垮整场——调用方记一条 log。5 秒的取值依据见 `docs/asr-backends.md` |
| `acquire_gpu()` | **可选成员**：把会中模型拉回显存。已在手就 no-op；经同一个 factory 重造；造不出来只记 warning、这次动作照常；不碰 `_stopped` 与 `health()`。权重真正的加载在继续之后的第一段转写 |

调用方（收尾第 4 步、暂停 / 继续）防御式取这两个可选成员：取不到、不可调用就跳过，替身省掉这两格照样跑得通。方向只有一条：服务把 sink 交给后端、后端往里送，后端不读服务的状态。行产物格式与失败占位符的正本在 `docs/state.md`（B12 / B13）。

### `PostProcessHook` —— 会后钩子（解析落点）

不带任何钩子方法：收尾第 4 步不拉起它；「生成会后精细稿」按钮走 `run_session(..., minutes=False)` 直路，`post.hook` 在那一侧只答「这一层接没接」（`null` → 按钮 404）。`capabilities_of()` 靠 `__module__` 把它当本实现、不当替身；它不带 `finalize` / `transcode`——那两项是采集侧能力，这两个键上的闸门不看它。

### `MinutesHook` —— 会后纪要钩子

| 成员 | 语义 |
|---|---|
| `claim(session_dir)` | **可选同步成员**：把「这一层在飞」落到盘上；盘上已停着一版 `running` 时抛 `AlreadyRunningError`。`post_minutes` 按钮在建线程之前调它（runtime `getattr` 找，取不到就跳过登记、照旧走线程体） |
| `launch(session_dir, *, force, claimed)` | 返回即算启动成功（不等跑完），返回之前先把 `state = "running"` 同步落盘。`force=True`（`meeting post --force` 那趟）：盘上已有 `running` 时建线程之前就抛 `AlreadyRunningError`，带 `force` 放行；`claimed=True`（按钮那趟 `claim()` 登记过）：不再重复登记 |

线程里的异常落进状态文件、不外传；重依赖在函数体内 import。这一层吃什么、经哪几步、怎么手动跑：`docs/minutes.md`。

## 2. 缺席档

| 替身 / 占位 | 行为 |
|---|---|
| `NullCaptureBackend` | 两路都 `unprobed`、`device_name` 为 `null`；`start()` 抛 `CaptureUnavailable`（服务回 `503 capture_unavailable` + 一句人话）；`finalize()` / `transcode()` / `stop()` 是安全空实现 |
| `NullTranscriberBackend` | 恒 `health = normal`、`backlog_depth() = 0`、`segment_timeout() = 20.0`；起停是安全空实现 |
| 两个钩子 | 没有替身，缺席占位在 `registry.py`；缺席那一档根本不会被调用（调用方先问 `has_implementation`）：界面上对应按钮回 404 `action_unavailable`，`meeting post` 补纪要的那一路按缺席档登记 |

采集与转写的缺席档只在显式配置（`null`，或解析失败回退）时出现；默认 `auto` 都解析到真实现——只有设备全起不来时采集才回 `503`。**缺席档的 `normal` 与真实现空闲态的 `stopped` 各说各的事**：`normal` 说「这个构建里没有转写这件事」，`stopped` 说「有转写、这一档没在跑」。null 替身不长合成电平那类测试逻辑——那是 dev 替身的事。

## 3. 解析与注入

`registry.py` 四个解析函数按约定路径找：

| 函数 | 解析谁 | `auto` 的约定路径 |
|---|---|---|
| `resolve_capture_backend()` | `capture.backend` | `meeting.backends.capture:CaptureBackend` |
| `resolve_transcriber_backend()` | `transcriber.backend` | `meeting.backends.transcribe:TranscriberBackend` |
| `resolve_post_hook()` | `post.hook` | `meeting.backends.post:PostProcessHook` |
| `resolve_minutes_hook()` | `minutes.hook` | `meeting.backends.minutes:MinutesHook` |

| 取值 | 含义 |
|---|---|
| `auto`（默认） | 按约定路径找 |
| `null` | 采集用 null 替身；两个钩子没有替身可用，这一档就是缺席 |
| `dev` | 开发替身，**按 key 分流**（只认采集与转写两个键；钩子键写 `dev` 会落到采集替身——它没有 `launch()`，离线尾巴如实记失败） |
| `module:Class` | 显式指定 |

- `minutes.hook` 多一层：配置里写 JSON 的 `null` 也在缺席档（不是「回退 `auto`」）。它是 config 里唯一「`null` 是取值、原样留着」的可空键——归一成空串或默认值都会把「关掉这一步」这件事吃掉。
- 构造规则：**类就无参实例化**（构造函数不许有必填参数，运行期依赖一律走 `bind()`）；导入失败、找不到、不可调用、实例化失败 → **回退替身 + 一条 warning**，服务不因后端缺席或写坏而起不来；**`emit()` / `line_sink()` 可从任何线程调**（服务在 `bind()` 里包一层线程安全投递，后端不必知道事件循环在哪，也不许自己碰状态）；**`config` 是只读视图**（`get(key)` 按点号路径取值、`as_dict()` 拿整份生效值，后端读配置、不能改配置）。

解析结果 `BackendResolution` 分开回答两个问题。判定规则：`has_implementation == false` → 该步记 `skipped`；`true` 且 `run_ok == false` → 记 `failed`。**缺席记「跳过」，在场却抛错记「失败」**——调用阶段的异常只许写进 `run_ok`：

| 字段 | 取值 | 说的是什么 |
|---|---|---|
| `has_implementation` | `true` / `false` | 这个构建里**有没有**可执行实现（null / dev 替身也算「有」）；`false` = 缺席 |
| `run_ok` | `null` / `true` / `false` | 那个实现**跑起来成不成**：被调用之前 `null`，走完 `true`，抛异常或超时 `false` |

`capabilities_of(implementation)` 另答采集实现有没有 `finalize` / `transcode` / `dev_inject` 三项：null / dev 没有前两项，收尾第 1 步记 `skipped`；真采集后端前两项都是 `True`（闸门只看采集侧）。`finalize` 落定那一刻，有 `transcode` 能力的构建自动在后台起转码独立工序（不占收尾预算，判据见 `docs/state.md`）。`dev_inject` 只有 dev 替身有——真后端**有意不加** `inject_fault`，加上会把非产品动作在真后端上打开。

`post.engine`（会后离线引擎）与两个钩子**分开解析**，换引擎时 `post.hook` 一字不动：`auto` 按约定路径落 `meeting.offline.engine:MossOfflineEngine`，`module:Class` 按它导入，`null` 当场抛错——这一层没有替身引擎。`post.dtype` 收引擎认得的几个名字，写错启动时就拦下。其余会后两层的配置键（`post.model` / `post.dtype` / `post.chunk_seconds`，纪要四键 `minutes.chunk_lines` / `minutes.max_segments` / `minutes.timeout_seconds` / `minutes.dsh_url`）的当前值与默认值正本在 `docs/service.md`，取值依据见 `docs/asr-backends.md`；都非热读、改了要重启。

## 4. dev 替身（采集与转写）

`dev.py` 是只给开发与测试用的非产品路径，默认关闭，取值 `dev` 按 key 分流：

- `DevCaptureBackend`：报两路 `ready`（`dev-mic` / `dev-loopback`），每 100ms 发一帧合成电平，接受三档故障注入（`silence` / `stall` / `lost`）；不写真实音频。
- `DevTranscriberBackend`：每 0.5 秒产一行两路轮换的合成转写；不读归档、不加载模型、不写产物文件，`health()` 恒 `normal`。它没有环境变量——`MEETING_CAPTURE` 只映射 `capture.backend`，开转写替身要写配置文件。

用法、三档故障的语义与恢复口径见 `docs/state.md`。

## 5. 相关文档

- 配置键与默认值正本：`docs/service.md`
- 四态、快照、收尾与转码、行格式与占位符、数字静音门、dev 替身用法：`docs/state.md`
- 纪要层流程与手动跑法：`docs/minutes.md`
- 两套模型的选型与参数依据：`docs/asr-backends.md`
- V1 的防护逻辑与标定（C10 / B12 / B13）：`docs/v1-lineage.md`
- 索引：`docs/spec.md`
