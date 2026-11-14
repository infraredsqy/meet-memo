# meeting · 状态、场次与推送

> 这份文档写**服务吐出来的那几样东西**：四态状态机怎么走、状态快照长什么形状、一场会在盘上留下什么、心跳怎么算、告警怎么判、SSE 推什么帧、dev 替身给什么。
>
> 读者是想接这份数据的下游开发者（前端、验收脚本、会后流程）。
>
> 代码落点：状态机与取值域在 `meeting/state.py`，场次目录与 `session.json` 在 `meeting/session.py`，告警判定在 `meeting/alerts.py`，推送在 `meeting/service/hub.py`，持有者是 `meeting/service/runtime.py` 的 `ServiceRuntime`；会后那一层在 `meeting/offline/`。HTTP 面见 `docs/contract.md`；**可机读的对外形状**（逐键 schema、帧形状、`code`/`detail` 表）同在那本，与本文冲突时以代码实际行为为准、权威落在契约那本。

## 1. 四态状态机与收尾

顶层状态只有四个：`idle` / `recording` / `alert` / `ended`。**没有第五个。**

| 状态 | 怎么进来 | 怎么出去 |
|---|---|---|
| `idle` | 进程刚起、还没开过任何一场 | `start` 成功 → `recording` |
| `recording` | `start` 成功，已选的路都在采 | 某路判 `lost` → `alert`；`stop` → `ended` |
| `alert` | 某路静默连续达 `alert.silence_seconds`，或后端报该路流中断 | 该路连续有信号达 `alert.recover_seconds` → `recording`；`stop` → `ended` |
| `ended` | `stop`。子状态 `phase` 从 `draining` 走到 `done` | `phase == "done"` 时下一场 `start` 放行 → `recording`；收尾没走完的不放行 |

三条铁律，代码与测试都盯着：

1. **进 `alert` 不等于停录**：没出事的路照常采、`elapsed_seconds` 照常走。
2. **采集出什么事都不自动结束会话**：所有路都挂了也只是 `alert`；进 `ended` 只有 `stop` 一条路。
3. **心跳失联不进状态机**：迟到的界面心跳由界面自己判，服务侧没有由它驱动的迁移。

### 收尾三步

`stop` 进 `ended·draining`，时长就地冻结，然后按固定顺序跑三步（**转码不在收尾序列里**，见下节）：

| 序 | 步 | 干什么 |
|---|---|---|
| 1 | `flush` | 停采集、归档落盘（后端 `finalize()`）。**落定那一刻转码在后台自动起**（真采集能力在场时；替身那档没有真实 PCM，不起） |
| 2 | `log` | 写这一场的结束标记（`session.json` + 场次 log 摘除）。永远在场，永远 `done` |
| 3 | `post` | 让会中模型退出显存（`release_gpu()`，失败只记 log）。**会后一个动作都不自动跑**，这一格如实停在 `waiting`（= 等你动手）；会后两趟由按钮或 `meeting post` 拉起 |

每步的 `status` 取五档：`waiting` / `running` / `done` / `failed` / `skipped`。

- `skipped` = 这个构建里没有真实实现（眼下只有 `flush` 会出它），不阻塞 `done`，只留一行 warning；`failed` = 有实现但这次调用抛错，带 `error`。某一步 `failed` 之后**后续步骤照常按顺序登记**，不许整段抛出不写盘。「缺席」只看后端的解析结果，不看调用时抛了什么。
- 每步的通用节奏：置 `running` → 写一次 `session.json` → 广播一次 → 执行 → 落终态（`detail` 写人话）→ 再写盘、记 log（开始/结束/耗时）→ 再广播。预算的输入与每步实测耗时逐条进 log。

**各步共享一条 deadline**，一趟收尾的等待总量以它为界、不随步数翻倍：

```
budget = 30 + backlog × (segment_timeout + 5)     下限 30 秒、封顶 300 秒
```

`backlog` / `segment_timeout` 取自转写后端，缺席时取 `0` / `20.0`。这三个数硬编码在 `meeting/service/runtime_drain.py`，**不开配置键**。唯一的例外是 `log` 步：它的 flush 与摘除至少拿 5 秒，不许前面的缺席步把 deadline 用光。超预算的那一步落 `failed`，`error` 写明 budget 与实测耗时。

### 终态判定

终态判定只在一处（`finish_drain()`）：

| 收尾各步结果 | `ended.phase` | 下一场 `start` |
|---|---|---|
| 各步全部落定（`done` / `skipped`；`post` 的终态 `waiting` 也算落定）且无 `failed` | `done`（`top_state` 停在 `ended`，`session` 保留、时长冻结） | 放行，从 `ended` 直接进 `recording` |
| 有任一步 `failed`，或各步没全落定就走到了出口 | 保持 `draining`（`detail` 带失败步与场次 log 路径） | 按 `docs/contract.md` 的 `draining` 档拒绝 |

**收尾走完不回 `idle`**：`idle` 只留给「进程刚起、还没开过任何一场」。

**收尾期间第二次 `Ctrl+C`：立即中止，顺序写死。** 先把盘上写成 `state: "draining"` + `interrupted: true`（原 `steps` 原样保留、不写 `done`、**不写 `ended_at`**），再对收尾 task 调 `cancel()`；取消时的登记只补步骤状态、不覆盖 `interrupted` / `state`，唯一的例外是「信号到达时各步恰好已全部落定」——那一档按终态规则由 `finish_drain()` 落。进程以强制打断档退出（`docs/service.md`）。

### 转码独立工序（`meeting/capture/transcode.py`）

转码是这一场的**独立后台工序**，与收尾三步并行：flush 落定自动起，也可由 `transcode_run` / `transcode_pause` / `transcode_resume` 三只动作或 `meeting post` 随时拉起。它不吃会中/会后模型的显卡，**不挡 `start`、不挡会后动作、不占收尾预算**。

- **完成判据是逐段凭据，不是产物文件**：`meeting_transcode_<source>_<NNN>.json`（临时文件 + `os.replace` 原子写）记下源 PCM 字节数/时长与产物 MP3 字节数。「这段写完了」只认凭据——凭据缺失或与源/产物对不上（源变了、产物缺了、半截产物）的那一段**重做**；MP3 先写临时名再原子落位。转完 PCM 照留。
- **进度与状态**：`meeting_transcode_status.json`（`running` / `paused` / `done` / `partial` / `failed` + 逐路 done/total/current_segment）与 `meeting_transcode_progress.jsonl`（逐段一行）。**判据读盘、不依赖进程内存**：服务重启后读数照旧；盘上停着 `running` 而本进程没有它在飞时，`GET /api/sessions/{id}` 的 `transcode` 格按 `in_process` 分「真的在跑」与「上次被杀停在半路」。
- **暂停 / 继续**：`transcode_pause` 立旗即回，工序在**段边界**落 `paused`；`transcode_resume` 摘旗接着前进，本进程没有它在飞时从盘上凭据接着做。同一场只能有一道工序在跑（`(种类, 场次)` 槽闸），与转写、纪要、别的场次互不设闸。
- **终态**：`done`（全部段凭据齐全）/ `partial` / `failed`。转码的失败**不进收尾终态**，PCM 一个字节不动，补跑从凭据接着做。

## 2. 状态快照

快照是**顶层九键**的对象，`GET /api/state` 的 body 与 SSE 首帧 `snapshot` 是同一个 `to_dict()` 出的同一份。

| 顶层键 | 取值 |
|---|---|
| `revision` | int，进程内单调递增，每次启动重新计、不跨进程、不落盘 |
| `service` | `started_at` / `version` / `port` / `url` / `log_path`，启动时定一次 |
| `top_state` | 四态之一 |
| `session` | `null`，或 `{id, dir, started_at, mode, elapsed_seconds, frozen, paused}` |
| `sources` | 恰好两项（`mic` / `loopback`），见下 |
| `alerts` | `{critical: Alert\|null, hints: [Alert]}` |
| `transcript` | `{health, last_line_at, buffered_lines, failed_segments}` |
| `ended` | `null`，或 `{phase, steps: [DrainStep], error}` |
| `settings` | `{alarm_sound, heartbeat_interval_seconds, summary_default_window, summary_preset_names, summary_active_preset, asr{…}, devices{mic, loopback}}` |

`sources[*]` 九键：`id` / `label`（`本地` / `远端`）/ `device_name` / `health` / `lost_since` / `restored` / `level_dbfs` / `peak_dbfs` / `envelope`。

- `health` 七取值：`unprobed` / `ready` / `probe_failed` / `capturing` / `lost` / `restored` / `stopped`。前三档由启动探测直取，后四档由迁移写值。
- `envelope` 是最近 30 秒的电平，15 根柱、一根 2 秒，柱值 = 窗口内各帧 `peak` 的最大值，空窗沿用左边那根；开场是 `[]`。这根柱由服务算，**前端不许自己造**。

各键的语义：

- **`session`**：`id` 就是工作区目录名（人给的，软件不生成不解析），`dir` 是绝对路径，与 `session.json` 全链路一致。`mode ∈ both / mic / loopback` 开场前定死、录音期间不切。`elapsed_seconds` 在 `recording` / `alert` 态走秒，进 `ended` 冻结——**实际录到的秒数，扣掉暂停**。`paused` 是「暂停中」的唯一落点，顶层四态不新增第五个。**`top_state` 停在 `ended` 时那场的 `session` 还留着**（成功失败都留），直下一场 `start` 才清。
- **`ended`**：`phase` 只有 `draining` / `done`（`failed` 属于盘上 `session.json` 的 `state`，快照里没有对应位置）；`steps` 每条四键 `{id, status, detail, error}`，顺序固定 `flush` / `log` / `post`。收尾失败后 `phase` 保持 `draining`，这份摘要与 `session` 一起留在快照里。
- **`settings`**：读到的是**运行期生效值**——热读键写盘那一刻就换，其余键要重启才可见（分档以 `meeting/config.py` 的 `HOT_KEYS` 为准，见 `docs/service.md`）。`asr` 五子键与配置键逐字同名（去前缀）；`devices` 两子键是「用户配的意图」，`sources[*].device_name` 才是「实际落到的设备」——配了没命中不报错，按回退顺序挑并留一行 `WARNING`；`summary_preset_names` / `summary_active_preset` 是预设名单与当前指针，预设的端点与密钥**不进快照**。
- **`transcript`**（四格由转写后端刷新，判定权收在 `_refresh_transcript_snapshot()` 一处）：
  - `health ∈ normal / behind / stopped`，直接来自后端：真实现**没起过场次时报 `stopped`**（空闲态快照当场就是它），worker 停摆/模型出事也是 `stopped`，积压过 20 段是 `behind`；后端缺席的缺省替身恒报 `normal`。
  - `last_line_at` 是最近一行转写的墙钟（ISO，没出过行是 `null`）；两个计数只进 log 与 `/debug`，不进产品界面。
  - **只有 `health` 的迁移参与状态变更**（给 `revision` 投票），两个计数不投。刷新路径四条：启动取初值、开场刷一次、每来一行刷一次、0.5 秒低频节拍兜住「worker 停摆没有行可来」那一档。读路径保持纯读。
  - 转写出事**不影响录音、电平与告警**，状态机一步不动。

**一次变更 = 一次 `revision`**：递增与广播只经 `ServiceRuntime.commit()` 一处，同一次事实处理里的多次改动合并成一次 `+1`、一次全量快照；被状态机拒绝的动作不涨 `revision`、不写盘、不广播。唯一例外见「SSE」一章的 `transcript` 通道那条。

## 3. 场次目录与 `session.json`

- **没有默认落点**：一场会落在 start 请求 / 命令行**显式给的已存在目录**里；服务不建目录、不按日期命名，目录名就是这场会的名字（日期是 `session.json` 的 `date` 元信息，不进目录名）。
- **对录过的目录按 start = 续接**：同一场会再录一段——旧段归档一个字节不碰（新段接着已有段号编），段记录与权威时长合并着写；续接开场失败时旧记录原样写回、本轮段文件收走，工作区回到按下去之前的样子。
- `session.json` **原子写**（同目录临时文件 + `os.replace`），每个阶段变更都写一次；被打断不留半截文件、不留 `.tmp` 残骸。**一场会的事实全在这一份文件里**，`meeting post` 靠它认场次。`start` 阶段出事（写文件失败、或采集起不来）→ **不进 `recording`**，回 503 并在主 log 留痕，盘上不留半套。

目录里会出现的文件：

| 文件 | 内容 |
|---|---|
| `session.json` | 场次记录（字段约定见下，逐键 schema 在 `docs/contract.md`） |
| `meeting_audio_<source>_<NNN>.pcm` | 两路各自的段归档：16k / 单声道 / int16 小端裸 PCM、无头、边采边写（强杀最多丢最后一块的尾巴）。NNN 三位零填充、从 1 起，**一段 = 一次录制**，段号一场会共一个计器（下一个 = 各路盘上实有最大段号 + 1）。**未选中的路整段写数字静音，`.pcm` 照常落盘**；暂停不切段 |
| `meeting_audio_<source>_<NNN>.mp3` | 转码独立工序逐段补的 64k MP3，**转码成功后 PCM 照样留着** |
| `meeting_transcript_<source>.md` | 会中临时稿，追加写、逐行 flush，每行 `[HH:MM] 正文`，时间戳取**分钟级的采集时刻**（段一入队就盖）。识别不出来也照落一行占位符（双层方括号、裸文本、不含 `**`）：`[[识别失败: 超时 20s]]` / `[[识别失败: 重复循环]]` / `[[识别失败: 无输出]]` / `[[识别失败: 错误]]`；纯噪声与暂停窗一行不产。**续接开场**各路先追加一条注释行 `<!-- [HH:MM] 续接：… -->`（不算发言行，读侧跳过），并经 SSE 各推一条 `kind="gap"` 的行。读头从盘上实有**最大段号**接起，旧正文不抄第二遍。**它是临时稿**：会后那份带说话人编号的稿另落一份，两份平级对拍（R11） |
| `meeting.log` | 场次级 log（挂摘与口径见 `README.md` 第 2 节） |
| `meeting_transcode_status.json` + `meeting_transcode_progress.jsonl` | 转码工序的状态与逐段记录（见「转码独立工序」一节） |
| `meeting_offline_*.md/.json/.jsonl` | 会后离线产物，见下 |
| `meeting_speaker_names.json` | 人名表：只存用户确认过的 `(编号, 名字)` 对，由改名动作 `speaker_rename` 写（原子写，`schema_version` 主版本不认识就拒收）。读侧宽容：缺表/坏表不挡出纪要；写侧谨慎：表读不出时改名拒写回 400——不拿读侧的宽容无声覆盖用户确认过的名字 |
| `meeting_final.md`、`meeting_minutes_status.json`、`working/` | 会后纪要的产物与中间物，见下 |

`session.json` 的字段约定（字段名与个数是冻结项，逐键 schema 在 `docs/contract.md`）：

- **`state` 的四取值（`recording` / `draining` / `done` / `failed`）与顶层四态是两套词汇**：`alert` 在盘上写 `recording`（告警不是盘上的一档），映射只有一处实现。
- `sources[*].archives` 是这一路的**段文件名表**（序号升序），**两路都无条件列出**——未选中的那一路是写满数字静音的文件，收尾照给它产 MP3；续接过后旧段名在前、保序。
- `segments` 是段列表：每段带 `index` / 起止戳 / 采哪几路 / 段内暂停区间 `pauses`（`end` 为 `null` = 还在暂停中）/ `duration_seconds` **在录时长**（不含暂停，还在录时为 `null`）。暂停、会中关一路、某路没开都不切段：那一路写数字静音（暂停窗的真音频在写侧被等长替换成数字零），暂停只进 `pauses`，两路时间轴始终等长。这些由动作 `pause` / `resume` / `set_source` 驱动；关掉与重开一路都是用户按的按钮，不报 `SourceLost`、不进告警（重开时设备打不开才回 `capture_unavailable`(503)）。
- `audio_duration_seconds` 是**权威时长**（R23）：实际录到的音频时长（不含暂停）= 合并后各段 `duration_seconds` 之和，单段从写侧内容字节算出。它也是这份文件里唯一被当判据的元信息——墙钟跨度、起止戳、空档时长**一律不校验、不核对、不改结论**。

### 会后离线产物

由「生成会后精细稿」按钮或 `meeting post` 拉起（收尾不自动跑它）。转写与对照表**按路各落一份、两路不合并**，说话人限定名一律 `${source}:${local}`（`mic:S01`），两路编号互不相认：

| 文件 | 内容 |
|---|---|
| `meeting_offline_transcript_<source>.md` | 会后精细稿（带说话人编号），每行 `[HH:MM] **S01**: 正文`（分钟级） |
| `meeting_offline_speakers_<source>.json` | 类编号 → 每段区间的对照表（`start` / `end` 秒级浮点；必填顶层键 `schema_version` / `source` / `speakers` / `segments`，段必填 `start` / `end` / `speaker` / `text`，主版本不认识就拒收） |
| `meeting_offline_report.md` | 运行报告：秒数 / 段数 / 类数 / 墙钟 / 实时率、耗时与失败分布、缺段与跳过段 |
| `meeting_offline_status.json` | 状态文件：五档 `running` / `done` / `partial` / `failed` / `interrupted`，外加 `partial` / `silent` / `silent_spans` / `error` / `gate` 与逐路整格 `artifacts.<source>`（每路都有、跑成与没跑成字段同形） |
| `meeting_offline_progress.jsonl` | 逐块进度记录：每跑完一块补一行（块文字与落点），**引擎加载失手也留一行**（`state = failed`、`error` 带 `engine_load_error`），否则进程不在场的读方从 `sources[].total` 上看到 `null` |

- 对照表给的是**类编号**，这条链不给编号起名；名字由用户在工作台确认、写进人名表（上表），下一次出纪要随提示词带给模型。
- 分块跑的临时块音频住 `<场次目录>/_offline_chunks/`，跑完一块都不留（正常跑完与中途抛出都扫干净）。
- **进度记录与正式稿是两份东西**：正式稿整路跑完一次写，进度记录一块补一行；`GET /api/sessions` 的 `has_offline` 不受它影响。

**数字静音门**（调引擎之前的一道闸，判据只住 `meeting/offline/` 一处）：

- 判据只有一条：这一段的**峰值够不着半格**（按 16 bit 满量程折算成格数；开区间——正好半格算有声）且样本数不少于 0.1 秒，就不送引擎。归档 PCM 的 int16 整数样本与解出来的 float32 两种形态折算成「满量程的几格」再比、**不截断成整数**（截断会让两种形态分家）。
- **安静但有声的段落照跑**：只要有一个够得着半格的样本就送——误杀一段真语音比留着一段噪声更坏。会中那条链的 `_is_noise_segment` 另有一档口径，各管各的。
- 单趟与分块两条路都过门。**整路数字静音时一块都不必送引擎，模型一次都不被拉起**，那一场照旧落 `done` + `silent: true`；`engine_load_error` 只在真有块要送引擎时才现形（加载探针推迟到第一块，只探一次）。
- **门判不了**（音频解不开、要的区间读不全）时那一段**照常送引擎**（按可能有声处理），并在产物里与一次健康运行分开记：逐块行的 `gate = "unjudged"` + `gate_method` / `gate_detail`，逐路的 `gate` 格（`applied` / `method` / 各计数）与整场顶层 `gate` 各留一份。读方按 `applied: false` 决定要不要把这场转写当可疑件看。

**没声、缺段、跑成、跑成而段全零时长：四档分得开**（各有各的格子）：

| 档 | 整场终态 | 逐块行的 `reason` |
|---|---|---|
| 跑成 | `done`，`partial: false` | `transcribed` |
| 没声跳过 | `done`，`silent: true`，区间在 `silent_spans`（顶层与逐路各一份） | `digital_silence` |
| 引擎失败 / 缺段 | `done`（每路都交回）/ `partial`（至少一路跑成、也至少一路整条没交回）/ `failed`（每路都没交回）；缺的区间在 `missing_spans` + `error` | `engine_error` / `no_segments` |
| 跑成而段全零时长 | `done`，逐路 `audio.zero_span_segments > 0` | `zero_span_segments` |

- 逐路 `artifacts.<source>.reason` 另有一套词：跑成的路是 `null`；一块产物都没落的路按次序挑第一个出现的词（`engine_load_error` > `no_audio` > `empty_pcm` > `engine_error` > `no_segments` > `all_chunks_failed`，见 `meeting/offline/runner/`）；`not_run` 只出现在兜底那一版给还没轮到的路现开的格上。逐块行与逐路格的 `reason` 是两套词、各说一件事。
- **缺段在三处留痕**：状态文件（`partial: true` + `error` 写明区间）、转写文件第一行的注释行（不占段号）、对照表 `audio` 格与报告。**每一块都没跑成的那一路落 `failed`**：转写只剩注释行、对照表是空表。整段一次跑的失败不落产物；没声那一档连整段一次跑都照落产物。
- **`partial` 一词两处各说一件事**：顶层 `state = "partial"` 说「至少一路整条没交回」；`partial: true` 那个键只说「有一路缺了区间」。两件事可以同时立。
- **零时长的段**（`end == start`：模型漏写结束标签时解析层故意退回、把正文留下）一段都不删，逐路数进 `audio.zero_span_segments` 与报告——这个形状同时是个可核对的异常信号。

**分块跑的类数读不出人数**：块内编号带命名空间（第 1 块的 `S01` 进产物是 `c01:S01`），块间不做映射与合并——同一个人在两块里各拿一个编号，`speakers` 数组长度等于各块类数之和、比真实人数大。落在 `headcount` 格：

| 跑法 | `meaningful` | `class_count` | `basis` |
|---|---|---|---|
| 整段一次跑 | `true` | 模型分出的类数 | `single_pass_classes` |
| 分块跑（≥2 块） | `false` | 各块类数之和 | `sum_of_chunk_classes` |

读这个数当人数之前先看 `meaningful`。

### 会后纪要的产物

由「生成纪要」按钮（`post_minutes`）或 `meeting post` 拉起；起线程就返回，跑多久都不占收尾预算，它炸了不带走离线那几份产物。这一层怎么跑（探针、两条分支、隔离闸、claim 锁、再入闸）见 `docs/minutes.md`；产物与状态字段在这里：

| 文件 | 内容 |
|---|---|
| `meeting_final.md`（场次目录根） | 纪要正文 + `## 说话人附录`（逐行人名、把握与依据，猜不出的单列一节） |
| `meeting_minutes_status.json` | 状态文件：`running` / `done` / `failed` / `delivered`，外加 `mode` / `branch` / `started_at` / `finished_at` / `error` / `artifacts` / `dsh`。读方对不认识的 `schema_version`、缺 `state`、`state` 不在四档里一律拒收；写盘原子替换。盘上停着一版 `running` 时拉起方拒跑（`AlreadyRunningError`）；进程被杀时钩子把在飞那一版补成 `failed` |
| `meeting_minutes_status.lock` | **瞬态** claim 锁，只盖住「读现状 + 写一版 `running`」，写完就删；不是产物 |
| `working/` | 输入清单、切块与行号区间表、编排判断、提示词与运行记录（形状见 `docs/minutes.md`） |

- **`done` 与 `delivered` 的分工**：`done` = 纪要认真校验过、产物在盘上（headless 路）；`delivered` = 投递成功、产物不经这一层校验（web 路）。读方按**产物**（`meeting_final.md` 过四条判据）判「补不补跑」，按 `delivered` / `running` 判「已交付过、别再拉起第二个会话」（`--force` 是重新交付的出路）。
- **输入四档**：两份稿都在 / 只有会中稿 / 只有会后稿都照常出纪要；一条都没有（`none`）不启动 DSH、状态记 `failed` + 原因。切块与提示词**逐份稿各留一份、两路永不合并**；附录只写有依据的话。

### 重启自洽

- **启动不读任何别的场次目录**：启动序列只做解析后端、探设备、组装初始快照——绝不自动开始录音、不改写盘上的记录。没走完收尾就被打断的那一场，盘上那版 `session.json` 原样留在原地，不拦新场次的 `start`。
- 工作区清单与「最近一场」住在配置目录 `workspaces.json`，界面经 `GET /api/sessions` 读（形状见 `docs/contract.md`）；每行的 `state` 是三档口径：`"recording"` 仅当这一行就是本进程此刻在录的活跃场次；盘上有 `session.json` 一律 `"ended"`；没有则 `null`。顶层 `post_jobs` 是本进程此刻在飞的那几趟活（`(种类, 场次)` 一格一趟，`kind` 认 `offline` / `minutes` / `transcode`），内存事实、不落盘、重启从空数组起步。
- **唯一的定向例外**：服务起「在飞」记录（配置目录 `post_jobs.json`，起 offline / minutes 时落、收工摘自己那一格），启动时逐条把记录点名的场 offline 会后核对成 `interrupted`（缺口从逐块记录读出），然后把整份清掉；minutes 档只清 + log；盘上不是 `running` 的只清记录。范围只有记录点名的几场，**不扫任何目录**；CLI（`meeting post`）不落记录也不被核对碰——它活在另一个进程，服务分不清「另一进程还在跑」与「都死了」，如实不猜。

## 4. 心跳与 `recording_alive`

`GET /api/heartbeat` 七字段：`service`（恒 `"meeting"`）/ `server_time` / `revision` / `top_state` / `recording_alive` / `seconds_since_last_tick` / `capture_gaps`。**任何状态下都 200**。这是心跳判据的正本。

- `recording_alive` 的判据是**服务侧的采集帧时钟**：只认 `LevelFrame`（`SourceFacts` / `SourceLost` 不刷新这个钟），窗口 = `max(2.0s, 3 × frame_interval)`——「两秒没收到一帧事实 = 采集停了」。
- **「还没有第一帧」单独一档**：自本场 `start` 起算同一只窗口，窗口内一帧都没到时报 `null`（未知）——首帧正常约 0.5s 才来，开场竞态不算「已停」；超窗仍无首帧报 `false`。
- `frame_interval` 由采集后端在 `bind()` 之后以实例属性报上来（协议默认 `0.5s`，dev 替身 `0.1s`）；读不到或非正数按 `0.5` 算，只记一条 warning。
- `seconds_since_last_tick` 是距最近一帧事实的秒数，一帧都没收到过是 `null`。
- 取值分档：`recording` / `alert` 态给 `bool`（开场窗口内没首帧的那段给 `null`）；`idle` / `ended` 态给 `null`。
- 界面那个 10 秒心跳周期（`heartbeat.interval_seconds`）**绝不参与**这个窗口——它管「界面还看得见服务吗」。
- 「判定权在服务侧」的硬判据：后端卡住不报任何东西时，`recording_alive` 由帧时钟算出来就是 `false`，HTTP 照常 200。
- **界面侧配套口径**：拿到 `false` 要**连续两次读数**才判「采集已停」（计数住界面层一处，任何非 `false` 读数清零），开场那拍 `null` 被拦在横幅之外；两拍规则只在界面层（`docs/contract.md`）。
- **`capture_gaps`：本场的帧空洞记录**。录中整进程冻结（GIL 饿死）时采帧与判停的线程一起冻住，解冻后帧时刻立刻刷新——`recording_alive` 全程 `true`，丢的内容在判停侧不留痕。留痕的检测点因此在**生产侧**：发帧线程每次产帧先与上一拍做差（墙钟不冻），超同一只窗口就报一条 `FrameGap` 事实、再刷新时刻——同一回合只报一次、首帧前不记。服务侧把事实落进 `capture_gaps`：`{total_count, total_seconds, recent[≤10]}`，区间重叠的合并为一条。**粒度是整档的**（不点名哪一路：冻结冻的是整个进程，两路一起丢）。暂停期间电平帧照发，所以暂停不构成空洞；dev 注入里 `silence`（发零电平帧）不算、`stall` 才算。这一格**只进心跳、不进快照**：不涨 `revision`、不广播，`recording_alive` 的语义一个字不动——空洞是历史事实，界面拿它在告警区摆一条提示行（不出声、不进两拍计数）。本场 `start` 清空记录。

## 5. 告警

**服务侧是告警的唯一判定者。** 采集后端只报原始电平（`LevelFrame`，每 500ms 一帧），静默秒数由 `meeting/alerts.py` 按阈值逐帧累计，**一律用帧自己的时间戳**，不拿墙钟做差。阈值即 `docs/service.md` 键表的 `alert.*` 四键（默认 -60 dBFS / 180s / 10s / 60s；静默判据必须是连续性——正常句间停顿有几十秒全零段）。

分级与合并：

- **暂停的那几段不进两条累计**：暂停期间电平帧照发（那是 `recording_alive` 与界面电平的输入），但判定器一帧都不判——安静房间里暂停三分钟不该弹「这一路静默已达 180 秒」。**恢复后的第一帧从零起算**（恢复时把每条计数的基准清掉）。
- **致命级**（某路采不到）进 `alerts.critical`，**同时只留一条**。
- **提示级**进 `alerts.hints`，**同类合并、上限 3**。合并键是 `(level, source, message)` 三元组：同一个键再次出现沿用原条的 `since`、把 `duration_seconds` 刷到当下；到上限后留 `since` 最早的三条，多出来的键在合并那一刻从判定器登记里回收，后来重新进榜算新起的一条。两条提示级都定在 `source = transcript` 上，靠 `message` 区分：`transcript.health` 掉到 `behind` / `stopped` 一条、待转写段数 ≥ 20 一条（阈值写死在代码里、不开配置键）。两处入口都接在 `_refresh_transcript_snapshot()` 里，四条刷新路径全覆盖；**每刷一次就调一次判定器**（不只迁移时），持续提示条的 `duration_seconds` 才会一直刷新。
- **提示级只在录音与告警态成立**：`idle` 与 `ended` 两档恒为空数组；离开这两个态时把挂着的那几条连内部登记一起清掉，下一场 `start` 从零开始。空闲态的 `health == "stopped"` 报「真转写后端还没起过场次」，不构成提示级。
- **两条提示各有对称的清除路径**：`health` 回 `normal` 撤健康度那条，积压回落到阈值以下撤积压那条——转写跟上了不代表队列已清空。
- **心跳失联那条致命级由界面自己判、自己画**，服务侧不产生它。

`Alert` 六字段：`{level, source, since, duration_seconds, message, muted}`；`level ∈ critical / hint`。

**`muted` 说的是这一条现在会不会出声**，判据是「本场静音生效期间」**或**「这一条还在抑制窗口里」。判定与计算全在服务侧：界面只看这个布尔值，`false` 就响一声，不算任何窗口。

- 抑制窗口的锚点是**上一次 raise 的时刻**，窗口 = `[锚点, 锚点 + alert.alarm_suppress_seconds)`。
- **首发那一帧给 `false`**（界面据此响一声）：刚起的那一条在重算时被认出「还没广播出去」，跳过窗口那一半的重算、把算好的 `false` 留到这一帧；会话级静音那一半照算。
- 此后同一条告警重算是 `true`（不重复出声）；恢复后再发生按**新事件**处理，首发又是 `false`。
- **`muted` 在下一次提交那一刻才重算**（唯一入口在 `ServiceRuntime.commit()` 里）：抑制窗口自己到点不产生任何提交，「窗口已过、下一次提交还没来」的那段里快照上的 `muted` 仍是上一次算出的 `true`——方向是**不重复出声**。会话级静音复位、下一场 `start` 复位同理，都在紧随其后的那次提交里落到快照上。

会话级动作 `mute_session` 见 `docs/contract.md`（下一场 `start` 自动复位，不改持久化开关 `alarm.sound`）。

## 6. SSE：四个事件与帧形状

`GET /api/events`，`Content-Type: text/event-stream`。帧形状的可机读正本在 `docs/contract.md`。

- **连上第一帧一定是 `snapshot`**（完整快照），新开的页面直接落在当前状态、不经过 `idle`；重连后仍先发 `snapshot`、**不重放断开期间的事件**（开头给一次 `retry: 2000`，断线由浏览器自己重连）。
- 帧格式一律 `event: <名字>` + `data: <一个 JSON 对象>`。`state` 与 `snapshot` 带 `id: <revision>`；`levels` 与 `transcript` 不带 `id:`，只在 `data` 里带当前 `revision` 作参照。
- 安静时每 **15 秒**一行注释帧 `: ping` 保活（只有这一行，不伪造事件）。
- **关页面只做一件事：把订阅者摘掉。** 不动会话、不停采集。

| 通道 | 什么时候发 | `data` 形状 |
|---|---|---|
| `snapshot` | 建连首帧、慢消费者重同步 | 完整快照；重同步时额外带 `resync: true` 与 `dropped: N` |
| `state` | 每次状态变更（**全量快照**，不做增量） | 完整快照 |
| `levels` | 录音 / 告警态下 **4 次/秒**（两路合在一帧里） | `{revision, frames: {mic: {…}, loopback: {…}}}`，每路带 `dbfs` / `peak` / `bar` / `envelope` |
| `transcript` | 每出一行转写 | `{revision, line: {ts, source, text, kind}}` |

- `levels` / `transcript` **都不占 `revision`**（不投票、不广播）。**一个例外**：`alerts.hints` 的**键集合**变化（提示级新增 / 移除 / 被上限挤掉 / 离开录音告警态整格清空）要能被界面看见，转写刷新返回「有变化」、借 `state` 通道提交一次（`revision +1` + 全量广播）；**同一个键集合里 `duration_seconds` 的刷新不算变化**——否则 0.5 秒的节拍会把 `revision` 一直推上去。判据落在 `ServiceRuntime._sync_transcript_hints()`：按 `(level, source, message)` 有序登记，键集合动了才返回真。
- `levels` 帧里每一路的 `ts` 恒为 `null`（电平帧自带固定节奏，客户端拿 `data.revision` 与到达时刻定位）；`transcript` 帧的 `line.ts` 由转写后端给，真值可以非 null。
- **慢消费者**：每个订阅者一个有界队列 512。满了就清空这个订阅者的队列、塞一份带 `resync: true` 与 `dropped: N` 的 `snapshot`；`levels` 满了只留最新一帧。一个卡住的页面拖不住别人，广播路径不同步等慢消费者。
- 多个连接看的是同一份状态：一次变更同一个 `revision` 同时到所有订阅者。

## 7. dev 替身与故障注入

`meeting/backends/dev.py` 是**只给开发与测试用的非产品路径，默认关闭**；怎么开见 `README.md` 第 4 节。同一个取值 `dev` 在两个后端键上按 key 分流：`capture.backend = dev` 落采集替身，`transcriber.backend = dev` 落转写替身（它**没有环境变量**，要开在配置文件里写）。

**采集替身**给两样东西：

1. **确定性合成电平**：种子是模块级常量，同一帧序下电平序列逐帧相同；每 **100ms** 一帧事实（`frame_interval = 0.1`），两路 `probe()` 都报 `ready`。
2. **故障注入**：走 `dev_inject` 动作（非 dev 后端回 `action_unavailable`(404)；`recording` / `alert` 态可注入，其余态回 `not_recording`(409)）。三档故障**到点生效、到点自动恢复**（`duration_seconds` 缺省 30 秒）；对同一路再次注入**替换**前一次并重新计时。三档都不动状态机的迁移路径——能不能回 `recording` 由判定说了算。

| `fault` | 干什么 | 拿它验什么 |
|---|---|---|
| `silence` | 这一路电平落到 −80 dBFS 以下并保持 | 静默累计 → 阈值调小后能进 `alert`，恢复后回 `recording` |
| `stall` | **两路的事实帧整体停发**（HTTP 照常应答） | `recording_alive` 变 `false` 而服务照常 200；开场无首帧那段先报 `null`。恢复后心跳的 `capture_gaps` 记下一条空洞（秒数 = 真实停发时长） |
| `lost` | 直接来一条 `SourceLost`，窗口内这一路整段不发帧 | 流中断立刻起致命级；恢复走 `alert.recover_seconds` |

**转写替身**是「会中能看见字幕在动」的那一条：开场 2.0 秒后首发、之后每 0.5 秒产一行两路轮换的合成转写。它不读归档、不加载模型、不写产物文件，`health()` 恒 `normal`、积压恒 `0`；`stop()` 之后一行都不再产。它**不写真实音频**：收尾 `flush` 在它身上记 `skipped`，转码工序不自动起。

## 8. 相关文档

- **可机读基准（端点口径、快照 schema、SSE 帧形状、`code`/`detail` 表）：`docs/contract.md`**
- 怎么起服务、配置键正本、跑测试：`docs/service.md`
- 会后纪要那一层怎么跑、产物四条判据：`docs/minutes.md`
- 会中总结那一层：`docs/summary.md`
- 后端怎么被找到、怎么插进来：`docs/backend-protocols.md`
- ASR 选型与参数依据：`docs/asr-backends.md`
- 界面怎么画这四态：`docs/ui-design.md`；前端工程：`web/README.md`
- 契约的 golden 样本与掩码规则：`contract/README.md`；索引：`docs/spec.md`
