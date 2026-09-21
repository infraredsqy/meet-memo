# meeting · 双端契约

> 这份文档是**服务与界面之间那份契约的正文**：端点的口径、快照 schema、SSE 帧形状、动作信封与 `code`/`detail` 表、心跳语义、客户端义务，以及一份**定名但还没实现**的保留登记。
>
> 读者是接这套服务写界面（或写验收脚本）的人。它与 `docs/api.md`（HTTP 面速查）、`docs/state.md`（状态与推送的现状叙述）讲的是同一件事，但**口径不同**：那两份是叙述，这份是**可机读的基准** —— 每一条都在 `contract/fixtures/` 里有一个 golden 样本，`tests/test_contract_fixtures.py` 每次跑测试都重走一遍真实服务路径逐字比对，服务侧形状一漂移就红。**与叙述性文档冲突时，以代码实际行为为准**，并把权威落在这本册子里。
>
> 代码落点：路由只在 `meeting/service/routes.py` 注册一次；快照的唯一产出形态是 `meeting/state.py` 的 `Snapshot.to_dict()`；SSE 帧由 `meeting/service/hub.py` 编码；动作与 `code`/`detail` 在 `meeting/service/runtime.py`；三道本地防线在 `meeting/service/app.py` 的 `_LocalGuardsMiddleware`；golden 样本与掩码在 `contract/fixtures/` 与 `tests/_contract.py`。
>
> **这本册子的编号**：`docs/contract.md` 的七章要求落在第 1–7 章（总则、快照 schema、SSE、动作、心跳、客户端义务、保留登记）；后面接了三章附录 —— 第 8 章讲 fixtures 与掩码、第 9 章讲复核手法、第 10 章回指索引。**保留登记里的端点与动作一律没有实现**，界面按「能力还没进来」处理。

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

**口径一句话**：**四条 `/api/*` + `/debug` + 静态挂载**。

| 路径 | 方法 | 给什么 |
|---|---|---|
| `/api/state` | GET | 完整快照（第 2 章），`200` |
| `/api/events` | GET | SSE（第 3 章），`text/event-stream` |
| `/api/heartbeat` | GET | 六字段心跳（第 5 章），**任何状态下都 200** |
| `/api/actions` | POST | 动作端点（第 4 章） |
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

**这一档的 fixtures**：`contract/fixtures/snapshot/*.json`（六态快照）、`contract/fixtures/sse/*.json`（五帧）、`contract/fixtures/actions/*.json`（十二个）、`contract/fixtures/heartbeat/*.json`（两个）。

## 2. 快照 schema

顶层**恰好十键**，逐键如下。`GET /api/state` 的 body、SSE 首帧 `snapshot`、SSE 的 `state` 帧三处逐字相同；**重同步**那一帧额外多两键（见 3.5）。

| # | 键 | 类型 | 取值 / 掩码 |
|---|---|---|---|
| 1 | `revision` | int | 进程内单调递增；每次服务启动从同一个起点重新计，**不跨进程延续、不落盘**。**掩码保留真实值** —— 它由固定的迁移与提交次数决定，同样确定 |
| 2 | `service` | object | 六子键，启动时定一次（见下） |
| 3 | `top_state` | string | 四态之一：`idle` / `recording` / `alert` / `ended`。**不新增第五个** |
| 4 | `session` | object \| null | `null`，或 `{id, dir, started_at, mode, elapsed_seconds, frozen}` |
| 5 | `sources` | array | **恰好两项**，次序恒为 `mic`、`loopback` |
| 6 | `alerts` | object | `{critical: Alert｜null, hints: [Alert]}` |
| 7 | `transcript` | object | `{health, last_line_at, buffered_lines, failed_segments}` |
| 8 | `ended` | object \| null | `null`，或 `{phase, steps: [DrainStep], error}` |
| 9 | `settings` | object | `{alarm_sound, heartbeat_interval_seconds, asr{…}, devices{}}` |
| 10 | `notice` | object \| null | `null`，或 `{kind, text, session_dir, log_path}` |

`service` 六子键：`started_at`（ISO，**掩码 `<iso>`**）/ `version`（非空字符串）/ `port`（int，**掩码 `<port>`**）/ `url`（**掩码 `<url>`**）/ `log_path` 与 `out_root`（**掩码 `<path>`**）。`port` 与 `url` **只能由监听 socket 的 `getsockname()` 读回值产生**，绑定之前取快照一律抛错 —— 不留退化的空值、不拿配置值或字面量顶替。

`session` 六子键：`id` 是**场次目录名**（`YYMMDD` 形状，**掩码 `<path>`**）/ `dir` 是场次目录路径（**掩码 `<path>`**）/ `started_at`（ISO，**掩码 `<iso>`**）/ `mode ∈ both｜mic｜loopback`（开场前定死，录音期间不切）/ `elapsed_seconds`（**掩码 0**）/ `frozen`（bool）。

- `elapsed_seconds` 在 `recording` / `alert` 态走秒（每次取快照现算、`round(…, 3)`），**进 `ended` 就冻结**（`frozen: true`，此后不再动）。
- **`top_state` 停在 `ended` 而那场的 `session` 还留着**（成功档与失败档都留）。直下一场 `start` 才换。

`sources[*]` **九键**：`id`（`mic` / `loopback`，**不掩码**：它是取值域字面量）/ `label`（`本地` / `远端`）/ `device_name`（string \| null）/ `health` / `lost_since`（ISO \| null，**掩码 `<iso>`**）/ `restored`（bool）/ `level_dbfs` 与 `peak_dbfs`（number \| null，**掩码 `<dbfs>`**）/ `envelope`（number 数组，**逐根掩码 `<dbfs>`**，长度不变）。

- `health` **七取值**：`unprobed` / `ready` / `probe_failed` / `capturing` / `lost` / `restored` / `stopped`。前三档由启动时的设备探测直取（后端 `probe()` 只许报这三档），后四档由状态迁移写值。
- `envelope` 是最近 30 秒的电平包络：**15 根柱、一根 2 秒**。一根柱的值 = 它那 2 秒窗口内所有 `LevelFrame.peak` 的最大值；窗口里一帧都没有就沿用左边那根。**录音中（乃至 `ended` 停留档）长度恰为 15**，会话开场（还没开始录）是**空数组 `[]`**。这根柱由服务算，**前端不许自己造**。
- 两路的位置是固定的：`sources[0]` 恒为 `mic`、`sources[1]` 恒为 `loopback`。

`alerts`：`critical` 是 `Alert｜null`（**致命级同时只留一条**），`hints` 是 `Alert` 数组（**同类合并、上限 3**）。`Alert` **六字段**：`{level, source, since, duration_seconds, message, muted}`。

- `level ∈ critical` / `hint`；`source` 是那一路的 id（提示级现在也用 `source = transcript`）。
- `since` 是 ISO（**掩码 `<iso>`**）；`duration_seconds` **掩码 0**。
- `muted` 说的是**这一条现在会不会出声**（会话级静音生效期间，或这一条还在抑制窗口里）。判定与窗口计算**全在服务侧**：界面只看这个布尔值，`false` 就响一声、`true` 就不出声、界面不算任何窗口。
- **心跳失联那条致命级由界面自己判、自己画**，服务侧不产生它。

`transcript`：`health ∈ normal｜behind｜stopped`（直接来自转写后端）；`last_line_at` 是 ISO \| null（**掩码 `<iso>`**）；`buffered_lines` 是积压段数、`failed_segments` 是累计识别失败段数 —— **这两个计数只进 log 与 `/debug`，产品界面不摆它们**。骨架阶段（没有转写后端）恒为 `normal` + 0。

`ended`：`phase ∈ draining｜done`（**`failed` 不是这个字段的取值** —— 它属于盘上 `session.json` 的 `state`，在快照里没有对应位置）；`steps` 是四步各一条，次序恒为 `flush` / `transcode` / `log` / `post`；`error` 是 string \| null。

`DrainStep` **四键齐**：`{id, status, detail, error}`。`id` 取上面四个字面量之一；`status ∈ waiting｜running｜done｜failed｜skipped`；`detail` 是给人看的一句话（string，可为空串）；`error` 是 string \| null（**`error` 键恒在**，没有错误时就是 `null`）。

- 收尾失败后 `phase` **保持 `draining`**、`ended.error` 是那一刻的原因（与失败步的 `error` 同文），这份摘要与 `session` 一起留在快照里。
- 成功档收尾的副作用会把两路 `health` 从 `stopped` 带回 `unprobed`；失败停留档**保留 `stopped` 现场**。

`settings` 四键：`alarm_sound`（bool）、`heartbeat_interval_seconds`（int，界面发心跳的周期）、`asr`（五子键，与配置键 `asr.*` **逐字同名**、只去掉前缀）、`devices`（**现在是空对象 `{}`**，子键等 `dca4539b` 定名后补，见第 7 章）。

- `asr` 五子键：`max_segment_seconds` / `timeout_seconds` / `max_new_tokens` / `repeat_max_unit` / `repeat_min_coverage`。
- 这里读到的是**运行期生效值**。非热读的键改了要重启才在快照里可见（第 4 章）。

`notice`：现在只有 `kind = "restart_unfinished"` 一种。`text` 是一句完整的话（「服务重启过。上一场的收尾没走完，音频在 <场次目录>，日志在 <服务级 log 路径>。」）；`session_dir` 指没收完的那一场（**掩码 `<path>`**）；`log_path` 指**本次启动的服务级日志**（与 `service.log_path` 同一处取值，**不是场次日志**；掩码 `<path>`）。

**`revision` 的语义**：递增与广播只经 `ServiceRuntime.commit()` 一处。同一次事实处理里的多次改动合并成一次 `+1`、一次全量快照；被状态机拒绝的动作**不涨 `revision`、不写盘、不广播**。界面拿它作「我手上这份是最新的吗」的参照，**不许自己算它**。

### resync 帧额外的两键

慢消费者重同步那一帧（第 3.5 节）在顶层十键之上**额外多两键**：

| 键 | 类型 | 取值 |
|---|---|---|
| `resync` | bool | 恒 `true` |
| `dropped` | int | ≥ 1：丢掉多少条之后补的这一份 |

顶层键集合因此恰好是**十二键**（十键 + `resync` + `dropped`）。**界面据此判断「我漏了一段」**：见到 `resync: true` 就整块换成这份快照，不要试图补齐中间过程。

**这一档的 fixtures**：`contract/fixtures/snapshot/{idle,idle-notice,recording,alert,ended-done,ended-draining}.json`、`contract/fixtures/sse/resync-snapshot.json`。

## 3. SSE：四事件与重连语义

`GET /api/events`，应答 `Content-Type: text/event-stream`，另带 `Cache-Control: no-store`、`X-Accel-Buffering: no`、`Connection: keep-alive`。

**连接即快照**：建立连接后第一帧**必须是 `snapshot`**（完整状态），新开的页面直接落在当前状态、**不经过 `idle`**。首帧时序上有一条硬要求：`stream()` 里「取快照」与「注册订阅」处在**同一段同步代码**、两者之间**不出现 `await`** —— 于是「注册前发生了一次状态变更 → 首帧落后于订阅时刻」的缝不存在。

**开头的 `retry`**：连接后先发一帧 `retry: 2000`（毫秒），再发首帧 `snapshot`。断线由浏览器自己按这个间隔重连。

### 3.1 四个事件的帧形状

帧格式一律 `event: <名字>` + `data: <一个 JSON 对象>`；`state` / `snapshot` **多一行 `id: <revision>`**，放在 `event:` 之前。JSON 用 `ensure_ascii=False` 序列化（中文原样出），帧尾是两个换行。

| 事件 | 什么时候发 | 带 `id:` 吗 | `data` 形状 |
|---|---|---|---|
| `snapshot` | 建连首帧；慢消费者重同步 | **带**，值 = 快照的 `revision` | 完整快照（重同步时多 `resync` / `dropped` 两键） |
| `state` | 每次状态变更（**全量快照，不做增量**） | **带**，值 = 快照的 `revision` | 完整快照（第 2 章十键） |
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
| `ts` | number | 后端给的秒数（**语义归后端**，服务只透传；fixtures 里那个 `1.5` 是假转写后端的固定值） |
| `source` | string | 哪一路（`mic` / `loopback`） |
| `text` | string | 这一行的正文 |
| `kind` | string | `line` 或 `gap`（断口） |

一行到来时服务侧只更新 `transcript.last_line_at` 并推这一帧，**不参与状态迁移、不涨 `revision`**。

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
{"action": "start", "args": {"mode": "both"}}
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

### 4.3 五个动作的语义

| 动作 | 语义 | `args` |
|---|---|---|
| `start` | 开一场：建场次目录 → 写第一版 `session.json` → 起采集 → 进 `recording`（`idle` 与 `ended` 都能出发） | `mode`：`both` / `mic` / `loopback`，**缺省 `both`** |
| `stop` | 结束本场：冻结时长 → 进 `ended·draining` → 服务按四步收尾跑到终态 | 无 |
| `mute_session` | 本场不再出声：`alerts.*.muted` 立刻变 `true`。**不改**持久化开关 `alarm.sound`，**下一场 `start` 自动复位** | 无 |
| `settings_update` | 写配置文件（原子写），键态两分见 4.6 | 「配置点号键名 → 值」的扁平映射 |
| `dev_inject` | 注入故障（**只在 dev 采集替身在场时可用**） | `source` / `fault` / `after_seconds` / `duration_seconds` |

**`start` 的失败顺序是固定的**：先判态（`already_recording` / `draining`）→ 再校验 `args`（`mode` 非法 → `invalid_setting`）→ 再判后端可用性（`capture_unavailable`）→ 最后才建目录写文件。**态不对或后端不可用时盘上不留任何场次目录。**

**`stop` 成功后 `revision` 会继续往上走**（收尾四步各自提交一次），`ended.phase` 最终落在 `done`（四步全部 `done` / `skipped`、无 `failed`）或**保持 `draining`**（有 `failed` 步，或收尾被取消）。**`ended` 不回到 `idle`**：下一场的 `start` 从 `ended` 直接进 `recording`。

**`dev_inject` 的三档故障**：`silence`（这一路电平落到阈值以下）/ `stall`（两路事实帧整体停发，HTTP 照常应答）/ `lost`（直接来一条 `SourceLost`）。`after_seconds` 到点生效（可为 0），`duration_seconds` 到点恢复（正数，可省，**缺省 30.0 秒**）。非 dev 后端回 `action_unavailable`(404)；没有正在进行的场次回 `not_recording`(409)。

### 4.4 `code` 表与逐档 `detail` schema

七个 `code`，一个不多一个不少：

| `code` | HTTP | 意思 | 谁会回它 |
|---|---|---|---|
| `already_recording` | 409 | 已经有一场在录 | `start` |
| `draining` | 409 | 上一场还在收尾（或收尾失败后停留、或启动扫描到的那一场没收完），这场不能开 | `start` |
| `capture_unavailable` | 503 | 采集后端不可用：没接入、或设备起不来 | `start` |
| `not_recording` | 409 | 现在没有在录的场次 | `stop` / `mute_session` / `dev_inject` |
| `invalid_setting` | 400 | 参数或键名非法（校验都在动手之前） | 各动作 |
| `persist_failed` | 500 | 配置写盘失败，这次改动一处都没生效 | `settings_update` |
| `action_unavailable` | 404 | 这个后端没有这个动作 | `dev_inject`（非 dev 后端） |

**逐档 `detail` schema**（`detail` 恒是对象；下面写「空」的就是**空对象 `{}`**）：

| `code` | `detail` 形状 | golden 样本 |
|---|---|---|
| `already_recording` | `{session_id, dir, elapsed_seconds}` —— 场次目录名 / 场次目录路径 / 已录秒数（掩码 0） | `actions/fail-already-recording.json` |
| `draining` | `{session_id, dir, failed_steps: [{id, error}], log_path}` —— 没有失败步时 `failed_steps` 是**空数组** | `actions/fail-draining.json` |
| `capture_unavailable` | **三档**：空对象；或 `{session_id, dir, error}`（第一版 `session.json` 写不进去）；或 `{session_id, dir, reason}`（采集起不来） | `actions/fail-capture-unavailable.json` |
| `not_recording` | 空 | `actions/fail-not-recording.json` |
| `invalid_setting` | **三档**：`{key, value, legal}`（值非法；`legal` 是合法取值数组）；或 `{source, fault, error}`（`dev_inject` 的取值非法）；或空。**未知动作名是第四档**：`{action, known}`（`known` 是五个动作名的数组） | `actions/fail-invalid-setting.json`、`actions/fail-invalid-setting-unknown-action.json` |
| `persist_failed` | `{error}` | `actions/fail-persist-failed.json` |
| `action_unavailable` | 空 | `actions/fail-action-unavailable.json` |

**`capture_unavailable` 的三档怎么分**：`start` 时采集后端**压根没接入**（`null` 替身直接抛 `CaptureUnavailable`）→ 空对象；第一版 `session.json` **写不进去** → `{session_id, dir, error}`（`error` 是 `类型名: 消息`）；采集后端 `start()` **起不来** → `{session_id, dir, reason}`（`reason` 是后端给的那句人话）。三档都是 503，**界面的正确姿势是按 `code` 选画法、把 `detail` 有哪个键就显示哪个键**。

**`invalid_setting` 的四档怎么分**：`key` 在 → 显示「这个键/参数不合法，合法取值是 …」；`source` 在 → 显示「注入的取值不对」；`action` 在 → 显示「不认识的动作名」并列出 `known`；一个键都没有 → 显示 `message` 就够了。

**`failed_steps[*]`** 恰好两键 `{id, error}`（`id` 是四步字面量之一、`error` 是那一刻的原因串）。界面把每一条列出来 + 服务级 log 路径（`log_path`）就够画「收尾失败块」了。

### 4.5 逐动作 × 逐态的可用性

| 动作 | `idle` | `recording` | `alert` | `ended`·`done` | `ended`·`draining` |
|---|---|---|---|---|---|
| `start` | 200；**扫到未收尾场次的 `idle` 例外 → 409 `draining`** | 409 `already_recording` | 409 `already_recording` | 200（从 `ended` 直接进 `recording`，顺手清掉上一场的 `ended` 摘要） | 409 `draining` |
| `stop` | 409 `not_recording` | 200 | 200（进 `ended` 不必先回 `recording`） | 409 `not_recording` | 409 `not_recording` |
| `mute_session` | 409 `not_recording` | 200 | 200 | 409 `not_recording` | 409 `not_recording` |
| `settings_update` | 200 | 200 | 200 | 200 | 200 |
| `dev_inject` | 409 `not_recording` | 200（非 dev 后端 404） | 200（非 dev 后端 404） | 409 `not_recording` | 409 `not_recording` |

`settings_update` 写的是配置文件，与「这一场录到哪儿」无关，因此**任何状态下都可用**。`mute_session` 只在本场收尾之前可用 —— 收尾完成后本场已无可静音，复位发生在下一场 `start`。

### 4.6 `settings_update`：21 键清单与两档

请求的 `args` 就是**「配置点号键名 → 值」的扁平映射**，只有这一层、不按键前缀分组：

```json
{"action": "settings_update", "args": {"asr.timeout_seconds": 30}}
```

**成功体与别处不同**（**没有 `message` 字段**，需要给人看的话由界面按两个数组自己组织）：

```json
{"ok": true, "revision": 3, "applied": ["asr.timeout_seconds"], "pending_restart": []}
```

**合法键名是逐条列出的 21 个**（不按前缀归组、没有前缀白名单）。表外的任何键名一律 `invalid_setting`(400) 且 `detail = {key, value, legal}`；**`devices.*` 里的名字也在表外**（见第 7 章）。

| 归类 | 键（个数） | 为什么 |
|---|---|---|
| `applied` | `asr.max_segment_seconds` / `asr.timeout_seconds` / `asr.max_new_tokens` / `asr.repeat_max_unit` / `asr.repeat_min_coverage`（**5 个**） | 这五个是**热读键**，每段重新读，写盘成功就立刻生效 |
| `pending_restart` | `web.port` / `web.dist_dir` / `output.root` / `log.level` / `log.dir` / `log.max_bytes` / `log.backup_count` / `alert.silence_dbfs` / `alert.silence_seconds` / `alert.recover_seconds` / `alert.alarm_suppress_seconds` / `alarm.sound` / `heartbeat.interval_seconds` / `capture.backend` / `transcriber.backend` / `post.hook`（**16 个**） | 这些值在**启动时读一次**，写盘成功但要重启才生效 |

两个数组里也是同一套点号键名，次序**照本次请求里的键逐条走**。

- **两个数组的判据只有一条**：该键在键态表里归哪一档。**「写盘成功」是前提** —— 写盘失败整笔回 `persist_failed`(500)，一处都不生效。
- **`alarm.sound` 归 `pending_restart`（已登记的偏离）**：设计稿（`docs/ui-design.md` 4.5 一节的交互清单）说勾选「立即生效」，而**服务侧的实际行为是「写盘成功、但要重启才改运行期行为」**，写盘时主 log 里留一行「本场出声开关仍取启动值」。**以服务侧行为为准**；这条偏离登记在此、待复核，界面上按「改了要重启」的现状说明（例如在开关旁给一句提示），**不要**按 `applied` 画成立即生效。
- **只写用户配置文件里已有的键 + 本次改动的键**，不把代码默认值落盘 —— 一次 `settings_update` 不会凭空长出一份写着全部默认值的配置文件。
- **校验在写盘之前**：至少落地方案点名的「超时必须大于段长」（`asr.timeout_seconds > asr.max_segment_seconds`，没改到的那一半取生效配置的当前值），违规回 `invalid_setting`(400) 且 `detail = {key, value, legal}`。
- **被环境变量压着的键不改分类**：`asr.*` 照样进 `applied`、`output.root` 照样进 `pending_restart`；「被环境变量压着」这件事只在主 log 留一条 warning（键名 + 写盘的值 + 压住它的环境变量名），运行期仍由环境变量说话。
- **配置文件本身与请求同位**：`~/.config/meeting/config.json` 里也只有一层点号扁平键名（例如 `{"alert.silence_seconds": 3}`）。**嵌套形状不合法**。
- **热读键生效之后 `revision` 会 `+1`**（服务侧重取一次生效值视图并提交）；全是 `pending_restart` 的改动**只写盘、不提交**，因此 `revision` 不动 —— 界面别拿 `revision` 当「设置改没改成功」的判据，看 `ok` 与两个数组。

**这一档的 fixtures**：`contract/fixtures/actions/{start-ok,stop-ok,mute-session-ok,settings-update-ok}.json` 与七个 `fail-*.json`。

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

**服务重启后的自洽**：服务重启后界面重新打开时向服务取状态；服务侧**没有**正在进行的会话，界面就停在 `空闲`。若上一场没走完收尾就被打断（`notice.restart_unfinished` 在），空闲态顶部给一行提示（正文就是 `notice.text`）。**界面不自己记状态、不从浏览器本地存储里恢复任何东西**（第 6 章）。

**这一档的 fixtures**：`contract/fixtures/heartbeat/idle.json`（两字段 `null`）、`contract/fixtures/heartbeat/recording.json`（`recording_alive` 为 `true`、`seconds_since_last_tick` 掩码 0）。

## 6. 客户端义务

下面七条是**界面必须做到的事**，每一条都由服务侧的某条硬约束逼出来。它们是契约的另一半 —— 服务履行它的那一半（推送、判定、掩码），界面履行这一半。

1. **只消费推送，不持本地权威状态。** 状态的唯一权威在服务侧（`ServiceRuntime` 手里的那一份快照）。界面手上那份是**它推过来的副本**，任何分歧以服务为准。
2. **不用浏览器本地存储（`localStorage` / `sessionStorage` / IndexedDB / Cookie）保存会中状态。** 会中状态（哪一态、时长、告警、静音、转写）一律从服务取；刷新、重开、多开看到的都是同一份。界面自己持久化的只能是**纯界面偏好**（例如心跳周期、面板展开与否这一类与服务无关的东西）。
3. **SSE 重连后以快照重同步**：服务**不重放**断开期间的事件（没有 `Last-Event-ID` 机制），重连后拿到的第一帧必然是完整 `snapshot`，界面据此**整块替换**。收到 `resync: true` 那一帧同样整块替换。
4. **动作带两个必需头**：`POST /api/actions` 必须带 `Content-Type: application/json` **且** `X-Meeting-Service: 1`，缺任一条服务回 403 且**不带失败信封**（它没走到端点）。界面的 403 处理要与第 4.2 节的失败块分开。
5. **心跳按配置周期轮询**：默认 10 秒、可配 1–60 秒（`settings.heartbeat_interval_seconds`）。判定用应答里的 `server_time` 作基准；失联按 5 章的处置做（冻结 + 压灰 + 横幅）。
6. **不自己造派生数据**：包络（`envelope`）与 `bar` 由服务算好推过来，界面**不许自己造**；告警的 `muted` 由服务判定，界面**不算任何抑制窗口**、只按布尔值决定响不响。
7. **按 `code` 分派失败画法、按 `detail` 有哪个键显示哪个键**：七个 `code` 固定、`detail` 的档位（尤其是 `capture_unavailable` 三档与 `invalid_setting` 四档）以第 4.4 节为准，**不按 `message` 做字符串匹配**。

## 7. 保留登记：定名但未实现

**这一章列的东西一个都没有实现。** 它们已经**定名**（名字与形状在节点里讨论过、被引用过），但服务侧眼下没有任何端点、动作或字段承载它们。列在这里的目的有两个：界面与验收脚本知道**将来会有什么、名字叫什么**；以及复核时能一眼确认**这一轮没有顺手实现**。

**处理方式**：界面与脚本按「能力还没进来」处理 —— 不要探测、不要留死代码分支、不要按这些形状写解析器。等各自归属的节点落地之后，它们**先做进代码、再回写进本文件第 1–6 章**（这一章只管「还没做的」）。

| 定名的东西 | 形状（草案） | 归属节点 | 现状 |
|---|---|---|---|
| 动作 `summarize` | `args` 含 `window`：`"5m"` / `"15m"` / `"30m"` / `"full"` | 手动总结节点 | **不在 `ACTION_NAMES` 里**：打这个动作名回 `invalid_setting`(400) 且 `detail = {action, known}`（`known` 里没有它） |
| 快照键 `summary` | 草案：`null`，或 `{status, window, started_at, text, error}`；**最终形状由实现节点回写** | 手动总结节点 | **不在顶层十键里**：`to_dict()` 不产出这个键，顶层键集合恰好是第 2 章那张表 |
| `GET /api/summaries` | 本场总结记录（按过的几次总结：时间 + 窗口 + 那几条） | 手动总结 / 会后节点 | **路由没有注册**：`all_routes()` 只有四条 `/api/*` + `/debug`，因此这个路径落到末端 404（回 `Not Found`） |
| `GET /api/sessions` | 场次列表（会后视图的会议选择器） | 会后节点 | 同上：未注册，末端 404 |
| `GET /api/sessions/{id}` | 单场状态（会后视图的进度区） | 会后节点 | 同上：未注册，末端 404 |
| 动作 `reprobe` | 重新枚举并试开设备 | `dca4539b`（对齐后启用） | **不在 `ACTION_NAMES` 里**：同 `summarize`，回 `invalid_setting`(400) |
| `settings_update` 键位 `devices.mic` / `devices.loopback` | 持久化哪一路用哪个设备 | `dca4539b`（对齐后启用） | **在键表之外**：21 个合法键名里没有它们，打过来回 `invalid_setting`(400) 且 `detail = {key, value, legal}`；快照 `settings.devices` 眼下是**空对象 `{}`**（位置留着，子键等定名） |
| 转写历史回看 | 中途订阅能不能拿到已经出过的转写行 | **待下游决断（`9e5db89c` / `070b31e0`）** | **现状是拿不到**：`transcript` 通道只发「订上之后新出的行」，连接时的首帧 `snapshot` 也不带转写正文（`transcript.last_line_at` 只是个时间戳、`buffered_lines` 是计数）。界面在会话中途打开时，**只能显示从那一刻起的新行**，拿不到已有行 |
| 说话人名字落盘 / 跳过编号 | 用户给某个编号起名、或标记「这个编号不认领」 | **会后管线（`41465462` / `7c6952e5`）** | **本契约不覆盖**：这是会后产物那一侧的事，服务侧的会中契约里没有对应字段 |

**「暂停」不做，不留位。** 需求锚里没有任何一条要暂停，而「暂停」恰好就是 R2/R4 要消灭的那种「看起来在录、实际没录」的状态（`docs/ui-design.md` 第 5 章明文）。因此本契约里**没有** `pause` / `resume` 动作、**没有**快照里的 `paused` 之类字段，**也不为将来留占位**。同理，**不做会中的音频源切换**：要录哪几路在开始前定（`mode`），录音期间不切。

## 8. fixtures 与掩码（附录）

契约的**可机读基准**是 `contract/fixtures/` 下的 25 个 golden 样本。它们一律**出自真实服务路径**，不是手写的。规则、出处与再生成办法写在 `contract/README.md`；这里给读者一份速查。

| 目录 | 样本 | 对应本文件 |
|---|---|---|
| `snapshot/` | `idle` / `idle-notice` / `recording` / `alert` / `ended-done` / `ended-draining`（**6**） | 第 2 章 |
| `sse/` | `first-snapshot` / `state` / `levels` / `transcript` / `resync-snapshot`（**5**） | 第 3 章 |
| `actions/` | 成功 `start-ok` / `stop-ok` / `mute-session-ok` / `settings-update-ok`，失败七 `code` 各一 + 未知动作档（**12**） | 第 4 章 |
| `heartbeat/` | `idle` / `recording`（**2**） | 第 5 章 |

**掩码（`tests/_contract.py` 的 `normalize()`）**：fixtures 由真实服务采集，采集里的墙钟、路径、端口、电平每次都不同，落盘前一律换成**自描述占位符**：

| 掩码的对象 | 占位符 / 处理 |
|---|---|
| ISO 时间戳（`started_at` / `since` / `lost_since` / `last_line_at` / `server_time` …） | `<iso>` |
| 路径（`log_path` / `out_root` / `dir` / `session_dir`，以及自由文本里内嵌的绝对路径） | `<path>` |
| **场次目录名**（`session.id` 与动作失败 `detail.session_id` 的裸 `YYMMDD` / `YYMMDD-N`） | **`<session-id>`** |
| 端口 | `<port>` |
| 服务 URL | `<url>` |
| 电平（`level_dbfs` / `peak_dbfs` / `dbfs` / `peak` / `bar`） | `<dbfs>` |
| `envelope` 数组 | **只保结构与长度**：长度不变、逐根换 `<dbfs>`（`bar == envelope[-1]` 的结构断言仍成立） |
| 走秒的量（`elapsed_seconds` / `duration_seconds` / `seconds_since_last_tick`） | `0` |
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

想手工确认「这份契约与代码一致」时，四条够用：

1. **抽一个快照 fixtures 与 `Snapshot.to_dict()` 逐字段对照**：`meeting/state.py` 的 `to_dict()` 是唯一产出形态，顶层十键、`session` 六子键、`sources` 九键、`DrainStep` 四键都能逐行对上。`ended` / `session` / `notice` 三个 `null` 档在 `idle` 与 `idle-notice` 两个样本里各能看一次。
2. **故意改坏一个键名跑比对测试，确认会红**（验完还原）：例如把 `contract/fixtures/snapshot/recording.json` 里的 `"top_state"` 改成 `"topState"`，`python -m pytest tests/test_contract_fixtures.py -q` 应当报出「与实时采集不一致」。
3. **任选两条 `detail` 与 `meeting/service/runtime.py` 的实际路径对照**：`already_recording` 三键在 `_already_recording_error()`、`draining` 四键在 `_draining_error()` / `_draining_error_for_dir()`（后者从盘上那一版读 `failed_steps`）。
4. **确认保留登记章里没有任何端点被顺手实现**：`python -c "from meeting.service.routes import all_routes; print([r.path for r in all_routes()])"` 应当只打出五条路径（四条 `/api/*` + `/debug`）；`ACTION_NAMES` 应当只有五个名字（`summarize` / `reprobe` 不在其中）。

## 10. 相关文档

- 索引与分册清单：`docs/spec.md`
- HTTP 面速查（端点、动作表、`code` 表、端口与防线）：`docs/api.md`
- 状态与推送的现状叙述（四态与迁移、收尾四步与共享预算、`session.json`、告警判定、dev 替身）：`docs/state.md`
- 界面设计的唯一权威说明（四态、告警分级、心跳本地态、交互清单）：`docs/ui-design.md`（第 4.5 章心跳口径、第 5 章交互清单）
- 怎么起服务、配置键、log、退出码、跑测试：`docs/service.md`
- 后端协议与解析入口：`docs/backend-protocols.md`
- golden 样本的出处、掩码规则与再生成办法：`contract/README.md`
