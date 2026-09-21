# meeting · 状态、场次与推送

> 这份文档写**服务吐出来的那几样东西**：四态状态机怎么走、状态快照长什么形状、一场会在盘上留下什么、心跳怎么算、告警怎么判、SSE 推什么帧。
>
> 读者是想接这份数据的下游开发者（前端、验收脚本、会后流程）。
>
> 代码落点：状态机与全部取值域在 `meeting/state.py`，场次目录与 `session.json` 在 `meeting/session.py`，告警判定在 `meeting/alerts.py`，推送在 `meeting/service/hub.py`，这一切的持有者是 `meeting/service/runtime.py` 的 `ServiceRuntime`。HTTP 面见 `docs/api.md`；**可机读的那份对外形状**（逐键 schema、帧形状、`code`/`detail` 表）见 `docs/contract.md` —— 这份文档与它冲突时以代码实际行为为准，权威落在契约那一本。

## 章节目录

| 章 | 标题 |
|---|---|
| 1 | 四态与迁移 |
| 2 | 状态快照的形状 |
| 3 | 场次目录与 `session.json` |
| 4 | 心跳与 `recording_alive` |
| 5 | 告警 |
| 6 | SSE：四个事件与帧形状 |
| 7 | dev 采集替身与故障注入 |

## 1. 四态与迁移

顶层状态只有四个：`idle` / `recording` / `alert` / `ended`。**没有第五个。**

| 状态 | 怎么进来 | 怎么出去 |
|---|---|---|
| `idle` | 进程刚起、还没开过任何一场（包括启动时扫到未收尾场次、挂了 `notice` 的那一种） | `start` 成功 → `recording` |
| `recording` | `start` 成功，已选的路都在采 | 某路判 `lost` → `alert`；`stop` → `ended` |
| `alert` | 某路静默连续达 `alert.silence_seconds`，或后端报该路流中断 | 该路连续有信号达 `alert.recover_seconds` → `recording`；`stop` → `ended` |
| `ended` | `stop`。子状态 `phase` 从 `draining` 走到 `done` | 下一场 `start`（`phase == "done"` 时放行）→ `recording`；收尾没走完的那一档不放行 |

三条铁律，代码与测试都盯着：

1. **进 `alert` 不等于停录**：没出事的路照常采、`elapsed_seconds` 照常走；快照里同时看得见哪路 `capturing`、哪路 `lost`。
2. **采集出什么事都不自动结束会话**：所有路都挂了也只是 `alert`；进 `ended` 只有 `stop` 一条路。
3. **心跳失联不进状态机**：`迟到` / `失联` 由界面按 `/api/heartbeat` 自己判，服务侧没有由界面心跳驱动的迁移。

### 收尾四步与终态

`stop` 进 `ended·draining`，时长就地冻结，然后按固定顺序跑四步：

| 序 | 步 | 这一步干什么 | 现在谁来填实现 |
|---|---|---|---|
| 1 | `flush` | 停采集、把归档落盘（后端 `finalize()`） | `dca4539b` |
| 2 | `transcode` | 收尾转换（后端 `transcode()`） | `dca4539b` |
| 3 | `log` | 写这一场的结束标记（`session.json` + 服务级主日志） | 本服务（永远在场，永远 `done`） |
| 4 | `post` | 拉起会后流程（**启动即算完成，不等它跑完**） | `65dd3e4c` |

每步的 `status` 取五档之一：`waiting` / `running` / `done` / `failed` / `skipped`。

- **`skipped` = 这个构建里没有真实实现**（采集替身不写真实音频、转写后端缺席、会后钩子缺席）。它不阻塞 `done`，只留一行 warning。
- **`failed` = 有实现，但这次调用抛错**。它带 `error`。
- 判「缺席」只看后端的解析结果（`meeting/backends/registry.py`），不看调用时抛了什么异常。

**终态判定只在一处**（`finish_drain()`）：

| 四步结果 | `ended.phase` | `top_state` | `session` | 新的 `start` |
|---|---|---|---|---|
| 四步全部落定（`done` / `skipped`）且无 `failed` | `done` | 停在 `ended` | 保留（时长冻结，界面画「已结束·已完成」） | 放行，从 `ended` 直接进 `recording` |
| 有任一步 `failed`，或四步没全落定就走到了出口 | 保持 `draining` | 停在 `ended` | 保留 | `draining`(409)，`detail` 带失败步与场次 log 路径 |

骨架构建（默认 `null` 替身或 dev 替身 + 转写缺席 + 钩子缺席）跑一场会的预期终态是固定的：`flush: skipped` / `transcode: skipped` / `log: done` / `post: skipped`，于是 `ended.phase == "done"`。

### 收尾怎么跑：触发、共享预算、超时与中断

**谁起这一趟**：`POST /api/actions {"action":"stop"}` 成功之后，以及录音 / 告警态下**第一次 `Ctrl+C`** —— 后一条走的是**同一条** `stop_session(reason="ctrl_c")` 路径。非 `recording` / `alert` 态收到 `stop` 回 `not_recording`(409)，不起收尾。

**每一步的通用节奏**（四步都照这个走）：

1. 该步置 `running` → 写一次 `session.json` → 广播一次全量状态；
2. 主 log 记一条该步的**开始**（带步 id）；
3. 执行该步的调用；
4. 落 `done` / `skipped` / `failed`（`detail` 写人话）→ 再写一次 `session.json` → 主 log 记一条**结束 + 耗时** → 再广播一次。

某一步 `failed` 之后，**后续步骤照常按顺序记账**（不许整段抛出而不写盘）。四步全部落定之后，终态由 `finish_drain()` 一处判定。

**四步共享一条 deadline**。收尾一开跑就先算这一趟的总上限：

```
budget = 30 + backlog × (segment_timeout + 5)     下限 30 秒、封顶 300 秒
```

`backlog` ← `TranscriberBackend.backlog_depth()`，`segment_timeout` ← `TranscriberBackend.segment_timeout()`；转写后端缺席时取 `0` / `20.0` 秒。每一步的调用都包在**剩余预算**里（`asyncio.wait_for(<该步调用>, <剩余预算>)`），所以一趟收尾的等待总量以那条 deadline 为界，**不随步数翻倍**。这三个数硬编码在 `meeting/service/runtime.py`，**不开配置键**。

- **唯一的例外是 `log` 步**：它的 flush 与摘除至少拿到 5 秒（`max(<剩余预算>, 5)`，不受剩余预算约束）。`log` 是四步里唯一在任何构建里都有实现、要做 IO 的一步；让前面的缺席步把 deadline 用光，本该「正常收尾」的一档就会被拖成收尾失败档。
- **超预算的那一步落 `failed`**，`error` 写明「超出收尾预算（budget=Ns，实测耗时 Ms）」—— 到点就扔下积压段走的写法在代码里不存在。
- 预算的输入（`backlog` / `segment_timeout`）、算出来的 `budget`、以及每一步的实测耗时**逐条进 log**。

**收尾期间按第二次 `Ctrl+C`：立即中止，顺序写死。** 先把盘上写成 `state: "draining"` + `interrupted: true`（**原 `steps` 原样保留**、不写 `done`、**不写 `ended_at`**），再对收尾 task 调 `cancel()`（句柄已经是「已结束」时跳过 `cancel()`、只做落盘）。取消时的记账只补步骤状态，**不覆盖刚才那一版的 `interrupted` / `state`**；唯一的例外是「信号到达时四步恰好已全部落定」——这一档盘上最终值按终态规则由 `finish_drain()` 那一趟落。进程随后以强制打断档退出（取值见 `docs/service.md` 的退出码表）。

**收尾走完不回 `idle`**：成功档与失败档都停在 `ended`，`idle` 只留给「进程刚起、还没开过任何一场」。下一场的 `start` 从 `ended` 直接进 `recording`。

## 2. 状态快照的形状

快照是**顶层十键**的对象，`GET /api/state` 的 body 与 SSE 首帧 `snapshot` 是同一个 `to_dict()` 出的同一份东西。

| 顶层键 | 取值 |
|---|---|
| `revision` | int，进程内单调递增；每次服务启动从同一个起点重新计，不跨进程延续、不落盘 |
| `service` | 六子键：`started_at` / `version`（非空字符串）/ `port` / `url` / `log_path` / `out_root`；启动时定一次 |
| `top_state` | 四态之一 |
| `session` | `null`，或 `{id, dir, started_at, mode, elapsed_seconds, frozen}` |
| `sources` | **恰好两项**（`mic` / `loopback`），见下表 |
| `alerts` | `{critical: Alert\|null, hints: [Alert]}` |
| `transcript` | `{health, last_line_at, buffered_lines, failed_segments}` |
| `ended` | `null`，或 `{phase, steps: [DrainStep], error}` |
| `settings` | `{alarm_sound, heartbeat_interval_seconds, asr{…}, devices{mic, loopback}}` |
| `notice` | `null`，或 `{kind, text, session_dir, log_path}` |

`sources[*]` 九键：`id`（`mic` / `loopback`）/ `label`（`本地` / `远端`）/ `device_name` / `health` / `lost_since` / `restored` / `level_dbfs` / `peak_dbfs` / `envelope`。

- `health` 七取值：`unprobed` / `ready` / `probe_failed` / `capturing` / `lost` / `restored` / `stopped`。前三档由启动时的设备探测直取（`probe()` 只报这三档），后四档由迁移写值。
- `envelope` 是最近 30 秒的电平，**15 根柱、一根 2 秒**。一根柱的值 = 它那 2 秒窗口内所有 `LevelFrame.peak` 的最大值；窗口里一帧都没有就沿用左边那根。会话开场（还没开始录）是空数组 `[]`。这根柱由服务算，**前端不许自己造**。

`session` 的语义：

- `id` 就是场次目录名（`250906` 这种），`dir` 是场次目录路径 —— 这两个名字全链路统一，与 `session.json` 里的字段名逐字一致。
- `mode ∈ both / mic / loopback`，开场前定死，录音期间不切。
- `elapsed_seconds` 在 `recording` / `alert` 态走秒，进 `ended` 就冻结。
- 一个容易看错的点：**`top_state` 停在 `ended` 而那场的 `session` 还留着**（成功档与失败档都留）。界面据此画「已结束·已完成」，直下一场 `start` 才清。

`ended` 的语义：

- `phase` 只有 `draining` / `done` 两取值。`failed` 属于盘上 `session.json` 的 `state`，在快照里没有对应位置。
- `steps` 是四步各一条，每条四键齐：`{id, status, detail, error}`。`id` 的顺序固定在 `DRAIN_STEP_IDS = ("flush", "transcode", "log", "post")`。
- 收尾失败后 `phase` 保持 `draining`、这份摘要与 `session` 一起留在快照里。

`settings` 的语义：

- `asr` 的五子键与配置键 `asr.*` 逐字同名（去掉前缀），取当前生效值：`max_segment_seconds` / `timeout_seconds` / `max_new_tokens` / `repeat_max_unit` / `repeat_min_coverage`。
- `devices` 的两个子键 `mic` / `loopback`（`dca4539b` 定名）与配置键 `devices.mic` / `devices.loopback` 逐字同名（去掉前缀），取当前生效值：值为**设备名字符串**，没配就是 `null`（按默认策略挑：mic 先挡掉 `CABLE Output` 这类抓取型虚拟端点，再拿**系统默认输入端点**的名字凑同名候选、按 host API 排序试开；loopback 优先 `CABLE Input` 且不含 `16ch`）。空值形状两端一致 —— 配置文件里也写 `null`。
- 这里读到的是**运行期生效值**。非热读的键改了要重启才在快照里可见，详见 `docs/service.md` 第 4 章的键态两分。

`transcript` 的语义：`health ∈ normal / behind / stopped`，`buffered_lines` 是积压段数、`failed_segments` 是累计识别失败段数。**这两个计数只进 log 与 `/debug`**，产品界面不摆它们。转写停了不影响录音、电平与告警，只影响总结入口。骨架阶段（没有转写后端）恒为 `normal` + 0。

`notice` 的语义：现在只有 `kind = "restart_unfinished"` 一种，`text` 是「服务重启过。上一场的收尾没走完，音频在 <场次目录>，日志在 <服务级 log 路径>。」，`session_dir` 指没收完的那一场，`log_path` 指**本次启动的服务级日志**（与 `service.log_path` 同一处取值，不是场次日志）。

### 一次变更 = 一次 revision

`revision` 的递增与广播只经 `ServiceRuntime.commit()` 一处：同一次事实处理里的多次改动合并成一次 `+1`、一次全量快照。被状态机拒绝的动作**不涨 `revision`、不写盘、不广播**。

## 3. 场次目录与 `session.json`

- 输出根 = `output.root`（默认 `%USERPROFILE%\Documents\meeting\out`）。**根不存在时在 `start` 那一刻建**，服务启动与扫描都不建。
- 场次目录名 = `YYMMDD`，同名已存在就让位到 `YYMMDD-2`、`YYMMDD-3`……**让位不复用**。
- 一场会一个目录，目录里只有一份 `session.json` 是服务现在写的（音频字节归 `dca4539b`）。
- `session.json` **原子写**：同目录临时文件 + `os.replace` + `flush`。**每个阶段变更都写一次**（`start` 的第一版、进收尾中、每一步的 `status`/`detail`、终态那一版），被打断不留半截文件、输出根里不留 `.tmp` 残骸。

字段全貌（**字段名与字段个数是冻结项**，示例里的取值只是某一版的示意）：

```json
{
  "session_id": "250906", "dir": "...", "mode": "both",
  "state": "recording | draining | done | failed",
  "created_at": "...", "started_at": "...", "ended_at": "... | null",
  "sources": [
    {"id": "mic", "device": "... | null", "archive": "meeting_audio_mic.pcm"},
    {"id": "loopback", "device": "... | null", "archive": "meeting_audio_loopback.pcm"}
  ],
  "drain": {"steps": [
    {"id": "flush", "status": "waiting", "detail": "", "error": null},
    {"id": "transcode", "status": "waiting", "detail": "", "error": null},
    {"id": "log", "status": "waiting", "detail": "", "error": null},
    {"id": "post", "status": "waiting", "detail": "", "error": null}
  ]},
  "interrupted": false,
  "service": {"pid": 1234, "started_at": "..."}
}
```

- `state` 的四取值与顶层四态是**两套词汇**（`alert` 在盘上写 `recording`：告警不是盘上的一档，会话还在录）。映射只有一处实现，读回时只做集合成员判断。
- `sources[*].archive` 的形状固定为 `meeting_audio_<source>.pcm`。音频字节的写入归 `dca4539b`。
- **一场会的事实全在这一份文件里**，不另开服务级状态文件。重启靠它判断上一场走没走完，`meeting post` 也靠它认场次。
- `start` 阶段写这份文件失败 → **不进 `recording`**，回 503 并把刚建的场次目录清掉、在主 log 留一条痕（「没有落痕就开始录」是不许的）。

### 重启自洽

服务启动时扫输出根，取**最新的一场**，判据只有一条：`state ∈ {recording, draining}` **或** `interrupted: true`。

命中就做两件事：在空闲态挂 `notice.restart_unfinished`；把那份记录**改写**成 `state: "draining"` + `interrupted: true`，**原 `steps` 原样保留、不伪造终态**。被强制打断的场次盘上本来就是这一副面孔，重启扫描照同一条路把它认领下来。

- **绝不自动续录、绝不把状态恢复成「录音中」**。
- 扫描只写这一处：只动那一份 `session.json`，场次目录里其余文件逐字不变，不新建任何目录。
- **这一场还要拦住新场次**：新的 `start` 回 `draining`(409)，`detail` 带场次目录 + 失败步 + 场次 log 路径。放行会让第一次 `start` 把现场覆盖掉。
- `done` / `failed` 的场次不触发 `notice`、不被改写。
- 输出根不存在时，扫的结果就是「没有未收尾场次」，并且**不趁机建目录**。

## 4. 心跳与 `recording_alive`

`GET /api/heartbeat` 六字段：`service`（恒 `"meeting"`）/ `server_time` / `revision` / `top_state` / `recording_alive` / `seconds_since_last_tick`。它在**任何状态**下都 200。

- `recording_alive` 的判据是**服务侧的采集帧时钟**：只认 `LevelFrame`（`SourceFacts` / `SourceLost` 不刷新这个钟），窗口 = `max(2.0s, 3 × frame_interval)`，两档算出来都是 **2.0 秒** —— 说白了就是「两秒没收到一帧事实 = 采集停了」。
- `frame_interval` 由采集后端在 `bind()` 之后**以实例属性**报上来（协议默认 `0.5s`，dev 替身 `0.1s`）。读不到或取值不是正数就按 `0.5` 算，只记一条 warning，不阻塞 `start`。
- `seconds_since_last_tick` 是距最近一帧事实的秒数；**一帧都没收到过就是 `null`**。
- 取值分档：`recording` / `alert` 态给 `bool`；`idle` / `ended` 态给 `null`。
- 界面那个 10 秒心跳周期（`heartbeat.interval_seconds`）**绝不参与**这个窗口的计算 —— 它管的是「界面还看得见服务吗」。
- 「判定权在服务侧」的硬判据：后端卡住不报任何东西时，`recording_alive` 由帧时钟算出来就是 `false`，HTTP 照常 200。

## 5. 告警

**服务侧是告警的唯一判定者。** 采集后端只报原始电平（`LevelFrame`，每 500ms 一帧），静默秒数由 `meeting/alerts.py` 按阈值逐帧累计，且**一律用帧自己的时间戳**，不拿墙钟做差。

| 配置键 | 默认 | 作用 |
|---|---|---|
| `alert.silence_dbfs` | `-60` dBFS | 低于这个值算「这一路在静默」 |
| `alert.silence_seconds` | `180` | 静默**连续**这么久才起致命级（正常句间停顿有 31–56 秒全零段，判据必须是连续性） |
| `alert.recover_seconds` | `10` | 连续有信号这么久才算恢复 |
| `alert.alarm_suppress_seconds` | `60` | 同一事件（哪一路 + 故障类型）这么久内不重复出声 |

分级与合并：

- **致命级**（某路采不到）进 `alerts.critical`，**同时只留一条**。
- **提示级**进 `alerts.hints`，**同类合并、上限 3**。合并的键是 `(level, source, message)` 三元组，同一个键再次出现时沿用原来那一条的 `since`、把 `duration_seconds` 刷新到当下；到上限后又来新的键，就留下 `since` 最早的三条，多出来的只进 log。两条提示级现在共用 `source = transcript`，靠 `message`（也就是故障类型文案）区分。
- 提示级现在有两个来源（数值只进 log 与 `/debug`）：`transcript.health` 从 `normal` 掉到 `behind` / `stopped`；待转写段数 ≥ 20 段（`BACKLOG_HINT_THRESHOLD`，写死在代码里、不加配置键）。
- **心跳失联那条致命级由界面自己判、自己画**，服务侧不产生它。

`Alert` 六字段：`{level, source, since, duration_seconds, message, muted}`。`level ∈ critical / hint`，`source` 是那一路的 id。

`muted` 说的是**这一条现在会不会出声**，判据是「本场静音生效期间」**或**「这一条还在抑制窗口里」。抑制窗口按「哪一路 + 故障类型」跟踪，**判定与计算全在服务侧**：界面只看这个布尔值，`false` 就响一声、`true` 就不出声，界面不算任何窗口。

两条相关动作见 `docs/api.md`：`mute_session`（会话级，下一场 `start` 自动复位，不改持久化开关 `alarm.sound`）与 `dev_inject`。抑制窗口到点、会话级静音复位、下一场开场之后，同一条告警的 `muted` 会自己回到 `false`。

## 6. SSE：四个事件与帧形状

`GET /api/events`，`Content-Type: text/event-stream`。

- **连上第一帧一定是 `snapshot`**（完整状态），新开的页面直接落在当前状态、**不经过 `idle`**。
- 帧格式一律 `event: <名字>` + `data: <一个 JSON 对象>`。`state` 与 `snapshot` 带 `id: <revision>`；**`levels` 与 `transcript` 不带 `id:`**，只在 `data` 里带当前 `revision` 作参照。
- 开头给一次 `retry: 2000`（断线由浏览器自己重连，**重连后仍然先发 `snapshot`、不重放断开期间的事件**）。
- 安静时每 **15 秒**一行注释帧 `: ping` 保活（只有这一行，不伪造 `event` / `data`）。
- **关页面只做一件事：把订阅者摘掉。** 不动会话、不停采集；最后一个订阅者走了服务照跑。

| 通道 | 什么时候发 | `data` 形状 |
|---|---|---|
| `snapshot` | 建连首帧、慢消费者重同步 | 完整快照；重同步时额外带 `resync: true` 与 `dropped: N` |
| `state` | 每次状态变更（**全量快照**，不做增量） | 完整快照 |
| `levels` | 录音 / 告警态下 **4 次/秒**（两路合在一帧里） | `{revision, frames: {mic: {...}, loopback: {...}}}`，每路带 `ts` / `dbfs` / `peak` / `bar` / `envelope` |
| `transcript` | 每出一行转写 | `{revision, line: {ts, source, text, kind}}` |

- `levels` **不占 `revision`**，只带当前值作参照。
- **`levels` 帧里每一路的 `ts` 恒为 `null`**：这一格的语义是「这条读数的时刻」，而电平帧自带固定节奏（4 次/秒），服务不按帧填它 —— 客户端拿 `data.revision` 与帧到达时刻定位即可。`transcript` 帧的 `line.ts` 是另一回事：那一格由转写后端给，真值可以是非 null。
- 慢消费者：每个订阅者一个**有界队列 512**。满了就清空这个订阅者的队列、塞一份带 `resync: true` 与 `dropped: N` 的 `snapshot`；`levels` 满了只留最新一帧。**一个卡住的页面拖不住别人**，广播路径是同步的、不 `await` 慢消费者。
- 多个连接看的是同一份状态：一次变更同一个 `revision` 同时到所有订阅者。

## 7. dev 采集替身与故障注入

`meeting/backends/dev.py` 是**只给开发与测试用的非产品路径，默认关闭**：只有把 `capture.backend` 配成 `dev`（或 `MEETING_CAPTURE=dev`）才会被解析到。

它给两样东西：

1. **确定性合成电平**：种子是模块级常量 `DEV_LEVEL_SEED`，同一个种子与同一帧序下电平序列逐帧相同；每 **100ms** 一帧事实（`frame_interval = 0.1`），两路 `probe()` 都报 `ready`。
2. **故障注入**：走 `POST /api/actions` 的 `dev_inject` 动作（非 dev 后端回 `action_unavailable`(404)）。

三档故障，**到点生效、到点自动恢复**（`after_seconds` 到点开始、`duration_seconds` 到点结束，`duration_seconds` 缺省 `30.0` 秒）：

| `fault` | 干什么 | 拿它验什么 |
|---|---|---|
| `silence` | 这一路电平落到 −80 dBFS 以下并保持 | 走静默累计 → 静默阈值调小后能进 `alert`，恢复后回 `recording` |
| `stall` | **两路的事实帧整体停发**（HTTP 照常应答） | `recording_alive` 变 `false` 而服务照常 200 |
| `lost` | 直接来一条 `SourceLost`，窗口内这一路整段不发帧 | 流中断立刻起致命级；恢复走 `alert.recover_seconds` |

- 对同一路再次注入（无论故障是否已恢复）**替换前一次注入**并按新的 `after_seconds` / `duration_seconds` 重新计时。
- 三档故障都**不动状态机的迁移路径**：能不能回 `recording` 由 `alerts.py` 的判定说了算。
- 它**不写真实音频**，`finalize()` / `transcode()` 只记账。所以收尾第 1、2 步在它身上记 `skipped`。
- `dev_inject` 要有正在进行的场次：`recording` / `alert` 态可注入，其余态回 `not_recording`(409)。

**拿它写用例时的一个实情**：静默阈值与恢复窗口这些等待是真实时长，而测试侧有一条「单条用例 ≤ 5 秒」的红线（看门在 `tests/conftest.py`）。用例的做法是**把阈值写进临时 `config.json` 调小**（代码默认值一个字不动），同时把轮询间隔与观察窗口收在红线以内 —— 被测的语义没有跟着改，改的只是测试等的时长。

## 8. 相关文档

- **双端契约（端点口径、快照 schema、SSE 帧形状、动作信封与 `code`/`detail` 表）：`docs/contract.md`**
- HTTP 面（端点、动作信封、`code` 表）：`docs/api.md`
- 怎么起服务、配置、日志、退出码、跑测试：`docs/service.md`
- 后端怎么被找到、怎么插进来：`docs/backend-protocols.md`
- 界面怎么画这四态：`docs/ui-design.md`
- 前端工程怎么跑、怎么构建：`web/README.md`
- 契约的可机读基准（25 个 golden 样本与掩码规则）：`contract/README.md`
- 索引：`docs/spec.md`
