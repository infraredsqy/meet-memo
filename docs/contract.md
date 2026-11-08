# meeting · 双端契约

> 服务与界面之间那份契约的正文：端点口径、快照 schema、SSE 帧形状、动作信封与 `code`/`detail` 表、心跳语义、客户端义务、定名未实现的保留登记。读者是接这套服务写界面（或写验收脚本）的人。
>
> **可机读基准**：每一条形状在 `contract/fixtures/` 里有 golden 样本，`tests/test_contract_fixtures/` 重走真实服务路径逐字比对，服务侧形状一漂移就红；样本逐档清单、掩码与再生成规则的**正本在 `contract/README.md`**（掩码占位符 `<iso>` / `<path>` / `<session-id>` / `<port>` / `<url>` / `<dbfs>`；`revision` 与取值域字面量逐字保留）。与叙述性文档冲突时以代码实际行为为准，权威落在本册。
>
> **编号**：第 1–8 章，第 8 章是附录；章内小节号（4.3、4.9…）是稳定编号，代码注释与各分册按它们落点。
>
> 代码落点：路由只在 `meeting/service/routes.py` 注册一次；快照唯一产出是 `meeting/state.py` 的 `Snapshot.to_dict()`；SSE 帧由 `meeting/service/hub.py` 编码；动作与 `code`/`detail` 在 `meeting/service/runtime.py`（方法体按职责拆在各 `runtime_*.py` mixin）；三道本地防线在 `meeting/service/app.py` 的 `_LocalGuardsMiddleware`。

## 1. 总则与端点

**单一契约声明**：服务对外只有**一个**契约面，就是这一份。`GET /api/state` 的 body、SSE 首帧 `snapshot`、SSE 的 `state` 帧**三者是同一个函数出的同一份东西**（`ServiceRuntime.to_dict()` → `Snapshot.to_dict()`），不存在「快照有两套形状」。

**口径一句话**：十五条 `/api/*` + `/debug` + 静态挂载。

| 路径 | 方法 | 给什么 |
|---|---|---|
| `/api/state` | GET | 完整快照（第 2 章），`200` |
| `/api/events` | GET | SSE（第 3 章），`text/event-stream` |
| `/api/heartbeat` | GET | 七字段心跳（第 5 章），**任何状态下都 200** |
| `/api/actions` | POST | 动作端点（第 4 章） |
| `/api/summaries` | GET | 本场总结记录（4.7），**任何状态下都 200** |
| `/api/sessions` | GET | 工作区注册表（4.8），**任何状态下都 200** |
| `/api/sessions/{id}` | GET | 单场状态（4.9），**任何状态下都 200**；寻址失败 404 |
| `/api/sessions/{id}/transcripts` | GET | 两份稿 × 两路的正文（4.13），**任何状态下都 200**；寻址失败 404 |
| `/api/dirs` | GET | 目录选择器的一层现场（4.8），**任何状态下都 200** |
| `/api/dirs` | POST | 新建目录（4.8）。**不走动作信封**，头哨见第 6 章第 4 条 |
| `/api/devices` | GET | 两路候选设备端点表（4.10），**任何状态下都 200** |
| `/api/sessions/{id}/materials` | GET | 会议资料的清单（4.12），**任何状态下都 200**（寻址失败 404） |
| `/api/sessions/{id}/materials` | POST | 资料上传，原样字节流式写盘（4.12）。**不走动作信封**，头哨见第 6 章第 4 条 |
| `/api/sessions/{id}/materials/{name}` | GET | 取原文件（4.12） |
| `/api/sessions/{id}/materials/{name}` | DELETE | 删除那一个文件（4.12） |
| `/debug` | GET | 骨架自检页：从**包内**读取、不依赖 `web/dist`；页面内显式标注非产品界面、不承担任何告警职责 |
| `/`（静态挂载） | GET | 三档见下；**存在性按每请求判定**，产物一建好刷新即可见、被删掉刷新回说明页 |

**`{id}` 一律是这一场的目录路径**（URL 编码的绝对路径）；路由用的是收得下斜杠的那一档（`/api/sessions/{dir:path}`），Windows 路径编码之后原样指到这一场。

**`/` 的三档**：产物目录存在且有内容 → 整棵 `/` 子树交给 `StaticFiles(html=True)`；目录不存在 → 一页说明（`GET/HEAD /` 给说明页，其余路径 404），跑 `npm run build` 后刷新即可见、**不必重启服务**；**目录在但空的 → 404**（构建产出被清掉与「还没构建」是两种现场，给同一句话会把后者说岔）。

挂载次序是硬约束：静态挂载**注册在所有 API 路由与 `/debug` 之后**，否则它会把 `/api/*` 一起吃掉；未匹配任何路由的路径由末端 404 兜底（回 `Not Found`，不是 Starlette 的 405）。**应答头**：`/api/*` 一律 `Cache-Control: no-store`（中间件无条件加，包括动作失败的应答）；**不发 `Access-Control-Allow-Origin`**（不开 CORS），前端与本服务同源。

**端口与监听**：只监听 `127.0.0.1`，不可配（没有 `web.host` 这类键）。默认端口 `8765`；`web.port = 0` 时由系统挑空闲端口，真实端口从监听 socket 的 `getsockname()` 读回，写进快照 `service.port` / `service.url` 并打进终端。端口被占拒绝启动、不静默换端口，口径见仓库根 `README.md`。

**Host 校验只比 hostname**：`Host` 头的 hostname 部分不是 `127.0.0.1` / `localhost` 就回 **403**（挡 DNS rebinding）。取 hostname：头以 `[` 开头取方括号之间那段（于是 **`Host: [::1]` 会被拒**——这是有意的，服务只绑 IPv4 回环）；否则取第一个 `:` 之前的部分（剥端口）；最后按 `lower()` 比对。

## 2. 快照 schema

顶层**恰好九键**。`GET /api/state` 的 body、SSE 首帧 `snapshot`、SSE 的 `state` 帧三处逐字相同；**重同步**那一帧额外多两键（本章末 resync 一节）。

| # | 键 | 取值 / 语义 |
|---|---|---|
| 1 | `revision` | 进程内单调递增；每次启动从同一起点重计，**不跨进程延续、不落盘**。掩码保留真实值（由固定的迁移与提交次数决定） |
| 2 | `service` | 五子键，启动时定一次（见下） |
| 3 | `top_state` | 四态之一：`idle` / `recording` / `alert` / `ended`。**不新增第五个** |
| 4 | `session` | `null`，或 `{id, dir, started_at, mode, elapsed_seconds, frozen, paused}` |
| 5 | `sources` | **恰好两项**，次序恒为 `mic`、`loopback` |
| 6 | `alerts` | `{critical: Alert｜null, hints: [Alert]}` |
| 7 | `transcript` | `{health, last_line_at, buffered_lines, failed_segments}` |
| 8 | `ended` | `null`，或 `{phase, steps: [DrainStep], error}` |
| 9 | `settings` | 七键（见下） |

**`service`** 五子键：`started_at`（ISO，掩码 `<iso>`）/ `version` / `port`（掩码 `<port>`）/ `url`（掩码 `<url>`）/ `log_path`（掩码 `<path>`）。`port` 与 `url` **只能由监听 socket 的 `getsockname()` 读回值产生**，绑定之前取快照一律抛错——不留退化的空值、不拿配置值顶替。**没有 `out_root`**：落点由工作区给，服务不设默认落点（`docs/design-intent.md` 第 4 条）。

**`session`** 七子键：`id` 是**工作区目录名**（这场会的名字，形状任意、不按日期命名，掩码 `<session-id>`）/ `dir`（掩码 `<path>`）/ `started_at`（掩码 `<iso>`）/ `mode ∈ both｜mic｜loopback`（开场前定死，录音期间不切）/ `elapsed_seconds`（掩码 0）/ `frozen`（bool）/ `paused`（bool）。

- `elapsed_seconds` 在 `recording` / `alert` 态走秒（每次取快照现算、`round(…, 3)`），进 `ended` 就冻结（`frozen: true`）。**暂停的那几段不计进去**：这一格是「这一场录到了多少」的读数，权威长度是实际录到的音频时长（不含暂停），权威数以盘上 `session.json` 的 `audio_duration_seconds` 为准。
- **`paused` 是「暂停中」的唯一落点**（`pause` / `resume` 两个动作翻它），顶层不新增第五态。暂停期间那一截写数字静音、段**不**切、`sources[*].health` 一档不动（电平帧照发，界面上两路照旧实时）。收尾一开始服务把它复位（`ended` 档恒 `false`），下一场 `start` 从 `false` 起。
- **`top_state` 停在 `ended` 而那场的 `session` 还留着**（成功档与失败档都留），直下一场 `start` 才换。

**`sources[*]`** 九键：`id`（`mic` / `loopback`，取值域字面量、不掩码）/ `label`（`本地` / `远端`）/ `device_name`（string｜null）/ `health` / `lost_since`（ISO｜null，掩码 `<iso>`）/ `restored`（bool）/ `level_dbfs` 与 `peak_dbfs`（number｜null，掩码 `<dbfs>`）/ `envelope`（number 数组，逐根掩码 `<dbfs>`，长度不变）。

- `health` **七取值**：`unprobed` / `ready` / `probe_failed`（启动探测直取，后端 `probe()` 只许报这三档）+ `capturing` / `lost` / `restored` / `stopped`（状态迁移写值）。**七档之外不新增第八个**：会中关掉某一路（`set_source` 的 `active=false`）落现成的 `stopped`，重开回 `capturing`——**干净开场**：不回 `lost`、不落「曾中断」的残留横幅、静默判定从零起算（关掉那一刻挂着的致命级已当场清空）。**这是用户按的按钮**：不报 `SourceLost`、不进告警，与 `lost` 分得清清楚楚。
- `device_name` 是**探测实际落到的那个端点名**；`settings.devices` 是用户配的**意图**（见下），配了没命中不报错、不改写快照。判断「这一场到底在录哪个设备」看 `device_name`。
- `envelope` 是最近 30 秒电平包络：**15 根柱、一根 2 秒**，柱值 = 窗口内所有 `LevelFrame.peak` 的最大值，窗口里一帧都没有就沿用左边那根。录音中（乃至 `ended` 停留档）长度恰 15，开场前是**空数组**。这根柱由服务算，**前端不许自己造**。

**`alerts`**：`critical` 是 `Alert｜null`（**致命级同时只留一条**），`hints` 数组（**同类合并、上限 3**）。`Alert` 六字段 `{level, source, since, duration_seconds, message, muted}`；`level ∈ critical` / `hint`，`source` 是那一路的 id，`since` 掩码 `<iso>`，`duration_seconds` 掩码 0。

- 提示级两条（转写 health 掉到 `behind` / `stopped` 一条、待转写段数 ≥ 20 一条）的 `source` 定名也是 `transcript`，各自对称地清掉。**`hints` 只在 `top_state ∈ {recording, alert}` 时可能有条目**，`idle` 与 `ended` 恒为空数组；空闲态 `health == "stopped"` 报的是「真转写后端还没起过场次」，与录音中「转写停了」是两种情形，不产生提示级；离开录音 / 告警态时服务把提示级连内部登记一起清掉。
- `muted` 说的是**这一条现在会不会出声**（会话级静音生效期间，或还在抑制窗口里）。判定与窗口计算**全在服务侧**：界面只看布尔值，**不算任何窗口**。重算只发生在 `ServiceRuntime.commit()` 那一刻——抑制窗口到点本身不产生提交，「窗口已过、下一次提交还没来」那段里 `muted` 仍是上一次算出的 `true`。**首发那一帧 `muted` 是 `false`**（界面据此响一声），此后同一条是 `true`；恢复后再次发生按新事件处理，首发又是 `false`；本场静音期间一律 `true`。
- **心跳失联那条致命级由界面自己判、自己画**，服务侧不产生它。

**`transcript`**：`health ∈ normal｜behind｜stopped`（直接来自转写后端；**空闲态是 `stopped`**——真实现没起过场次时报的就是它；缺席档的缺省替身恒报 `normal`）；`last_line_at` 是 ISO｜null（掩码 `<iso>`，服务收到最近那一行时的墙钟）；`buffered_lines` 与 `failed_segments` 是计数——**只进 log 与 `/debug`，产品界面不摆**，写进快照但不给 `revision` 投票（只有 `health` 的迁移才提交）。刷新路径：开一场就地刷一次、每出一行刷一次、一条 0.5 秒低频节拍兜住「worker 停摆、没有行可等」；**读路径（`GET /api/state`、SSE 首帧）不碰后端、不改状态**。会中稿逐行落 `meeting_transcript_<source>.md`，会后那份带说话人编号的稿另落一份、平级对拍（第 7 章）。

**`ended`**：`phase ∈ draining｜done`（**`failed` 不是这个字段的取值**——它属于盘上 `session.json` 的 `state`，快照里没有对应位置）；`steps` 次序恒为 `flush` / `log` / `post`（转码已摘出收尾序列：flush 落定自动在后台起，读数在 4.9 的 `transcode` 格）；`error` 是 string｜null。`DrainStep` **四键齐** `{id, status, detail, error}`；`status ∈ waiting｜running｜done｜failed｜skipped`；**`error` 键恒在**，没有错误时就是 `null`。

- 收尾失败后 `phase` **保持 `draining`**、`ended.error` 带那一刻的原因，摘要与 `session` 一起留在快照里。
- 成功档收尾的副作用把两路 `health` 从 `stopped` 带回 `unprobed`；失败停留档**保留 `stopped` 现场**。

**`settings`** 七键：`alarm_sound`（bool）/ `heartbeat_interval_seconds`（int，界面发心跳的周期）/ `summary_default_window`（`5m` / `15m` / `30m` / `full`）/ `summary_preset_names`（字符串数组，按配置里的次序）/ `summary_active_preset`（字符串，空串 = 不选预设、用平铺四键）/ `asr` 五子键（`max_segment_seconds` / `timeout_seconds` / `max_new_tokens` / `repeat_max_unit` / `repeat_min_coverage`）/ `devices{mic, loopback}`。`asr` 与 `devices` 与配置键**逐字同名、只去掉前缀**；预设两格**只有名字与指针**，端点与密钥不进快照。

- `devices` 两子键取值是**设备名字符串**，没配就是 `null`（按默认策略挑）；**空字符串与纯空白串按没配处理**，`settings_update` 写 `"devices.mic": ""` 会照常落盘、落成 `null`。
- 这里读到的是**运行期生效值**：热读键（`alarm.sound`、`asr.*` 五个、两个设备键位、`heartbeat.interval_seconds`、`summary.default_window`、会中切模型两键）写盘那一刻就换过来，其余键改了要重启才可见（4.6）。

**`revision` 的语义**：递增与广播只经 `ServiceRuntime.commit()` 一处；同一次事实处理里的多次改动合并成一次 `+1`、一次全量快照；被状态机拒绝的动作**不涨 `revision`、不写盘、不广播**。界面拿它作「我手上这份是不是最新」的参照，**不许自己算它**。

**`notice` 槽位已退场**：顶层不再有这个键；「最近一场」由 `GET /api/sessions` 的 `recent` 回答（4.8），启动序列对场次目录照旧一个字不读。

### resync 帧额外的两键

慢消费者重同步那一帧（3.5）在顶层九键之上**额外多两键**：`resync`（bool，恒 `true`）与 `dropped`（int，≥ 1，丢掉多少条之后补的这一份），顶层键集合因此恰好是**十一键**。界面见到 `resync: true` 就**整块替换**，不要试图补齐中间过程。

## 3. SSE：四事件与重连语义

`GET /api/events`，应答 `Content-Type: text/event-stream`，另带 `Cache-Control: no-store`、`X-Accel-Buffering: no`、`Connection: keep-alive`。

**连接即快照**：建立连接后第一帧**必须是 `snapshot`**，新开的页面直接落在当前状态、**不经过 `idle`**。「取快照」与「注册订阅」处在同一段同步代码、两者之间不出现 `await`——「注册前发生了一次状态变更 → 首帧落后于订阅时刻」的缝不存在。

**开头的 `retry`**：连接后先发一帧 `retry: 2000`（毫秒）再发首帧；断线由浏览器自己按这个间隔重连。

### 3.1 四个事件的帧形状

帧格式一律 `event: <名字>` + `data: <一个 JSON 对象>`；`state` / `snapshot` **多一行 `id: <revision>`**，放在 `event:` 之前。JSON 用 `ensure_ascii=False` 序列化（中文原样出），帧尾两个换行。

| 事件 | 什么时候发 | 带 `id:` 吗 | `data` 形状 |
|---|---|---|---|
| `snapshot` | 建连首帧；慢消费者重同步 | **带**，值 = 快照的 `revision` | 完整快照（重同步时多 `resync` / `dropped` 两键） |
| `state` | 每次状态变更（**全量快照，不做增量**） | **带**，值 = 快照的 `revision` | 完整快照（第 2 章九键） |
| `levels` | **只在 `recording` / `alert` 态**，**4 次/秒**（两路合在一帧里） | 不带 | `{revision, frames: {mic: {…}, loopback: {…}}}` |
| `transcript` | 每出一行转写 | 不带 | `{revision, line: {ts, source, text, kind}}` |

- `levels` / `transcript` 是高频通道，**不占 `revision`**（它们的到来不代表状态变更），只在 `data` 里带当前 `revision` 作参照；`state` / `snapshot` 的 `id` **与 `data.revision` 是同一次提交的那个数、逐值相等**——界面可以说「id 变了就是状态变了」。
- **保活**：安静时每 **15 秒**一行注释帧 `: ping`（只有这一行，**不伪造 `event` / `data`**），让中间层与浏览器不把连接判死。

### 3.2 levels 帧

`data = {revision, frames}`，`frames` 恰好 `mic` / `loopback` 两键，每路五键：`ts`（**恒为 `null`，这是契约的一部分**——界面要显示时间用 `data.revision` 或自己那侧的到达时刻，不要等 `ts` 出值）/ `dbfs`（掩码 `<dbfs>`）/ `peak`（掩码 `<dbfs>`）/ `bar`（**恒等于该路 `envelope[-1]`**，同一个数的两种读法）/ `envelope`（长度恰 15，逐根掩码 `<dbfs>`）。

`bar == envelope[-1]` 与包络长度 15 是**结构不变量**，fixtures 比对时在掩码前的原始载荷上断言。

### 3.3 transcript 帧

`data = {revision, line}`，`line` 恰好四键：`ts`（后端给的秒数，**语义归后端、服务只透传**——真后端给采集时刻的墙钟 epoch 秒，段一入队就盖章，识别慢不会把后面几行挤到一起）/ `source`（哪一路）/ `text`（这一行的正文；**识别失败的段落也照出一行**，正文是占位符——双层方括号裸文本，不带时间戳、不含 `**`：`[[识别失败: 超时 20s]]` / `[[识别失败: 重复循环]]` / `[[识别失败: 无输出]]`）/ `kind`（`line` 或 `gap`）。

- 失败段落也走 `line`，靠占位符形状认出来（界面画成缺口，别按正文渲染）；服务推 `gap` 的场合是**续接开场**——两段之间没录的时间各路各推一条。
- 一行到来时服务只更新 `transcript.last_line_at` 并推这一帧，**不参与状态迁移、不涨 `revision`**。一个写死的例外：`alerts.hints` 的**键集合**变化（提示级新增 / 移除 / 被上限 3 挤掉、离开录音告警态整格清空）要能被界面看见，借既有调用点走 `state` 通道提交一次（`revision +1` + 全量广播）；**同一个键集合里 `duration_seconds` 的刷新不算变化**、不提交——否则 0.5 秒的节拍会把 `revision` 一直推上去。

### 3.4 重连语义

- **服务不重放断开期间的事件**：没有 `Last-Event-ID` 机制，也没有事件缓冲。**重连后仍然先发 `snapshot`**，界面拿到它**整块替换**——这就是全部的重同步手段。
- 界面的正确姿势：**以 `snapshot` / `state` 为唯一权威、以 `levels` / `transcript` 为增量流**；重连后不要拿 `id` 去补齐中间过程（第 6 章）。
- **关页面只做一件事**：把订阅者从 hub 摘掉。**不动会话、不停采集**；最后一个订阅者走了服务照跑、录音继续。

### 3.5 慢消费者 resync

每个订阅者一个**有界队列，容量 512**，外加一个只留最新一帧的 `levels` 槽。

- 队列**满**（`state` / `transcript` 两路）→ **清空该订阅者的队列、塞一份新的 `snapshot`**，带 `resync: true` 与 `dropped: N`，事件 id 取快照的 `revision`。**宁可让它重同步，也不许它拖住别人。** `levels` 满了**不触发重同步**：只留最新一帧，旧的直接盖掉。
- **广播路径是同步的、不 `await` 慢消费者**：任何一次广播都不会因某个订阅者的队列满而阻塞；清队列与补快照之间**不产生新的丢帧窗口**（补进去的是当前快照）。
- 界面：`resync: true` → 整块替换快照、把「我漏了 N 条」如实告诉用户；**不要**把 `dropped` 当成错误码。

## 4. 动作：信封、`code` 表与逐档 `detail`

### 4.1 请求信封

```json
{"action": "start", "args": {"mode": "both", "dir": "C:\\会议\\跟老王聊周会"}}
```

`action` 是动作名；`args` 是**对象**（省略时按 `{}` 处理）。

**两个必需请求头（客户端义务）**：`POST /api/actions` 必须同时带 `Content-Type: application/json` 与 `X-Meeting-Service: 1`，**缺任一条回 403**（`Content-Type` 按前缀比对）。这道校验在**中间件里、先于路由匹配**生效，挡的是「本机浏览器里的恶意页面用表单式简单请求打过来」。403 的 body 是一行**纯文本**，走中间件自己的出口，**4.2 的失败信封在这里用不上**——请求压根没走到端点。

### 4.2 应答形状

**成功**（`200`；`settings_update` 例外见 4.6）：`{"ok": true, "revision": N}`，部分动作在通用信封上加自己的键（4.3）。

**失败**：`{"ok": false, "code": "...", "message": "...", "detail": {...}}`。`code` 是机器可读的那个（界面据此选失败块的画法）；`message` 直接能显示；**`detail` 恒是对象**——有补充就逐键带着，没有补充给**空对象 `{}`**（写 `null` 会让界面多一条判空分支）。HTTP 状态码由 `code` 决定，同一个 `code` 固定。**请求体本身的毛病**（不是 JSON / 不是对象 / `args` 不是对象）也走这个信封：`code = invalid_setting`、`status = 400`、`detail = {}`。

### 4.3 十九个动作

`start` / `stop` / `mute_session` / `pause` / `resume` / `set_source` / `settings_update` / `reprobe` / `dev_inject` / `summarize` / `workspace_open` / `workspace_remove` / `post_offline` / `post_minutes` / `post_stop` / `transcode_run` / `transcode_pause` / `transcode_resume` / `speaker_rename`。

| 动作 | 语义 | `args` |
|---|---|---|
| `start` | 开一场：写第一版 `session.json` → 起采集 → 进 `recording`（`idle` 与 `ended` 都能出发）。目录里已有 `session.json` 过得了校验 = **续接**（同一场会再录一段，旧段归档一个字节不碰、新段接着已有段号编）。失败顺序固定，见本节末 | `dir` **必给**：一条**已存在**目录的路径（服务**不建目录、不设默认落点、不按日期命名**，`docs/design-intent.md` 第 3、4 条）；`mode ∈ both｜mic｜loopback`，缺省 `both` |
| `stop` | 结束本场：冻结时长 → 进 `ended·draining` → 按收尾三步跑到终态。flush 落定那一刻转码在后台自动起（不占收尾预算）；**`post` 那格不自动拉起任何会后流程**，做完该做的停在 `waiting` | 无 |
| `mute_session` | 本场不再出声：`alerts.*.muted` 立刻变 `true`，静音期间新起的告警同样继承。**不改**持久化开关 `alarm.sound`，**下一场 `start` 自动复位** | 无 |
| `pause` / `resume` | 暂停 / 继续：不录内容（归档改写数字静音）、段不切、时长停在原地、**暂停的那几段不计入静默判定**；顺带让会中模型退出 / 拉回显存（`release_gpu()` / `acquire_gpu()`，异步提交，应答不等）。都幂等，顶层四态一个字不动 | 无 |
| `set_source` | 会中关掉 / 重开某一路：关掉——停流、改写静音、**不报 `SourceLost` 不进告警**、落 `health = stopped`、这一路挂着的致命级当场清空；重开——重新挑设备重开流，成功回 `capturing`（干净开场），**设备打不开如实回 `capture_unavailable`(503)**、那一路停在 `stopped`。幂等，两个方向都不切段 | `source` **必给**（`mic` / `loopback`）；`active` **必给**（bool） |
| `settings_update` | 写配置文件（原子写），见 4.6 | 「配置点号键名 → 值」的扁平映射 |
| `reprobe` | 重新枚举并试开设备，结果落进快照两路（`revision +1`）。**只在没有场次在跑时可用**（`recording` / `alert` 回 `already_recording`(409)） | 无 |
| `dev_inject` | 注入故障（**只在 dev 采集替身在场时可用**）：`silence`（电平落到阈值下）/ `stall`（两路事实帧整体停发，HTTP 照常应答）/ `lost`（直接来一条 `SourceLost`）。非 dev 后端回 `action_unavailable`(404)，没有场次回 `not_recording`(409) | `source` / `fault` / `after_seconds`（到点生效，可 0）/ `duration_seconds`（到点恢复，正数可省，**缺省 30.0 秒**） |
| `summarize` | 会中一键总结（4.7）。**只读**：不写快照、不动 `revision` | `window ∈ 5m｜15m｜30m｜full`，缺省取配置 `summary.default_window` |
| `workspace_open` | 打开 / 收编一个**已存在**目录，登记进工作区注册表（4.8）。**任何状态下都可用**（清单维护不碰快照） | `dir` **必给**；`confirm`：非空无材料那一档确认收编时 `true` |
| `workspace_remove` | **只把这一条从注册表摘掉**，目录与文件一个字节不动（`docs/design-intent.md` 第 5 条）。没登记过也照样 200（`removed: false`，重复删除安全） | `dir` 必给 |
| `post_offline` | **「生成会后精细稿」**：只跑会后**离线**那条链（走离线模型，出带说话人编号的稿），**纪要不碰**。起一条后台线程即算启动成功，跑成什么样由场次目录 `meeting_offline_status.json` 回答 | `dir` **可省**——省略跑收尾刚落定的那一场；校验见本节末的闸 |
| `post_minutes` | **「生成纪要」**：把这一场**手上有的稿子**交给 DSH（会后稿在就两份一起给），**不补跑离线**。起后台线程即算启动成功。**纪要免闸**但**说破降级**（见本节末） | 同 `post_offline` |
| `post_stop` | **停掉正在跑的「生成会后精细稿」**：**立起取消旗标即回**（不等块边界）——线程当前块跑完收场，盘上状态由 `run_session` 写成 `interrupted`（终态写入只归它）；正在跑的那块不掐（引擎是同步推理）。范围只有 offline 一档 | `dir` **可省**——在飞的 offline 可不止一场：省略时恰好一场才停它，多场在飞按 400 让 `dir` 点名（不猜） |
| `transcode_run` | **补跑转码工序**（转码已摘出收尾序列，flush 落定自动在后台起，这只是人手那一半）：从盘上凭据接着做——凭据核对通过的段跳过、没凭据的段（含被强杀留下的半截产物）重做。**判据与 `meeting post` 的归档补齐共用一份**（`meeting/capture/transcode.py`，不许抄第二份） | `dir` **可省**；本进程**在录的那一场**如实拒（400：PCM 还在长，凭据对不上） |
| `transcode_pause` / `transcode_resume` | **暂停**：立起暂停旗标即回，工序在**段边界**落 `paused` 并就地等旗标（同一条线程活着等）。**继续**：在飞且暂停中 → 摘旗接着前进；本进程没有这道工序在飞（重启过、或暂停那一刻进程死了）→ 从盘上凭据接着做。**进度与暂停状态都活在盘上，重启后照样续得上** | `dir` 可省——恰一道在跑时省略即停它，多场在跑必给（400，不猜） |
| `speaker_rename` | **给说话人编号起名**（4.11）：只写这一场目录根的人名表，不动快照（不提交、不涨 `revision`、不广播）。**任何状态下都可用**（在录那一场除外，见本节末） | `dir` **必给**（**没有省略语义**）；`key` **必给**（`mic` / `loopback`，或 `mic:S01` 这种带路前缀的限定名）；`name` **必给**非空（跳过 = 不起名；重名合法——编号是分开的） |

**闸的现行口径**：不再有「任何一趟会后在飞就全拒」的进程级一刀切；每趟活按 `(种类, 场次)` 一格，同一种重活在同一场上只能有一趟。**唯一硬闸 = 本地大模型互斥**。都落在既有 `code` 上，不加新 `code`：

- **这一场还在录音中**（运行时 `top_state ∈ recording / alert`，或 `args["dir"]` 点名的那场正是本进程在录的）→ `draining`(409)——唯一硬闸的一侧（录音中不并排跑会后精细稿）。
- **同一场已有一趟同一种活**（`(种类, 场次)` 槽闸）→ `draining`(409)，`detail` 带那一趟的 `{kind, dir, started_at}`。**别的场次、别的种类、在飞的转码都不挡**。
- **minutes 层状态停在 `running`**（`meeting_minutes_status.json` 读得出 `state = "running"`）→ `draining`(409)，`detail` 带 `status_path` 与那一版的原因——在动作本体里同步判，重复按「生成纪要」拿到的是拒跑档，不是「已起」的假回执。
- **`post_minutes` 纪要免闸**：同场精细稿在飞照常受理，但成功体多 `inputs`（当时读到的输入档位 `both` / `live_only` / `offline_only` / `none`）与 `note` 说破句——**不许静默降级**。
- **这台机器没接这一层**（`post.hook` / `minutes.hook = null`）→ `action_unavailable`(404)。会后成功体 = 通用信封 + `post` 键 `{kind: "offline"｜"minutes", dir}`（`post_minutes` 多 `inputs` / `note`）——回执里**没有**「跑完了没有」，那一趟可能跑一小时。
- **唯一硬闸的另一侧（`start`）**：任意一场会后精细稿在飞时 `start` → `draining`(409)，`detail` 带那一趟的 `{kind, dir, started_at}`。界面先弹窗确认（逐字「启动新会议会打断会后总结，是否继续」）确认才打断再起录；服务侧这道 409 拦的是没走弹窗的调用方。**转码在飞不构成 `start` 的阻挡**（不吃模型显卡）。
- **转码三只的闸**：`transcode_run` 同场已有一道在跑 → `draining`(409)（同一把槽闸）；`transcode_pause` / `transcode_resume` 没有在跑的工序可暂停 → `draining`(409)（resume 从盘上凭据接着做，照常 200）。
- **`post_stop` 的拒绝档**：没有任何 offline 在飞且省略 `dir` → `draining`(409)，`detail` 空对象；省略 `dir` 而多场在飞 → `invalid_setting`(400)，`detail = {key: "dir", value: null, legal}`（`legal` 是在飞那几场的目录，不猜）；点名那场在飞的是纪要 → `draining`(409)，`detail` 与槽闸同形（停的动作只接 offline，如实拒）；点名那场没有在飞的 → `draining`(409)。成功体多一格 `stopping: true`：「停没停成」由盘上 `meeting_offline_status.json` 的 `interrupted` 回答。
- **会后与转码动作的 `dir` 校验**（`post_offline` / `post_minutes` / `transcode_run` 与 `speaker_rename` 给 `dir` 那一支）：与 `GET /api/sessions` 同一套注册表 / 目录判读——不在注册表里、目录不在原地 → `invalid_setting`(400)。**盘上残档不拒**：被强杀丢在录音半路（盘上停 `recording`）或收尾半路（`draining`）的那一场照常受理，在录的那一场由运行时闸先拦下；「盘上 `state` 是 `recording` 就拒」的旧判据作废。`speaker_rename` 的本进程在录那一场按 `invalid_setting`(400) 拒（会中不提供改名）。

**`start` 的失败顺序是固定的**：先判态（`already_recording`；收尾闸收窄后的 `draining`——只挡「收尾 task 真在飞且 flush 尚未落定」与「历史失败档」两档；会后精细稿在飞的唯一硬闸 `draining`）→ 再校验 `args`（`dir` 缺失 / 目录不存在、`mode` 非法 → `invalid_setting`）→ 再判后端可用性（`capture_unavailable`）→ 最后才写第一版文件。flush 已落定而收尾尾巴（log / post）还在飞的那档放行（路由先等尾巴落定再动手）。**态不对或后端不可用时盘上不落这个目录里的任何新东西**（目录本身是用户给的，软件也不删它）；**续接失败旧记录原样保留、本轮段文件收走，工作区回到按下去之前的样子**。

**`reprobe` 成功体就是通用的 `{ok, revision}`（没有额外字段）**，挑不到设备不构成失败——如实落进快照两路的 `health = probe_failed`。**`stop` 成功后 `revision` 会继续往上走**（收尾各步各自提交一次），`ended.phase` 最终落 `done`（前两步 `done` / `skipped`、`post` 停 `waiting`、无 `failed`）或保持 `draining`（有 `failed` 步，或收尾被取消）；**`ended` 不回到 `idle`**，下一场 `start` 从 `ended` 直接进 `recording`。

**`pause` / `resume` / `set_source` / `speaker_rename` 与转码三只都没有 golden 样本**：`contract/fixtures/actions/` 里只放成功七档与九个 `code` 的失败档，这些动作的语义由既有测试逐条实测（`tests/test_capture_engine.py` / `test_api_actions.py` / `test_set_source_alerts.py` / `test_speaker_names.py` / `test_transcode_job.py`）。

### 4.4 `code` 表与逐档 `detail` schema

九个 `code`，一个不多一个不少：

| `code` | HTTP | 意思 | 谁会回它 |
|---|---|---|---|
| `already_recording` | 409 | 已经有一场在录 | `start` / `reprobe` |
| `draining` | 409 | 收尾失败档停留（或收尾尾巴迟迟不落定）；会后精细稿在飞时 `start` 拿它回「先把那一趟停下」（唯一硬闸另一侧）；会后按钮拿它回「还在录音中 / 同一场已有同一趟在飞」；`post_stop` 拿它回「没有在飞的会后可停 / 在飞的是纪要 / 点名的这场没有在飞的」；`transcode_pause` 拿它回「没有正在跑的工序可暂停」 | `start` / `post_offline` / `post_minutes` / `post_stop` / `transcode_pause` |
| `capture_unavailable` | 503 | 采集后端不可用：没接入、或设备起不来 | `start` / `set_source`（重开某一路时设备打不开） |
| `not_recording` | 409 | 现在没有在录的场次 | `stop` / `mute_session` / `pause` / `resume` / `set_source` / `dev_inject` / `summarize` |
| `invalid_setting` | 400 | 参数或键名非法（校验都在动手之前） | 各动作 |
| `persist_failed` | 500 | 配置 / 记录写盘失败，这次改动一处都没生效 | `settings_update` / `speaker_rename`（人名表写不进去） |
| `action_unavailable` | 404 | 这个后端没有这个动作；会后两个按钮拿它回「这台机器上没接这一层」 | `dev_inject`（非 dev 后端）/ `post_offline`（`post.hook` 缺席）/ `post_minutes`（`minutes.hook` 缺席） |
| `transcript_unavailable` | 503 | 转写没在跑（没起过，或已经停了） | `summarize` |
| `summary_failed` | 502 | 会中总结没拿到结果：模型端点没配齐，或预算用尽 / 端点报错 / 解析连试带败 | `summarize` |

**逐档 `detail` schema**（`detail` 恒是对象；「空」即空对象 `{}`）：

| `code` | `detail` 形状 |
|---|---|
| `already_recording` | `{session_id, dir, elapsed_seconds}`（掩码 0） |
| `draining` | **五档**：`{session_id, dir, failed_steps: [{id, error}], log_path}`（`start` 的收尾闸；没有失败步时 `failed_steps` 是空数组）；或 `{kind, dir, status_path, reason}`（minutes 层停在 `running`）；或 `{kind, dir, started_at}`（槽闸 / 唯一硬闸）；或 `{top_state, session_id, dir}`（还在录音中）；或空对象（`post_stop` 没有在飞的会后可停） |
| `capture_unavailable` | **四档**：空对象；或 `{session_id, dir, error}`（第一版 `session.json` 写不进去，`error` 是 `类型名: 消息`）；或 `{session_id, dir, reason}`（采集起不来，`reason` 是后端给的人话）；或 `{source, error}`（`set_source` 重开时设备打不开，那一场照常往下录、那一路停在 `stopped`） |
| `not_recording` | 空 |
| `invalid_setting` | **四档**：`{key, value, legal}`（值非法，`legal` 是合法取值数组）；或 `{source, fault, error}`（`dev_inject` 取值非法）；或空；或 `{action, known}`（**未知动作名**，`known` 是十九个动作名的数组） |
| `persist_failed` | `{error}` |
| `action_unavailable` | 空 |
| `transcript_unavailable` | `{health: "stopped"}`——恒这一格 |
| `summary_failed` | **两档**：`{missing: ["summary.base_url", …]}`（端点没配齐，点名缺的配置键；预设模式下报 `summary.presets_json[名字].格`）；或 `{elapsed_seconds, attempts}`（预算用尽 / 端点报错 / 解析失败，如实报等了多久、试了几次） |

**界面按 `code` 选画法、`detail` 有哪个键就显示哪个键**（第 6 章第 7 条）：`invalid_setting` 的 `key` 在 → 「这个键不合法，合法取值是…」；`source` 在 → 「注入的取值不对」；`action` 在 → 「不认识的动作名」并列出 `known`；一个键都没有 → 显示 `message` 就够。`failed_steps[*]` 恰好两键 `{id, error}`，加上服务级 `log_path` 够画「收尾失败块」。

### 4.5 逐动作 × 逐态的可用性

| 动作 | `idle` | `recording` / `alert` | `ended`·`done` | `ended`·`draining` |
|---|---|---|---|---|
| `stop` / `mute_session` / `pause` / `resume` / `set_source` / `dev_inject` / `summarize` | 409 `not_recording` | 200（各自语义与失败档见 4.3） | 409 `not_recording` | 409 `not_recording` |
| `start` | 200（进 `recording`；落点是 `args.dir` 显式给的已存在目录） | 409 `already_recording` | 200（从 `ended` 直接进 `recording`，清掉上一场 `ended` 摘要）；**会后精细稿在飞 → 409 `draining`** | 只挡两档：**收尾 task 真在飞且 flush 未落定** → 409 `draining`；**历史失败档** → 409 `draining`（失败步明细带回）。flush 已落定、尾巴还在飞放行；**转码在飞不构成阻挡** |
| `settings_update` | 200 | 200 | 200 | 200 |
| `reprobe` | 200（两路快照随之更新） | 409 `already_recording` | 200 | 200（不干扰在飞的东西） |
| `workspace_open` / `workspace_remove` | 200（写注册表不碰快照；`workspace_open` 缺 `dir` / 目录不存在 / 非空未确认 → 400） | 200 | 200 | 200 |
| `post_offline` / `post_minutes` | 带 `dir` 且那一行合法 → 200；不带 → 409 `draining`（手上没有可跑后会的场次） | 409 `draining`（还在录音中——唯一硬闸这一侧） | 200（`post.hook` / `minutes.hook` 缺席 → 404 `action_unavailable`；同一场同种活已有一趟 → 409 `draining`；`post_minutes` 免闸、说破降级） | 同 `ended·done`（收尾没落定不再拦，**盘上残档照常受理**） |
| `post_stop` | 409 `draining`（没有在飞的会后可停） | 409 `draining` | 200（`dir` 点名 / 省略与拒绝档见 4.3） | 同 `ended·done`（**停止只看在飞，不看收尾**） |
| `transcode_run` | `dir` 点名或省略走收尾刚落定那一场 → 200；本进程在录的那一场 → 400 `invalid_setting` | 400 `invalid_setting`（归档还没落盘） | 200（同场已有一道在跑 → 409 `draining`；与 offline / minutes、别的场次、收尾都并行） | 同 `ended·done`；转码在飞不构成任何阻挡 |
| `transcode_pause` / `transcode_resume` | 409 `draining`（没有在跑的工序可暂停；resume 从盘上凭据接着做照常 200） | 409 `draining` | 200（暂停立旗即回、段边界落 `paused`；继续摘旗前进，本进程没有这道工序时从盘上凭据接着做） | 同 `ended·done` |
| `speaker_rename` | `dir` / `key` / `name` 齐且那一行合法 → 200；缺一格或不合法 → 400 `invalid_setting` | 400 `invalid_setting`（本进程正在录的那一场，会中不提供改名） | 200 | 同 `ended·done`（**盘上残档照常受理**） |

补充口径：`settings_update` 写配置文件，与录到哪儿无关，**任何状态下都可用**。`mute_session` 只在本场收尾之前可用（复位在下一场 `start`）。`reprobe` 只在**没有场次在跑**时可用——设备在开场前定下来，会中换设备是明文不做的那一档。`summarize` 只在**有场次在跑**时可用。会后两个按钮在**录音已结束**之后都可用（`ended·done` 与 `ended·draining` 同档）：拦它的只剩 4.3 那几道闸，**「收尾是否落定」不是判据**——被强杀丢在收尾半路的那一场，工作区读数就是「录音结束」，按钮照按。

### 4.6 `settings_update`：40 键清单与两档

请求的 `args` 就是**「配置点号键名 → 值」的扁平映射**，只有这一层、不按键前缀分组。**成功体与别处不同**（**没有 `message` 字段**，给人看的话由界面按两个数组自己组织）：`{"ok": true, "revision": N, "applied": [...], "pending_restart": [...]}`，两数组里是同一套点号键名、次序照本次请求的键逐条走。

**合法键名是逐条列出的 40 个**（不按前缀归组、没有前缀白名单；两档合起来正好是 `meeting/config.py` 的 `DEFAULT_VALUES` 键全集，判定由 `SETTING_KEY_GROUPS` 一处给出）。表外的任何键名一律 `invalid_setting`(400)，`detail = {key, value, legal}`。

- **`applied`（12 个热读键，写盘成功就立刻生效）**：`alarm.sound` / `asr.max_segment_seconds` / `asr.timeout_seconds` / `asr.max_new_tokens` / `asr.repeat_max_unit` / `asr.repeat_min_coverage` / `devices.mic` / `devices.loopback` / `heartbeat.interval_seconds` / `summary.default_window` / `summary.presets_json` / `summary.active_preset`。
- **`pending_restart`（28 个，启动时读一次，写盘成功但要重启才生效）**：`web.port` / `web.dist_dir` / `log.level` / `log.dir` / `log.max_bytes` / `log.backup_count` / `alert.silence_dbfs` / `alert.silence_seconds` / `alert.recover_seconds` / `alert.alarm_suppress_seconds` / `capture.backend` / `transcriber.backend` / `post.hook` / `post.engine` / `post.model` / `post.dtype` / `post.chunk_seconds` / `minutes.hook` / `minutes.chunk_lines` / `minutes.max_segments` / `minutes.timeout_seconds` / `minutes.dsh_url` / `summary.base_url` / `summary.api_key` / `summary.model` / `summary.extra_body_json` / `summary.deadline_seconds` / `summary.max_attempts`。

**口径**：

- **两个数组的判据只有一条**：该键归哪一档；**「写盘成功」是前提**——写盘失败整笔回 `persist_failed`(500)，一处都不生效。
- **热读键写盘成功那一刻**，服务侧重取一次生效值视图并提交（`revision +1`），快照 `settings.*` 当场换新值——界面当场按新值决定响不响，设备改完紧跟着的 `reprobe` 按新值挑。**全是 `pending_restart` 的改动只写盘、不提交**，`revision` 不动——界面别拿 `revision` 当「设置改没改成功」的判据，看 `ok` 与两个数组。
- **只写用户配置文件里已有的键 + 本次改动的键**，不把代码默认值落盘。**校验在写盘之前**：键间规则（至少「超时必须大于段长」`asr.timeout_seconds > asr.max_segment_seconds`，没改到的那一半取生效配置当前值）违规回 `invalid_setting`(400)。
- **被环境变量压着的键不改分类**：照常进 `applied`，「被压着」只在主 log 记一条 warning，运行期仍由环境变量说话。**配置文件与请求同位**：`~/.config/meeting/config.json` 里也只有一层点号扁平键名，**嵌套形状不合法**。`pending_restart` 的键在主 log 里点名（热读键不进那一行）。
- **会中总结那九个 `summary.*` 键**（键名与取值范围的判据在 `meeting/config.py`；`summary.api_key` **可空**——`null` = 没配，与设备键位同形）：**预设两键**——`summary.presets_json` 收「一段 JSON 数组的文本」，每套一个对象、只认 `name` / `base_url` / `api_key` / `model` / `extra_body_json` 五格，`name` 必填、非空白、不重名（当前指针按它挑）；`summary.active_preset` 收空串或某套预设的 `name`，指向的名字不在清单里当场 `invalid_setting`(400)。**回落**：预设未配、清单解析失败、指针空或指向的名字不在清单里，端点四件套一律取平铺四键——存量 `config.json` 一个字不用改。预设里的密钥不进快照、不在界面回显。

### 4.7 会中总结：`summarize` 与 `GET /api/summaries`

会中有人跟丢了，按一下：把**最近这段时间的实时转写**读一遍，给回三小段。`window` 取 `5m` / `15m` / `30m` / `full`，**省略时取配置的 `summary.default_window`**；给了取值域外的值**不静默落缺省档**，当场回 `invalid_setting`(400)。

**成功体**比通用信封多一个 `summary` 键：`{window, window_label, at, elapsed_seconds, lines, what, where, lost}`。`revision` 是**当前**版本号——**总结是只读的**：不写快照、不动 `revision`、不广播。`window_label` 是人话标签（`5 分钟` / `15 分钟` / `30 分钟` / `整场`），`lines` 是窗口里的行数；三小段是**字符串数组**（`what` 刚才在讲什么 / `where` 讲到哪儿了 / `lost` 丢掉了什么）。**窗口里一行都没有时照旧 `ok: true`**，三段空数组、`lines` 为 0——这种情形**一个模型请求都不发**，空态文案由界面给。

**失败**（走 4.2 的失败信封）：

| 情形 | `code` | HTTP | `detail` |
|---|---|---|---|
| 没有在录的场次（`idle` / `ended`） | `not_recording` | 409 | `{}` |
| `window` 取值非法 | `invalid_setting` | 400 | `{key: "window", value, legal}` |
| 转写停了 / 没起过 | `transcript_unavailable` | 503 | `{"health": "stopped"}` |
| 模型端点没配 | `summary_failed` | 502 | `{missing: [...]}`（预设模式下报那套预设缺的格） |
| 预算用尽 / 端点报错 / 解析连试带败 | `summary_failed` | 502 | `{elapsed_seconds, attempts}` |

**判定的先后是固定的**：判态 → 校验 `window` → 看转写健康 → 看模型端点配置 → 才动手。**前面几档没过，一个请求都不发出去、一条记录都不写**。`summary.deadline_seconds`（默认 25 秒）与 `summary.max_attempts`（默认 3）管那道墙钟预算，判据在 `meeting/summary/` 的模块说明；`summary_failed` 的预算 / 端点那一档要一个真会等满预算的假端点，由 `tests/test_summary_action.py` 逐档实测、没有采成样本。

**本场总结记录** `GET /api/summaries`（**任何状态下都 200**）：`{"session_id": "<场次目录名或 null>", "entries": [{window, window_label, at, elapsed_seconds, what, where, lost}]}`。取的是本场按过的、**成功产出的**那几次，按时间先后；**失败的不记**。记录**活在服务侧内存里、会话级、不落文件**：新的一场 `start` 清空，服务关停一并清掉。读它**不涨 `revision`**、不广播。**并发不做闸**：两个页面同时按下就是两次独立的总结，界面自己按「进行中」禁按钮。

### 4.8 工作台收编与目录选择器

**`workspace_open`**（成功体 = 通用信封 + `workspace: {dir, kind, suspected, first_time}`）：先判名目（`meeting/workspace.py` 的 `classify_workspace_dir()`），**三档三条路**——`kind: "empty"` 空目录当一场新会，直接登记；`kind: "meeting"` 有会议材料（`session.json` 或任一 `meeting_audio_*.pcm`）就收编，**`suspected: true` = 有音频归档名但没有 `session.json` 的疑似会议目录**，照收编处理、不当杂物目录；`kind: "unknown"` 非空、没有会议材料——`confirm` 缺失时回 **400 `invalid_setting`**（`detail = {key: "confirm", value: null, legal}`；**目录与盘面一个字节没动**），带 `confirm: true` 重发才登记。`dir` 给到不存在的路径回 **400**（已存在是硬前提，本服务不建目录）；注册表写盘失败回 **500 `persist_failed`**。去重判据是**规范化后的绝对路径**：同一目录选两次只占一行，已登记的只更新那一行的 `last_opened_at`（`first_time: false`）。**任何状态下都可用**，不碰快照、`revision` 不因登记涨。

**`workspace_remove`**：成功体 `{ok, revision, workspace: {dir, removed}}`。只把这一条从注册表摘掉，文件一个字节不动；之后随时可用 `workspace_open` 收回来。

**`GET /api/sessions`**（**任何状态下都 200**，纯读、不涨 `revision`）：

```json
{"sessions": [{"dir", "session_id", "dir_exists", "kind": "empty|meeting|unknown|missing",
               "suspected", "state": "recording|ended|null", "date", "mode",
               "segments": [{"index", "started_at", "ended_at", "duration_seconds"}],
               "has_offline", "added_at", "last_opened_at"}],
 "recent": "<path>|null",
 "post_jobs": [{"kind": "offline|minutes|transcode", "dir", "started_at"}]}
```

- 清单是**软件自己记的那一份**（配置目录 `workspaces.json`，按路径去重）；**列表从登记来**——本端点不列任何没登记过的目录，`start` 与 `workspace_open` 是仅有的两个登记入口。每行的「目录还在不在、里面是什么」由服务**现场判**（注册表条目只记两个时刻）：`dir_exists: false` 如实等着界面写「路径已不在」，与 `state` 的三档**不折算**。
- **`state` 是三档口径，只表达「这场录音走到哪了」**：`"recording"` **仅当**这一行就是本进程此刻在录的活跃场次；盘上 `session.json` 存在一律 `"ended"`（被强杀丢在收尾半路的那一场，读数就是「录音结束」）；没有 `session.json` 是 `null`。落盘、转码、会后、纪要的进度**不进这个口径**（那些读数在 4.9）。
- **`has_offline`** = 这一场有没有会后精细稿：判据是场次目录里在不在 `meeting_offline_transcript_<source>.md`（只判名目、不读内容、不猜）。**这一格走本端点，不进快照**。**`segments`** = 盘上 `session.json` 段记录的原文，每段四格（`ended_at` 在录那段是 `null`，`duration_seconds` 还没写完的段是 `null`；在录时长、不含暂停）——给人看的读数，不进快照、不参与任何判定。
- **`recent`** = 最近一次被打开 / 切换那条的目录（`workspace_open` 与开成一场的 `start` 都刷新）；**只从注册表读，服务与界面都不扫任何场次目录**（`docs/design-intent.md` 第 4 条）。
- **顶层第三格 `post_jobs`** = 服务进程里此刻在飞的那几趟活，按 `(种类, 场次)` 一格一趟，**跑完那一刻那一项退出清单**；`kind` 答 `offline` 精细稿 / `minutes` 纪要 / `transcode` 转码工序。它是界面那格全局「显卡忙不忙」的**会后一半读数**（另一半「本机正在录」从快照读；转码不吃显卡，界面不把它数进忙）。`meeting post` 命令行那趟活在另一进程，这一格看不见——如实显示，不编。**这一格在应答体顶层、不进任何一行**（每行照旧恰好十二键）。

**`GET /api/dirs`**（**任何状态下都 200**，纯读）：`{"home", "cwd", "up", "here": {name, dir, kind, suspected}, "dirs": [{name, dir, kind, suspected}]}`。`?dir=` 省略时从用户主目录起步（落点由用户挑）；**`up` 到盘根才落 `null`**（UNC 路径不在口径里）；`here` 是**正在浏览的这一层自己**的名目判读——这层本身可选，不必退到上一层再点那一行（盘根没有末段名，`name` 如实给整条路径）。每项只判名目（同一套 `classify_workspace_dir()`），不读内容、不写任何东西；`?dir=` 给到不存在的目录回 400 `invalid_setting`。**`?files=1`**（可选项）：应答体多一个 `files` 键——这一层的普通文件每项 `{name, dir, size_bytes}`（`dir` 是文件绝对路径），按名字排序、与 `dirs` 分开两列；**不带这个参数时没有这个键**。

**`POST /api/dirs`（新建目录）**——`POST /api/dirs?dir=<父层路径>&name=<新目录名>`，两个参数都走查询串、请求体为空。`dir` 省略时用户主目录（与 GET 同一起点），`name` **必给、必须用户亲手起**：软件可以替你建目录，但**不自动命名、不自动挑落点**；服务侧**拒绝式校验、不清洗不改写**（非法名原样拒回，与资料上传那套清洗相反）。

| `code` | HTTP | 什么时候 | `detail` |
|---|---|---|---|
| `invalid_setting` | 400 | 那一层不存在（与 GET 同档），或名字非法（带路径分隔符或冒号、`.`/`..`、控制字符、以点结尾、两头空白、保留设备名、超长） | `{key, value, legal}`（`key = "name"` 是名字那档） |
| `dir_exists` | 409 | 同名已存在——目录或普通文件都算占位（这一片自己的 `code`） | `{dir, name}` |
| `persist_failed` | 500 | 盘上出岔（无权限等） | `{error}` |

成功体 = 通用信封 + `directory: {name, dir, kind, suspected}`（刚建出来是 `empty`）；界面拿它把新行按服务同一条次序（名字小写序）插进清单并**直接置为选定**，后续确认走既有「选定这个目录」链路。**只建一层、不做删除、不做改名**；动手前校验全过才碰盘（`mkdir()` 单调用，建不出来就是什么都没有），独立于四态快照——不提交、不涨 `revision`、不广播。写端点头哨要 `X-Meeting-Service: 1`（第 6 章第 4 条，核对表在 `meeting/service/app.py` 的 `_write_guard_path()`）。

### 4.9 单场状态：`GET /api/sessions/{id}`

**任何状态下都 200**；纯读：不提交、不涨 `revision`、不广播。`{id}` 与第 1 章同一写法（URL 编码的目录路径）。前八格与 `GET /api/sessions` 的同一行同源（同一套注册表 / 目录判读 / 盘上 `session.json` 读法，逐格语义见 4.8），这一只端点要回答的是后面几格「这一场此刻跑到哪儿了、这一场的说话人有谁」：

```json
{"dir", "session_id", "kind", "state", "date", "mode", "segments", "has_offline",
 "post": [{"running", "kind": "offline|minutes", "dir", "started_at", "finished_at"}],
 "transcode": {"state": "running|paused|done|partial|failed|idle", "in_process",
               "started_at", "finished_at", "elapsed_seconds", "current_source",
               "sources": [{"source", "done", "total", "remaining", "current_segment"}]},
 "minutes": {"state": "running|done|failed|delivered|idle", "started_at", "finished_at", "error"},
 "offline": {"ledger", "state": "running|done|partial|failed|interrupted|idle|ledger", "in_process",
             "started_at", "finished_at", "elapsed_seconds", "current_source",
             "sources": [{"source", "done", "total", "remaining", "current_chunk",
                          "counts": {"transcribed", "silent", "failed", "unjudged"}}]},
 "speakers": [{"key", "source", "label", "name", "segments"}]}
```

- **`post`** = `(种类, 场次)` 多槽登记里**这一场**此刻在飞 / 刚收工的那几趟活（转码那一趟不进这张清单，它的读数在 `transcode` 格）。每项五格，`running` 按线程活没活着答（与顶层 `post_jobs` 同一套判据）。**多槽登记后跨场次互不挤占**：另一场的会后照跑，这一场的清单不被清空；这一场没有任何活时是**空数组**。跑完很久 / 服务重启过之后，这一场跑成什么样改由 `offline` 的盘上记录回答。
- **`offline`** = 会后离线的**逐块进度读数**。`sources` **按路各一份**，分母是**这一路**切了几块（`post.chunk_seconds` 切出来的块数）——合成一个整场分母就是在造一个不存在的量（`docs/design-intent.md` 第 9 条）。`total` 不因某一块失败或跳过而增减；`current_chunk` 在这一路跑完 / 还没轮到时是 `null`。`counts` 四档 `transcribed` / `silent` / `failed` / `unjudged` **都算「跑过了一块」**。
  - **`state` 与 `in_process` 回答两件事，判据分开写死**：`state` 照**盘上那份状态文件**如实答（`running` / `done` / `partial` / `failed` / `interrupted`）——**盘上那几档都不折成 `done`**，进程崩在半路（状态文件停在上次落的 `running`）如实答 `running`；**`interrupted` 是盘上自己的终态**，不折回 `running`（那等于把「人停的、可以重按」说回「还在跑」）；这一趟不在本进程、盘上也没有状态文件但逐块记录在 → `ledger`；记录与状态文件都不在 → `idle`。`in_process`（布尔）答「**本进程里此刻有没有这一趟**」（判据是那条线程活没活着，与 `post.running` 同源），**跑完那一刻翻回 `false`**。`false` + `interrupted` = 打断了、可以重按；`false` + `running` = 盘上停着一版在跑、其实谁都没在跑。
- **`transcode`** = 转码工序的进度读数（判据与状态文件都在 `meeting/capture/transcode.py` 一处）。`state` 照盘上 `meeting_transcode_status.json` 如实答（`running` / `paused` / `done` / `partial` / `failed`；不在答 `idle`）——**判据读盘、不依赖进程内存**，重启后读数照旧；`in_process` 另答本进程有没有这道工序；`elapsed_seconds` 只在本进程在飞时给（跨进程算不出单调钟差值，如实 `null`）。`sources` 按路各一份，分母是这一路的段数（一段 = 一次录制）。**完成判据是逐段凭据**（`meeting_transcode_<source>_<NNN>.json`）：产物文件在那儿不算完成，半截产物没有凭据必被重做——「这段写完了」谁说了算，正本在 `docs/state.md`。
- **`minutes`** = 纪要那一层的盘上读数（判据在 `meeting/minutes/status.py` 一处，同一套「判据读盘、重启后照旧」）。`state` 照盘上 `meeting_minutes_status.json` 如实答；状态文件不在、读不动或形状不合契约时如实答 `idle`。**`delivered`** 是「已交付 DSH 生成」那一档（投递成功即算交付），**`done`** 是「纪要认真校验过、产物在盘上」——两档各说各的。**「本进程里在不在跑」不另立一格**：看 `post` 清单里 `kind: "minutes"` 那一项的 `running`。盘上 `running` 版落的是空串收尾戳，这里折成 `null`；`error` 是失败原因原话（没失败 `null`）。
- **`speakers`** = 这一场盘上的身份集（形状正文在 4.11）：每个身份一行五格——`key`（限定键）、`source`（路）、`label`（人话标签）、`name`（现名；没起过名或人名表读不出时 `null`）、`segments`（离线对照表里这一号出现的段数；路级身份如实 `null`）。**读侧只做结构解析**：人名表缺席或坏了当「没有确认过的名字」，不报错、不挡这只端点。给人看的读数，改名走 4.11。
- **读数来自哪里**：这一趟正在本进程里跑 → 用会后那条线程手里的现场（「已经跑了多久」按单调钟当场算）；否则读场次目录里那份逐块记录，跨进程 `elapsed_seconds` 如实 `null`。**它是给人看的读数，不带校验性质的含义**：任何代码不许拿它们核对、报错、改结论（`docs/design-intent.md` 第 6、7 条）；这一场跑成什么样仍由 `meeting_offline_status.json` 与产物回答。**不进快照**（推送只认第 2 章九键；列表每行照旧十二键）。

**逐块记录**：会后离线每跑完一块，往场次目录里 `meeting_offline_progress.jsonl` 补一行（这一块的文字 + 落点；JSON Lines，第一行表头）。**引擎加载失手那一档也留一行**（`state = failed`、`error` 带 `engine_load_error`）：不然盘上只剩表头，不在场的读方从 `sources[].total` 上看到的是 `null`，而现在读到的仍是这一路切出来的块数。正式稿 `meeting_offline_transcript_<source>.md` 的写时机不变（整路跑完一次写），那份记录不参与任何判定（幂等闸读的仍是状态文件），字段契约在 `meeting/offline/progress.py` 的模块说明。

**失败体**（HTTP **404**，通用失败信封）：`session_dir_missing`——目录已不在原地，`detail = {dir, kind: "missing"}`；`invalid_setting`——目录在、但这一场没在工作台清单里（规范化绝对路径比对），`detail = {dir, kind}`。

### 4.10 设备清单：`GET /api/devices`

**任何状态下都 200**；纯读、不涨 `revision`。设置抽屉的两个设备下拉读它。**只枚举、不开任何设备**（`probe()` 那条会短暂试开的路不在它身上）：`{"mic": [...], "loopback": [...]}`，每项**六格**照 `DeviceEndpoint` 原样给：`name`（端点名，`settings_update` 里配的就是它）/ `host_api`（render 端点没有 host API，恒空串）/ `role`（`input` / `render`）/ `index`（`sounddevice` 的设备序号；render 端点是枚举序）/ `channels` / `is_default`（只有 mic 侧会标，种子是只读的 `sd.default.device[0]`）。

- 两路的键就是两路的 id；同名端点在多个 host API 下会重复出现，界面按 `name` 去重摆（配置按名字挑）。「按默认策略挑」永远可选（键位写 `null` 就是这一档），本端点不必替界面摆这个选项。
- 端点表读不出来时回 **503 `capture_unavailable`**（`detail = {error}`）。
- **会中打开抽屉时清单照给**（枚举不碰任何在飞的东西）；换不换得了由界面按第 6 章判——服务侧 `reprobe` 在 `recording` / `alert` 态本来就回 `already_recording`。

### 4.11 说话人改名：`speaker_rename` 与人名表

记的载体住在这场目录里——`<场次目录>/meeting_speaker_names.json`：`{"schema_version": 1, "names": [{"key": "mic:S01", "name": "老王"}, …]}`。

- **请求**三格都必给：`dir`（校验同 `post_offline` 给 `dir` 那一支，**没有省略语义**；本进程在录的那一场拒，**盘上残档不拒**）；`key` 必须是 4.9 `speakers` 那一格摆出来的键形状之一；`name` 非空。**同一键再改一次就是改名**（覆盖），两个键写同一个名字合法——编号是分开的。
- **成功体**（200）：通用信封 + `speaker: {dir, key, name}`。`revision` 随信封原样带上、保持现值：这只动作只写人名表，快照一个字不动；界面改完当场生效走**重问单场状态**那一跳（4.9 的 `speakers` 格）。
- **表里只存用户确认过的 (编号, 名字) 对**：没起过名的编号不进文件，跳过 = 不起名、不落任何状态。它是一本**纯记录**——注册制声纹明文不做，「应该有谁」的校验同样不做（`docs/design-intent.md` 第 6 条）。**写侧当场拦两样**：键的形状（必须带两路之一作前缀或就是路本身——裸 `S01` 说不出是哪一路，拒）与空名字（跳过与起名在形状上要分得开）；**名字的内容不校验**。人名表写不进去按 `persist_failed`(500) 如实回。
- **读侧只做结构解析**：顶层对象、`schema_version` 主版本号认识、`names` 每项带 `key` / `name` 非空字符串，三条过了就照单全收。缺席或坏了当「没有确认过的名字」——`speakers` 格的名字如实 `null`、提示词里如实渲染「没有确认过的人名」，**不报错、不挡出纪要、不改结论**。
- **记下去之后用在哪**：下一次出纪要，`meeting/minutes/prompt.py` 的 `{{SPEAKER_NAMES}}` 把这张表渲染进编排提示词（确认归确认、推断归推断，两件事在提示词里分开说话）；已生成的 `meeting_final.md` 一个字都不改，改名也**不拉起**任何离线 / 纪要流程。
- **没有 golden 样本**（4.3 末尾那条先例），语义与失败档由 `tests/test_speaker_names.py` 实测。

### 4.12 会议资料：场次目录里的 `meeting_materials/`

资料是给纪要 AI 的 context；`meeting_materials/` 这个目录名是与服务端和纪要提示词两侧共享的固定产品约定——**不设配置键、不可改名**，**首次上传时由服务创建**。资料怎么进纪要见 `docs/minutes.md`。四条端点都挂在 `/api/sessions/{id}/materials` 之下，实现与清洗规则在 `meeting/service/runtime_materials.py`。

**目标目录只认两种**：服务的**当前活跃场次**、注册表里**登记过的场次**；其余一律拒收。寻址失败回 **404** + 通用失败信封：目录已不在原地 → `session_dir_missing`（`detail = {dir, kind: "missing"}`）；目录在、两样都不是 → `invalid_setting`（`detail = {dir, kind}`）；上传缺 `name`（空白也算没给）→ **400** `invalid_setting`（`detail.key = "name"`）。**活跃场次放在最前**：录制中照收——哪怕那一行刚被 `workspace_remove` 摘出清单（只摘名单、文件不动），**资料跟着场次走、不跟着清单行走**。

**上传**（`POST …/materials?name=<文件名>`）：

- 请求体就是文件的原样字节，**流式写盘**（收到多少写多少、不整段进内存，写在工作线程上——事件循环照转，心跳与三个 SSE 通道不被一次大上传按住）。**不转码、不压缩、不限类型、大小不设服务端硬顶**；请求的 `Content-Type` 只是发送方的说法，服务不读也不校验。**`?from=<路径>`**（可选项）：字节不从请求正文来，从盘上那个**已有文件流式拷入**（一块一兆）——选择器文件模式挑中的是盘上路径，拷入归服务做。`from` 给到不存在的文件回 **400 `invalid_setting`**（`detail.key = "from"`），此时盘上什么都不落；给了 `from` 请求正文被忽略，源文件一个字节不动。清洗 / 重名 / 贴上时刻 / 头哨与正文上传同一套。
- `name` **不可信**，服务侧清洗（五步次序固定、逐字确定：只留最后一段路径 → Windows 非法字符与控制字符换 `_` → 掐结尾的点与空格 → 保留设备名前置 `_`，按扩展名前那段判 → 超长截断保留扩展名）。清洗**不报错**：清到空交兜底名 `未命名`；解析结果永远落在资料目录这一层，`session.json` 等场次文件够不着。
- **重名自动加序号后缀、保留用户看到的名字**（`报告.pdf` → `报告 (2).pdf`）；「查完重名到落盘」之间的竞态由独占创建（`xb`）+ 让位重试收口。**不保留原文件时间戳**：落盘那一刻的 mtime 就是「贴上时刻」（`added_at`）。中途失败把**半截文件收走**，不留残骸（盘上出岔 → 500 `persist_failed`；客户端断开同）。成功体（200）：通用信封 + `material: {name, size_bytes, added_at}`。

**清单**（`GET …/materials`）：`{"dir", "materials_dir", "items": [{name, size_bytes, added_at}]}`，按名字排序（casefold），**任何状态下都 200**（寻址失败除外）。**没有旁注清单文件**：目录就是清单，每条三格，不另设 JSON 记录、无备注元数据；`meeting_materials/` 还不在时 `items` 是空数组、**目录也不建**；目录里混进的子目录不冒充一条资料。

**取原文件**（`GET …/materials/{name}`）：原始字节流式读出，`Content-Type` 按扩展名判（标准库 `mimetypes`，认不出给 `application/octet-stream`）——只给界面的提示，服务不校验内容。

**删除**（`DELETE …/materials/{name}`）：**只删那一个文件**（场次目录与资料目录里别的文件一个字节不动；这不是 4.8 的「删除工作区」）。成功体：通用信封 + `material: {name}`。

**取不到 / 删不到**：名字清不出现存的文件（含**重复删除**）→ **404 `material_missing`**，`detail = {name}`——资料这一片自己的 `code`（不在 4.4 那张动作 `code` 表里，因为这四条不走 `/api/actions`）；盘上出岔 → **500 `persist_failed`**，`detail = {error}`。`{name}` 在取 / 删两侧再过一遍与上传同一套清洗，URL 里手写的穿越段剥成裸文件名。

**写入的头哨**：上传与删除两条写端点**不走** `/api/actions` 的 JSON 信封（正文是二进制），`X-Meeting-Service: 1` 由中间件按端点扩到它们身上（`_write_guard_path()` 一张核对表，`POST /api/dirs` 同在表里）：缺头 **403**、不带失败信封。清单与取原文件是纯读，与其余 `GET /api/*` 同档（Host 校验已盖住）。**独立于四态快照**：四条端点都是纯读写文件——不提交、不涨 `revision`、不广播；成功信封里的 `revision` 只是当前版本号。快照九键、列表十二键、4.9 的应答体一个字不动。

### 4.13 稿子正文：`GET /api/sessions/{id}/transcripts`

**任何状态下都 200**（寻址失败除外）；纯读。`{id}` 与失败口径跟 4.9 同一套（`session_dir_missing` / `invalid_setting`）。

**两份稿 × 两路各一份**，文件名是上游冻结的契约（`meeting/minutes/chunk.py` 同一套模板，服务按名字找、不猜）：会中稿 `meeting_transcript_<source>.md`、会后稿 `meeting_offline_transcript_<source>.md`。两份**平级**（谁也不叫「权威」，实测并不保证哪一份更好，`docs/design-intent.md` 第 11 条）；`kinds` 的次序 `live → offline` 与每份里 `sources` 的次序 `mic → loopback` 都是契约。应答体 `{"dir", "kinds": [{"kind": "live|offline", "sources": [{"source", "text"｜{"source", "reason"}}]}]}`：

- **每一路只回答两件事之一**：文件在 → 那格恰好 `source` + `text`，`text` 是正文**原样**（`utf-8` + `errors="replace"`——半个坏字节折成替换字符，不让一份稿把整只端点带走）；文件不在 → `source` + `reason`：`unrecorded`（这一场 `session.json` 的 `mode` 没采这一路——那一截音频从没录过）或 `absent`（采了但盘上没有：会中稿被清 / 会后稿还没生成 / 那路没跑成）。`session.json` 都不在的疑似目录按两路全集答（缺稿落 `absent`，不落 `unrecorded`——判不出的事不猜）。
- **缺稿话归界面翻**（句子归界面，服务只给档位）。**不筛路、不切页**：一次给全四份（正文至多几 MB，本地盘读），界面上两份稿就地切、切换零等待。
- **目录读不动（无权限等）与「文件不在」同一档**：按缺稿如实答，不把端点带走。**不带校验性质的含义**：正文是盘上原文的转手，界面按它显示，不改任何结论。

## 5. 心跳

`GET /api/heartbeat`，**任何状态下都 200**（包括没有正在录的场次、包括收尾进行中）。应答**恰好七字段**：`service`（**恒 `"meeting"`**——端口探测靠这个字面量认亲，判据是「200 + body 含 `"service": "meeting"`」，因此必须存在）/ `server_time`（服务侧此刻的 ISO 时间戳，本地时区、秒精度，掩码 `<iso>`；迟到 / 失联判定以它为准）/ `revision`（当前快照的 `revision`）/ `top_state`（四态之一）/ `recording_alive`（bool｜null，**采集还活着吗**，判据见下）/ `seconds_since_last_tick`（float｜null，距最近一帧事实的秒数，掩码 0；**一帧都没收到过就是 `null`**）/ `capture_gaps`（object，**本场的帧空洞记录**，任何态都在、空记录不给 `null`）。

**`recording_alive` 的判据是服务侧的采集帧时钟**：

- `recording` / `alert` 态给 **bool**；`idle` / `ended` 态给 **`null`**（没有在录的场次，问「还活着吗」没有意义）。窗口 = `max(2.0s, 3 × frame_interval)`，**只认 `LevelFrame`**（`SourceFacts` / `SourceLost` **不刷新这个钟**）——协议默认 `frame_interval = 0.5s` 与 dev 替身的 `0.1s` 算出来都是 **2.0 秒**，即「两秒没收到一帧事实 = 采集停了」。采样后端 `frame_interval` 读不到或不是正数时按 `0.5` 算，只记一条 warning。
- **「还没有第一帧」报 `null`（未知），超窗仍无首帧才报 `false`**：自本场 `start` 起算窗口，首帧正常比 start 晚约 0.5s，窗口内的「一帧都没见过」按未知交出去（界面的 10s 心跳落进这段竞态不会误判成「已停」）；窗口走完仍一帧都没有，如实落 `false`——真的饿死仍然报得出来。这一档里 `seconds_since_last_tick` 照旧 `null`。
- **后端卡住不报任何东西时 `recording_alive` 会算成 `false`，而 HTTP 照常 200**——这正是「只看 HTTP 通不通会漏掉录音线程已卡死」的判据。
- **`seconds_since_last_tick` 与 `recording_alive` 不同源**：前者是「多久没收到帧」的**原始读数**（`round(…, 3)`），后者是**布尔结论**。界面显示「多久没读到数据」用前者，画「采集停了」的红灯用后者。

**`capture_gaps`：本场的帧空洞记录**。录中发生 GIL 饿死 / 整进程冻结时，采帧与判停的线程一起冻住，解冻后 `last_frame_ts` 立刻刷新——`recording_alive` 全程 `true`，内容丢了多少秒在判停那一侧不留痕。空洞记录补的正是这一半「留痕」：

- **检测点在生产侧**：发帧线程每次产帧先与上一拍做差（墙钟不冻，解冻后第一拍就得到真实空洞时长），超过与 `recording_alive` **同一把尺**的窗口记为一次空洞事件、**再**刷新时刻——同一空洞回合只记一条；**首帧前无上一帧，天然不记**。**暂停不构成空洞**（暂停期间电平帧照发）；dev 注入里 `silence`（发的是零电平**帧**）不构成、`stall`（两路整体停发）才构成；单路 `lost` 也不构成（另一路帧照发，断流由 `SourceLost` 的致命级在报，两句话不重复）。
- **粒度 = 「任意存活路」的整档**，记录**不点名哪一路**（冻结冻的是整个进程，两路一起丢，点名没有意义）；两路刻度线同一次冻结各报一条时，服务按**区间重叠**合并为一条，累计秒数只加增量。**`recording_alive` 的语义一个字不动**：它只回答「现在采不采得到」。空洞是**历史事实**，恢复之后 `recording_alive` 回 `true`、空洞照旧在记录里——界面据此在告警区摆一条提示行（「曾中断 X 秒，该段内容未记上」），提示级：不出声、不弹红横幅、不进两拍计数。
- **字段形状（恰好三格）**：`total_count`（本场累计次数，合并过的回合只算一次）、`total_seconds`（本场累计秒数，掩码 0）、`recent`（**最近至多 10 条**，每条 `{at, seconds}`——`at` 是空洞被记录那一刻的墙钟，掩码 `<iso>`；`seconds` 掩码 0；超过 10 条只在两个累计格里留数）。**只在心跳域**：不进快照、不涨 `revision`、不广播；本场 `start` 清空记录；空记录是 `{"total_count": 0, "total_seconds": 0.0, "recent": []}`。

**两个「心跳」绝不串线**（这是最容易踩的一处）：`heartbeat.interval_seconds` / `settings.heartbeat_interval_seconds` 是**界面**发心跳的周期（默认 10 秒）；`max(2.0s, 3 × frame_interval)` = 2.0 秒是**服务侧**采集帧时钟的窗口（`recording_alive` 的判据）。**界面的 10 秒周期绝不参与 `recording_alive` 的计算**；反过来 `recording_alive == false` 也不会让心跳请求失败——它只是一份数据。

**`recording_alive === false` 在界面上要连续两次读数才判「采集已停」**（两拍规则，只在界面层）：单次 `false` 可能是竞态窗口里的误读；连续计数由界面单一归并点登记（`web/src/live/heartbeat.ts` 的 `applyHeartbeatReading()`），任何非 `false` 读数把计数清零；冻结判据与横幅判据都只读这一个数。连续计数**不参与服务侧任何计算**，发声判据也不看它——出声照旧只由失联档与快照上的致命级驱动。

**客户端轮询口径**：心跳由**界面**按配置周期（默认 10 秒、可配 1–60 秒，第 6 章第 5 条）向服务要一次应答，判定用应答里的 `server_time` 作基准。界面侧的四级本地态及其判据、失联时的界面处置、两条「出事了」的话不得同屏、服务重启后的自洽——**正本在 `docs/ui-design.md` 的心跳一节**，此处不再各写一份。

## 6. 客户端义务

下面八条是**界面必须做到的事**，每一条都由服务侧的某条硬约束逼出来。它们是契约的另一半——服务履行它的那一半（推送、判定、掩码），界面履行这一半。

1. **只消费推送，不持本地权威状态。** 状态的唯一权威在服务侧；界面手上那份是它推过来的副本，任何分歧以服务为准。
2. **不用浏览器本地存储（`localStorage` / `sessionStorage` / IndexedDB / Cookie）保存会中状态。** 会中状态一律从服务取，刷新、重开、多开看到的都是同一份；界面自己持久化的只能是**纯界面偏好**（心跳周期、面板展开与否这一类与服务无关的东西）。
3. **SSE 重连后以快照重同步**：服务**不重放**断开期间的事件，重连后第一帧必然是完整 `snapshot`，界面据此**整块替换**；收到 `resync: true` 那一帧同样整块替换。
4. **写请求带必需头**：`POST /api/actions` 必须带 `Content-Type: application/json` **且** `X-Meeting-Service: 1`；会议资料的两条写端点（4.12）与新建目录那条（`POST /api/dirs`）必须带 `X-Meeting-Service: 1`。缺了服务回 403 且**不带失败信封**（没走到端点）——界面的 403 处理要与 4.2 的失败块分开。
5. **心跳按配置周期轮询**：默认 10 秒、可配 1–60 秒（`settings.heartbeat_interval_seconds`）；判定用应答里的 `server_time` 作基准。失联时的界面处置见 `docs/ui-design.md` 的心跳一节。
6. **不自己造派生数据**：包络与 `bar` 由服务算好推过来，界面**不许自己造**；告警的 `muted` 由服务判定，界面**不算任何抑制窗口**、只按布尔值决定响不响。`recording_alive === false` 的连续两次判停计数（第 5 章两拍规则）是**对数据字段的确认计数**、与声音无关：响不响照旧只读 `settings.alarm_sound` 与 `alerts.*.muted` 两个布尔。
7. **按 `code` 分派失败画法、按 `detail` 有哪个键显示哪个键**：九个 `code` 固定、`detail` 的档位（尤其 `capture_unavailable` 四档与 `invalid_setting` 四档）以 4.4 为准，**不按 `message` 做字符串匹配**。
8. **决定响不响的时候，持久化开关与本条告警的 `muted` 一起读。** 两个字段各管一件事：快照 `settings.alarm_sound` 是用户按的「告警出声」开关（持久化，跨场次记住），管**这一台机器要不要出声**；`alerts.*.muted` 由服务侧每次提交时重算，管**这一条告警此刻该不该出声**（会话级静音 ∨ 抑制窗口）。`alarm.sound` 关掉时服务推的 `muted` **仍是 `false`**——服务侧只管「本场静音」与「抑制窗口」两半，持久化开关是界面这一侧的事。响一声的判据是**两个都放行**：`settings.alarm_sound === true` **且** 这一条的 `muted === false`。只读 `muted` 会在开关关掉后照样响，只读 `alarm_sound` 会在抑制窗口里重复响。

## 7. 保留登记：定名但未实现

**这一章列的东西一个都没有实现。** 名字与形状已定名（被讨论过、被引用过），但服务侧没有任何端点、动作或字段承载它们。列在这里是让界面与验收脚本知道「将来会有什么、名字叫什么」，以及复核时一眼确认**没有顺手实现**。**处理方式**：按「能力还没进来」处理——不探测、不留死代码分支、不按这些形状写解析器；哪一条落地了，先做进代码、再回写进第 1–6 章（这一章只管「还没做的」）。

| 定名的东西 | 现状 |
|---|---|
| 快照键 `summary` | **明确不做，也不留位**：总结的结果不进快照——结果是一过性的视图产物（关掉即消失、不落文件），快照是**会议的**权威状态；前端契约层的两道硬门（`.strict()` 与「顶层恰好九键」）正照这条口径立。结果走 `summarize` 的应答体、记录走 `GET /api/summaries`（4.7） |
| 转写历史回看 | **拿不到**：`transcript` 通道只发「订上之后新出的行」，首帧 `snapshot` 也不带转写正文（`last_line_at` 只是个时间戳、`buffered_lines` 是计数）。中途打开的界面只能显示从那一刻起的新行。会中这些行落在 `meeting_transcript_<source>.md`，两份稿都没有读它们的端点 |
| 说话人「跳过编号」 | **仍在这一章**：跳过 = 不起名，界面上如实显示「待确认」，服务侧没有任何状态承载「不认领」这个标记。起名与改名那一半已落地（4.11） |

**会后产物不在本契约里**：会后离线转写与说话人聚类已落地，产物住在**场次目录里的文件**上——`meeting_offline_transcript_<source>.md`（每路一份的会后精细稿，一行 `[HH:MM] **S01**: 正文`）、`meeting_offline_speakers_<source>.json`（类编号与区间对照表）、`meeting_offline_report.md`、`meeting_offline_status.json`，另有**逐块进度记录** `meeting_offline_progress.jsonl`（与那四份并列平放）。**它们没有各自的 HTTP 端点，快照里也没有对应字段**（要看这一场跑到哪一档读 4.9）；要读产物本身按场次目录里的文件读，字段契约在 `meeting/offline/artifacts.py` 与 `meeting/offline/progress.py` 的模块说明。**两路各落一份、两路不合并**（文件名与限定名 `mic:S01` 里始终带着 `source`）。

**会中的音频源切换不做**：要录哪几路在开始前定（`mode`），录音期间不切——会中能做的只有关掉本场在采的那一路与重开它（`set_source`），对不在 `mode` 里的那一路回 `invalid_setting`(400)。

## 8. fixtures 与掩码（附录）

契约的**可机读基准**是 `contract/fixtures/` 下的 golden 样本，一律**出自真实服务路径**、不是手写的。逐档清单（每个文件采的是哪条路径、固定的是什么）、掩码占位符与再生成 / 漂移比对的规则，**全部只写在 `contract/README.md` 一处**，这里不复制品。速记两条：`python -m pytest tests/test_contract_fixtures -q` 就是漂移测试——重走真实服务路径采集样本与盘上 fixtures 逐文件比对，形状一漂移就红；改了服务行为就重跑 `--regen-contract` 并复核差异，fixtures 的 diff 就是契约的 diff。

## 相关文档

- 索引与分册清单：`docs/spec.md`
- 状态与推送的现状叙述（四态与迁移、收尾三步与共享预算、转码独立工序、`session.json`、告警判定、dev 替身）：`docs/state.md`
- 界面设计的唯一权威说明（四态、告警分级、心跳本地态、交互清单）：`docs/ui-design.md`
- 怎么装起、配置键正本、跑测试：`docs/service.md`（log 与退出码的口径在 `README.md`）
- 后端协议与解析入口：`docs/backend-protocols.md`
- golden 样本的出处、掩码规则与再生成办法：`contract/README.md`
