# meeting · HTTP 面

> 这份文档写服务对外的那几条线：十五条端点（十三条 `/api/*` + 静态首页 `GET /` + `/debug`）、动作的信封、动作表与 `code` 表、以及本地防线。
>
> 读者是接这套 API 的人：前端、验收脚本、命令行复核。四态状态机、快照字段、`session.json`、SSE 帧形状、告警判定写在 `docs/state.md`；怎么起服务写在 `docs/service.md`。
>
> 代码落点：路由只在 `meeting/service/routes.py` 注册一次；动作的实现与 `code` 表在 `meeting/service/runtime.py`（`ServiceRuntime` 的方法体按职责拆在各 `runtime_*.py` mixin 模块里，`runtime.py` 只留构造、状态权威核心与 re-export，下同）。

## 章节目录

| 章 | 标题 |
|---|---|
| 1 | 十五条端点 |
| 2 | 请求信封与应答形状 |
| 3 | 动作表与 `code` 表 |
| 4 | `settings_update`：写哪些键、什么时候生效 |
| 5 | 端口与本地最低防线 |
| 6 | 复核时怎么打 |

## 1. 十五条端点

| 端点 | 给什么 |
|---|---|
| `GET /` | 构建产物（`web.dist_dir`，默认 `web/dist`）有内容就是那份 React 应用；目录不存在就给一页说明（**存在性按每请求判定**，产物一建好刷新即可见，不必重启服务）；**目录在但是空的回 404**。三档与说明页文案的全文见 `docs/contract.md` 第 1 章 |
| `GET /debug` | 骨架自检页：读原始快照、按开始 / 结束 / 静音、看事件流、看心跳原始应答。零依赖、无构建，页面内显式标注**非产品界面、不承担任何告警职责** |
| `GET /api/state` | 完整状态快照，`200`。body 与 SSE 首帧那份 `snapshot` 同源（同一个 `to_dict()`） |
| `GET /api/events` | SSE。**第一帧一定是 `snapshot`**，之后是 `state` / `levels` / `transcript` 三条通道 |
| `GET /api/heartbeat` | `{"service": "meeting", "server_time", "revision", "top_state", "recording_alive", "seconds_since_last_tick", "capture_gaps"}`，`200`，任何状态下都给。`service` 这个字面量是端口探测认亲用的，必须有 |
| `GET /api/summaries` | 本场按过的、成功产出的那几次会中总结（时刻 + 窗口 + 那几条），`200`，任何状态下都给。记录活在服务侧内存里、**不落文件**，新的一场 `start` 清空 |
| `GET /api/sessions` | 工作区注册表：软件自己记的那份"我这些会"（住在配置目录 `workspaces.json`、按路径去重），`200`，任何状态下都给；`recent` = 最近一次被打开/切换的那一场；每行带这一场各段的时长与时间（`segments`，续接过的会看得出接过）；顶层还有一格 `post_job`（此刻在飞的那趟会后，`null` = 没有）—— 界面那格全局「显卡忙不忙」的会后一半读数。**纯读**，形状与现行口径见 `docs/contract.md` 4.8 |
| `GET /api/sessions/{id}` | 单场状态：这一场此刻跑到哪一档、各段的时长与时间（`segments`，与清单同一来源）、会后离线跑到第几块 / 共几块、正在跑哪一路、已经跑了多久，`200`。`{id}` 是**这一场的目录路径**（URL 编码的绝对路径）。**纯读**，形状与失败口径见 `docs/contract.md` 4.9 |
| `GET /api/dirs` | 目录选择器的一个现场：一层子目录的名目判读（起点用户主目录，`?dir=` 换层），`200`。**纯读**，只挑已有目录、不建目录；`?files=1`（资料界面的选择器文件模式，160d1a78）时应答体多一个 `files` 键——这一层的普通文件每项 `{name, dir, size_bytes}`，与 `dirs` 分开两列 |
| `GET /api/devices` | 两路各自的候选设备端点表（`{mic: [...], loopback: [...]}`，每项六格），`200`，任何状态下都给。**纯读、只枚举、不开任何设备**（`probe()` 会短暂试开，清单不走它）；设置抽屉的两个设备下拉读它 |
| `POST /api/actions` | 动作端点，见第 2–4 章 |
| `GET /api/sessions/{id}/materials` | 会议资料的清单：`{dir, materials_dir, items:[{name, size_bytes, added_at}]}`，`200`。**目录就是清单**（没有 JSON 记录）；`meeting_materials/` 还不在时 `items` 是空数组、**目录也不建**（首次上传才由服务创建）。**纯读**。资料层整体见第 3 章末尾那一节 |
| `POST /api/sessions/{id}/materials?name=<文件名>` | 上传：请求体就是文件的原样字节，**流式写盘**（不转码、不压缩、不限类型、大小不设服务端硬顶）；`name` 必给、不可信（服务侧清洗防路径穿越），重名自动加序号后缀。成功体 = 通用信封 + `material: {name, size_bytes, added_at}`。二进制正文装不进 JSON 动作信封，这是与 `POST /api/actions` 并列的写端点（头哨见第 5 章）。`?from=<路径>`（选择器文件模式，160d1a78）：字节改从盘上那个已有文件流式拷入，`from` 给到不存在的文件 400 `invalid_setting`（`detail.key = "from"`）；清洗 / 重名 / 头哨同一套，源文件一个字节不动 |
| `GET /api/sessions/{id}/materials/{name}` | 取原文件：原始字节 + 按扩展名判的 `Content-Type`（认不出给 `application/octet-stream`），从盘上流式读出 |
| `DELETE /api/sessions/{id}/materials/{name}` | 删除：只删那一个文件。成功体 = 通用信封 + `material: {name}` |

`{id}` 一律是**这一场的目录路径**（URL 编码的绝对路径），与 `GET /api/sessions/{id}` 同一个写法。

`/api/*` 的应答一律带 `Cache-Control: no-store`，并且**不发 `Access-Control-Allow-Origin`**（不开 CORS，前端与本服务同源）。

## 2. 请求信封与应答形状

请求：

```json
{"action": "start", "args": {"mode": "both", "dir": "C:\\会议\\跟老王聊周会"}}
```

- **两个必需请求头**：`Content-Type: application/json` **且** `X-Meeting-Service: 1`。缺任一条回 **403**（挡表单式简单请求）。这道校验在中间件里，先于路由匹配生效。
- 成功：`200 {"ok": true, "revision": N}`。
- 失败：`4xx {"ok": false, "code": "...", "message": "...", "detail": {...}}`。`code` 是机器可读的（界面据此选失败块的画法），`message` 直接能显示。

## 3. 动作表与 `code` 表

十六个动作：`start` / `stop` / `mute_session` / `settings_update` / `reprobe` / `dev_inject` / `summarize` / `workspace_open` / `workspace_remove` / `post_offline` / `post_minutes` / `post_stop` / `pause` / `resume` / `set_source` / `speaker_rename`。`post_offline` / `post_minutes` 是会后两个按钮（「生成会后精细稿」/「生成纪要」，R26）：起一条后台线程即算启动成功，成功体在通用信封上多一个 `post: {kind, dir}`；跑成什么样由场次目录里的状态文件回答。`post_stop` 停掉正在跑的「生成会后精细稿」（R28）：立起取消旗标即回（受理即回，不等块边界），成功体同一个信封、多一格 `stopping: true`，盘上状态写成 `interrupted`。`pause` / `resume` 是暂停与继续，`set_source` 是会中关掉某一路 / 重新打开某一路（R24 / R25）；`speaker_rename` 把一个说话人编号的名字写进这一场目录根的人名表（R10 第四条 / R30，成功体多一个 `speaker: {dir, key, name}`）。语义见 `docs/contract.md` 4.3 / 4.11。九个 `code`：

| `code` | HTTP | 意思 | 谁会回它 |
|---|---|---|---|
| `already_recording` | 409 | 已经有一场在录 | `start` / `reprobe` |
| `draining` | 409 | 上一场还在收尾（或收尾失败后停留），这场不能开；会后两个按钮拿它回「收尾没落定 / 有一趟会在飞」；`post_stop` 拿它回「没有在飞的会后可停 / 在飞的是纪要那一趟」 | `start` / `post_offline` / `post_minutes` / `post_stop` |
| `capture_unavailable` | 503 | 采集后端不可用：没接入、或设备起不来；`set_source` 那一档的 `detail` 是 `{source, error}` | `start` / `set_source` |
| `not_recording` | 409 | 现在没有在录的场次 | `stop` / `mute_session` / `pause` / `resume` / `set_source` / `dev_inject` / `summarize` |
| `invalid_setting` | 400 | 参数或键名非法（校验都在动手之前） | 各动作 |
| `persist_failed` | 500 | 配置写盘失败，这次改动一处都没生效；`speaker_rename` 的人名表写不进去也回它（`detail = {error}`） | `settings_update` / `speaker_rename` |
| `action_unavailable` | 404 | 这个后端没有这个动作；会后两个按钮拿它回「这台机器上没接这一层」 | `dev_inject`（非 dev 后端）/ `post_offline`（`post.hook` 缺席）/ `post_minutes`（`minutes.hook` 缺席） |
| `transcript_unavailable` | 503 | 转写没在跑（没起过，或者已经停了）；`detail = {"health": "stopped"}` | `summarize` |
| `summary_failed` | 502 | 会中总结没拿到结果：`detail` 是 `{missing: [...]}`（端点没配齐）或 `{elapsed_seconds, attempts}`（预算用尽 / 端点报错 / 解析连试带败） | `summarize` |

### 逐动作 × 逐态的可用性

| 动作 | `idle` | `recording` | `alert` | `ended`·`done` | `ended`·`draining` |
|---|---|---|---|---|---|
| `start` | 200：写 `session.json`、进 `recording`（落点是 `args.dir` 显式给的 **已存在** 目录） | 409 `already_recording` | 409 `already_recording` | 200：从 `ended` 直接进 `recording`，顺手清掉上一场的 `ended` 摘要 | 409 `draining` |
| `stop` | 409 `not_recording` | 200：进 `ended·draining`，时长冻结 | 200：同上（进 `ended` 不必先回 `recording`） | 409 `not_recording` | 409 `not_recording` |
| `mute_session` | 409 `not_recording` | 200：`alerts.*.muted` 立刻变 `true` | 200：同上 | 409 `not_recording` | 409 `not_recording` |
| `pause` / `resume` | 409 `not_recording` | 200：`session.paused` 翻过来（暂停那一截改写静音、不切段、时长停住；暂停顺带让会中模型退出显存、继续再把它拉回来） | 200：同上 | 409 `not_recording` | 409 `not_recording` |
| `set_source` | 409 `not_recording` | 200：关掉 / 重新打开那一路（重开时设备打不开 → 503 `capture_unavailable`；`source` / `active` 非法、或本场没采这一路 → 400 `invalid_setting`） | 200：同上 | 409 `not_recording` | 409 `not_recording` |
| `settings_update` | 200 | 200 | 200 | 200 | 200 |
| `reprobe` | 200：重新枚举并试开设备，两路快照随之更新（`revision +1`） | 409 `already_recording` | 409 `already_recording` | 200：同上 | 200：同上（采集已经停了，重新探测不干扰在飞的东西） |
| `dev_inject` | 409 `not_recording` | 200（非 dev 后端 404） | 200（非 dev 后端 404） | 409 `not_recording` | 409 `not_recording` |
| `summarize` | 409 `not_recording` | 200：**只读**，结果在应答体的 `summary` 键里（转写没在跑 503、端点没配齐 502） | 200：同上 | 409 `not_recording` | 409 `not_recording` |
| `workspace_open` | 200：写工作区注册表（缺 `dir` / 目录不存在 / 非空未确认 → 400） | 200 | 200 | 200 | 200 |
| `workspace_remove` | 200：摘一行名单，文件不碰 | 200 | 200 | 200 | 200 |
| `post_offline` / `post_minutes` | 带 `dir` 且那一行合法 → 200；不带 → 409 `draining`（手上没有可以跑后会的场次） | 409 `draining` | 409 `draining` | 200（这一层没接实现 → 404 `action_unavailable`；有一趟在飞 → 409 `draining`） | 收尾落定（`post` 停 `waiting` 算落定）→ 200；还没落定 → 409 `draining` |
| `post_stop` | 409 `draining`（没有在飞的会后可停） | 409 `draining`（同左） | 409 `draining`（同左） | 200（在飞的 offline 被受理 → `{kind, dir, stopping: true}`；在飞的是 minutes → 409 `draining`；`dir` 指到另一场 → 400 `invalid_setting`） | 200（同 `ended·done`——停止只看在飞，不看收尾） |
| `speaker_rename` | `dir` / `key` / `name` 齐且那一行合法 → 200（写这一场目录里的人名表） | 400 `invalid_setting`（盘上 `state` 是 `recording`，会中不提供改名） | 400 `invalid_setting`（同左） | 200 | 400 `invalid_setting`（盘上 `state` 是 `draining`） |

`settings_update` 写的是配置文件，与「这一场录到哪儿」无关，因此**任何状态下都可用**。`mute_session` 只在本场收尾之前可用 —— 收尾完成后本场已无可静音，复位发生在下一场 `start`。`reprobe` 只在**没有场次在跑**的时候可用（`recording` / `alert` 两态回 `already_recording`）：设备在开场前定下来，会中换设备是明文不做的那一档。`summarize` 只在**有场次在跑**的时候可用（它读的是这一场的实时转写）。

### 逐个动作的语义

**`start`** —— 失败顺序是固定的：先判态（`already_recording` / `draining`）→ 再校验 `args`（`dir` 缺失 / 目录不存在、`mode` 非法 → `invalid_setting`(400)；目录里已有 `session.json` 过得了这道校验 —— 那一档是**续接**，同一场会再录一段）→ 再判后端可用性（`capture_unavailable`）→ 最后才写第一版文件。**态不对或后端不可用时盘上不落这个目录里的任何新东西；续接失败那一档旧记录原样保留、本轮段文件收走，工作区回到按下去之前的样子。**

- `args.dir` **必给**：一条**已存在**目录的路径，是这场会的工作区与落点。**没有默认落点、也不新建目录、不按日期命名**（`docs/design-intent.md` 第 3、4 条）——目录名就是这场会的名字，由发起的人从磁盘里挑。目录里已有 `session.json` 时这一场是续接：旧段的归档一个字节不碰（新段接着已有段号编），段记录与权威时长把旧段合并着写盘。
- `args.mode` 取值 `both` / `mic` / `loopback`，**缺省 `both`**。
- `189` 的拒绝路径 `detail` 带 `session_id` / `dir` / `elapsed_seconds`（场次目录名、场次目录路径、已录秒数），够界面画「已经有一场在录」。
- `draining` 的拒绝路径 `detail` 带三项：场次目录（`session_id` / `dir`）、失败步（`failed_steps`，每项 `{id, error}`，没有失败步就是空数组）、场次 log 路径（`log_path`）。

**`stop`** —— 进 `ended·draining`，时长就地冻结，然后服务按四步收尾跑到终态（四步与终态规则见 `docs/state.md` 第 1 章）。收尾成功档下 `top_state` 停在 `ended`、`ended.phase == "done"`、`session` 保留。

**`mute_session`** —— 本场不再出声：`alerts.*.muted` 与横幅上那句「本场已静音」一起变，`revision +1`。它**不改**持久化开关 `alarm.sound`，**下一场 `start` 自动复位**。静音期间新起的告警同样继承静音。

**`pause` / `resume`** —— 暂停与继续（R24）：

```json
{"action": "pause"}
```

- 无 `args`。暂停期间那一截**不录内容**（两路的归档改写数字静音，`docs/design-intent.md` 第 8 条），快照 `session.paused` 变 `true`，段**不**切（第 9 条），`session.elapsed_seconds` 停在原地（第 7 条：权威长度是实际录到的音频时长，不含暂停）。
- 暂停的那几段时间**不计入静默判定**：安静房间里暂停多久都不会冒出一条「这一路静默已达 N 秒」的致命级（第 17 条）。电平帧照发，心跳的 `recording_alive` 照旧为真。
- 暂停那一刻顺带让**会中模型退出显存**，继续时再把它拉回来（可选成员 `release_gpu()` / `acquire_gpu()`，`10770b41`）：两次调用都**异步提交**到后台的单线程执行器，应答不等它们做完 —— 直接跑在事件循环上，这按钮就得等完释放（等在飞推理，上界 5 秒）与拉起（秒级），那几秒里心跳与三条 SSE 一起停摆，界面会判成「心跳失联」。继续那一刻拿回来的是识别器，权重的加载落在继续之后第一段转写时。
- 两个动作都幂等；只在 `recording` / `alert` 两态可用，其余态回 `not_recording`(409)。顶层四态一个字不动（「暂停中」落在 `session.paused` 这一格上）。

**`set_source`** —— 会中关掉某一路 / 重新打开某一路（R25）：

```json
{"action": "set_source", "args": {"source": "mic", "active": false}}
```

- `source` 取 `mic` / `loopback`，`active` 是 bool（`false` 关掉、`true` 打开），两个都必给。
- 关掉：那一路停流、改写静音，**不报 `SourceLost`、不进告警**（用户按的按钮与「这一路真死了」是两件事），快照里那一路落 `health = stopped`（复用现成的档位，取值域不进第八个）。**关断即收场**：这一路挂着的致命级当场清空——横幅消失、胶囊回「录音中」；同场还有别的路真在 `lost` 时致命级换挂那一路、顶层停在 `alert`。
- 重新打开：重新挑设备、重开流，成功回 `capturing`（**干净开场**：不回 `lost`、不落「曾中断」的残留横幅，静默判定从零起算——关掉那一刻这一路挂着的致命级已当场清空）。**打不开**（设备又被别的程序占着）回 503 `capture_unavailable`，`detail = {source, error}`，那一路照旧停在 `stopped` —— 悄悄不打开会让屏幕写着「在录」而其实没录（第 1 条）。
- `source` / `active` 非法、或这一场根本没采这一路（`mode` 开场就定死了）→ 400 `invalid_setting`。两个方向都不切段。

**`reprobe`** —— 重新枚举并试开设备，把结果落进快照两路（无 `args`）：

```json
{"action": "reprobe"}
```

- 成功体就是通用的 `{"ok": true, "revision": N}`（`revision +1`），两路 `sources[*].device_name` / `health` 随之更新。
- **只在没有场次在跑的时候可用**：`recording` / `alert` 两态回 `already_recording`(409)。录到一半插上设备之后要重新探测，得先结束这一场。
- 探测结果**照实写**：一条设备都挑不出来时该路落 `probe_failed`、`device_name` 为 `null`，动作本身照样回 200（挑不到设备属于探测结论，动作仍然成功）。
- 采集方式（哪个设备、怎么试开）见 `meeting/capture/devices.py` 与 `meeting/capture/sources.py`；快照里那两格读的是 `devices.mic` / `devices.loopback` 的生效值。

**`dev_inject`** —— 只在 dev 采集替身在场时可用，注入 `silence` / `stall` / `lost` 三档故障：

```json
{"action": "dev_inject",
 "args": {"source": "loopback", "fault": "silence",
          "after_seconds": 5, "duration_seconds": 3}}
```

- `source` 取 `mic` / `loopback`，`fault` 取 `silence` / `stall` / `lost`，非法取值回 `invalid_setting`(400)。
- `after_seconds` 到点生效（可为 0），`duration_seconds` 到点恢复（正数，可省，缺省 `30.0` 秒）。
- 非 dev 后端回 `action_unavailable`(404)；没有正在进行的场次回 `not_recording`(409)。三档故障的语义与「用完怎么恢复」见 `docs/state.md` 第 7 章。

**`summarize`** —— 会中一键总结：把最近这段时间的实时转写读一遍，给回「刚才在讲什么 / 讲到哪儿了 / 丢掉了什么」三小段：

```json
{"action": "summarize", "args": {"window": "15m"}}
```

- `window` 取 `5m` / `15m` / `30m` / `full`，**省略时取配置的 `summary.default_window`**（默认 `15m`）；取值非法回 `invalid_setting`(400)。
- 成功体比通用信封多一个 `summary` 键：`{window, window_label, at, elapsed_seconds, lines, what, where, lost}`。**结果不进快照、不动 `revision`**（`revision` 只是当前版本号）；窗口里一行都没有时照旧 200、三段都是空数组。
- 逐档失败：没有在录的场次 `not_recording`(409)、`window` 非法 `invalid_setting`(400)、转写没在跑 `transcript_unavailable`(503)、模型端点没配或预算用尽 `summary_failed`(502)。**判定次序固定**（判态 → 校验 `window` → 看转写健康 → 看模型端点配置 → 才动手），前面几档没过时**一个模型请求都不发出去、一条记录都不写**。
- 本场记录用 `GET /api/summaries` 取（只记成功产出的那几次，按时间先后；服务侧不做并发闸，两个页面同时按下就是两次独立的总结）。逐档 `detail` 与成功体的全文见 `docs/contract.md` 第 4.7 节。

**`workspace_open`** —— 「打开目录」的登记通道（会后工作台收编，形状全文见 `docs/contract.md` 4.8）：

```json
{"action": "workspace_open", "args": {"dir": "C:\\会议\\跟老王聊周会", "confirm": false}}
```

- 判名目三档：空目录当新会、有会议材料（`session.json` 或 `meeting_audio_*.pcm`；**有音频名没有 `session.json` 的疑似会议目录照收编**）直接登记、非空且无材料要 `confirm: true` 才登记（不带它回 400 `invalid_setting`，`detail.key = "confirm"`，目录没动过一个字节）。
- 目录不存在回 400（已存在是硬前提，不建目录）；同一目录选两次只占一行（按规范化路径去重），再选只更新那一行的 `last_opened_at`。
- 成功体 = `{"ok": true, "revision": N, "workspace": {dir, kind, suspected, first_time}}`。

**`workspace_remove`** —— 「删除」：`{"action": "workspace_remove", "args": {"dir": "…"}}`，成功体 `{ok, revision, workspace: {dir, removed}}`。**只把这一条从注册表摘掉**，工作区目录与文件一个字节不动（`docs/design-intent.md` 第 5 条）；没登记过这条也照样 200（`removed: false`）。

**`post_offline` / `post_minutes`** —— 会后两个按钮（R26），判据与逐档 `detail` 见 `docs/contract.md` 4.3：

```json
{"action": "post_offline", "args": {"dir": "C:\\会议\\跟老王聊周会"}}
```

- `args.dir` **可省**：给了就跑它指定的那一场，且要过与 `GET /api/sessions` 同一套注册表 / 目录判读（不在注册表里、目录不在原地、盘上 `state` 还是 `recording` / `draining` → `invalid_setting`(400)）；省略时跑**收尾刚落定的那一场**（命令行与既有调用走这一路）。
- 拒绝档固定三条：这一场还没结束 / 收尾还没落定，或已经有一趟会后活在飞 → 409 `draining`；`post_minutes` 那一层的状态停在 `running`（纪要 claim 闸同步判）→ 409 `draining`，`detail` 带 `status_path`；这一层没接实现（`post.hook` / `minutes.hook` 缺席）→ 404 `action_unavailable`。
- 成功体是通用信封 + `post: {kind: "offline"|"minutes", dir}`；回执只说「起没起、跑的哪一场」，跑成什么样由场次目录里的 `meeting_offline_status.json` / `meeting_minutes_status.json` 回答；**跑到哪一档**（第几块 / 共几块、正在跑哪一路、已经跑了多久）由 `GET /api/sessions/{id}` 回答。

**`post_stop`** —— 停掉正在跑的「生成会后精细稿」（R28），语义全文见 `docs/contract.md` 4.3：

```json
{"action": "post_stop", "args": {"dir": "C:\\会议\\跟老王聊周会"}}
```

- `args.dir` **可省**：在飞的永远只有一场，省略即停它；给了但指到另一场 → `invalid_setting`(400)，`legal` 里是在飞那一场的目录。
- **受理即回**：立起取消旗标就交回执，不等块边界——那条线程在当前块跑完、下一块开跑前收场，盘上状态由 `run_session` 写成 `interrupted`（终态写入只归它，`GET /api/sessions/{id}` 的 `offline.state` 照盘答 `interrupted`）。正在跑的那一块不掐（引擎是同步推理）。
- 拒绝档：没有在飞的会后 → 409 `draining`（`detail` 空对象，那一场重按按钮即可）；在飞的是纪要那一趟 → 409 `draining`（`detail` 带 `kind`——停的动作只接 offline，机制两样，如实拒）；`dir` 指到另一场 → 400 `invalid_setting`。
- 成功体是通用信封 + `post: {kind: "offline", dir, stopping: true}`。打断之后重按两个按钮、或再敲 `meeting post`，都**从头再跑**（幂等闸只拦 `running`，不需要 `--force`）。

**`speaker_rename`** —— 给一个说话人编号起名（R10 第四条 / R30），形状全文见 `docs/contract.md` 4.11：

```json
{"action": "speaker_rename", "args": {"dir": "C:\\会议\\跟老王聊周会", "key": "mic:S01", "name": "老王"}}
```

- 三格 `args` **都必给**：`dir` 过与 `post_offline` 同一套注册表 / 目录判读（**没有省略语义**）；`key` 是 `mic` / `loopback` 或 `mic:S01` 这种带路前缀的限定名；`name` 非空。同一键再改一次就是改名；两个键写同一个名字合法——编号是分开的，可以重名。
- **只写这一场目录根的人名表** `meeting_speaker_names.json`：不拉离线、不拉纪要、不改已生成的 `meeting_final.md`、不动快照（`revision` 保持现值）。名字落下去之后，下一次出纪要的提示词里带上这张表。
- 成功体是通用信封 + `speaker: {dir, key, name}`；界面当场生效走 `GET /api/sessions/{id}` 的 `speakers` 一格（改名后重问一次即可）。人名表读不出时那一格的名字如实 `null`，读侧只做结构解析、不报错。

**`GET /api/sessions` / `GET /api/dirs`** —— 两只纯读端点：前者给注册表 + `recent`（每行带 `has_offline`：这一场有没有会后精细稿；带 `segments`：这一场各段的时长与时间），后者给选择器的一层子目录名目。**"最近一场"只从注册表读**：启动、请求都不扫任何场次目录（`docs/design-intent.md` 第 4 条）。

**`GET /api/sessions/{id}`** —— 单场状态（会后视图的进度区）：`{id}` 是这一场的目录路径（URL 编码的绝对路径），应答体见 `docs/contract.md` 4.9。会后离线那条链每跑完一块就往场次目录里那份 `meeting_offline_progress.jsonl` 上补一行（这一块的文字 + 这一块的落点），这只端点把「第几块 / 共几块、正在跑哪一路、已经跑了多久」按路各交一份回来；`speakers` 一格带这一场盘上的身份集（限定键 / 路 / 人话标签 / 现名或 `null` / 可得的次数读数，4.11）。**纯读**：不涨 `revision`、不广播。

### 会议资料的四条端点（资料节点 285bf58c）

场次目录下固定的子目录 **`meeting_materials/`** 是一场会放资料的地方（资料是给纪要 AI 的 context）：目录名与服务端和纪要提示词两侧共享，**不设配置键、不可改名**；**首次上传时由服务创建**。四条端点都挂在 `/api/sessions/{id}/materials` 之下（`{id}` 同样是 URL 编码的目录路径），实现与清洗规则在 `meeting/service/runtime_materials.py`，契约正文在 `docs/contract.md` 4.12；纪要提示词那一侧已落地（节点 `e8fe7da9`），资料怎么进纪要见 `docs/minutes.md` 第 3 节。

- **只认两种目标目录**：服务的**当前活跃场次**、工作区注册表（`workspaces.json`）里**登记过的场次**；其余一律拒收。拒收口径与 `GET /api/sessions/{id}` 同一套寻址：目录已不在原地 → **404 `session_dir_missing`**（`detail = {dir, kind: "missing"}`）；目录在但两样都不是 → **404 `invalid_setting`**（`detail = {dir, kind}`）。活跃场次放在最前：录制中照收——哪怕那一行刚被 `workspace_remove` 摘出清单（只摘名单、文件不动），资料跟着场次走、不跟着清单行走。
- **上传**（`POST …/materials?name=<文件名>`）：请求体就是文件的原样字节，**流式写盘**——收到多少写多少、不整段进内存，写在工作线程上（事件循环照转，心跳与三个 SSE 通道不被一次大上传按住）。**不转码、不压缩、不限类型、大小不设服务端硬顶**（「超过 100M 要不要确认」是界面的事）；请求的 `Content-Type` 只是发送方的说法，服务不读它也不校验它。`?from=<路径>`（160d1a78）：字节改从盘上那个已有文件流式拷入（选择器文件模式用）——清洗 / 重名让位 / 贴上时刻 / 头哨与正文上传同一套，`from` 给到不存在的文件 400 `invalid_setting`（`detail.key = "from"`）、盘上什么都不落，给了 `from` 请求正文被忽略，源文件一个字节不动。`name` **必给**（缺 / 空白 → 400 `invalid_setting`，`detail.key = "name"`）、**不可信**：服务侧清洗防路径穿越（只留最后一段路径、Windows 非法字符与控制字符换 `_`、掐结尾点与空格、保留设备名前置 `_`、超长截断保留扩展名），重名自动加序号后缀、保留用户看到的名字（`报告.pdf` → `报告 (2).pdf`）。**不保留原文件的时间戳**：落盘那一刻的 mtime 就是「贴上时刻」。中途失败（盘上出岔 → 500 `persist_failed`；客户端断开）把半截文件收走，不留残骸。成功体 = `{"ok": true, "revision": N, "material": {name, size_bytes, added_at}}`。
- **清单**（`GET …/materials`）：`{"dir": "<path>", "materials_dir": "<path>", "items": [{name, size_bytes, added_at}]}`，按名字排序（casefold）。**没有旁注清单文件**：目录就是清单，每条三格——名字、大小、贴上时刻（= 文件修改时间），不另设 JSON 记录、无备注元数据。`meeting_materials/` 还不在时 `items` 是空数组、**目录也不建**。
- **取原文件**（`GET …/materials/{name}`）：原始字节从盘上流式读出，`Content-Type` 按扩展名判（标准库 `mimetypes`，认不出给 `application/octet-stream`）——它只是给界面的提示，服务不校验内容。
- **删除**（`DELETE …/materials/{name}`）：**只删那一个文件**（场次目录与资料目录里别的文件一个字节不动）。成功体 = `{"ok": true, "revision": N, "material": {name}}`。
- **取不到 / 删不到**：名字清不出现存的文件（含**重复删除**）→ **404 `material_missing`**（`detail = {name}`）——这是资料这一片自己的 `code`（不是动作信封那九个之一，先例是 `session_dir_missing`）；盘上出岔（建不了目录 / 写不进去 / 清单读不出来 / 删不掉）→ **500 `persist_failed`**（`detail = {error}`）。
- **寻址与名字都先过清洗**：`{name}` 在取 / 删两测再过一遍与上传同一套清洗，URL 里手写的穿越段剥成裸文件名，解析结果永远落在 `meeting_materials/` 这一层，`session.json` 等场次文件够不着。
- **独立于四态快照**：四条端点都是纯读写文件——不提交、不涨 `revision`、不广播；成功信封里的 `revision` 只是当前版本号（先例是 `summarize`）。
- golden 样本：`contract/fixtures/materials/`（四份），语义与失败档由 `tests/test_materials.py` 实测。

## 4. `settings_update`：写哪些键、什么时候生效

请求的 `args` 就是**「配置点号键名 → 值」的扁平映射**，只有这一层、不按键前缀分组：

```json
{"action": "settings_update", "args": {"asr.timeout_seconds": 30}}
```

应答是 `{"ok": true, "revision": N, "applied": [...], "pending_restart": [...]}` —— 两个数组里也是同一套点号键名。**成功体里没有 `message` 字段**，需要给人看的话由界面按两个数组自己组织。

**合法键名是逐条列出的 40 个**（不按前缀归组、没有前缀白名单）。表外的任何键名一律 `invalid_setting`(400)，`devices.mic_device` 这种没定名的名字也在表外。

| 归类 | 键 | 为什么 |
|---|---|---|
| `applied`（12 个） | `alarm.sound` / `asr.max_segment_seconds` / `asr.timeout_seconds` / `asr.max_new_tokens` / `asr.repeat_max_unit` / `asr.repeat_min_coverage` / `devices.mic` / `devices.loopback` / `heartbeat.interval_seconds` / `summary.default_window` / `summary.presets_json` / `summary.active_preset` | 这十二个是热读键，写盘成功就立刻生效。`alarm.sound` 收在这一档，于是勾选出声开关**当场**改运行期行为；设置抽屉那四项（`311f84f5`）与总结模型预设两键收在这一档，改完无需重启 —— 设备改完 `reprobe` 按新值挑，心跳周期与总结缺省档的快照格当场换新值，会中换一套模型预设写盘即生效 |
| `pending_restart`（28 个） | `web.port` / `web.dist_dir` / `log.level` / `log.dir` / `log.max_bytes` / `log.backup_count` / `alert.silence_dbfs` / `alert.silence_seconds` / `alert.recover_seconds` / `alert.alarm_suppress_seconds` / `capture.backend` / `transcriber.backend` / `post.hook` / `post.engine` / `post.model` / `post.dtype` / `post.chunk_seconds` / `minutes.hook` / `minutes.chunk_lines` / `minutes.max_segments` / `minutes.timeout_seconds` / `minutes.dsh_url` / `summary.base_url` / `summary.api_key` / `summary.model` / `summary.extra_body_json` / `summary.deadline_seconds` / `summary.max_attempts` | 这些值在启动时读一次，写盘成功但要重启才生效 |

- **会中总结那九个键**（`summary.base_url` / `summary.api_key` / `summary.model` / `summary.extra_body_json` / `summary.presets_json` / `summary.active_preset` / `summary.deadline_seconds` / `summary.max_attempts` / `summary.default_window`）里**缺省档与预设两键收在热读档**，其余六键归 `pending_restart`：`summary.base_url` / `summary.model` 收非空字符串（默认 `https://api.deepseek.com/v1` 与 `deepseek-chat`），`summary.api_key` 是**可空键**（`null` = 没配，与两个设备键位同形），`summary.extra_body_json` 收「一段合法 JSON 对象的文本」（厂商特有字段），`summary.default_window` 只认 `5m` / `15m` / `30m` / `full` 四档（默认 `15m`；写盘那一刻快照 `settings.summary_default_window` 就换过去），`summary.deadline_seconds` 收 `1.0`–`3600.0` 秒（默认 `25.0`），`summary.max_attempts` 收 `1`–`10`（默认 `3`）。**预设两键**（会中切模型）：`summary.presets_json` 收「一段 JSON 数组的文本」，每套一个对象 —— `name` 必填（非空白、不重名），`base_url` / `api_key` / `model` / `extra_body_json` 可省（省了按没配算）；`summary.active_preset` 收空串（不选预设，走平铺四键）或某套预设的 `name`，指向的名字不在清单里当场 `invalid_setting`(400)。两键都写盘那一刻生效：每次总结都是现读配置、现造客户端，设置抽屉「快速总结模型」下拉会中换一套当场走新的。预设未配、清单解析失败、指针空或指向的名字不在清单里时，总结一律回落平铺四键 —— 存量 `config.json` 零迁移。键名与取值范围在 `meeting/config.py`；这一层怎么用见第 3 章 `summarize` 那一段与 `docs/summary.md` 第 5 章。

- **`alarm.sound` 与 `asr.*` 一起当场生效**：写盘那一刻主 log 落一行「alarm.sound 收在热读档，本刻起出声开关 = …」，服务侧同时重取一次生效值视图并提交 —— 紧接着的 `GET /api/state` 里 `settings.alarm_sound` 就是新值，`revision` 涨一格，界面当场按新值决定响不响（设计稿 `docs/ui-design.md` 4.5 要的「勾选立即生效」在这里成立）。
- **会后那四个键**（`post.engine` / `post.model` / `post.dtype` / `post.chunk_seconds`）也归 `pending_restart`：`post.engine` 与三个后端选择键同形（`auto` / `null` / `module:Class`），`post.model` 收 HF 仓库 id 或本地快照路径，`post.dtype` 只认 `bfloat16` / `bf16` / `float16` / `fp16` / `float32` / `fp32` 这几个档位名，`post.chunk_seconds` 是每块音频的长度上限（整数 `0`–`86400`，默认 `450`，`0` = 关掉分块、整段一次跑）。键名与取值范围的判据在 `meeting/config.py`，档位名要跟 `meeting/offline/engine.py` 的 `DTYPE_NAMES` 对得上，写错的名字在写盘前就被拦下。
- **会后纪要那五个键**（`minutes.hook` / `minutes.chunk_lines` / `minutes.max_segments` / `minutes.timeout_seconds` / `minutes.dsh_url`）同样归 `pending_restart`：`minutes.hook` 与三个后端选择键同形（`auto` / `null` / `module:Class`），另外**收 JSON 的 `null`**（＝这台机器不接纪要这一层：「生成纪要」按钮当场回 404 `action_unavailable`，`meeting post` 补纪要那一路安静跳过）；`minutes.chunk_lines` 是每块行数（整数 `10`–`1000`）、`minutes.max_segments` 是每条 source 的分段上限（整数 `1`–`20`）、`minutes.timeout_seconds` 是一次生成的墙钟上限（整数 `60`–`86400`）、`minutes.dsh_url` 是 DSH web 服务端的地址（非空字符串，默认 `http://127.0.0.1:3080`）。键名与取值范围在 `meeting/config.py`。这一层怎么跑见 `docs/minutes.md`。
- **配置文件本身的键形与请求同位**：`~/.config/meeting/config.json` 里也只有一层点号扁平键名，例如 `{"alert.silence_seconds": 3}`。嵌套形状不合法。
- **只写用户配置文件里已有的键 + 本次改动的键**，不把代码里的默认值落盘 —— 一次 `settings_update` 不会凭空长出一份写着全部默认值的配置文件。
- **校验在写盘之前**：至少落地方案点名的「超时必须大于段长」（`asr.timeout_seconds > asr.max_segment_seconds`），违规回 `invalid_setting`(400) 并说明是哪两个键。
- **被环境变量压着的键不改分类**：`asr.*` 照样进 `applied`；「被环境变量压着」这件事只在主 log 留一条 warning（键名 + 写盘的值 + 压住它的环境变量名），运行期仍由环境变量说话。
- **写盘失败回 `persist_failed`(500)**，`failed` 一处都不生效：要么全部写入并生效，要么原样不动。

## 5. 端口与本地最低防线

- 默认端口 **8765**，只监听 **`127.0.0.1`**，这一条不可配（没有 `web.host` 这类键）。
- 端口被占**不猜、不静默换端口**，两种情形都以退出码 `2` 结束：占用者是 meeting 服务（`GET /api/heartbeat` 应答里有 `"service": "meeting"`）→ 说「已经有一个 meeting 服务在跑」；占用者是别的程序 → 写明端口被谁占着、怎么改。
- 三道最低防线，挡的是「本机浏览器里的恶意页面往 `127.0.0.1:8765` 发请求」：
  1. **Host 校验**：`Host` 不是 `127.0.0.1[:port]` / `localhost[:port]` 就 `403`（挡 DNS rebinding）。
  2. **写语义端点要自定义头**：带写语义的端点必须带 `X-Meeting-Service: 1`，缺了 `403`（挡表单式简单请求——本机浏览器里的恶意页面发不出带自定义头的请求）。现在守两处：`POST /api/actions`（还要求 `Content-Type: application/json`；改状态的动作集中在这一个信封里，这是新动作不另开端点的原因）与**会议资料的两条写端点**（`POST …/materials` 上传、`DELETE …/materials/{name}` 删除）——那两条的正文是二进制、装不进 JSON 信封，头哨由中间件按端点扩到它们身上（`meeting/service/app.py` 的 `_materials_write_path()` 一张核对表），防线仍然只守中间件这一处。校验都先于路由匹配生效。
  3. **不开 CORS**：不发 `Access-Control-Allow-Origin`，前端由本服务同源发出。

## 6. 复核时怎么打

起服务的技术细节见 `docs/service.md` 第 1 章。想真跑一场用 dev 替身：

```powershell
$env:MEETING_CAPTURE="dev"; $env:MEETING_NO_BROWSER="1"; meeting web
```

几条常用的（**地址一律用终端打出的那个**，配 `MEETING_PORT=0` 时端口由系统分配）：

```powershell
curl.exe -s http://127.0.0.1:8765/api/state
curl.exe -s -N http://127.0.0.1:8765/api/events     # 首帧 snapshot，之后 state / levels / transcript
curl.exe -s http://127.0.0.1:8765/api/heartbeat
curl.exe -s http://127.0.0.1:8765/api/sessions
# 单场状态（会后进度读数；{id} 是这一场的目录路径，得 URL 编码）
curl.exe -s "http://127.0.0.1:8765/api/sessions/C%3A%5C%E4%BC%9A%E8%AE%AE%5C%E8%B7%9F%E8%80%81%E7%8E%8B%E8%81%8A%E5%91%A8%E4%BC%9A"
curl.exe -s http://127.0.0.1:8765/api/dirs
# 会议资料（清单；{id} 同样是 URL 编码的目录路径）
curl.exe -s "http://127.0.0.1:8765/api/sessions/C%3A%5C%E4%BC%9A%E8%AE%AE%5C%E8%B7%9F%E8%80%81%E7%8E%8B%E8%81%8A%E5%91%A8%E4%BC%9A/materials"
# 上传（请求体 = 文件原样字节；写请求必须带头）与取原文件
curl.exe -s -X POST "http://127.0.0.1:8765/api/sessions/<URL编码目录路径>/materials?name=%E6%8A%A5%E5%91%8A.pdf" -H "X-Meeting-Service: 1" --data-binary "@报告.pdf"
curl.exe -s -o - "http://127.0.0.1:8765/api/sessions/<URL编码目录路径>/materials/%E6%8A%A5%E5%91%8A.pdf"
curl.exe -s -X POST http://127.0.0.1:8765/api/actions `
  -H "Content-Type: application/json" -H "X-Meeting-Service: 1" `
  -d '{\"action\":\"workspace_open\",\"args\":{\"dir\":\"C:\\\\会议\\\\跟老王聊周会\"}}'
```

默认 `capture.backend = auto` 解析到真采集后端：设备起得来时 `start` 真的开一场（设备全起不来才回 `503 capture_unavailable`，`detail.reason` 写明哪一路为什么）；显式配 `null` 替身时 `start` 回 `503 capture_unavailable`（如实报「采集后端未接入」，盘上不落任何新东西）；dev 替身给开发与测试用（合成电平 + 故障注入，不写真实音频）。

## 7. 相关文档

- **双端契约（端点口径、快照 schema、SSE 帧形状、动作信封与 `code`/`detail` 表）：`docs/contract.md`**
- 四态、快照字段、`session.json`、心跳、告警、SSE 帧形状：`docs/state.md`
- 怎么起服务、配置、日志、退出码、跑测试：`docs/service.md`
- 后端协议与 dev 替身：`docs/backend-protocols.md`
- 前端工程怎么跑、怎么构建：`web/README.md`
- 契约的可机读基准（golden 样本与掩码规则）：`contract/README.md`
- 索引：`docs/spec.md`
