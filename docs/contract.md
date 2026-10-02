# meeting · 双端契约

> 这份文档是**服务与界面之间那份契约的正文**：端点的口径、快照 schema、SSE 帧形状、动作信封与 `code`/`detail` 表、心跳语义、客户端义务，以及一份**定名但还没实现**的保留登记。
>
> 读者是接这套服务写界面（或写验收脚本）的人。它与 `docs/api.md`（HTTP 面速查）、`docs/state.md`（状态与推送的现状叙述）讲的是同一件事，但**口径不同**：那两份是叙述，这份是**可机读的基准** —— 每一条都在 `contract/fixtures/` 里有一个 golden 样本，`tests/test_contract_fixtures.py` 每次跑测试都重走一遍真实服务路径逐字比对，服务侧形状一漂移就红。**与叙述性文档冲突时，以代码实际行为为准**，并把权威落在这本册子里。
>
> 代码落点：路由只在 `meeting/service/routes.py` 注册一次；快照的唯一产出形态是 `meeting/state.py` 的 `Snapshot.to_dict()`；SSE 帧由 `meeting/service/hub.py` 编码；动作与 `code`/`detail` 在 `meeting/service/runtime.py`；三道本地防线在 `meeting/service/app.py` 的 `_LocalGuardsMiddleware`；golden 样本与掩码在 `contract/fixtures/` 与 `tests/_contract.py`。
>
> **这本册子的编号**：要求落在第 1–7 章（总则、快照 schema、SSE、动作、心跳、客户端义务、保留登记）；后面接了三章 —— 第 8 章讲 fixtures 与掩码、第 9 章讲复核手法、第 10 章回指索引。**第 8、9 两章是附录**（读契约正文用不上），第 10 章与其余分册的「相关文档」同形。**第 7 章登记表里还剩下的那几条一律没有实现**，界面按「能力还没进来」处理；会中总结那两样（`summarize` 动作、`GET /api/summaries`）已经落地，正文在第 4 章，登记表里只留一句「不做」与去处；**单场状态 `GET /api/sessions/{id}` 也已落地**（会后进度读数节点 `a9affaa8`），正文在第 4.9 节、登记表里不再列它。

## 章节目录

| 章 | 标题 |
|---|---|
| 1 | 总则：这份契约管什么 |
| 2 | 快照 schema |
| 3 | SSE：四事件与重连语义 |
| 4 | 动作：信封、`code` 表与逐档 `detail` |
| 5 | 心跳 |
| 6 | 客户端义务 |
| 7 | 保留登记：定名但未实现 |
| 8 | fixtures 与掩码（附录） |
| 9 | 复核手法 |
| 10 | 相关文档 |

## 1. 总则：这份契约管什么

**单一契约声明**：服务对外只有**一个**契约面，就是这一份。`GET /api/state` 的 body、SSE 的 `snapshot` 帧、SSE 的 `state` 帧**三者是同一个函数出的同一份东西**（`ServiceRuntime.to_dict()` → `Snapshot.to_dict()`），不存在「快照有两套形状」这回事。界面拿到哪一份都可以按第 2 章逐键读。

**口径一句话**：**九条 `/api/*` + `/debug` + 静态挂载**（`sessions` / `dirs` 两只读端点来自工作台收编节点 e2f6166b，单场状态 `sessions/{id}` 来自会后进度读数节点 `a9affaa8`，设备清单 `devices` 来自设置抽屉节点 `311f84f5`，形状分别在 4.8、4.9 与 4.10）。

| 路径 | 方法 | 给什么 |
|---|---|---|
| `/api/state` | GET | 完整快照（第 2 章），`200` |
| `/api/events` | GET | SSE（第 3 章），`text/event-stream` |
| `/api/heartbeat` | GET | 六字段心跳（第 5 章），**任何状态下都 200** |
| `/api/actions` | POST | 动作端点（第 4 章） |
| `/api/summaries` | GET | 本场总结记录（第 4.7 章），**任何状态下都 200** |
| `/api/sessions` | GET | 工作区注册表（第 4.8 章），**任何状态下都 200** |
| `/api/sessions/{id}` | GET | 单场状态：这一场此刻跑到哪一档、会后离线跑到第几块（第 4.9 章），**任何状态下都 200**；路径打不到已登记的工作区时 404 |
| `/api/dirs` | GET | 目录选择器的一层现场（第 4.8 章），**任何状态下都 200** |
| `/api/devices` | GET | 两路各自的候选设备端点表（第 4.10 章），**任何状态下都 200** |
| `/debug` | GET | 骨架自检页。从**包内**读取，不依赖 `web/dist`，与产物在不在无关。页面内显式标注非产品界面、不承担任何告警职责 |
| `/`（静态挂载） | GET | **三档**：产物目录有内容 → 整棵 `/` 子树交给 `StaticFiles(html=True)`；目录不存在 → 一页说明（`GET/HEAD /` 本身给说明页，其余路径回 404）；**目录在、里面是空的 → 404**。**存在性按每请求判定**：产物一建好刷新即可见，又被删掉刷新回说明页 |

挂载次序是硬约束：静态挂载**注册在所有 API 路由与 `/debug` 之后**，否则它会把 `/api/*` 一起吃掉。未匹配任何路由的路径由末端 404 兜底（回 `Not Found`，不是 Starlette 的 405）。

**`/` 的三档**（复审实测，三档各有出路）：

| 产物目录 | `GET /` 给什么 | 怎么走到别档 |
|---|---|---|
| 存在且有内容 | 产物里的 `index.html`（整棵 `/` 子树交给 `StaticFiles(html=True)`） | —— |
| 不存在 | 一页说得清的说明（`前端产物还没构建。跑 npm run build，或先用 /debug 自检页看服务状态`），`200`；`/` 之外的路径回 404 | 跑 `npm run build` 产出 `web/dist`，刷新即可见（**不必重启服务**） |
| **存在但是空的** | **`404 Not Found`** | 跑 `npm run build` 重新产出内容 |

第三档是 `StaticFiles(html=True)` 自己的形状：目录在、里面没有 `index.html`，它回 404 —— 旧实现同样如此，这一档随本轮实现一起保留。它和「目录不存在」分成两档的道理是：说明页承诺的是「产物还没构建」，而空目录意味着**构建产出被人清掉了**，这两种现场给同一句话会把后者说岔。出路一样，都是跑 `npm run build` 重新产出。

**`/api/*` 一律 `Cache-Control: no-store`**：这条由 ASGI 中间件无条件加上（先把应答里原有的 `cache-control` 头摘掉再补一个 `no-store`），因此每一条 `/api/*` 应答都带它，包括动作失败的应答。**不发 `Access-Control-Allow-Origin`**（不开 CORS），前端与本服务同源。

**只监听 `127.0.0.1`，不可配**（没有 `web.host` 这类键）。默认端口 `8765`；`web.port = 0` 时由系统挑一个空闲端口，真实端口从监听 socket 的 `getsockname()` 读回，写进快照的 `service.port` / `service.url` 并打进终端。

**Host 校验只比 hostname**：`Host` 头的 hostname 部分不是 `127.0.0.1` / `localhost` 就回 **403**（挡 DNS rebinding）。比对规则写死两条：

- 头以 `[` 开头（IPv6 字面量）→ 取 `[` 与 `]` 之间的那一段。于是 **`Host: [::1]` 会被判 403** —— `::1` 不在允许集合里，尽管它同样是回环地址。
- 否则头里含 `:` → 取第一个 `:` 之前的部分（剥掉端口）。于是 `127.0.0.1:8765` 与 `localhost:8765` 都放行。
- 最后按 `lower()` 比对。

**排障说明（`[::1]` 的 403）**：这条规则是**有意为之**的现状，行为符合预期。浏览器打 `http://127.0.0.1:8765` 或 `http://localhost:8765` 都会给出允许的 `Host`；只有手工指定 `http://[::1]:8765` 才会撞上 403。撞上时的正确处置是**换成 `127.0.0.1` 或 `localhost` 再访问**，不要去改 Host 校验的允许集合 —— 服务只绑在 IPv4 回环上，`[::1]` 本来也连不上这个监听 socket。

**这一档的 fixtures**：`contract/fixtures/snapshot/*.json`（五档快照）、`contract/fixtures/sse/*.json`（五帧）、`contract/fixtures/actions/*.json`（十七个：成功七档 `start-ok` / `stop-ok` / `mute-session-ok` / `settings-update-ok` / `summarize-ok` / `workspace-open-ok` / `workspace-remove-ok`；失败十档 = 九个 `code` 各一 + 未知动作名那一档，其中 `summary_failed` 这份是**缺键**那一档）、`contract/fixtures/heartbeat/*.json`（两个）、`contract/fixtures/summaries/*.json`（一个）、`contract/fixtures/sessions/*.json`（三个）、`contract/fixtures/devices/*.json`（一个）。

## 2. 快照 schema

顶层**恰好九键**，逐键如下。`GET /api/state` 的 body、SSE 首帧 `snapshot`、SSE 的 `state` 帧三处逐字相同；**重同步**那一帧额外多两键（见 3.5）。

| # | 键 | 类型 | 取值 / 掩码 |
|---|---|---|---|
| 1 | `revision` | int | 进程内单调递增；每次服务启动从同一个起点重新计，**不跨进程延续、不落盘**。**掩码保留真实值** —— 它由固定的迁移与提交次数决定，同样确定 |
| 2 | `service` | object | 五子键，启动时定一次（见下） |
| 3 | `top_state` | string | 四态之一：`idle` / `recording` / `alert` / `ended`。**不新增第五个** |
| 4 | `session` | object \| null | `null`，或 `{id, dir, started_at, mode, elapsed_seconds, frozen, paused}` |
| 5 | `sources` | array | **恰好两项**，次序恒为 `mic`、`loopback` |
| 6 | `alerts` | object | `{critical: Alert｜null, hints: [Alert]}` |
| 7 | `transcript` | object | `{health, last_line_at, buffered_lines, failed_segments}` |
| 8 | `ended` | object \| null | `null`，或 `{phase, steps: [DrainStep], error}` |
| 9 | `settings` | object | `{alarm_sound, heartbeat_interval_seconds, summary_default_window, asr{…}, devices{mic, loopback}}` |

`service` 五子键：`started_at`（ISO，**掩码 `<iso>`**）/ `version`（非空字符串）/ `port`（int，**掩码 `<port>`**）/ `url`（**掩码 `<url>`**）/ `log_path`（**掩码 `<path>`**）。`port` 与 `url` **只能由监听 socket 的 `getsockname()` 读回值产生**，绑定之前取快照一律抛错 —— 不留退化的空值、不拿配置值或字面量顶替。**没有 `out_root`**：会放哪由工作区给，服务不设定默认落点（`docs/design-intent.md` 第 4 条）。

`session` 七子键：`id` 是**工作区目录名**（就是这一场会的名字，由 start 请求显式给的目录来，**形状任意**、不再按日期命名，**掩码 `<session-id>`**）/ `dir` 是场次目录路径（**掩码 `<path>`**）/ `started_at`（ISO，**掩码 `<iso>`**）/ `mode ∈ both｜mic｜loopback`（开场前定死，录音期间不切）/ `elapsed_seconds`（**掩码 0**）/ `frozen`（bool）/ `paused`（bool）。

- `elapsed_seconds` 在 `recording` / `alert` 态走秒（每次取快照现算、`round(…, 3)`），**进 `ended` 就冻结**（`frozen: true`，此后不再动）。**暂停的那几段不计进去**：这一格说的是「这一场录到了多少」，暂停期间它停在原地（第 7 条的口径 —— 权威长度是实际录到的音频时长，不含暂停；权威数仍以盘上 `session.json` 的 `audio_duration_seconds` 为准，这一格是它的读数）。
- **`paused` 是「暂停中」的唯一落点**（`pause` / `resume` 两个动作翻它）：**顶层不新增第五个状态**，`top_state` 照旧只有四态。暂停期间那一截写数字静音、不录内容（第 8 条），段**不**切（第 9 条），`sources[*].health` 一档都不动（设备还开着、电平帧照发，界面上那两路照旧实时）。收尾一开始服务把这一格复位（`ended` 那一档恒为 `false`），下一场 `start` 从 `false` 起。
- **`top_state` 停在 `ended` 而那场的 `session` 还留着**（成功档与失败档都留）。直下一场 `start` 才换。

`sources[*]` **九键**：`id`（`mic` / `loopback`，**不掩码**：它是取值域字面量）/ `label`（`本地` / `远端`）/ `device_name`（string \| null）/ `health` / `lost_since`（ISO \| null，**掩码 `<iso>`**）/ `restored`（bool）/ `level_dbfs` 与 `peak_dbfs`（number \| null，**掩码 `<dbfs>`**）/ `envelope`（number 数组，**逐根掩码 `<dbfs>`**，长度不变）。

- `health` **七取值**：`unprobed` / `ready` / `probe_failed` / `capturing` / `lost` / `restored` / `stopped`。前三档由启动时的设备探测直取（后端 `probe()` 只许报这三档），后四档由状态迁移写值。**七档之外不新增第八个**：会中关掉某一路（动作 `set_source` 的 `active=false`）落的就是现成的 `stopped` 那一档（`capturing → stopped`），重新打开回到 `capturing`（关掉之前那一路还挂着致命级时回 `lost`，让有信号时的恢复路照常走）——**这是用户按的按钮**：不报 `SourceLost`、不进告警，与 `lost` 那一档分得清清楚楚。
- `device_name` 是**探测实际落到的那个端点名**（试开通过的那一条），与用户在配置文件里配的名字是两件事 —— 配置表达的是**意图**，这一个字段表达的是**结果**，两者的关系见第 2 章 `settings.devices` 那一段。
- `envelope` 是最近 30 秒的电平包络：**15 根柱、一根 2 秒**。一根柱的值 = 它那 2 秒窗口内所有 `LevelFrame.peak` 的最大值；窗口里一帧都没有就沿用左边那根。**录音中（乃至 `ended` 停留档）长度恰为 15**，会话开场（还没开始录）是**空数组 `[]`**。这根柱由服务算，**前端不许自己造**。
- 两路的位置是固定的：`sources[0]` 恒为 `mic`、`sources[1]` 恒为 `loopback`。

`alerts`：`critical` 是 `Alert｜null`（**致命级同时只留一条**），`hints` 是 `Alert` 数组（**同类合并、上限 3**）。`Alert` **六字段**：`{level, source, since, duration_seconds, message, muted}`。

- `level ∈ critical` / `hint`；`source` 是那一路的 id。提示级那两条（`AlertJudge.on_transcript_health()` / `on_backlog_depth()`）的 `source` 定名也是 `transcript`，服务侧**每刷一次转写快照就把当前 health 与积压段数交给它们**（接线落在 `ServiceRuntime._refresh_transcript_snapshot()`，见第 3 章那一格与 `docs/state.md` 第 6 章）：`transcript.health` 掉到 `behind` / `stopped` 出一条、待转写段数 ≥ 20 出一条，两条各自对称地清掉（health 回 `normal`、积压回落到 20 以下）。
- **`hints` 只在 `top_state ∈ {recording, alert}` 时可能有条目**：`idle` 与 `ended` 两档恒为空数组（`docs/ui-design.md` 章 2.5 的「什么状态显示什么」表把空闲态的告警区定成不出现）。空闲态那一格 `transcript.health == "stopped"` 报的是「真转写后端还没起过场次」，与录音中「转写停了」属于两种情形，它不产生提示级；离开录音 / 告警态时服务侧把提示级连内部记账一起清掉，下一场 `start` 从零开始。
- `since` 是 ISO（**掩码 `<iso>`**）；`duration_seconds` **掩码 0**。
- `muted` 说的是**这一条现在会不会出声**（会话级静音生效期间，或这一条还在抑制窗口里）。判定与窗口计算**全在服务侧**：界面只看这个布尔值，`false` 就响一声、`true` 就不出声、界面不算任何窗口。**重算只发生在 `ServiceRuntime.commit()` 那一刻**：抑制窗口到点本身不产生提交，所以「窗口已过、下一次提交还没来」那段里快照上的 `muted` 仍是上一次算出来的 `true`（不重复出声）。
- **首发那一帧是 `false`**：一条**新起**的告警在广播出去的第一帧 `muted` 是 `false`（界面据此响一声），此后同一条是 `true`（不重复出声）；恢复后再次发生按新事件处理，首发又是 `false`。本场静音期间一律 `true`（那条「刚起、还没广播」的条目在静音期间同样算 `true`）。`contract/fixtures/snapshot/alert.json` 抓的就是首发那一帧（`"muted": false`）。
- **心跳失联那条致命级由界面自己判、自己画**，服务侧不产生它。

`transcript`：`health ∈ normal｜behind｜stopped`（直接来自转写后端。**空闲态是 `stopped`** —— 真实现没起过场次时报的就是它，`contract/fixtures/snapshot/idle.json` 那一格就是这个值；缺席档的缺省替身恒报 `normal`）；`last_line_at` 是 ISO \| null（**掩码 `<iso>`**，服务侧收到最近那一行时的墙钟）；`buffered_lines` 是积压段数、`failed_segments` 是累计识别失败段数 —— **这两个计数只进 log 与 `/debug`，产品界面不摆它们**，它们写进快照但不给 `revision` 投票（四格里只有 `health` 的迁移才提交）。刷新路径：开一场就地刷一次、每出一行刷一次，另有一条 0.5 秒的低频节拍兜住「worker 停摆、没有行可等」那一档；**读路径（`GET /api/state`、SSE 首帧）不碰后端，也不改状态**。同一批行逐行落在场次目录里的 `meeting_transcript_<source>.md`（会中临时稿，一行 `[HH:MM] 正文`，见 `docs/state.md` 第 3 章）；**会中这份是临时稿**，会后那份带说话人编号的稿（`meeting_offline_transcript_<source>.md`）另落一份，两份平级放在一起对拍（每路一份、两路不合并，第 7 章）。

`ended`：`phase ∈ draining｜done`（**`failed` 不是这个字段的取值** —— 它属于盘上 `session.json` 的 `state`，在快照里没有对应位置）；`steps` 是四步各一条，次序恒为 `flush` / `transcode` / `log` / `post`；`error` 是 string \| null。

`DrainStep` **四键齐**：`{id, status, detail, error}`。`id` 取上面四个字面量之一；`status ∈ waiting｜running｜done｜failed｜skipped`；`detail` 是给人看的一句话（string，可为空串）；`error` 是 string \| null（**`error` 键恒在**，没有错误时就是 `null`）。

- 收尾失败后 `phase` **保持 `draining`**、`ended.error` 是那一刻的原因（与失败步的 `error` 同文），这份摘要与 `session` 一起留在快照里。
- 成功档收尾的副作用会把两路 `health` 从 `stopped` 带回 `unprobed`；失败停留档**保留 `stopped` 现场**。

`settings` 五键：`alarm_sound`（bool）、`heartbeat_interval_seconds`（int，界面发心跳的周期）、`summary_default_window`（四档取值 `5m` / `15m` / `30m` / `full`，快速总结的缺省档，`311f84f5`）、`asr`（五子键，与配置键 `asr.*` **逐字同名**、只去掉前缀）、`devices`（两子键 `mic` / `loopback`，与配置键 `devices.mic` / `devices.loopback` **逐字同名**、只去掉前缀，见第 7 章）。

- `asr` 五子键：`max_segment_seconds` / `timeout_seconds` / `max_new_tokens` / `repeat_max_unit` / `repeat_min_coverage`。
- `devices` 两个子键的取值是**设备名字符串**；没配就是 `null`（按默认策略挑设备）。空值形状两端一致：配置文件里也写 `null`，**空字符串与纯空白串按没配处理**（`meeting/config.py` 的 `validate_value()` 把两个设备键位上的空串与纯空白串归一成 `null`）——`settings_update` 写 `"devices.mic": ""` 或 `"   "` 会照常落盘、落成 `null`。
- **`settings.devices` 是用户配的意图，`sources[*].device_name` 是探测实际落到的那个设备**（`dca4539b`；落地代码见 `meeting/capture/devices.py` 的 `mic_candidates()` / `loopback_candidates()`）。两件事各说各的，配了却没命中时**不会**因此报错、也不会把快照里的名字改写：
  - `mic`：按配置的名字找端点（精确名先行，找不到再按前缀兜一次）；候选为空时**回落到系统默认输入端点**（`sd.default.device[0]`，读不出来再退 `sd.query_devices(kind="input")`），主 log 一行 `WARNING`；
  - `loopback`：精确名命中即用；没命中就按「名字含 `CABLE Input` 且不含 `16ch`」挑；再没有就取**系统默认输出**（`sc.default_speaker()`），主 log 一行 `WARNING`（`meeting/capture/devices.py` 的 `loopback_candidates()`，搬自 V1 `recorder.py:451-465` 的原行为）；
  - 于是快照里的 `settings.devices.mic` 仍是用户写下的那个名字，而 `sources[0].device_name` 是**真正在采的那条端点**。判断「这一场到底在录哪个设备」时看 `device_name`。
- 这里读到的是**运行期生效值**。非热读的键改了要重启才在快照里可见（第 4 章）；热读键（`alarm.sound`、`asr.*` 五个与设置抽屉那四项：两个设备键位、`heartbeat.interval_seconds`、`summary.default_window`）写盘那一刻就换过来。

**`notice` 槽位已退场**（`docs/design-intent.md` 第 4 条）：原「启动扫最近一场 → 空闲态挂 `restart_unfinished` 提示」整条随「默认输出根」一并拆掉，快照顶层不再有这个键；**工作区的登记与"最近一场"已由收编节点落地**（`GET /api/sessions` 的 `recent`，形状见 4.8）——启动序列对场次目录照旧一个字不读。

**`revision` 的语义**：递增与广播只经 `ServiceRuntime.commit()` 一处。同一次事实处理里的多次改动合并成一次 `+1`、一次全量快照；被状态机拒绝的动作**不涨 `revision`、不写盘、不广播**。界面拿它作「我手上这份是最新的吗」的参照，**不许自己算它**。

### resync 帧额外的两键

慢消费者重同步那一帧（第 3.5 节）在顶层九键之上**额外多两键**：

| 键 | 类型 | 取值 |
|---|---|---|
| `resync` | bool | 恒 `true` |
| `dropped` | int | ≥ 1：丢掉多少条之后补的这一份 |

顶层键集合因此恰好是**十一键**（九键 + `resync` + `dropped`）。**界面据此判断「我漏了一段」**：见到 `resync: true` 就整块换成这份快照，不要试图补齐中间过程。

**这一档的 fixtures**：`contract/fixtures/snapshot/{idle,recording,alert,ended-done,ended-draining}.json`、`contract/fixtures/sse/resync-snapshot.json`。

## 3. SSE：四事件与重连语义

`GET /api/events`，应答 `Content-Type: text/event-stream`，另带 `Cache-Control: no-store`、`X-Accel-Buffering: no`、`Connection: keep-alive`。

**连接即快照**：建立连接后第一帧**必须是 `snapshot`**（完整状态），新开的页面直接落在当前状态、**不经过 `idle`**。首帧时序上有一条硬要求：`stream()` 里「取快照」与「注册订阅」处在**同一段同步代码**、两者之间**不出现 `await`** —— 于是「注册前发生了一次状态变更 → 首帧落后于订阅时刻」的缝不存在。

**开头的 `retry`**：连接后先发一帧 `retry: 2000`（毫秒），再发首帧 `snapshot`。断线由浏览器自己按这个间隔重连。

### 3.1 四个事件的帧形状

帧格式一律 `event: <名字>` + `data: <一个 JSON 对象>`；`state` / `snapshot` **多一行 `id: <revision>`**，放在 `event:` 之前。JSON 用 `ensure_ascii=False` 序列化（中文原样出），帧尾是两个换行。

| 事件 | 什么时候发 | 带 `id:` 吗 | `data` 形状 |
|---|---|---|---|
| `snapshot` | 建连首帧；慢消费者重同步 | **带**，值 = 快照的 `revision` | 完整快照（重同步时多 `resync` / `dropped` 两键） |
| `state` | 每次状态变更（**全量快照，不做增量**） | **带**，值 = 快照的 `revision` | 完整快照（第 2 章九键） |
| `levels` | **只在 `recording` / `alert` 态**，**4 次/秒**（两路合在一帧里） | **不带** | `{revision, frames: {mic: {…}, loopback: {…}}}` |
| `transcript` | 每出一行转写 | **不带** | `{revision, line: {ts, source, text, kind}}` |

编码形态逐字举例（`data` 压成一行只是为了排版）：

```
retry: 2000

id: 3
event: state
data: {"revision": 3, ...}

event: levels
data: {"revision": 3, "frames": {...}}

: ping
```

- **`levels` 与 `transcript` 不带 `id:`**，只在 `data` 里带当前 `revision` 作参照。这两路是高频通道，**不占 `revision`**（它们的到来不代表状态变更）。
- `state` / `snapshot` 的 `id` **与 `data.revision` 是同一次提交的那个数、逐值相等**；界面可以说「id 变了就是状态变了」。
- `levels` / `transcript` 的 `revision` 是**参照值**：它说的是「这帧数据属于哪一版状态」。

**保活**：安静时每 **15 秒**一行注释帧 `: ping`（只有这一行，**不伪造 `event` / `data`**）。它的作用是让中间层与浏览器不要把这条连接判死。

### 3.2 levels 帧

`data = {revision, frames}`，`frames` 恰好两键 `mic` / `loopback`。每路**五键**：

| 键 | 类型 | 取值 |
|---|---|---|
| `ts` | null | **恒为 `null`**（这一条写死：服务抽帧时不带帧时间戳） |
| `dbfs` | number \| null | 瞬时电平（**掩码 `<dbfs>`**） |
| `peak` | number \| null | 峰值保持（**掩码 `<dbfs>`**） |
| `bar` | number \| null | **恒等于该路 `envelope[-1]`**（同一个数的两种读法） |
| `envelope` | number 数组 | 长度恰 15（**逐根掩码 `<dbfs>`**） |

**`ts` 恒为 `null` 是契约的一部分**（这一格永远填 `null`）：界面要显示时间，用 `data.revision` 或自己那一侧的到达时刻，**不要等 `ts` 出值**。

`bar == envelope[-1]` 是**结构不变量**，fixtures 比对时在掩码前的原始载荷上断言。包络长度 15 同理。

### 3.3 transcript 帧

`data = {revision, line}`，`line` 恰好四键：

| 键 | 类型 | 取值 |
|---|---|---|
| `ts` | number | 后端给的秒数（**语义归后端**，服务只透传；fixtures 里那个 `1.5` 是假转写后端的固定值）。真转写后端给的是**采集时刻的墙钟秒数**（epoch 秒）：段一入队就盖章，识别慢不会把后面几行的时间戳挤到一起 |
| `source` | string | 哪一路（`mic` / `loopback`） |
| `text` | string | 这一行的正文。识别失败的段落也照出一行，正文是**占位符**（双层方括号的裸文本，不带时间戳、不含 `**`）：`[[识别失败: 超时 20s]]` / `[[识别失败: 重复循环]]` / `[[识别失败: 无输出]]` |
| `kind` | string | `line` 或 `gap`（断口）。真转写后端产出的照旧是 `line`，失败段落也走 `line`，靠 `text` 的占位符形状认出来 —— 界面按那个形状把它画成缺口（`docs/ui-design.md` 第 2 章），别按正文渲染 |

一行到来时服务侧只更新 `transcript.last_line_at` 并推这一帧，**不参与状态迁移、不涨 `revision`**。

这条「不涨 `revision`」带**一个**例外，口径写死如下：`alerts.hints` 的**键集合**变化（提示级新增 / 移除 / 被上限 3 挤掉，以及离开 `recording` / `alert` 时整格清空）要能被界面看见，于是转写刷新返回「有变化」、由既有调用点借 `state` 通道提交一次（`revision +1` + 一次全量 `state` 广播）；**同一个键集合里 `duration_seconds` 的刷新不算变化**，返回假、不提交 —— 否则 0.5 秒的节拍会把 `revision` 一直推上去，上面那句「不涨 `revision`」当场变成假命题。判据落在 `ServiceRuntime._sync_transcript_hints()`，逐句口径见 `docs/state.md` 第 6 章。

### 3.4 重连语义

断线重连（浏览器自己按 `retry: 2000` 重连）之后：

- **服务不重放断开期间的事件**。没有 `Last-Event-ID` 的重放机制，也没有事件缓冲。
- **重连后仍然先发 `snapshot`**。界面拿到它**整块替换**自己手上的那份 —— 这就是全部的重同步手段。
- 界面的正确姿势因此是：**以 `snapshot` / `state` 为唯一权威、以 `levels` / `transcript` 为增量流**；重连后不要试图拿 `id` 去补齐中间过程（第 6 章）。

**关页面只做一件事**：把订阅者从 hub 摘掉。**不动会话、不停采集**；最后一个订阅者走了服务照跑、录音继续。

### 3.5 慢消费者 resync

每个订阅者一个**有界队列，容量 512**，外加一个只留最新一帧的 `levels` 槽。

- 队列**满了**（`state` / `transcript` 这两路）→ **清空该订阅者的队列、塞一份新的 `snapshot`**，带顶层额外两键 `resync: true` 与 `dropped: N`（`N ≥ 1`，是丢掉多少条之后补的这一份），事件 id 取快照的 `revision`。**宁可让它重同步，也不许它拖住别人。**
- `levels` 这一路满了**不触发重同步**：每个订阅者只留**最新一帧**，旧的直接盖掉。
- **广播路径是同步的、不 `await` 慢消费者**：任何一次广播都不会因为某个订阅者的队列满而阻塞。一个卡住的页面拖不住别人、也不拖住状态机。
- 清队列与补快照这两步之间**不产生新的丢帧窗口**：补进去的那一份是**当前**快照（采集那一刻的最新值）。

**界面据此做什么**：见到 `resync: true` → 整块替换快照、把「我漏了 N 条」如实告诉用户（或至少知道刚才那段电平/转写不连续）。**不要**把 `dropped` 当成错误码。

**这一档的 fixtures**：`contract/fixtures/sse/{first-snapshot,state,levels,transcript,resync-snapshot}.json`。

## 4. 动作：信封、`code` 表与逐档 `detail`

### 4.1 请求信封

```json
{"action": "start", "args": {"mode": "both", "dir": "C:\\会议\\跟老王聊周会"}}
```

- `action` 是动作名（string）。
- `args` 是**对象**（省略时按 `{}` 处理）。

**两个必需请求头（客户端义务）**：`POST /api/actions` 必须同时带

```
Content-Type: application/json
X-Meeting-Service: 1
```

**缺任一条回 403**（`Content-Type` 按前缀比对，`application/json; charset=utf-8` 也算过）。这道校验在**中间件里、先于路由匹配**生效，挡的是「本机浏览器里的恶意页面用表单式简单请求打过来」。403 的 body 是一行**纯文本**，走的是中间件自己那条出口，**第 4.2 节的失败信封在这里用不上** —— 请求压根没走到端点。

### 4.2 应答形状

**成功**（`200`；`settings_update` 例外，见 4.6）：

```json
{"ok": true, "revision": 2}
```

**失败**：

```json
{"ok": false, "code": "not_recording", "message": "现在没有在录的场次，结束动作无效", "detail": {}}
```

- `ok` 恒为 `false`；`code` 是机器可读的那一个（第 4.4 节，界面据此选失败块的画法）；`message` 直接能显示；`detail` **恒是对象** —— 有补充信息就逐键带着，没有补充信息时给**空对象 `{}`**（这一格写 `null` 会让界面多一条判空分支）。
- HTTP 状态码由 `code` 决定，同一个 `code` 的状态码是固定的。

**请求体本身的毛病**（不是 JSON / 不是对象 / `args` 不是对象）也走这个信封：`code = invalid_setting`、`status = 400`、`detail = {}`。

### 4.3 十四个动作的语义

| 动作 | 语义 | `args` |
|---|---|---|
| `start` | 开一场：校验落点 → 写第一版 `session.json` → 起采集 → 进 `recording`（`idle` 与 `ended` 都能出发） | `dir`：**必给** —— 一条**已存在**的目录的路径，这一场的工作区与落点（服务**不建目录、不设默认落点、不按日期命名**，`docs/design-intent.md` 第 3、4 条；目录里已有另一场的 `session.json` 时拒）；`mode`：`both` / `mic` / `loopback`，**缺省 `both`** |
| `stop` | 结束本场：冻结时长 → 进 `ended·draining` → 服务按四步收尾跑到终态。**第 4 步 `post` 不自动拉起任何会后流程**（R26）：它做完该做的（让会中模型退出显存）就停在 `waiting`（＝等你动手），会后归下面两个动作 | 无 |
| `mute_session` | 本场不再出声：`alerts.*.muted` 立刻变 `true`。**不改**持久化开关 `alarm.sound`，**下一场 `start` 自动复位** | 无 |
| `pause` | **暂停**：这一场从这一刻起不录内容 —— 两路的归档改写数字静音（第 8 条），快照 `session.paused` 变 `true`，段**不**切（第 9 条），时长读数停在原地（第 7 条）。暂停的那几段时间**不计入静默判定**：安静房间里暂停多久都不会冒出一条「这一路静默已达 N 秒」的致命级（第 17 条）。顺带让**会中模型当场退出显存**（可选成员 `release_gpu()`，异步提交，应答不等它做完；第 21 条点名了「暂停」这一格）。幂等；顶层四态一个字不动 | 无 |
| `resume` | **继续**：归档回到录内容，`session.paused` 回到 `false`，时长接着走。顺带把**会中模型拉回来**（配对的 `acquire_gpu()`，同样异步提交）。幂等 | 无 |
| `set_source` | **会中关掉某一路 / 重新打开某一路**：`active=false` 关掉 —— 那一路停流、改写静音，**不报 `SourceLost`、不进告警**（用户按的按钮与「这一路真死了」是两件事），快照里那一路落 `health = stopped`；`active=true` 重新打开 —— 重新挑设备、重开流，成功回 `capturing`（关掉之前那一路还挂着致命级时回 `lost`，等有信号的自然恢复），**打不开**（设备又被别的程序占着这一档）如实回 `capture_unavailable`(503) 且那一路照旧停在 `stopped`。幂等；两种方向都不切段 | `source`：**必给**，`mic` / `loopback`；`active`：**必给**，bool（`true` 打开、`false` 关掉） |
| `settings_update` | 写配置文件（原子写），键态两分见 4.6 | 「配置点号键名 → 值」的扁平映射 |
| `reprobe` | 重新枚举并试开设备，结果落进快照两路（`revision +1`）。**只在没有场次在跑的时候可用**：`recording` / `alert` 回 `already_recording`(409) | 无 |
| `dev_inject` | 注入故障（**只在 dev 采集替身在场时可用**） | `source` / `fault` / `after_seconds` / `duration_seconds` |
| `summarize` | 会中一键总结：把最近这段时间的实时转写读一遍，给回三小段（见 4.7）。**只读**：不写快照、不动 `revision` | `window`：`5m` / `15m` / `30m` / `full`，**缺省取配置的 `summary.default_window`**（`15m`） |
| `workspace_open` | 打开/收编一个**已存在**的目录，登记进工作区注册表（4.8）。空目录当新会、有会议材料（含疑似会议目录）当收编、**非空且无材料**要 `confirm: true` 才登记。**任何状态下都可用**（清单维护不碰快照） | `dir`：**必给**，已存在目录的路径；`confirm`：非空无材料那一档确认收编时为 `true` |
| `workspace_remove` | **只把这一条从注册表摘掉**：工作区目录与里面所有文件一个字节不动（`docs/design-intent.md` 第 5 条）。没登记过这一条也照样 200（`removed: false`，重复删除安全） | `dir`：必给 |
| `post_offline` | **「生成会后精细稿」**：只跑会后**离线**那一条链（走离线模型，慢，出带说话人编号的那份稿），**纪要不碰**。起一条后台线程即算启动成功（回执只说跑了哪一场）；跑成什么样由场次目录里的 `meeting_offline_status.json` 回答 | `dir`：**可省** —— 要跑的那一场（工作台里选中并按下按钮的那一行，界面那一格 `has_offline` 说的也是它）。该路径与 `GET /api/sessions` 同一套注册表 / 目录判读：不在注册表里、目录不在原地、或盘上 `state` 是 `recording` / `draining` → `invalid_setting`(400)。**省略时跑收尾刚落定的那一场**（命令行与既有调用走这一路） |
| `post_minutes` | **「直接出纪要」**：把这一场**手上有的稿子**交给 DSH 出纪要（会后稿在就两份一起给，缺它按现有档位降级）。**不补跑离线那一层**（R9：那一条是可选的）。起一条后台线程即算启动成功 | 同上（`dir` 的校验与省略语义一致） |

**`pause` / `resume` / `set_source` 三个动作都没有 golden 样本**：照 `dev_inject` / `reprobe` 的先例，`contract/fixtures/actions/` 里只放成功七档与九个 `code` 的失败档，这三个动作的语义由 `tests/test_pause_actions.py` 逐条实测（走 `tests._capture_faulty:FaultyCaptureBackend` 那一档假采集）。


**这两个会后动作的三档拒绝**（都落在既有的 `code` 上，不加新 `code`）：

- **这一场还没结束 / 收尾还没落定**，或**已经有一趟会后活没跑完** → `draining`(409)；
- **`post_minutes` 那一层的账停在 `running`**（纪要那一层还在飞：`meeting_minutes_status.json`
  读得出 `state = "running"`）→ `draining`(409)，`detail` 带 `status_path` 与那一版的原因
  —— 这一格在**动作本体**里同步判（复用 `meeting/minutes/status.py` 那把 claim 闸），
  因此重复按「直接出纪要」拿到的是拒跑档，不会拿到一个「已起」的假回执；
- 这台机器上**没接这一层的实现**（`post.hook = null` / `minutes.hook = null`）→ `action_unavailable`(404)；
- 成功体是通用信封 + 一个 `post` 键：`{ok, revision, post: {kind: "offline"｜"minutes", dir}}`
  —— 回执里**没有**「跑完了没有」这一格，那一趟可能跑一小时。

**`start` 的失败顺序是固定的**：先判态（`already_recording` / `draining`）→ 再校验 `args`（`dir` 缺失 / 目录不存在 / 目录里已有另一场的记录、`mode` 非法 → `invalid_setting`）→ 再判后端可用性（`capture_unavailable`）→ 最后才写第一版文件。**态不对或后端不可用时盘上不落这个目录里的任何新东西（目录本身是用户给的，软件也不删它）。**

**`reprobe` 的 `detail` 形状**：成功体就是通用的 `{ok, revision}`（**没有额外字段**），挑不到设备不构成动作失败 —— 那一档如实落进快照两路的 `health = probe_failed` 与 `probe()` 的 `detail` 里。

**`stop` 成功后 `revision` 会继续往上走**（收尾四步各自提交一次），`ended.phase` 最终落在 `done`（前三步 `done` / `skipped`、第 4 步停在 `waiting`、无 `failed`）或**保持 `draining`**（有 `failed` 步，或收尾被取消）。**`ended` 不回到 `idle`**：下一场的 `start` 从 `ended` 直接进 `recording`。

**`dev_inject` 的三档故障**：`silence`（这一路电平落到阈值以下）/ `stall`（两路事实帧整体停发，HTTP 照常应答）/ `lost`（直接来一条 `SourceLost`）。`after_seconds` 到点生效（可为 0），`duration_seconds` 到点恢复（正数，可省，**缺省 30.0 秒**）。非 dev 后端回 `action_unavailable`(404)；没有正在进行的场次回 `not_recording`(409)。

### 4.4 `code` 表与逐档 `detail` schema

九个 `code`，一个不多一个不少：

| `code` | HTTP | 意思 | 谁会回它 |
|---|---|---|---|
| `already_recording` | 409 | 已经有一场在录 | `start` / `reprobe` |
| `draining` | 409 | 上一场还在收尾（或收尾失败后停留），这场不能开；会后两个按钮拿它回「收尾没落定 / 有一趟会在飞」 | `start` / `post_offline` / `post_minutes` |
| `capture_unavailable` | 503 | 采集后端不可用：没接入、或设备起不来 | `start` / `set_source`（重新打开某一路时那个设备打不开） |
| `not_recording` | 409 | 现在没有在录的场次 | `stop` / `mute_session` / `pause` / `resume` / `set_source` / `dev_inject` / `summarize` |
| `invalid_setting` | 400 | 参数或键名非法（校验都在动手之前） | 各动作 |
| `persist_failed` | 500 | 配置写盘失败，这次改动一处都没生效 | `settings_update` |
| `action_unavailable` | 404 | 这个后端没有这个动作；会后两个按钮拿它回「这台机器上没接这一层」 | `dev_inject`（非 dev 后端）/ `post_offline`（`post.hook` 缺席）/ `post_minutes`（`minutes.hook` 缺席） |
| `transcript_unavailable` | 503 | 转写没在跑（没起过，或者已经停了）：会中总结吃的是实时转写，现在没有可用的那一路 | `summarize` |
| `summary_failed` | 502 | 会中总结没拿到结果：模型端点没配齐，或预算用尽 / 端点报错 / 解析连试带败 | `summarize` |

**逐档 `detail` schema**（`detail` 恒是对象；下面写「空」的就是**空对象 `{}`**）：

| `code` | `detail` 形状 | golden 样本 |
|---|---|---|
| `already_recording` | `{session_id, dir, elapsed_seconds}` —— 场次目录名 / 场次目录路径 / 已录秒数（掩码 0） | `actions/fail-already-recording.json` |
| `draining` | **四档**：`{session_id, dir, failed_steps: [{id, error}], log_path}`（`start` 那一档；没有失败步时 `failed_steps` 是**空数组**）；或 `{kind, dir, status_path, reason}`（纪要那一层的账停在 `running`：重复按「直接出纪要」拿到的是这一档，见 4.3）；或 `{kind, dir, started_at}`（已经有一趟会后活在跑，再按两个按钮中的任何一个）；或 `{top_state, session_id, dir}`（还在录 / 收尾还没落定） | `actions/fail-draining.json`（`start` 那一档；会后按钮那三档由 `tests/test_post_actions.py` 覆盖） |
| `capture_unavailable` | **四档**：空对象；或 `{session_id, dir, error}`（第一版 `session.json` 写不进去）；或 `{session_id, dir, reason}`（采集起不来）；或 `{source, error}`（`set_source` 重新打开某一路时那个设备打不开，`error` 是 `类型名: 消息`） | `actions/fail-capture-unavailable.json` |
| `not_recording` | 空 | `actions/fail-not-recording.json` |
| `invalid_setting` | **四档**：`{key, value, legal}`（值非法；`legal` 是合法取值数组）；或 `{source, fault, error}`（`dev_inject` 的取值非法）；或空。**未知动作名是第四档**：`{action, known}`（`known` 是十四个动作名的数组） | `actions/fail-invalid-setting.json`、`actions/fail-invalid-setting-unknown-action.json` |
| `persist_failed` | `{error}` | `actions/fail-persist-failed.json` |
| `action_unavailable` | 空 | `actions/fail-action-unavailable.json` |
| `transcript_unavailable` | `{health: "stopped"}` —— 恒这一格（转写没在跑就是这一档，健康度只有一个取值） | `actions/fail-transcript-unavailable.json` |
| `summary_failed` | **两档**：`{missing: ["summary.base_url", …]}`（端点没配齐，点名缺的配置键）；或 `{elapsed_seconds, attempts}`（预算用尽 / 端点报错 / 解析连试带败，如实报等了多久、试了几次） | 缺键那一档 `actions/fail-summary-failed-missing.json`；预算 / 端点那一档要一个真会等满预算的假端点，由 `tests/test_summary_action.py` 逐档实测 |

**`capture_unavailable` 的四档怎么分**：`start` 时采集后端**压根没接入**（`null` 替身直接抛 `CaptureUnavailable`）→ 空对象；第一版 `session.json` **写不进去** → `{session_id, dir, error}`（`error` 是 `类型名: 消息`）；采集后端 `start()` **起不来** → `{session_id, dir, reason}`（`reason` 是后端给的那句人话）；`set_source` 想把已经关掉的那一路重新打开、**设备起不来**（又被别的程序占着）→ `{source, error}`，那一场照常往下录（另一路照旧），快照里那一路照旧停在 `stopped`。四档都是 503，**界面的正确姿势是按 `code` 选画法、把 `detail` 有哪个键就显示哪个键**。

**`invalid_setting` 的四档怎么分**：`key` 在 → 显示「这个键/参数不合法，合法取值是 …」（`set_source` 的 `source` / `active` 取值非法、或那一场根本没采这一路时走的也是这一档）；`source` 在 → 显示「注入的取值不对」（`dev_inject` 那一档）；`action` 在 → 显示「不认识的动作名」并列出 `known`；一个键都没有 → 显示 `message` 就够了。

**`failed_steps[*]`** 恰好两键 `{id, error}`（`id` 是四步字面量之一、`error` 是那一刻的原因串）。界面把每一条列出来 + 服务级 log 路径（`log_path`）就够画「收尾失败块」了。

### 4.5 逐动作 × 逐态的可用性

| 动作 | `idle` | `recording` | `alert` | `ended`·`done` | `ended`·`draining` |
|---|---|---|---|---|---|
| `start` | 200：写 `session.json`、进 `recording`；落点是 `args.dir` 显式给的已存在目录，没有默认落点 | 409 `already_recording` | 409 `already_recording` | 200（从 `ended` 直接进 `recording`，顺手清掉上一场的 `ended` 摘要） | 409 `draining` |
| `stop` | 409 `not_recording` | 200 | 200（进 `ended` 不必先回 `recording`） | 409 `not_recording` | 409 `not_recording` |
| `mute_session` | 409 `not_recording` | 200 | 200 | 409 `not_recording` | 409 `not_recording` |
| `pause` | 409 `not_recording` | 200（`session.paused` 变 `true`、那一截改写静音；顺带把会中模型放出显存） | 200（同上；告警照旧挂着） | 409 `not_recording` | 409 `not_recording` |
| `resume` | 409 `not_recording` | 200（`session.paused` 回到 `false`；顺带把会中模型拉回来） | 200（同上） | 409 `not_recording` | 409 `not_recording` |
| `set_source` | 409 `not_recording` | 200（关掉 / 重新打开那一路；重新打开时设备打不开 → 503 `capture_unavailable`；`source` 非法或本场没采这一路 → 400 `invalid_setting`） | 200（同上） | 409 `not_recording` | 409 `not_recording` |
| `settings_update` | 200 | 200 | 200 | 200 | 200 |
| `reprobe` | 200（重新枚举并试开设备，两路快照随之更新、`revision +1`） | 409 `already_recording` | 409 `already_recording` | 200 | 200（采集已经停了，重新探测不干扰在飞的东西） |
| `dev_inject` | 409 `not_recording` | 200（非 dev 后端 404） | 200（非 dev 后端 404） | 409 `not_recording` | 409 `not_recording` |
| `summarize` | 409 `not_recording` | 200（转写没在跑 503 `transcript_unavailable`、端点没配齐 502 `summary_failed`） | 200（同上两档 503 / 502） | 409 `not_recording` | 409 `not_recording` |
| `workspace_open` | 200（写注册表，不碰快照；缺 `dir` / 目录不存在 / 非空未确认 → 400 `invalid_setting`） | 200 | 200 | 200 | 200 |
| `workspace_remove` | 200（摘一行名单；文件不碰） | 200 | 200 | 200 | 200 |
| `post_offline` | 带 `dir` 且那一行合法 → 200；不带 → 409 `draining`（手上没有可以跑后会的场次） | 409 `draining` | 409 `draining` | 200（`post.hook` 缺席 → 404 `action_unavailable`；有一趟在飞 → 409 `draining`） | 收尾落定（四步都到终态，`post` 停 `waiting` 算）→ 200；还没落定 → 409 `draining` |
| `post_minutes` | 同 `post_offline` | 409 `draining` | 409 `draining` | 200（`minutes.hook` 缺席 → 404；纪要层的账停在 `running` → 409 `draining`，claim 同步判） | 同 `post_offline` |

`settings_update` 写的是配置文件，与「这一场录到哪儿」无关，因此**任何状态下都可用**。`mute_session` 只在本场收尾之前可用 —— 收尾完成后本场已无可静音，复位发生在下一场 `start`。`reprobe` 只在**没有场次在跑**的时候可用（`recording` / `alert` 两态回 `already_recording`）：设备在开场前定下来，会中换设备是明文不做的那一档。`summarize` 只在**有场次在跑**的时候可用（它读的是这一场的实时转写），两档失败 503 / 502 的判据见 4.7。会后那两个按钮反过来，只在**这一场收完了**的时候可用：判据不是 `ended.phase` 而是「收尾四步是否都落定」（`post` 停 `waiting` 算落定），因为收尾里有失败步时 `phase` 停在 `draining`、而那一场的会后照样该由人按 —— 拒绝档见 4.3 末尾那张三档清单。

### 4.6 `settings_update`：38 键清单与两档

请求的 `args` 就是**「配置点号键名 → 值」的扁平映射**，只有这一层、不按键前缀分组：

```json
{"action": "settings_update", "args": {"asr.timeout_seconds": 30}}
```

**成功体与别处不同**（**没有 `message` 字段**，需要给人看的话由界面按两个数组自己组织）：

```json
{"ok": true, "revision": 3, "applied": ["asr.timeout_seconds"], "pending_restart": []}
```

**合法键名是逐条列出的 38 个**（不按前缀归组、没有前缀白名单）。表外的任何键名一律 `invalid_setting`(400) 且 `detail = {key, value, legal}`。

| 归类 | 键（个数） | 为什么 |
|---|---|---|
| `applied` | `alarm.sound` / `asr.max_segment_seconds` / `asr.timeout_seconds` / `asr.max_new_tokens` / `asr.repeat_max_unit` / `asr.repeat_min_coverage` / `devices.mic` / `devices.loopback` / `heartbeat.interval_seconds` / `summary.default_window`（**10 个**） | 这十个是**热读键**，写盘成功就立刻生效。`alarm.sound` 收在这一档，于是勾选出声开关**当场**改运行期行为；设置抽屉那四项（`311f84f5`）收在这一档，改完无需重启 |
| `pending_restart` | `web.port` / `web.dist_dir` / `log.level` / `log.dir` / `log.max_bytes` / `log.backup_count` / `alert.silence_dbfs` / `alert.silence_seconds` / `alert.recover_seconds` / `alert.alarm_suppress_seconds` / `capture.backend` / `transcriber.backend` / `post.hook` / `post.engine` / `post.model` / `post.dtype` / `post.chunk_seconds` / `minutes.hook` / `minutes.chunk_lines` / `minutes.max_segments` / `minutes.timeout_seconds` / `minutes.dsh_url` / `summary.base_url` / `summary.api_key` / `summary.model` / `summary.extra_body_json` / `summary.deadline_seconds` / `summary.max_attempts`（**28 个**） | 这些值在**启动时读一次**，写盘成功但要重启才生效 |

热读键那十键里，`alarm.sound` 与 `asr.*` 五个是原来那一档；设置抽屉节点（`311f84f5`）把四项收了进来：两个设备键位写盘成功后内存生效值当场换掉，紧接着的 `reprobe` 就按**新设备值**挑（会中换设备仍然不做，`reprobe` 在 `recording` / `alert` 态照旧回 `already_recording`）；`heartbeat.interval_seconds` 与 `summary.default_window` 的消费方（前端轮询、每次 `summarize` 取缺省档）都不持有运行期旧值，写盘即生效。其余 **28 个**全归 `pending_restart` —— 两档合起来正好是 `meeting/config.py` 的 `DEFAULT_VALUES` 键全集（**38 个**），判定由 `SETTING_KEY_GROUPS` 一处给出（守卫用例 `tests/test_api_actions.py::test_settings_key_groups_cover_the_whole_table` 逐条钉住 `10 + 28 = 38`）。

**会中总结那七个 `summary.*` 键**（`1987cb20` phase1 加进来的）：缺省档 `summary.default_window` 收在热读档（`311f84f5`），其余六键归 `pending_restart`：`summary.base_url` / `summary.model` / `summary.extra_body_json` 收字符串，`summary.api_key` 是**可空键**（`null` = 没配，与两个设备键位同形），`summary.deadline_seconds` 收 `1.0`–`3600.0` 秒、`summary.max_attempts` 收 `1`–`10`。`summary.default_window` 只认 `5m` / `15m` / `30m` / `full` 四档，写盘那一刻快照 `settings.summary_default_window` 就换过去。键名与取值范围的判据在 `meeting/config.py`，用法见第 4.7 节。

两个数组里也是同一套点号键名，次序**照本次请求里的键逐条走**。

- **两个数组的判据只有一条**：该键在键态表里归哪一档。**「写盘成功」是前提** —— 写盘失败整笔回 `persist_failed`(500)，一处都不生效。
- **`alarm.sound` 与 `asr.*` 一样当场生效**：写盘成功那一刻主 log 落一行「alarm.sound 收在热读档，本刻起出声开关 = …」，服务侧同时重取一次生效值视图并提交，于是紧接着的 `GET /api/state` 里 `settings.alarm_sound` 就是新值，界面当场按新值决定响不响（设计稿 `docs/ui-design.md` 4.5 要的「勾选立即生效」在这里成立）。设置抽屉那四项同路：写盘成功即重取生效值视图并提交，`settings.devices.*` / `heartbeat_interval_seconds` / `summary_default_window` 三格当场换新值。
- **只写用户配置文件里已有的键 + 本次改动的键**，不把代码默认值落盘 —— 一次 `settings_update` 不会凭空长出一份写着全部默认值的配置文件。
- **校验在写盘之前**：至少落地方案点名的「超时必须大于段长」（`asr.timeout_seconds > asr.max_segment_seconds`，没改到的那一半取生效配置的当前值），违规回 `invalid_setting`(400) 且 `detail = {key, value, legal}`。
- **被环境变量压着的键不改分类**：`asr.*` 照样进 `applied`；「被环境变量压着」这件事只在主 log 留一条 warning（键名 + 写盘的值 + 压住它的环境变量名），运行期仍由环境变量说话。
- **配置文件本身与请求同位**：`~/.config/meeting/config.json` 里也只有一层点号扁平键名（例如 `{"alert.silence_seconds": 3}`）。**嵌套形状不合法**。
- **`pending_restart` 的键在主 log 里点名**：本次改动里有这一档的键时，落一行「… 归 pending_restart，仍取启动时的生效值（改了要重启才生效）」并列出这些键名；热读键不进那一行（它们已经在上面生效了）。
- **热读键生效之后 `revision` 会 `+1`**（服务侧重取一次生效值视图并提交）；全是 `pending_restart` 的改动**只写盘、不提交**，因此 `revision` 不动 —— 界面别拿 `revision` 当「设置改没改成功」的判据，看 `ok` 与两个数组。

**这一档的 fixtures**：`contract/fixtures/actions/{start-ok,stop-ok,mute-session-ok,settings-update-ok,summarize-ok}.json` 与**十个** `fail-*.json`（九个 `code` 各一 + 未知动作名那一档）。`summary_failed` 在**盘上只有缺键那一份**样本（`fail-summary-failed-missing.json`），它另一档 `{elapsed_seconds, attempts}` 需要「一个每次都慢过预算的假端点」，只由 `tests/test_summary_action.py` 逐档实测，没有采成样本（逐档 `detail` 见表）。

### 4.7 会中总结：`summarize` 与 `GET /api/summaries`

会中有人跟丢了，按一下：把**最近这段时间的实时转写**读一遍，给回三小段。范围由人挑，四档窗口。

**请求**：

```json
{"action": "summarize", "args": {"window": "15m"}}
```

- `window` 取 `5m` / `15m` / `30m` / `full` 四档之一；**省略时取配置的 `summary.default_window`**（默认 `15m`）。
  给了取值域外的值**不静默落缺省档**，当场回 `invalid_setting`(400)。

**成功体**，比通用信封多一个 `summary` 键：

```json
{"ok": true, "revision": 12,
 "summary": {"window": "15m", "window_label": "15 分钟", "at": "<iso>",
             "elapsed_seconds": 9.4, "lines": 37,
             "what": ["..."], "where": ["..."], "lost": ["..."]}}
```

- `revision` 是**当前**版本号。**总结是只读的**：不写快照、不动 `revision`、不广播 —— 拿它前后各取一次快照，`revision` 与顶层九键逐字相同。结果走这个应答体，记录走下面的 `GET`。
- `window_label` 是人话标签（`5 分钟` / `15 分钟` / `30 分钟` / `整场`），`lines` 是**窗口里的行数**。
- 三小段是**字符串数组**：`what`（刚才在讲什么）/ `where`（讲到哪儿了）/ `lost`（丢掉了什么）。
- **窗口里一行都没有时照旧 `ok: true`**，三段都是空数组，`lines` 为 0 —— 这种情形**一个模型请求都不发**。空态文案由界面给。

**失败**（走第 4.2 节的失败信封，`detail` 恒是对象）：

| 情形 | `code` | HTTP | `detail` |
|---|---|---|---|
| 没有在录的场次（`idle` / `ended`） | `not_recording` | 409 | `{}` |
| `window` 取值非法 | `invalid_setting` | 400 | `{key: "window", value: "…", legal: ["5m","15m","30m","full"]}` |
| 转写停了 / 没起过 | `transcript_unavailable` | 503 | `{"health": "stopped"}` |
| 模型端点没配 | `summary_failed` | 502 | `{"missing": ["summary.base_url", …]}` |
| 预算用尽 / 端点报错 / 解析连试带败 | `summary_failed` | 502 | `{"elapsed_seconds": 25.0, "attempts": 3}` |

**判定的先后是固定的**（照 `start` 的先例）：判态 → 校验 `window` → 看转写健康 → 看模型端点配置 → 才动手。**前面几档没过，一个请求都不发出去、一条记录都不写**（判据是记账的假客户端读数为 0）。

`summary_failed` 的 `detail` 由会中总结内核（`meeting/summary/runner.py` 的 `SummaryOutcome.detail`）直接给出：配置缺键是 `{missing}`，其余是 `{elapsed_seconds, attempts}` —— 如实报「等了多久、试了几次」。那道墙钟预算由 `summary.deadline_seconds`（默认 25 秒）与 `summary.max_attempts`（默认 3）管，判据写在 `meeting/summary/` 的模块说明里。

**本场总结记录**：`GET /api/summaries`，**任何状态下都 200**：

```json
{"session_id": "<场次目录名或 null>", "entries": [
  {"window": "15m", "window_label": "15 分钟", "at": "<iso>", "elapsed_seconds": 9.4,
   "what": ["..."], "where": ["..."], "lost": ["..."]}]}
```

- 取的是**本场按过的、成功产出的**那几次，按时间先后排。**失败的那几次不记**（没有「那几条」可记）。
- 记录**活在服务侧的内存里、会话级、不落文件**：新的一场 `start` 把它清空，服务关停一并清掉。
- 没有场次或一次都没按过时 `session_id` 可以为 `null`、`entries` 是空数组。
- 读它**不涨 `revision`**、不广播。

**并发**：服务侧**不做闸**。同一次会里两个页面同时按下就是两次独立的总结，各自记一条；界面自己按「进行中」禁按钮。

**这一档的 fixtures**：`contract/fixtures/actions/summarize-ok.json`、`actions/fail-transcript-unavailable.json`、`actions/fail-summary-failed-missing.json` 与 `contract/fixtures/summaries/after-one.json`。

### 4.8 工作台收编：`workspace_open` / `workspace_remove` 与两只读端点

**`workspace_open`**（`POST /api/actions`，成功体 = 通用信封 + `workspace` 键）：

```json
{"action": "workspace_open", "args": {"dir": "C:\\会议\\跟老王聊周会", "confirm": false}}
```

成功体：

```json
{"ok": true, "revision": 1,
 "workspace": {"dir": "<path>", "kind": "meeting", "suspected": false, "first_time": true}}
```

- 先判名目（`meeting/workspace.py` 的 `classify_workspace_dir()`），**三档三条路**（R20）：
  - `kind: "empty"` —— 空目录当一场新会，直接登记；
  - `kind: "meeting"` —— 有会议材料（`session.json` 或任一 `meeting_audio_*.pcm`：名目判据是 `meeting_audio_` 前缀 + `.pcm` 后缀，段文件的命名 `meeting_audio_<source>_<NNN>.pcm` 也落在里面）就收编；**`suspected: true` = 有音频归档名但没有 `session.json` 的疑似会议目录**（兜底档：照收编处理，不当杂物目录）；
  - `kind: "unknown"` —— 非空、没有会议材料：`confirm` 缺失时回 **400 `invalid_setting`**（`detail = {key: "confirm", value: null, legal: [...]}`；**目录与盘面一个字节没动**），带 `confirm: true` 重发才登记。
- `dir` 给到不存在的路径回 **400 `invalid_setting`**（已存在是硬前提，本服务不建目录）；注册表写盘失败回 **500 `persist_failed`**。
- 去重判据是**规范化后的绝对路径**：同一个目录选两次（拖尾分隔符之类的写法差也算同一处）只占一行；已登记过的路径只更新这一行的 `last_opened_at`（`first_time: false`）。**任何状态下都可用**；它不碰快照、`revision` 不因登记涨。

**`workspace_remove`**：成功体 `{ok, revision, workspace: {dir, removed}}`。**只把这一条从注册表摘掉**，工作区目录与里面所有文件一个字节不动（R21）；之后随时可用 `workspace_open` 把它收回来。没登记过这条也照样 200（`removed: false`）。重复删除安全。

**`GET /api/sessions`**（**任何状态下都 200**；纯读、不涨 `revision`、不广播）：

```json
{"sessions": [{"dir": "<path>", "session_id": "<session-id>|null", "dir_exists": true,
               "kind": "empty|meeting|unknown|missing", "suspected": false,
               "state": "recording|draining|done|failed|null", "date": "<YYYY-MM-DD>|null",
               "mode": "both|mic|loopback|null", "has_offline": false,
               "added_at": "<iso>", "last_opened_at": "<iso>"}],
 "recent": "<path>|null",
 "post_job": {"kind": "offline|minutes", "dir": "<path>", "started_at": "<iso>"}|null}
```

- 清单是**软件自己记的那一份**（住在服务侧配置目录 `workspaces.json`，按路径去重）；**列表从登记来** —— 本端点不列任何没登记过的目录，`start` 与 `workspace_open` 是仅有的两个登记入口。
- 每行的"目录还在不在、里面是什么"由服务**现场判**（注册表条目只记两个时刻）：`dir_exists: false` 的那一行如实等着界面写「路径已不在」；`state` 只在盘上 `session.json` 读得出来时带值、四档之外如实 `null`（读盘那份**只判集合成员关系**，不反查映射、不读成 `top_state`）。
- **`has_offline` = 这一场有没有会后精细稿**（R29）：判据是场次目录里在不在 `meeting_offline_transcript_<source>.md`（只判名目、不读内容、不猜）。「出纪要」那个按钮上那一格按它渲染 —— 界面因此一眼看得出 AI 手上有没有那份更细的稿。**这一格走本端点，不进快照**（推送出去的东西只认那九个顶层键）。
- `recent` = **最近一次被打开/切换**的那一条的目录（`workspace_open` 与开成一场的 `start` 都刷新这一格）；**只从注册表读，服务与界面都不扫任何场次目录**（`docs/design-intent.md` 第 4 条的退场判据仍在）。
- **顶层第三格 `post_job`**（会后进度界面节点 `52b24392`）：服务进程里此刻在飞的那一趟会后 —— `{kind, dir, started_at}` 三格（`kind` 答**这一趟跑的是哪条路**：`offline` 精细稿 / `minutes` 纪要），没有在飞的就是 `null`。它是界面那格全局「显卡忙不忙」的**会后一半读数**（另一半「本机正在录」从快照状态读，不走这里）；读数就是 `runtime.post_job_state()` 的原文，**跑完那一刻线程收工、这一格翻回 `null`**。`meeting post` 命令行那趟活在另一进程，这一格看不见 —— 如实显示，不编。**这一格在应答体顶层、不进任何一行**：每行照旧恰好十一键，快照九键一个字没动。

**`GET /api/dirs`**（目录选择器的一个现场；**任何状态下都 200**；纯读）：

```json
{"home": "<path>", "cwd": "<path>", "up": "<path>|null", "dirs": [
  {"name": "空会", "dir": "<path>", "kind": "empty", "suspected": false}]}
```

`?dir=` 省略时从用户主目录起步（设计第 4 条：选择器起点 `~/`，落点由用户挑）；`up` 到主目录时为 `null`（不再往上给）。每项只判名目（`classify_workspace_dir()`），不读内容、不写任何东西；`?dir=` 给到不存在的目录回 400 `invalid_setting`。

**这一档的 fixtures**：`contract/fixtures/sessions/empty.json`、`sessions/after-open.json`、`actions/workspace-open-ok.json`、`actions/workspace-remove-ok.json`。

### 4.9 单场状态：`GET /api/sessions/{id}`

**任何状态下都 200**；纯读：不提交、不涨 `revision`、不广播（与 4.7 / 4.8 同一档）。

**`{id}` 是这一场的目录路径**（URL 编码的绝对路径）。目录名就是这一场会的名字（`docs/design-intent.md` 第 3 条），`GET /api/sessions` 每一行也以 `dir` 作主键；路由用的是**收得下斜杠**的那一档路径参数（`/api/sessions/{dir:path}`），因此 Windows 上的 `C:\会议\250906` 编码之后也能原样指到这一场。

```json
{"dir": "<path>", "session_id": "<session-id>|null", "kind": "empty|meeting|unknown",
 "state": "recording|draining|done|failed|null", "date": "<YYYY-MM-DD>|null",
 "mode": "both|mic|loopback|null", "has_offline": true,
 "post": {"running": false, "kind": "offline|minutes", "started_at": "<iso>",
          "finished_at": "<iso>|null"},
 "offline": {"ledger": "meeting_offline_progress.jsonl",
             "state": "running|done|partial|failed|idle|ledger", "in_process": false,
             "started_at": "<iso>|null", "finished_at": "<iso>|null",
             "elapsed_seconds": 12.5,
             "current_source": "mic|loopback|null",
             "sources": [{"source": "mic", "done": 1, "total": 3, "remaining": 2,
                          "current_chunk": 2,
                          "counts": {"transcribed": 1, "silent": 0, "failed": 0,
                                     "unjudged": 0}}]}}
```

**前七格与 `GET /api/sessions` 的同一行同源**（同一套注册表 / 目录判读 / 盘上 `session.json` 的读法），逐格语义见 4.8 —— 那七格是「这一场是哪一场」，这一只端点要回答的是后两格「这一场此刻跑到哪儿了」。

- **`post`** = 会后那两趟活（4.3 的 `post_offline` / `post_minutes`）此刻在不在飞、跑的是哪一趟、什么时候起的；**这三格说的是服务此刻手上这一趟**（`running` 翻 `false`、`finished_at` 补上，是这一趟自己的收工读数）。**跨场次跑过之后**，同一台服务再对任何一场起一趟会后活，上一场这三格就交回 `null`（单槽记录，后一趟把前一趟整个换掉）——上一场跑成什么样，改由那一场的 **`offline`** 那份账回答：`state` / `started_at` / `finished_at` 都还在，跨场次、跨重启都读得到（见本节 `offline` 那一条）。这一场一次都没跑过后会时它是 `null`。
- **`offline`** = **会后离线的逐块进度读数**（节点 `a9affaa8`）。`sources` **按路各一份**，分母是**这一路**切了几块（`post.chunk_seconds` 切出来的块数）—— 两路各算各的，合成一个整场分母就是在造一个不存在的量（`docs/design-intent.md` 第 9 条）。
  - `done` = 这一路跑过几块，`total` = 这一路共几块（切块计划定下来的，**不因某一块失败或跳过而增减**），`remaining` = 还差几块，`current_chunk` = 此刻在跑第几块（这一路跑完、或还没轮到它时是 `null`）。
  - `counts` 四档：`transcribed`（跑成了）/ `silent`（数字静音、跳过引擎）/ `failed`（这一块没成）/ `unjudged`（跑成了，但门没在这一块上做成判断）。**四档都算"跑过了一块"**。
  - `state` 与 `in_process` 回答的是**两件事**，判据分开写死：
    - **`state`**（`running` / `done` / `partial` / `failed` / `idle` / `ledger`）照**盘上那份**
      如实答 —— 状态文件落 `done` 答 `done`、落 `partial` 答 `partial`、落 `failed` 答 `failed`，
      状态文件写着别的（`running`）就答 `running`；这一趟不在本进程里、盘上也没有状态文件时答
      `ledger`（那份账在，成没成没人回答），账与状态文件都不在时答 `idle`。**盘上那几档都不折成
      `done`**，它也不替盘上的话做二次解读：进程崩在半路的那一场（进程没了、状态文件停在上一次
      落的那版 `running`）如实答 `running`。
    - **`in_process`**（布尔）答「**本进程里此刻有没有这一趟离线活**」：判据是会后那条
      线程手里的现场（这一趟的路是 `offline`、跑的是这一场、**那条线程还活着**，与
      `post.running` 同源）。**跑完那一刻它翻回 `false`**，此后读的是盘上那份账。它答
      `false` 而 `state` 答 `running`，读起来就是「盘上停着一版在跑，其实谁都没在跑」——
      这一档**不另立终态**（「被打断」是另一个节点的账），界面要写什么由它自己定。
  - `ledger` 是这一场那份逐块账的文件名（`meeting_offline_progress.jsonl`，见 4.9 末段）。
- **读数来自哪里**：这一趟正在本进程里跑 → 用会后那条线程手里的现场（「已经跑了多久」按单调钟当场算）；本进程没在跑这一趟（服务重启过、命令行跑的那一趟、跑完很久了）→ 读场次目录里那份逐块账。跨进程算不出单调钟的差值，**`elapsed_seconds` 那一格在这种情况下如实给 `null`**。
- **它是给人看的读数，不带校验性质的含义**：块数、已跑时长这一类错了就错了，任何代码不许拿它们对账、报错、改结论（`docs/design-intent.md` 第 6、7 条；唯一权威是实际录到的音频时长）。这一场跑成什么样**仍由 `meeting_offline_status.json` 与那四份产物回答**，这张应答不替它们下结论。
- **它不进快照**：推送出去的东西只认第 2 章那九个顶层键，`GET /api/sessions` 每一行照旧十一键（快照九键与列表十一键这两处一个字都没动；这只端点与那张列表各自成体，列表唯一的加法是 4.8 写的顶层第三格 `post_job`，不在任何一行里）。

**逐块账**：会后离线每跑完一块，就往场次目录里那份 `meeting_offline_progress.jsonl` 上补一行 —— **这一块的文字 + 这一块的落点**（JSON Lines：第一行表头 `{kind: "ledger", schema_version, session_id, sources}`，之后一行一块，带 `source` / `chunk` / `chunks_total` / `span` / `state` / `text` / `segments` / `wall_seconds` / `error`）。**引擎加载失手那一档也留一行**（`state = failed`、`error` 里带 `engine_load_error`）：不然这一路在盘上只剩表头，进程不在场的读方从上面 `sources[].total` 上看到的是 `null`，而现在读到的仍是这一路切出来的块数。**正式稿 `meeting_offline_transcript_<source>.md` 的写时机不变**（整路跑完一次写），那份账是另一份文件、另一个名字，`has_offline` 那一格不受它影响。这份账不参与任何判定（`meeting/cli.py` 的幂等闸读的仍是状态文件），字段契约的正文在 `meeting/offline/progress.py` 的模块说明里。

**这一档的失败体**（回的是通用失败信封 `{ok, code, message, detail}`，HTTP **404**）：

| `code` | 什么时候 | `detail` |
|---|---|---|
| `session_dir_missing` | 这一场的目录已不在原地 | `{dir, kind: "missing"}` |
| `invalid_setting` | 目录在，但这一场**没在工作台清单里**（判据与 4.8 同一套：规范化后的绝对路径比对） | `{dir, kind}` |

**这一档的 fixtures**：`contract/fixtures/sessions/status-after-run.json`。

### 4.10 设备清单：`GET /api/devices`

**任何状态下都 200**；纯读：不提交、不涨 `revision`、不广播（与 4.7 / 4.8 / 4.9 同一档）。
设置抽屉的两个设备下拉读它（`311f84f5`）。**只读、纯枚举、不开任何设备**：
枚举走 `meeting/capture/sources.py` 的 `list_input_endpoints()` / `list_render_endpoints()`
（只读端点表），`probe()` 那条会短暂试开的路不在它身上 —— 「本节点不真开设备」这条
在清单端点上同样成立。

```json
{"mic": [{"name": "Analogue 1 + 2 (2- Focusrite US", "host_api": "MME",
          "role": "input", "index": 1, "channels": 2, "is_default": true}],
 "loopback": [{"name": "CABLE Input (VB-Audio Virtual Cable)", "host_api": "",
               "role": "render", "index": 0, "channels": 0, "is_default": false}]}
```

- 每项**六格**照 `DeviceEndpoint` 原样给：`name`（端点名，`settings_update` 里配的就是它）/
  `host_api`（render 端点没有 host API，恒空串）/ `role`（`input` / `render`）/
  `index`（`sounddevice` 的设备序号；render 端点是枚举序）/ `channels` / `is_default`
  （只有 mic 侧会标，种子是只读的 `sd.default.device[0]`）。
- **两路的键就是两路的 id**（`mic` / `loopback`）；同名端点在多个 host API 下会重复出现，
  界面按 `name` 去重摆（配置按名字挑）。
- 「按默认策略挑」永远可选：`settings_update` 把设备键位写成 `null` 就是这一档，
  本端点不必（也不该）替界面摆这个选项。
- 端点表读不出来时回 **503 `capture_unavailable`**（复用冻结 `code`，`detail = {error}`）。
- **会中打开抽屉时清单照给**（枚举端点表不碰任何在飞的东西）；换不换得了由界面按第 7 章
  在 `recording` / `alert` 态禁用设备格 —— 服务侧的 `reprobe` 在这两态本来就回
  `already_recording`。

**这一档的 fixtures**：`contract/fixtures/devices/all.json`（设备枚举在采集里走假件，
名字与 `is_default` 是确定值）。


## 5. 心跳

`GET /api/heartbeat`，**任何状态下都 200**（包括没有正在录的场次、包括收尾进行中）。应答**恰好六字段**：

| 字段 | 类型 | 语义 |
|---|---|---|
| `service` | string | **恒 `"meeting"`**。端口探测靠这个字面量认亲（`probe_meeting_service()` 的判据就是「200 + body 含 `"service": "meeting"`」），因此**必须存在** |
| `server_time` | string | 服务侧此刻的 ISO 时间戳（本地时区、秒精度；**掩码 `<iso>`**）。迟到/失联判定以它为准 |
| `revision` | int | 当前快照的 `revision` |
| `top_state` | string | 四态之一 |
| `recording_alive` | bool \| null | **采集还活着吗**（判据见下） |
| `seconds_since_last_tick` | float \| null | 距最近一帧事实的秒数（**掩码 0**）；**一帧都没收到过就是 `null`** |

**`recording_alive` 的语义**：判据是**服务侧的采集帧时钟**。

- `recording` / `alert` 态给 **bool**；`idle` / `ended` 态给 **`null`**（没有在录的场次，问「采集还活着吗」没有意义）。
- 判据：窗口 = `max(2.0s, 3 × frame_interval)`，**只认 `LevelFrame`**（`SourceFacts` / `SourceLost` **不刷新这个钟**）。协议默认 `frame_interval = 0.5s` 与 dev 替身的 `0.1s` 算出来都是 **2.0 秒** —— 说白了就是「**两秒没收到一帧事实 = 采集停了**」。
- **后端卡住不报任何东西时 `recording_alive` 会算成 `false`，而 HTTP 照常 200** —— 这正是「只看 HTTP 通不通会漏掉录音线程已卡死」的判据。
- 采样后端 `frame_interval` 读不到或取值不是正数时按 `0.5` 算，只记一条 warning。

**`seconds_since_last_tick` 与 `recording_alive` 不同源**：前者是「多久没收到帧」这个**原始读数**（`round(…, 3)`），后者是**布尔结论**。界面显示「多久没读到数据」用前者，画「采集停了」的红灯用后者。

**两个「心跳」绝不串线**（这是最容易踩的一处）：

| 名字 | 是谁的 | 管什么 |
|---|---|---|
| `heartbeat.interval_seconds` / `settings.heartbeat_interval_seconds` | **界面**发心跳的周期 | 界面多久问一次服务（默认 10 秒） |
| `max(2.0s, 3 × frame_interval)` = **2.0 秒** | **服务侧**的采集帧时钟窗口 | `recording_alive` 的判据 |

**界面的 10 秒周期绝不参与 `recording_alive` 的计算。** 反过来，`recording_alive == false` 也不会让心跳请求失败 —— 它只是一份数据。

### 客户端轮询口径（从 `docs/ui-design.md` 4.5 搬来的原口径）

心跳由**界面**按固定频率（**默认 10 秒，可配 1–60 秒**）向服务要一次应答。应答必须**带服务侧时间戳与录音存活字段** —— 只看「HTTP 通不通」会漏掉「服务还活着、录音线程已卡死」这一类。

| 名字 | 取值 | 判据 |
|---|---|---|
| `心跳状态` | `正常` / `迟到` / `失联` / `已恢复` | 按应答里的**服务侧时间戳**（也就是 `server_time`）与本地时钟的时间差算 |

- `正常`：时间差 < 1 个周期。不显示任何东西。
- `迟到`：时间差 ≥ 1 个周期且 < 2 个周期。弱提示，一行字：`心跳迟到 · 8 秒没收到应答`。**不出声、无横幅。**
- `失联`：时间差 ≥ 2 个周期。**全局横幅 + 出声**（若开关打开）。
- `已恢复`：横幅自动消失，状态区旁留一条短记录 30 秒：`已恢复，中断 42 秒`。

**心跳优先级**：失联横幅**盖在任何状态之上**，包括空闲与已结束。

**失联时界面必须做的三件事**（这是口径，照做）：两路电平**冻结 + 压灰**、健康位写 `数据已停`、读数退成 `—`（估出来的那个数字不再顶着）；包络时间戳停在服务最后一次应答的时刻；时长标签改 `最后读数`。**不把估出来的数据继续画成在动** —— 录音已经死掉时界面还在忽悠人，是本项目最不能犯的错。

**两条「出事了」的话不得同屏**：失联横幅说的是「界面看不见服务、整场录音已经中断」，致命级告警横幅说的是「服务还在、某一路没了」。心跳失联下**整块告警区（含致命横幅与它上面的按钮）不出现**，屏幕上关于「出事了」的话只有全屏横幅这一处。

**服务重启后的自洽**：服务重启后界面重新打开时向服务取状态；服务侧**没有**正在进行的会话，界面上工作台首页列出注册表里的那些会并选中 `recent` 那一场（最近一场只从注册表读，`docs/design-intent.md` 第 4 条；未注册的目录一概不上列表）。**界面不自己记状态、不从浏览器本地存储里恢复任何东西**（第 6 章）。

**这一档的 fixtures**：`contract/fixtures/heartbeat/idle.json`（两字段 `null`）、`contract/fixtures/heartbeat/recording.json`（`recording_alive` 为 `true`、`seconds_since_last_tick` 掩码 0）。

## 6. 客户端义务

下面八条是**界面必须做到的事**，每一条都由服务侧的某条硬约束逼出来。它们是契约的另一半 —— 服务履行它的那一半（推送、判定、掩码），界面履行这一半。

1. **只消费推送，不持本地权威状态。** 状态的唯一权威在服务侧（`ServiceRuntime` 手里的那一份快照）。界面手上那份是**它推过来的副本**，任何分歧以服务为准。
2. **不用浏览器本地存储（`localStorage` / `sessionStorage` / IndexedDB / Cookie）保存会中状态。** 会中状态（哪一态、时长、告警、静音、转写）一律从服务取；刷新、重开、多开看到的都是同一份。界面自己持久化的只能是**纯界面偏好**（例如心跳周期、面板展开与否这一类与服务无关的东西）。
3. **SSE 重连后以快照重同步**：服务**不重放**断开期间的事件（没有 `Last-Event-ID` 机制），重连后拿到的第一帧必然是完整 `snapshot`，界面据此**整块替换**。收到 `resync: true` 那一帧同样整块替换。
4. **动作带两个必需头**：`POST /api/actions` 必须带 `Content-Type: application/json` **且** `X-Meeting-Service: 1`，缺任一条服务回 403 且**不带失败信封**（它没走到端点）。界面的 403 处理要与第 4.2 节的失败块分开。
5. **心跳按配置周期轮询**：默认 10 秒、可配 1–60 秒（`settings.heartbeat_interval_seconds`）。判定用应答里的 `server_time` 作基准；失联按 5 章的处置做（冻结 + 压灰 + 横幅）。
6. **不自己造派生数据**：包络（`envelope`）与 `bar` 由服务算好推过来，界面**不许自己造**；告警的 `muted` 由服务判定，界面**不算任何抑制窗口**、只按布尔值决定响不响。
7. **按 `code` 分派失败画法、按 `detail` 有哪个键显示哪个键**：九个 `code` 固定、`detail` 的档位（尤其是 `capture_unavailable` 三档与 `invalid_setting` 四档）以第 4.4 节为准，**不按 `message` 做字符串匹配**。
8. **决定响不响的时候，持久化开关与本条告警的 `muted` 一起读。** 这两个字段各管一件事，缺一条读法就会读错：

   | 字段 | 谁写的 | 管什么 |
   |---|---|---|
   | 快照 `settings.alarm_sound` | 用户按的「告警出声」开关（持久化，跨场次记住） | **这一台机器要不要出声** |
   | `alerts.*.muted` | 服务侧每次提交时重算 | 这一条告警**此刻**该不该出声（会话级静音 ∨ 抑制窗口） |

   `alarm.sound` 关掉时服务推的 `muted` **仍是 `false`** —— 服务侧只管「本场静音」与「抑制窗口」这两半，持久化开关是界面这一侧的事。所以响一声的判据是**两个都为「放行」**：`settings.alarm_sound === true` **且**这一条告警的 `muted === false`。只读 `muted` 会在开关关掉之后照样响，只读 `alarm_sound` 会在抑制窗口里重复响。

## 7. 保留登记：定名但未实现

**这一章列的东西一个都没有实现。** 它们已经**定名**（名字与形状在节点里讨论过、被引用过），但服务侧眼下没有任何端点、动作或字段承载它们。列在这里的目的有两个：界面与验收脚本知道**将来会有什么、名字叫什么**；以及复核时能一眼确认**这一轮没有顺手实现**。

**处理方式**：界面与脚本按「能力还没进来」处理 —— 不要探测、不要留死代码分支、不要按这些形状写解析器。等各自归属的节点落地之后，它们**先做进代码、再回写进本文件第 1–6 章**（这一章只管「还没做的」）。

**会中总结那两样已经从这一章搬走了**（`1987cb20` phase2）：动作 `summarize` 与 `GET /api/summaries` 在代码里、正文搬进了第 4.7 节，这一章不再列它们。同一轮里**快照键 `summary` 改写成「不做」**：它不再是「将来会有的东西」，而是**明文不做的裁定**，理由与去处见下表那一行。

**单场状态那一行也搬走了**（会后进度读数节点 `a9affaa8`）：`GET /api/sessions/{id}` 已经做进代码（路由 `/api/sessions/{dir:path}`）、正文搬进了第 4.9 节，这一章的表里不再列它。这一轮同时改对了下面那句「会后产物没有 HTTP 端点」—— 会后那五份产物（四份产物 + 一份逐块账）**仍然住在文件上**，而「这一场跑到哪一档、会后离线跑到第几块」现在由这只只读端点回答。

| 定名的东西 | 形状（草案） | 归属节点 | 现状 |
|---|---|---|---|
| 快照键 `summary` | **不做**：会中总结的结果**不进快照**，结果走 `summarize` 动作的应答体、记录走 `GET /api/summaries`（正文见 4.7） | 手动总结节点（`1987cb20`，已定论） | **明确不做，也不留位**。理由有两条：结果是一过性的视图产物（设计章 2.3「关掉即消失、不落文件」），而快照是**会议的**权威状态；前端契约层的两道硬门（`web/src/contract/snapshot.ts` 的 `.strict()` 与 `contract.test.ts` 的「顶层恰好九键、不许多 `summary`」）正是照这条口径立的。`to_dict()` 不产出这个键，顶层键集合恰好是第 2 章那张表 |
| `GET /api/sessions` | **已落地**（工作台收编节点 e2f6166b）：形状与现行口径在 4.8 —— 载的是工作区注册表；会后视图的会议选择器建立在这只端点之上，不再把它列作未实现 | 本节点（会后节点沿用，不另开形状） | **路由已注册**：`GET /api/sessions` 在 `all_routes()` 里；`?dir=` 的现场形状也是 4.8 |
| 转写历史回看 | 中途订阅能不能拿到已经出过的转写行 | **待下游决断（`9e5db89c` / `070b31e0`）** | **现状是拿不到**：`transcript` 通道只发「订上之后新出的行」，连接时的首帧 `snapshot` 也不带转写正文（`transcript.last_line_at` 只是个时间戳、`buffered_lines` 是计数）。界面在会话中途打开时，**只能显示从那一刻起的新行**，拿不到已有行。会中这些行落在场次目录的 `meeting_transcript_<source>.md` 里，会后那份带说话人编号的稿也住在同一个目录，两样都没有读它们的端点 |
| 说话人名字落盘 / 跳过编号 | 用户给某个编号起名、或标记「这个编号不认领」 | **会后管线（`41465462` / `7c6952e5`）** | **本契约不覆盖**：这是会后产物那一侧的事，服务侧的会中契约里没有对应字段。会后离线这一层（`7c6952e5`）落地之后仍然没有承载它的字段：`meeting_offline_speakers_<source>.json` 里只有类编号（`mic:S01` 这种限定名）与每段的起止秒数，**名字既不推断、也没有可写的位置**，「不认领」这个标记同样无处安放 |

**会后产物不在本契约里**：会后离线转写与说话人聚类（`7c6952e5`）已经落地，产物住在**场次目录里的文件**上 —— 四份产物：`meeting_offline_transcript_<source>.md`（每路一份的会后精细稿，一行 `[HH:MM] **S01**: 正文`）、`meeting_offline_speakers_<source>.json`（类编号与区间对照表）、`meeting_offline_report.md`、`meeting_offline_status.json`；另一份是**逐块进度账** `meeting_offline_progress.jsonl`（`a9affaa8` 落地的读数文件，名字与 `meeting_offline_transcript_` 那个前缀对不上，与那四份产物并列平放）。**它们没有各自的 HTTP 端点，快照里也没有对应字段**（要看这一场跑到哪一档、会后离线跑到第几块，读 4.9 那只单场状态端点；那条路走的是那份逐块账与状态文件的起止时刻），本契约的端点口径、快照 schema 与 SSE 帧形状都管不到；要读产物本身，就按场次目录里的文件读，四份产物的字段契约正文在 `meeting/offline/artifacts.py` 的模块说明里、逐块账的在 `meeting/offline/progress.py` 的模块说明里。**两路各落一份、两路不合并**（文件名与限定名 `mic:S01` 里始终带着 `source`）。这一场跑成什么样由 `meeting_offline_status.json` 回答：收尾第 4 步的 `post` 不再拉起会后钩子，做完释放显存就如实停在 `waiting`（＝等你动手）；会后离线由「生成会后精细稿」按钮或 `meeting post` 拉起，跑不跑得成、缺没缺一段写在状态文件与运行报告里。

**「暂停」的动作面已经进了契约**（`10770b41`）。R24 / R25（`docs/design-intent.md` 第 8、9 条）要的暂停与「段只表达录了几次」由 `6ea32d8d` 把机制做进引擎：暂停期间写数字静音、**不录内容**、不切段（暂停只进 `session.json` 段账的 `pauses` 区间），会中关掉某一路写静音、**不报 `SourceLost`**。本节点把动作面接上：`pause` / `resume` / `set_source` 三个动作（正文见 4.3 / 4.4 / 4.5）、快照 `session` 的第七子键 `paused`（第 2 章）、fixtures 已随之重生（第 8 章），会中界面那两个入口见 `docs/ui-design.md`。**会中的音频源切换照旧不做**：要录哪几路在开始前定（`mode`），录音期间不切 —— 会中能做的只有「关掉本场在采的那一路」与「把它重新打开」，`set_source` 对不在 `mode` 里的那一路回 `invalid_setting`(400)。

## 8. fixtures 与掩码（附录）

契约的**可机读基准**是 `contract/fixtures/` 下的 34 个 golden 样本。它们一律**出自真实服务路径**，不是手写的。规则、出处与再生成办法写在 `contract/README.md`；这里给读者一份速查。

| 目录 | 样本 | 对应本文件 |
|---|---|---|
| `snapshot/` | `idle` / `recording` / `alert` / `ended-done` / `ended-draining`（**5**） | 第 2 章 |
| `sse/` | `first-snapshot` / `state` / `levels` / `transcript` / `resync-snapshot`（**5**） | 第 3 章 |
| `actions/` | 成功七档 `start-ok` / `stop-ok` / `mute-session-ok` / `settings-update-ok` / `summarize-ok` / `workspace-open-ok` / `workspace-remove-ok`；失败十档 = 九个 `code` 各一 + 未知动作名那一档，其中 `summary_failed` 这份是**缺键**那一档（预算用尽那一档要一个真等满预算的假端点，由 `tests/test_summary_action.py` 实测）（**17**） | 第 4 章 |
| `summaries/` | `after-one`（按过一次之后的本场总结记录，**1**） | 第 4.7 节 |
| `sessions/` | `empty`（空注册表） / `after-open`（登记三档目录之后的清单 + `recent`） / `status-after-run`（单场状态：两份块账 + 状态文件落好之后的那一读）（**3**） | 第 4.8 / 4.9 节 |
| `devices/` | `all`（两路各一份候选端点表；枚举走假件，名字与 `is_default` 是确定值）（**1**） | 第 4.10 节 |
| `heartbeat/` | `idle` / `recording`（**2**） | 第 5 章 |

**掩码（`tests/_contract.py` 的 `normalize()`）**：fixtures 由真实服务采集，采集里的墙钟、路径、端口、电平每次都不同，落盘前一律换成**自描述占位符**：

| 掩码的对象 | 占位符 / 处理 |
|---|---|
| ISO 时间戳（`started_at` / `since` / `lost_since` / `last_line_at` / `server_time` …） | `<iso>` |
| 路径（`log_path` / `dir` / `session_dir`，以及自由文本里内嵌的绝对路径） | `<path>` |
| **场次目录名**（`session.id` 与动作失败 `detail.session_id` 的工作区目录名，形状任意） | **`<session-id>`** |
| 端口 | `<port>` |
| 服务 URL | `<url>` |
| 电平（`level_dbfs` / `peak_dbfs` / `dbfs` / `peak` / `bar`） | `<dbfs>` |
| `envelope` 数组 | **只保结构与长度**：长度不变、逐根换 `<dbfs>`（`bar == envelope[-1]` 的结构断言仍成立） |
| `elapsed_seconds` 与 `duration_seconds` / `seconds_since_last_tick` / `wall_seconds`（走秒的量，含会中总结那次实测耗时与会后逐块的墙钟） | `0` |
| 自由文本里内嵌的 Windows 绝对路径 | 就地替换成 `<path>`（见下例） |
| **`revision`**（含 SSE 帧级的 `id:`） | **保留真实值** —— 它由固定的迁移与提交次数决定，同样确定 |

场次目录名与路径**分成两个占位符**：`session.id` 在真服务里是**裸目录名**（`260921`），早先它与 `dir` 同掩成 `<path>`，于是「掩码通过」冒充了「真值通过」（前端 schema 曾按路径形状收这一格，线上 `recording` / `alert` / `ended` 三态整份快照解析失败）。占位符现在各说各的形状：读 fixtures 的人从 `<session-id>` 就知道这一格该长什么样，前端契约层也按这个形状收（`sessionIdSchema`）。

有两处**逐字保留**，别当成漏掩码：`sources[*].id`（`mic` / `loopback`）与 `sources[*].label`（`本地` / `远端`）是取值域字面量，掩掉就看不出形状了；`alert.message` / `DrainStep.detail` / 失败 `message` 是给人看的整句，只掩其中的路径，句式原样留着。

**掩码在自由文本里的样子**（真实样本 `actions/fail-draining.json` 的 `error`）：

```
会后流程启动失败：RuntimeError: 假会后钩子按要求抛错；可复制命令：meeting post <path>；log 路径：<path>
```

**占位符是自描述的**：带尖括号、看得出是被人换掉的；**真值里不可能出现 `<iso>` / `<path>` 这样的串**，因此比对时不会把占位符和真值混淆。`null` 与 `false` / `true` 一律**不掩码**（它们是语义值），空数组与空对象也不掩码。

**再生成**：改了服务行为之后，跑

```powershell
python -m pytest tests/test_contract_fixtures.py --regen-contract -q
```

重写 fixtures（默认模式是逐文件比对、不一致即红）。**掩码的全部目的就是确定性**：同一份代码连跑两次 `--regen-contract` 落盘的字节必须逐字相同。

**默认模式就是漂移测试**：`python -m pytest tests/test_contract_fixtures.py -q` 会重走一遍真实服务路径采集样本，与盘上 fixtures 逐文件比对；服务侧形状一漂移（键改名、字段增删、`detail` 换档）就红，报出「fixtures 是什么、实时采集是什么」。**改了服务行为就重跑 `--regen-contract` 并复核差异** —— fixtures 的 diff 就是契约的 diff。

## 9. 复核手法

想手工确认「这份契约与代码一致」时，下面四条够用：

1. **抽一个快照 fixtures 与 `Snapshot.to_dict()` 逐字段对照**：`meeting/state.py` 的 `to_dict()` 是唯一产出形态，顶层九键、`session` 七子键、`sources` 九键、`DrainStep` 四键都能逐行对上。`ended` / `session` 两个 `null` 档都能在 `idle` 样本里看一次。
2. **故意改坏一个键名跑比对测试，确认会红**（验完还原）：例如把 `contract/fixtures/snapshot/recording.json` 里的 `"top_state"` 改成 `"topState"`，`python -m pytest tests/test_contract_fixtures.py -q` 应当报出「与实时采集不一致」。
3. **任选两条 `detail` 与 `meeting/service/runtime.py` 的实际路径对照**：`already_recording` 三键在 `_already_recording_error()`、`draining` 四键在 `_draining_error()` / `_draining_error_for_dir()`（后者从盘上那一版读 `failed_steps`）。
4. **确认保留登记章里没有任何端点被顺手实现**：`python -c "from meeting.service.routes import all_routes; print([r.path for r in all_routes()])"` 应当只打出十条路径（九条 `/api/*` + `/debug`：`/api/state` / `/api/events` / `/api/heartbeat` / `/api/actions` / `/api/summaries` / `/api/sessions` / `/api/sessions/{dir:path}` / `/api/dirs` / `/api/devices` / `/debug`）；`ACTION_NAMES` 应当有十四个名字（`start` / `stop` / `mute_session` / `settings_update` / `reprobe` / `dev_inject` / `summarize` / `workspace_open` / `workspace_remove` / `post_offline` / `post_minutes` / `pause` / `resume` / `set_source`），会中总结那一个的正文在第 4.7 节、工作台那三个在第 4.8 节、会后两个按钮在第 4.3 节、单场状态那一只在第 4.9 节、设备清单那一只在第 4.10 节、暂停与逐路开关那三个在第 4.3 节。

## 10. 相关文档

- 索引与分册清单：`docs/spec.md`
- HTTP 面速查（端点、动作表、`code` 表、端口与防线）：`docs/api.md`
- 状态与推送的现状叙述（四态与迁移、收尾四步与共享预算、`session.json`、告警判定、dev 替身）：`docs/state.md`
- 界面设计的唯一权威说明（四态、告警分级、心跳本地态、交互清单）：`docs/ui-design.md`（第 4.5 章心跳口径、第 5 章交互清单）
- 怎么起服务、配置键、log、退出码、跑测试：`docs/service.md`
- 后端协议与解析入口：`docs/backend-protocols.md`
- golden 样本的出处、掩码规则与再生成办法：`contract/README.md`
