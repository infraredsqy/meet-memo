# meeting · 状态、场次与推送

> 这份文档写**服务吐出来的那几样东西**：四态状态机怎么走、状态快照长什么形状、一场会在盘上留下什么、心跳怎么算、告警怎么判、SSE 推什么帧。
>
> 读者是想接这份数据的下游开发者（前端、验收脚本、会后流程）。
>
> 代码落点：状态机与全部取值域在 `meeting/state.py`，场次目录与 `session.json` 在 `meeting/session.py`，告警判定在 `meeting/alerts.py`，推送在 `meeting/service/hub.py`，这一切的持有者是 `meeting/service/runtime.py` 的 `ServiceRuntime`；会后那一层在 `meeting/offline/`（`artifacts.py` 管产物的写盘与字段契约、`runner.py` 管主流程），拉起它的钩子是 `meeting/backends/post.py`。HTTP 面见 `docs/api.md`；**可机读的那份对外形状**（逐键 schema、帧形状、`code`/`detail` 表）见 `docs/contract.md` —— 这份文档与它冲突时以代码实际行为为准，权威落在契约那一本。

## 章节目录

| 章 | 标题 |
|---|---|
| 1 | 四态与迁移 |
| 2 | 状态快照的形状 |
| 3 | 场次目录、`session.json` 与会后离线产物 |
| 4 | 心跳与 `recording_alive` |
| 5 | 告警 |
| 6 | SSE：四个事件与帧形状 |
| 7 | dev 替身与故障注入 |

## 1. 四态与迁移

顶层状态只有四个：`idle` / `recording` / `alert` / `ended`。**没有第五个。**

| 状态 | 怎么进来 | 怎么出去 |
|---|---|---|
| `idle` | 进程刚起、还没开过任何一场 | `start` 成功 → `recording` |
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
| 1 | `flush` | 停采集、把归档落盘（后端 `finalize()`：停流 + join + 关文件 + `fsync`） | `dca4539b`（真采集后端已落地） |
| 2 | `transcode` | 收尾转换（后端 `transcode()`：两份 PCM → 64k MP3，**转完保留 PCM**） | `dca4539b`（真采集后端已落地） |
| 3 | `log` | 写这一场的结束标记（`session.json` + 服务级主日志） | 本服务（永远在场，永远 `done`） |
| 4 | `post` | 让会中模型退出显存（转写后端的可选成员 `release_gpu()`；释放失败只记一条 log）。**会后一个动作都不自动跑**：这一格如实停在 `waiting`（＝等你动手），两趟会后活由「生成会后精细稿」/「直接出纪要」按钮或 `meeting post` 拉起 | 本服务（释放那一步，`796bbff` 起；`7c6952e5` 的 `PostProcessHook` 不再由这一步拉起） |

每步的 `status` 取五档之一：`waiting` / `running` / `done` / `failed` / `skipped`。

- **`skipped` = 这个构建里没有真实实现**（采集替身不写真实音频）。它不阻塞 `done`，只留一行 warning。收尾四步里眼下只有第 1、2 步会出现它 —— 第 4 步 `post` 对任何构建都落在 `waiting`，不再有「钩子缺席记 `skipped`」那一档。
- **`failed` = 有实现，但这次调用抛错**。它带 `error`。
- 判「缺席」只看后端的解析结果（`meeting/backends/registry.py`），不看调用时抛了什么异常。

**第 1、2 步的闸门**（`meeting/service/runtime.py` 的 `_drain_flush()` / `_drain_transcode()`）：两步都只看**采集侧**——解析结论**与** `capture_capabilities["finalize"]` / `capture_capabilities["transcode"]`，后者由 `registry.capabilities_of()` 按「实现在不在场」回答：真采集后端（`meeting.backends.capture:CaptureBackend`）两项都是 `True`，`null` / `dev` 两个替身都是 `False`。因此**真采集那一场**这两步记 `done`、产物是场次目录里的两份 `.mp3`；替身那一场照旧 `skipped`。transcode 是采集后端的能力，转写后端在不在场与这道闸门无关（转写缺席不影响第 2 步记账，解析结论与 `run_ok` 也记在采集侧）。

**终态判定只在一处**（`finish_drain()`）：

| 四步结果 | `ended.phase` | `top_state` | `session` | 新的 `start` |
|---|---|---|---|---|
| 四步全部落定（`done` / `skipped`；`post` 那一格的终态是 `waiting`，也算落定）且无 `failed` | `done` | 停在 `ended` | 保留（时长冻结，界面画「已结束·已完成」） | 放行，从 `ended` 直接进 `recording` |
| 有任一步 `failed`，或四步没全落定就走到了出口 | 保持 `draining` | 停在 `ended` | 保留 | `draining`(409)，`detail` 带失败步与场次 log 路径 |

骨架构建（`null` 替身或 dev 替身 + `post.hook = null`）跑一场会的预期终态是固定的：`flush: skipped` / `transcode: skipped` / `log: done` / `post: waiting`（第 4 步对任何构建都停在 `waiting`），于是 `ended.phase == "done"`。

**真采集那一场**（`capture.backend` 取默认值 `auto`，解析到 `meeting.backends.capture:CaptureBackend`）的终态是 `flush: done` / `transcode: done` / `log: done` / `post: waiting`；收尾跑完之后场次目录里躺着两路归档：`meeting_audio_mic.pcm` + `.mp3`、`meeting_audio_loopback.pcm` + `.mp3`（未选中的那一路是 0 字节的 `.pcm`，不产 `.mp3`）；会后那两趟（离线链与纪要层）不自动跑，由「生成会后精细稿」/「直接出纪要」按钮或 `meeting post` 拉起，产物才往同一目录里落（见第 3 章）。第 1 步落盘失败（例如磁盘满）与第 2 步转码失败（例如 `ffmpeg` 不在 PATH 上）各记 `failed`，**PCM 一个字节都不删**。第 4 步停在 `waiting` 说的是**这一格等你动手**（`ended.phase` 仍判 `done`），释放显存那一步自身出事只记一条 log、不改这一格；会后那一趟跑成什么样由场次目录里的 `meeting_offline_status.json` 与 `meeting_minutes_status.json` 回答。

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

快照是**顶层九键**的对象，`GET /api/state` 的 body 与 SSE 首帧 `snapshot` 是同一个 `to_dict()` 出的同一份东西。

| 顶层键 | 取值 |
|---|---|
| `revision` | int，进程内单调递增；每次服务启动从同一个起点重新计，不跨进程延续、不落盘 |
| `service` | 五子键：`started_at` / `version`（非空字符串）/ `port` / `url` / `log_path`；启动时定一次 |
| `top_state` | 四态之一 |
| `session` | `null`，或 `{id, dir, started_at, mode, elapsed_seconds, frozen}` |
| `sources` | **恰好两项**（`mic` / `loopback`），见下表 |
| `alerts` | `{critical: Alert\|null, hints: [Alert]}` |
| `transcript` | `{health, last_line_at, buffered_lines, failed_segments}` |
| `ended` | `null`，或 `{phase, steps: [DrainStep], error}` |
| `settings` | `{alarm_sound, heartbeat_interval_seconds, asr{…}, devices{mic, loopback}}` |

`sources[*]` 九键：`id`（`mic` / `loopback`）/ `label`（`本地` / `远端`）/ `device_name` / `health` / `lost_since` / `restored` / `level_dbfs` / `peak_dbfs` / `envelope`。

- `health` 七取值：`unprobed` / `ready` / `probe_failed` / `capturing` / `lost` / `restored` / `stopped`。前三档由启动时的设备探测直取（`probe()` 只报这三档），后四档由迁移写值。
- `envelope` 是最近 30 秒的电平，**15 根柱、一根 2 秒**。一根柱的值 = 它那 2 秒窗口内所有 `LevelFrame.peak` 的最大值；窗口里一帧都没有就沿用左边那根。会话开场（还没开始录）是空数组 `[]`。这根柱由服务算，**前端不许自己造**。

`session` 的语义：

- `id` 就是工作区目录名（人给的那一个，形状任意 —— 目录名就是这一场会的名字，软件不按日期生成、不解析它；`docs/design-intent.md` 第 3 条），`dir` 是工作区目录的绝对路径 —— 这两个名字全链路统一，与 `session.json` 里的字段名逐字一致。
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
- **这两个子键说的是「用户配的意图」，`sources[*].device_name` 说的是「探测实际落到的那个设备」**（`dca4539b` 的契约陈述，同一段也写在 `docs/contract.md` 第 2 章与 `docs/service.md` 第 8 章）：配了却没命中任何端点时**不报错、也不改写快照里的名字**，按「精确名 → `CABLE Input`（不含 `16ch`）→ 系统默认输出」这条回退顺序走，主 log 一行 `WARNING`（V1 就是这个行为，见 `meeting/capture/devices.py` 的 `loopback_candidates()`）。想知道这一场到底在录哪个设备，看 `sources[*].device_name`。
- 这里读到的是**运行期生效值**。`pending_restart` 那一档的键改了要重启才在快照里可见，`alarm.sound` 与 `asr.*` 一样是热读键、写盘那一刻就换过来，详见 `docs/service.md` 第 4 章的键态两分。

`transcript` 的语义（四格都由转写后端刷新，判定权收在 `meeting/service/runtime.py` 的 `_refresh_transcript_snapshot()` 一处）：

- `health ∈ normal / behind / stopped`，三种取值**直接来自转写后端**（`TranscriberBackend.health()`）。真实现（`meeting.backends.transcribe:TranscriberBackend`）在**没起过场次时报 `stopped`** —— 空闲态快照里那一格当场就是这个值；一场里 worker 停摆、模型加载或识别出事也是 `stopped`，积压压过 20 段（`BACKLOG_BEHIND_THRESHOLD`）是 `behind`。转写后端**缺席**（显式 `null` 或解析失败回退）那一档的缺省实现恒报 `normal`。
- `last_line_at` 是服务侧收到最近那一行转写的墙钟（ISO；这一场还没出过行就是 `null`）。
- `buffered_lines` 是后端报的积压段数；`failed_segments` 是服务侧累计的识别失败段数 —— 正文以 `[[识别失败` 开头的那一行就是失败档，计数在这里 `+1`。**这两个计数只进 log 与 `/debug`**，产品界面不摆它们。
- **只有 `health` 的迁移参与状态变更**：`buffered_lines` 与 `failed_segments` 照样写进快照（`/api/state` 读得到），但不给 `revision` 投票 —— 「积压 5 段变 4 段」不是一次状态迁移。
- **刷新路径四条**：`startup()` 里取一次初值、`start_session()` 里就地刷一次（`stopped -> normal` 跟着那一场自己的提交走）、每来一行刷一次，外加一条 **0.5 秒的低频节拍**（`TRANSCRIBER_HEALTH_INTERVAL_SECONDS`）—— worker 停摆之后不再有行到来，那一档只能靠节拍刷到快照上。读路径（`/api/state`、SSE 首帧）保持纯读：谁读一次都不会涨 `revision`。
- 转写这一侧出事**不影响录音、电平与告警**，录音照跑、状态机一步都不动。

**`notice` 槽位已退场**：原「重启提示（`restart_unfinished`）」随默认输出根一并退场，快照顶层不再有这个键；重启自洽的现行口径见本章末「重启自洽」。

### 一次变更 = 一次 revision

`revision` 的递增与广播只经 `ServiceRuntime.commit()` 一处：同一次事实处理里的多次改动合并成一次 `+1`、一次全量快照。被状态机拒绝的动作**不涨 `revision`、不写盘、不广播**。

## 3. 场次目录、`session.json` 与会后离线产物

- **没有默认落点**（`docs/design-intent.md` 第 4 条）：一场会落在 start 请求 / 命令行**显式给的已存在目录**里；服务不建目录、不按日期命名，目录名就是这场会的名字。日期仍是每一场的元信息（`session.json` 的 `date` 字段，取开场时刻），只是它不再写进目录名。
- 一次只接受一个新场次：目标目录里已经有另一场的 `session.json` 时 `start` 拒绝（`invalid_setting`）。
- 一场会一个目录，目录里只有一份 `session.json` 是服务现在写的。**两路音频归档也落在这里**（`dca4539b` 已落地）：`meeting_audio_mic.pcm` 与 `meeting_audio_loopback.pcm`，16k / 单声道 / int16 小端裸 PCM、无头，**边采边写**（无缓冲追加写，服务被强杀最多丢最后一个块的尾巴）；收尾第 2 步在同目录同名换扩展名产出 `.mp3`（64k），**转码成功之后 PCM 照样留着**（`sources[*].archive` 声明的就是它）。未选中的那一路也建出文件，内容是 0 字节。
- **两路转写各自落一本会中临时稿**（`9e5db89c` 已落地）：`meeting_transcript_mic.md` 与 `meeting_transcript_loopback.md`，追加写、逐行 flush，每行 `[HH:MM] 正文` —— 时间戳取**分钟级**的采集时刻：段一入队就盖上，识别慢也不会把后面几行的时间戳挤到一起。某一段识别不出来也照落一行，正文是**占位符**：`[[识别失败: 超时 20s]]` / `[[识别失败: 重复循环]]` / `[[识别失败: 无输出]]`（双层方括号、裸文本、不带时间戳、**不含 `**`**；识别过程抛错那一档落 `[[识别失败: 错误]]`）；纯噪声的段落一行都不写，连占位符也不写。同一行同时经 SSE 的 `transcript` 通道推给界面（第 6 章）。它是**临时稿**：会后那条带说话人编号的稿另落一份，两份平级放在一起对拍、谁也不顶掉谁（R11，`docs/ui-design.md` 第 3 章的标签口径）。
- `session.json` **原子写**：同目录临时文件 + `os.replace` + `flush`。**每个阶段变更都写一次**（`start` 的第一版、进收尾中、每一步的 `status`/`detail`、终态那一版），被打断不留半截文件、工作区目录里不留 `.tmp` 残骸。

字段全貌（**字段名与字段个数是冻结项**，示例里的取值只是某一版的示意）：

```json
{
  "session_id": "250906", "dir": "...", "date": "...", "mode": "both",
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
- `sources[*].archive` 的形状固定为 `meeting_audio_<source>.pcm`。**这两个文件无条件都列出来**（冻结项），所以采集侧开场就把两份都建出来 —— 未选中的那一路是 0 字节文件，收尾也不给它产 `.mp3`（`dca4539b` 已落地）。
- **一场会的事实全在这一份文件里**，不另开服务级状态文件。`meeting post` 靠它认场次（服务自己不再扫未收尾场次，见本章末「重启自洽」）。
- `start` 阶段写这份文件失败 → **不进 `recording`**，回 503 并把本场写入的这份 `session.json` 收走、在主 log 留一条痕（工作区目录本身是用户给的，服务不删）；「没有落痕就开始录」是不许的。

### 会后离线产物

会后离线那一套跑过之后（`7c6952e5` 已落地；由「生成会后精细稿」按钮或 `meeting post` 拉起 —— 收尾第 4 步不再自动跑它，那一格停在 `waiting`），一场会在场次目录里多出五类文件（转写与对照表按路各落一份，两路的场次一共七份）：底下四份的写盘与字段契约在 `meeting/offline/artifacts.py`，逐块进度账那一份（`a9affaa8` 落地）在 `meeting/offline/progress.py`。这一层用哪个模型、按什么块长切、精度档与显存读数的来由，见 `docs/asr-backends.md`：

| 文件 | 内容 | 读者 |
|---|---|---|
| `meeting_offline_transcript_<source>.md` | 会后精细稿（带说话人编号），每行 `[HH:MM] **S01**: 正文`（**分钟级**） | 人 + 下游对账 |
| `meeting_offline_speakers_<source>.json` | 类编号 → 每段区间的对照表（`start` / `end` 是**秒级浮点**） | 下游程序 |
| `meeting_offline_report.md` | 运行报告：逐路的秒数 / 段数 / 类数 / 墙钟 / 实时率、段耗时分布、失败原因分布、缺了哪几段、没声跳过了哪几段、零时长的段有几段 | 人 |
| `meeting_offline_status.json` | 状态文件：`running` / `done` / `failed`，外加 `partial` / `silent` / `silent_spans` / `error` | 下游程序 + 界面（这一场跑成什么样） |
| `meeting_offline_progress.jsonl` | **逐块进度账**（`a9affaa8` 已落地）：每跑完一块补一行 —— 这一块的文字与这一块的落点（`source` / `chunk` / `chunks_total` / `span` / `state` / `text` / `segments` / `wall_seconds` / `error`），第一行是表头 | 人 + `GET /api/sessions/{id}`（这一场跑到第几块） |

- **每路各一份，两路不合并**：转写与对照表的文件名里都带 `<source>`，说话人的限定名一律是 `${source}:${local}`（`mic:S01` 这种形状）。两路的编号互不相认，混成一份就说不出某一行的出处了。进度账是一份、按 `source` 分节，**分母按路各算一份**（这一路切了几块）。
- **进度账与正式稿是两份东西**：正式稿整路跑完一次写（时机没变），进度账一块跑完补一行；后者的文件名与 `meeting_offline_transcript_` 那个前缀对不上，`GET /api/sessions` 的 `has_offline` 那一格因此不受它影响。它是给人看的读数（块数、已跑时长都不是判据），字段契约在 `meeting/offline/progress.py` 的模块说明里。
- 对照表给的是**类编号**（`S01` 这种），这条链不给编号起名，表里也没有名字那一格。
- 分块跑时的临时块音频住在 `<场次目录>/_offline_chunks/` 下面，跑完一块都不留：正常跑完与中途抛出来都会扫干净。
- 对照表的必填顶层键是 `schema_version` / `source` / `speakers` / `segments`，`segments` 每一项必填 `start` / `end` / `speaker` / `text`。`schema_version` 缺失或主版本号读方不认识、`source` 缺失的对照表，读方拒绝并如实报错，读的人拿不到一份半信半疑的表。

#### 分块跑的类数读不出人数

`post.chunk_seconds` 到点之后，一场会被切成**连续不重叠**的块逐块调引擎，再把结果拼回整场时间轴（每段的秒数加上该块在原录音上的偏移）。切点是从头开始的固定间隔（`0`、`chunk_seconds`、`2 × chunk_seconds`…），音频在哪里安静不影响切点。

块与块之间不做映射、不做相似度比较、不做合并：块内编号带命名空间（第 1 块的 `S01` 进产物就是 `c01:S01`），**同一个人在两块里各拿一个编号**。于是对照表里 `speakers` 数组的长度在分块跑时等于**各块类数之和**，比这一场真实的人数大。这件事落在 `headcount` 那一格：

| 跑法 | `meaningful` | `class_count` | `chunk_count` | `basis` |
|---|---|---|---|---|
| 整段一次跑（1 次引擎调用） | `true` | 模型在这段音频里分出的类数 | `1` | `single_pass_classes` |
| 分块跑（≥2 块） | `false` | 各块类数之和 | 实际块数（也就是引擎调用次数） | `sum_of_chunk_classes` |

`chunk_count` 记的是**数出来的实际块数**：单趟跑没有块数组、记 `1`，那一次同样是一次引擎调用；块长除不尽时它与「总时长 ÷ 块长」算出来的商对不上（5943.3 秒配 1200 秒块长，商 4.95、实际 5 块）。读这个数当人数之前先看 `meaningful`，下游按它分档。

#### 没声的那几段：调引擎之前的一道数字静音门

音频从采集层到会后这一层可能**整段是零**（实测那一档见下面）。这种输入送进引擎，引擎会**编出正文**：`250906` 那次验收里，环回那一路的 19 块有 5 块是全零样本，引擎在它们身上吐了 20 690 字，占那一路整份输出的 **69.32%**（四块各一句 `[S01][S01][S01]I'm sorry, I can't assist with that request.`、末块把 `The.` 重复 4091 遍），而那一趟每一块都报 `state: done`、`error: null`。会后离线这一层因此**在调引擎之前**过一道门：

- **判据只有一条**：这一段音频的**峰值够不着半格**（按 16 bit 满量程折算成格数；半格是一条开区间规则，正好落在半格上的样本算有声。边界的由来与两种波形形态的折算见下），并且样本数不少于 0.1 秒。实现里那一行是 `peak < SILENT_PEAK_FLOOR`、`SILENT_PEAK_FLOOR = 0.5`，与这里写的「够不着半格」逐字对应。实测那一档的五块正是 `volumedetect` 读出 `-91.0 dB` 的全零样本，这条判据一条不漏地拦下它；
- **安静但有声的段落照跑**：峰值为 ±1 的样本（16 bit 满量程的 -90.3 dB，也就是 1 格）照样送进引擎。会后这条链出的稿要跟会中稿放在一起对拍、还要交给纪要那一层，按能量阈值误杀一段真语音，比留着一段噪声更坏，所以这里取的是最严的一档 —— 只要有一个够得着半格的样本，这一块就照跑。会中那条链的 `_is_noise_segment` 另有一档口径（VAD + RMS + 语音帧三条 AND），它服务于逐段预筛，与会后这一层各管各的；
- **两种波形形态同一个分界**：归档 PCM 上读出来的是 int16 整数样本，`decode_audio()` 解出来的是 `[-1, 1]` 的 float32。判据把两者都折算成「满量程的几格」再比，而且**不截断**成整数。半格这个分界是一条**开区间**规则：`peak < 0.5` 格算静音，`peak = 0.5` 格算有声。于是 float 的 `3.0e-5`（0.983 格）与 `3.05e-5`（0.999 格）都算有声，int16 的 ±1（1 格）与 float 的 `1/32767`（1 格）落在同一侧，同一个样本在两种形态上给同一个答案；截断成整数会把那两个 float 读数一起抹成 0，那才是两种形态分家；
- **单趟与分块两条路都过这道门**：判据按成本从低到高挑一条路 —— 归档 PCM 上按字节扫那一段（见到第一个非零字节就出结果）、已经解好的波形切片判（分块跑的 mp3 与单趟跑的 mp3 都用它）、实在没有波形时才现解一次音频文件。**短于 `post.chunk_seconds` 的录音走单趟**（默认块长 450 秒，也就是每一场短会议），那一条路原先不经过门，一段全零的短录音因此会在会后那份稿里整段留下假正文；现在它也过门，代价是那一段音频多解一次（短会议的几秒到几十秒）；
- **跳过的那几段一个字都不写**，也没花那一次引擎调用，于是这一路更快（实测那一趟 19 块里有 5 块不必调）。

#### 没声、缺段、跑成、跑成而段全零时长：四档分得开

「这一段没声」「这一段的转写丢了」「引擎跑了但什么都没交回」「引擎交回的段全是零时长」是四件事，它们在产物里各有各的格子：

| 档 | 整场终态 | 逐路 | 那几段区间在哪 | 逐块那一行的 `reason` |
|---|---|---|---|---|
| 跑成 | `done`，`partial: false`，`silent: false` | `state: done` | 无 | `transcribed` |
| **没声跳过** | `done`，`partial: false`，**`silent: true`** | `state: done`、`audio.silent_spans` | `silent_spans`（状态文件顶层与逐路 `artifacts.<source>` 各一份）+ `audio.silent_spans` | `digital_silence` |
| 引擎失败 / 缺段 | `failed`，或 `done` + `partial: true` | `state: failed` / `audio.partial` | `missing_spans` + `error` 里那句话 | `engine_error`（抛错）或 `no_segments`（一段都没交回） |
| **跑成而段全零时长** | `done`，`partial: false` | `state: done`、`audio.zero_span_segments > 0` | 无（正文在这里） | `zero_span_segments` |

- **逐块那一行自带结论词**（`chunks[].reason`，机器可读）：下游原先只能从整场的那几格反推「这一段为什么没有字」，现在每一块自己说得出来。同一行还带 `gate`（门在这一块上的结论：`silent` / `not_silent` / `unjudged`）、`gate_method`（门凭什么判的，见下）、`gate_detail` 与 `zero_span_segments`（这一块里零时长段的条数）。**门判不了**那几段时，`gate` 是 `unjudged`，而 `reason` 说的是引擎那一边的结果（跑成了就叫 `gate_unjudged`、一段没交回就是 `no_segments`）——两个结论各说一件事；
- **`gate_method` 的取值是四加一**：`pcm_bytes`（在归档 PCM 上按字节扫）、`waveform`（在已经解好的波形上切片判）、`decode_at_plan`（量长度那一步就没解开，结论是「判不了」）、`decode_now`（门现解一次音频文件），另加一个 `unjudged`（这一段门没做成判断，与逐块 `gate` 那一格同名的那个词）。产品路径上读得到的是前三条与 `unjudged`：**单趟跑的 MP3 记的是 `waveform`** —— `_plan_for_source()` 为了量长度已经把整份音频解成了波形，门切片用掉的就是那一段，门口不会现解第二遍；`decode_now` 那一支只对直接调用 `measure_span()`、自己注入波形或另给路径的调用方存在，产品路径到不了它（非 PCM 的音频要么在量长度那一步解开了、要么在那里就没解开）。两处记法各有一格：**逐路**那一格的 `method` 只由判过的块构成（判过的块按它们各自那把尺子拼起来，多把就写成 `a/b`），一块都没判成、或这一路压根没有可判的音频时它是 `null`，**门判不了的那一段在那里读不到尺子名**；**逐块**那一行的 `gate_method` 记的是这一块原本打算用的那把尺子，`gate` 是 `unjudged` 的块同样带着它（解不开那一档因此读得到 `decode_at_plan`），「没判成」这件事由 `gate` 与 `gate_detail` 说；
- **转写文件第一行多一条注释**（`<!-- ... -->` 形状，与缺段那条并列、各说一件事）：写明这一段没声、没有送进引擎。读方按行格式读正文时两条注释都会跳过，段号不受影响；
- `silent_spans` 的长度 = **跳过的块数**：分块跑时一块一个区间；整段一次跑时要么空、要么就是**整段音频**那一个区间（一次引擎调用对应一块）。它与 `missing_spans` **不相交**；
- 报告开头因此会多一行「这一场有没声而跳过的音频」——它**不**报「这一场的转写不完整」，那一句话留给真正的缺段。

#### 门做成没做成判断（`gate`）

这道闸自己也要交代清楚：音频解不开、要的那一段读不全（文件比区间短）时，那一段**照常送引擎**（读不到就按可能有声处理），而这一趟必须在产物里与一次健康的运行分得开 —— 少了这一格，一段没人判过静音的音频与一次正常的运行在产物里长得一样。三层都记：

- **逐块**那一行的 `gate = "unjudged"`、`gate_method` 与 `gate_detail`（为什么判不了）；
- **逐路**的 `gate`：`applied` / `method` / `spans_total` / `spans_judged` / `spans_unjudged` / `unjudged_spans` / `unjudged_reason` / `detail`（对照表 `audio.gate`、状态文件 `artifacts.<source>.gate`）；
- **整场**的 `gate`（状态文件顶层，按路列出判不了的那几段）+ 报告开头那一行与单列的一节。

`applied: false` 说的就是「这一趟的门在哪几段上没做成判断」，读方按它决定要不要把这一场的转写当可疑件看。

#### 零时长的段

产物里的 `segments` 允许出现 `end == start` 的段：解析层对「模型漏写了结束标签」的那一段故意把 `end` 退回 `start`，把正文留下（丢掉它就是丢掉一段真正文）。这类段**一段都不删**，但逐路数出条数写进 `audio.zero_span_segments` 与报告：实测的假正文**每一段都是零时长**，这个形状因此同时是个可核对的异常信号。

#### 缺了一段的时候

分成多块跑之后，某一块就可能没成（显存不够、单块抛错、一段都没交回）。这一路缺的那几段音频因此没有转写，**产物照落、整场仍落 `done`**，而这件事在三处同时留痕（与「没声跳过」那一档分开记，见上一节）：

- 状态文件落 `done` 加 `partial: true`，`error` 写明缺的是哪几段区间（秒级，落在原录音时间轴上）；
- 转写文件的**第一行**是一条注释行（`<!-- ... -->` 形状），写明缺了哪一段 —— 读方按行格式读正文时会跳过它，它不占段号；
- 对照表的 `audio` 那一格带 `partial` 与 `missing_spans`，`chunks` 数组逐块记成功与失败；运行报告单列一节列出缺失区间。

**每一块都没跑成的那一路落 `failed`**：那一路的转写只剩那一条注释行、对照表是一份空表，终态与原因写在对照表的 `state` / `error` 两格上。整场的终态看两路：有一路跑成就是 `done`（失败那一路的原因进状态文件与报告），两路都失败才落 `failed`。**整段一次跑**的失败不落产物（没有「哪几块没成」可说），失败原因只在状态文件与报告里；没声那一档走的是另一条线（上面「没声的那几段」那一节），它连整段一次跑都照落产物。

### 会后纪要生成

纪要那一层由「直接出纪要」按钮（`post_minutes`）或 `meeting post` 拉起（`41465462` 已落地）——后者在离线链的尾巴上通知一次（`run_session()` 的 `_announce_minutes()`），或离线段已是终态时只补这一层；「生成会后精细稿」按钮那一趟（`run_session(..., minutes=False)`）**不**拉起它，纪要归另一个按钮。拉起点起一条 daemon 线程跑 `MinutesHook`，返回即算启动成功，它跑多久都不占收尾预算。这一趟的产物分两处：

| 文件 | 内容 | 读者 |
|---|---|---|
| `meeting_final.md`（**场次目录根**） | 纪要正文 + `## 说话人附录`（附录里逐行写人名、把握与依据，猜不出的单列一节） | 人 |
| `meeting_minutes_status.json` | 状态文件：`running` / `done` / `failed` / `delivered`，外加 `mode` / `branch` / `started_at` / `finished_at` / `error` / `artifacts` / `dsh` | 下游程序 |
| `meeting_minutes_status.lock` | **瞬态**的 claim 锁：只盖住「读一遍现状 + 写一版 `running`」那一小段，写完就删掉。**它不是产物**（进程被强杀时可能留一份残锁，下一趟按有界等待兜住）；上面那张产物清单里因此没有它的位置 | 拉起方自己 |
| `working/inputs.json` | 输入清单：这一场有哪几条转写、每条几行、走的是哪一档 | 程序 + 人 |
| `working/<source>/<kind>/chunks/chunk_NN.md` + `working/<source>/<kind>/00_index.md` | 按行切出来的块（默认 100 行一块）与行号区间表（`total_lines` / `chunk_size` / `chunk_count` 与每块的闭区间） | DSH 子会话 |
| `working/segment-*.md`、`working/segmentation.json` | 逐段总结与分段方案（子会话产出，一份方案覆盖一到两条输入、每条段带 `source`） | DSH 子会话 |
| `working/run_prompt.md`、`working/minutes-run.log` | 那一次运行的提示词正文，以及这一趟留下的账：headless 那一趟是子进程的 stdout / stderr，web 那一趟是三步 RPC 的请求与应答摘要 | 排查现场 |

- **起线程就返回，不占收尾预算**：它炸了不带走进度更早的产物，`meeting_offline_status.json` 与那四份离线产物一个字节都不改（`try/except BaseException` 兜住，只记主 log 一条）。
- **两条分支：探一次 DSH web 服务端，在跑就交付、没在跑才自己起子进程**。拉起之前先探 `minutes.dsh_url` 指的那台（`meeting/minutes/dsh_web.py` 的 `probe()`：先 TCP，再发一次只读、无参数的 RPC 确认协议与鉴权）：**在跑**走 web 分支，把这一场经 `/api` 交付给 DSH web 服务端（三步：`workspace/create` → `session/create`（只带 `workspaceId`）→ `session/prompt` 送提示词全文），**投递 accepted 即算交付完成**，终态记 **`delivered`**；**没在跑**才落回现成的 headless 分支（`launch.launch_headless()`：等子进程退出 + 四条判据校验产物，终态 `done` / `failed`）。走哪一条由探针当场答，**`minutes.hook` 里写 `web` 或 `headless` 都改不了它** —— 配置里那个词只是记在「在飞」那一版上（见下一格）。
- **`branch` 记实际走的那一条，但「在飞」那一版是例外**：终局那一版记 `web`（交付给 DSH web 服务端）或 `headless`（本进程自己起的子进程）；**拉起方在建线程之前落的那一版 `running` 记的是配置里写的那个词**（`launch.configured_branch()` 校验取值域后原样交回 `auto` / `web` / `headless`）—— 那一刻还没探，配置里写 `web` 而那一台没在跑时，读终局那一版才看得到实际落的是 `headless`。
- **`delivered` 与 `done` 的分工**：`done` 是「纪要认真校验过、产物在盘上」（headless 那一趟走的四条判据）；`delivered` 是「投递成功了，产物还没（或者不会）经这一层校验」—— 这一层**不等** DSH 那边把纪要写完、也不校验产物，**停不停 DSH 是用户的事**。读方按**产物**判「这一场补不补跑」（`meeting_final.md` 过四条判据就是已完成，终态落在 `done` 与 `delivered` 哪一档都算），按 `delivered` / `running` 判「这一场已经交付过、别再拉起第二个会话」（`meeting post` 的重复拉起闸把这两档并列，`--force` 是重新交付的出路）。
- **状态文件拉起 DSH 之前先落一份 `running`**（`finished_at` 是空串）：这一版由拉起方在建线程**之前**同步落（`meeting/minutes/status.py` 的 `mark_running()`），终局那一趟覆盖它，进程被杀时钩子把它补成 `failed`，这一格不会永远停在 `running`。盘上已经停着一版 `running` 时，`launch()` 当场抛 `AlreadyRunningError`、不拉起第二个子会话（`--force` 是出路）—— 这一版里的 `branch` 记的是**配置里写的那个词**（那一刻还没探 DSH web 服务端），实际走的那一条由终局那一版覆盖；那一版落盘用一把 **claim 锁**（瞬态文件 `meeting_minutes_status.lock`）把「读现状 + 写 running」裹成跨进程原子的一段，**锁不在产物清单里**，见 `docs/minutes.md` 第 1.1 节。
- **再入闸那一趟不写状态文件**：环境里有 `MEETING_MINUTES_CHILD=1`（这一趟是 DSH 子会话自己）时 `runner.run()` 当场返回 `skipped`，盘上那份状态文件归父进程那一趟所有。
- **读方对不认识的 `schema_version`、缺 `state`、`state` 不在四档里一律拒收**，不猜；写盘与离线那一份同档：原子替换。（再入闸那一趟的 `skipped` 只出现在返回值里，它不落盘，读方那四档因此一个字不动。）
- **输入四档**：两份都在（`both`）/ 只有会中稿（`live_only`）/ 只有会后稿（`offline_only`）都照常出纪要；两个来源一条都没有（`none`）那一档**不启动 DSH**、状态记 `failed` + 原因。切块与提示词清单都**逐份稿**各留一份（`working/<source>/<kind>/`），**两份平级、谁也不叫"权威"、两路永不合并**；附录只写有依据的话：某一路缺了带聚类编号的那份稿时，如实写「这一路分不出个人」，不为任何人名给出推断。
- **产物四条判据只归 headless 那一趟**：纪要文件非空、写着 `## 说话人附录`、写着 `### 猜不出的`、附录那一节里有一张真有数据行的表 —— 缺一条这一趟就是 `failed`，退出码 0 不算数（见 `docs/minutes.md` 第 5 节）。web 分支交付即完事，这四条一条都不跑，终态记 `delivered`。

那一层吃什么、经哪几步出一份纪要、怎么手动跑一遍，见 `docs/minutes.md`。

### 重启自洽

**启动时不再去读任何别的场次目录**（`docs/design-intent.md` 第 4 条）：原「扫输出根下最新的一场 → 挂 `notice.restart_unfinished` → 把那份记录改写成 `draining + interrupted` → 拦住新场次」这一整条，随「默认输出根」的退场一并拆掉，**不影子保留**。启动序列现在只做：解析后端、探设备、组装初始快照 —— 绝不自动开始录音、也绝不读别的场次目录。

- 没走完收尾就被打断的那一场，盘上那版 `session.json` 原样留在原地：谁都不去改写它、也不拦新场次的 `start`。
- 工作区的登记与"最近一场"由收编节点（e2f6166b）落地：注册表住在配置目录 `workspaces.json`，界面经 `GET /api/sessions` 读（形状见 `docs/contract.md` 4.8）——启动序列对场次目录照旧一个字不读。
- **这一场此刻跑到哪一档**由 `GET /api/sessions/{id}` 回答（`a9affaa8` 已落地，形状见 `docs/contract.md` 4.9）：会后离线跑到第几块 / 共几块、正在跑哪一路、已经跑了多久。它也是纯读：**启动序列照旧不碰任何场次目录**，这只端点是有人问才去读那一场。

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
- **提示级**进 `alerts.hints`，**同类合并、上限 3**。合并的键是 `(level, source, message)` 三元组，同一个键再次出现时沿用原来那一条的 `since`、把 `duration_seconds` 刷新到当下；到上限后又来新的键，就留下 `since` 最早的三条，**多出来的那个键在合并那一刻就从判定器的记账里回收**（`hints()` 与记账本因此恒同长；被挤掉的那条只进 log），同一个键后来重新进榜算新起的那一条。两条提示级都定在 `source = transcript` 上，靠 `message`（也就是故障类型文案）区分。
- 判定器里那两条提示级的入口：`AlertJudge.on_transcript_health()`（`transcript.health` 从 `normal` 掉到 `behind` / `stopped`）与 `AlertJudge.on_backlog_depth()`（待转写段数 ≥ 20 段，`BACKLOG_HINT_THRESHOLD` 写死在代码里、不加配置键）。**两处都在 `ServiceRuntime._refresh_transcript_snapshot()` 里接线**，而那个函数是转写刷新的唯一落点（启动、开场、每行、0.5 秒节拍四条路径都经过它），所以接一次就覆盖四条路径。
- **每刷一次就调一次判定器**（不是只在 `health` 迁移那一刻）：持续提示条要在窗口内一直刷新 `duration_seconds`，只在迁移时调的话它会停在 `0`。判定器那边本来就是幂等的（同一个三元组沿用原来那一条、只刷时长）。
- **提示级只在录音与告警态成立**：`top_state ∈ {recording, alert}` 时这一格才可能有条目，`idle` 与 `ended` 两档恒为空数组（`docs/ui-design.md` 章 2.5 那张「什么状态显示什么」表里，空闲态与结束档的告警区都是**不出现**）。判定权随状态收口：在那两个态里服务侧才把 `health` / 积压读数交给判定器；离开 `recording` / `alert` 时把已经挂着的那几条**连内部记账一起清掉**（`ServiceRuntime._clear_hints()`：判定器的条目与记时表、快照那一格、`_hint_keys` 三样同进同退），于是下一场 `start` 从零开始。**空闲态那个 `health == "stopped"` 报的是「真转写后端还没起过场次」**（见第 4 章那两格 fixtures），与录音中「转写停了」属于两种情形，它不构成一条提示级。
- **两条提示各有对称的清除路径**：`health` 回到 `normal` 撤掉健康度那两条文案（「转写落后了」/「转写停了」），积压回落到 20 段以下撤掉积压那一条。两类共用 `source = transcript`、靠 `message` 分辨，各清各的 —— 转写跟上了并不代表队列已经清空。
- **心跳失联那条致命级由界面自己判、自己画**，服务侧不产生它。

`Alert` 六字段：`{level, source, since, duration_seconds, message, muted}`。`level ∈ critical / hint`，`source` 是那一路的 id。

`muted` 说的是**这一条现在会不会出声**，判据是「本场静音生效期间」**或**「这一条还在抑制窗口里」。抑制窗口按「哪一路 + 故障类型」跟踪，**判定与计算全在服务侧**：界面只看这个布尔值，`false` 就响一声、`true` 就不出声，界面不算任何窗口。

**首发那一帧给 `false`** —— 这是本场告警能不能响的那一步：

- 抑制窗口的锚点是**上一次 raise 的时刻**（`_SourceTrack.last_raise_ts`），窗口 = `[锚点, 锚点 + alert.alarm_suppress_seconds)`；
- 一条**新起**的告警在广播出去的第一帧 `muted` 是 `false`（界面据此响一声）。`ServiceRuntime.commit()` 在写下锚点之后还会重算一遍 `muted`，而那次重算就发生在锚点刚开的同一刻 —— 所以刚起的那一条在 `_SourceTrack.raise_in_flight` 上挂一个记号，`AlertJudge.refresh_muted()` 认出「正是这一条还没出去」就跳过窗口那一半的重算、把它算好的 `false` 留到这一帧，跑完把记号清掉；**会话级静音那一半照算**，本场静音期间这条标记条目照样是 `true`；
- 此后同一条告警重算是 `true`（不重复出声）；`alert.alarm_suppress_seconds` 保留为「同一条事件在窗口内反复 raise 不重新放行」的那道闸；
- `mark_recovered()` 关掉窗口，恢复后再发生按**新事件**处理，首发又是 `false`；本场静音期间一律 `true`。

`contract/fixtures/snapshot/alert.json` 抓的就是首发那一帧（`"muted": false`）。

两条相关动作见 `docs/api.md`：`mute_session`（会话级，下一场 `start` 自动复位，不改持久化开关 `alarm.sound`）与 `dev_inject`。

**`muted` 什么时候重算**：它的唯一入口是 `AlertJudge.refresh_muted()`，而那个函数只在 `ServiceRuntime.commit()` 里被调一次 —— 也就是说，快照上这个布尔值**在下一次提交那一刻**才按当下的窗口与会话级静音重新算一遍（`revision` 只在那时 `+1` 并广播）。抑制窗口自己到点不产生任何提交，`AlertJudge.is_muted()` 在全仓也没有读路径或定时路径调用它，因此「窗口已过、下一次提交还没来」的那一段里，快照上的 `muted` 仍是上一次算出来的 `true`（方向是**不重复出声**：事故还在继续时不会又响一次）。会话级静音复位、下一场 `start` 复位同理 —— 都在紧随其后的那次提交里落到快照上。

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
- **`transcript` 通道同样不占 `revision`**（第 3.3 节：那一格不参与状态迁移）。提示级是这条规矩上唯一的例外，口径写死如下：`alerts.hints` 的**键集合**变化（提示级新增 / 移除 / 被上限 3 挤掉，以及离开 `recording` / `alert` 时整格清空）**要能被界面看见**，于是转写刷新返回「有变化」、由既有调用点借 `state` 通道提交一次（`revision +1` + 一次全量 `state` 广播）；**同一个键集合里 `duration_seconds` 的刷新不算变化**，返回假、不提交 —— 否则 0.5 秒的节拍会把 `revision` 一直推上去，契约给界面的那句「`id` 变了就是状态变了」当场变成假命题。判据落在 `ServiceRuntime._sync_transcript_hints()`：它按 `(level, source, message)` 有序记账，键集合动了才返回真。
- **`levels` 帧里每一路的 `ts` 恒为 `null`**：这一格的语义是「这条读数的时刻」，而电平帧自带固定节奏（4 次/秒），服务不按帧填它 —— 客户端拿 `data.revision` 与帧到达时刻定位即可。`transcript` 帧的 `line.ts` 是另一回事：那一格由转写后端给，真值可以是非 null。
- 慢消费者：每个订阅者一个**有界队列 512**。满了就清空这个订阅者的队列、塞一份带 `resync: true` 与 `dropped: N` 的 `snapshot`；`levels` 满了只留最新一帧。**一个卡住的页面拖不住别人**，广播路径是同步的、不 `await` 慢消费者。
- 多个连接看的是同一份状态：一次变更同一个 `revision` 同时到所有订阅者。

## 7. dev 替身与故障注入

`meeting/backends/dev.py` 是**只给开发与测试用的非产品路径，默认关闭**。同一条取值 `dev` 在两个后端键上**按 key 分流**：`capture.backend = dev`（或 `MEETING_CAPTURE=dev`）落采集替身 `DevCaptureBackend`，`transcriber.backend = dev` 落转写替身 `DevTranscriberBackend`。

**采集替身**给两样东西：

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

**转写替身**（`DevTranscriberBackend`）是「会中能看见字幕在动」的那一条：开场 2.0 秒后首发、之后每 0.5 秒经行出口产一行两路轮换的合成转写（`kind = "line"`，正文形如 `dev 替身合成的一行转写（mic #1）`）。它不读归档、不加载模型、**不写产物文件**，`health()` 恒 `normal`、`backlog_depth()` 恒 `0`、`segment_timeout()` 报 `20.0`；`stop()` 之后线程收队、一行都不再产。**它没有环境变量** —— `MEETING_CAPTURE` 只映射 `capture.backend`，要开它在配置文件里写 `"transcriber.backend": "dev"`。

**拿它写用例时的一个实情**：静默阈值与恢复窗口这些等待是真实时长，而测试侧有一条「单条用例 ≤ 5 秒」的红线（看门在 `tests/conftest.py`）。用例的做法是**把阈值写进临时 `config.json` 调小**（代码默认值一个字不动），同时把轮询间隔与观察窗口收在红线以内 —— 被测的语义没有跟着改，改的只是测试等的时长。

## 8. 相关文档

- **双端契约（端点口径、快照 schema、SSE 帧形状、动作信封与 `code`/`detail` 表）：`docs/contract.md`**
- HTTP 面（端点、动作信封、`code` 表）：`docs/api.md`
- 怎么起服务、配置、日志、退出码、跑测试：`docs/service.md`
- 会后纪要那一层怎么跑、产物四条判据、三个手动子命令：`docs/minutes.md`
- 会中总结那一层（窗口怎么切、模型输出契约、七个配置键、动作与记录端点）：`docs/summary.md`
- 后端怎么被找到、怎么插进来：`docs/backend-protocols.md`
- 会中与会后两套模型的选型、四个 `post.*` 键的取值依据与本机实测：`docs/asr-backends.md`
- 界面怎么画这四态：`docs/ui-design.md`
- 前端工程怎么跑、怎么构建：`web/README.md`
- 契约的可机读基准（33 个 golden 样本与掩码规则）：`contract/README.md`
- 索引：`docs/spec.md`
