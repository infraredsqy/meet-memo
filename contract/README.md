# contract/ —— 双端契约的 golden 样本

> 这个目录是**服务与界面之间那份契约的可机读基准**：`fixtures/` 下的 JSON 样本一律出自真实服务路径，`tests/test_contract_fixtures/` 每次跑测试都重走一遍采集、与盘上样本逐字比对，服务侧形状一漂移就红。
>
> 契约正文在 `docs/contract.md`（端点口径、快照 schema、SSE 帧形状、动作信封与 `code`/`detail` 表、心跳语义、客户端义务、保留登记）。这个 README 只讲三件事：**样本从哪儿来、掩码怎么打、怎么再生成**。
>
> 代码落点：掩码与采集辅助在 `tests/_contract.py`；比对与再生成在 `tests/test_contract_fixtures/`；再生成开关 `--regen-contract` 注册在 `tests/conftest.py` 的 `pytest_addoption`。

## 1. 样本清单

| 目录 | 文件 | 采集路径 | 固定的是什么 |
|---|---|---|---|
| `snapshot/` | `idle.json` | 进程内 TestClient（`capture.backend: dev`） | 空闲态：`session` / `ended` 两处 `null`、两路 `ready`、包络空数组 |
| | `recording.json` | 进程内 TestClient（dev 替身）+ 等两路包络长满 15 根 | 录音态：`session` 六键、两路 `capturing`、包络长度 15 |
| | `alert.json` | 进程内 TestClient：配 `alert.silence_seconds: 1` + `dev_inject(loopback, silence)` | 告警态：`alerts.critical` 一条、告警路 `lost`、另一路 `capturing` |
| | `ended-done.json` | 进程内 TestClient：`stop` 之后等 `ended.phase == done` | 成功档：收尾三步 `skipped/done/waiting`（转码已摘出收尾序列；`post` 照实停 `waiting` 等人按按钮）、两路回 `unprobed`、`session.frozen` 为真 |
| | `ended-draining.json` | 进程内 TestClient：换一个抛错的假场次 log（`tests/_drain_fakes.py`） | 失败停留档：`log` 步 `failed`、`ended.error` 有值、`post` 照常停 `waiting`、两路停 `stopped` |
| `sse/` | `first-snapshot.json` | **真服务子进程**的 `/api/events` 首帧 | 首帧是 `snapshot`、带 `id`、`data` 就是九键快照 |
| | `state.json` | 同上，`mute_session` 触发一次广播 | `state` 是全量快照、`id == data.revision` |
| | `levels.json` | 同上，`read_until("levels")` | 两路五键、`ts` 恒 `null`、包络 15、`bar == envelope[-1]` |
| | `transcript.json` | 同上，假转写后端经 `module:Class` 挂进子进程 | `{revision, line}`、`line` 四键 |
| | `resync-snapshot.json` | **进程内走 `hub.py` 的真实广播路径**：注册真订阅者 → 灌满 512 队列 → 下一次广播走 `resync()` | 带 `resync: true` / `dropped: 1`、顶层十一键、`id == data.revision` |
| `actions/` | `start-ok.json` / `stop-ok.json` / `mute-session-ok.json` | 进程内 TestClient 的真实动作端点 | 成功体 `{ok, revision}`；`revision` 是那一趟的真实提交号 |
| | `summarize-ok.json` | 同上（注入假总结客户端 `_contract.SummaryStub` + 假转写后端，窗口里铺 12 行） | 成功体 = 通用信封 + `summary` **八格**（`window` / `window_label` / `at` / `elapsed_seconds` / `lines` / `what` / `where` / `lost`；`lines` 是窗口行数、三段来自假客户端的固定输出。**`GET /api/summaries` 那一条少一格 `lines`**，见下面 `summaries/after-one.json`） |
| | `settings-update-ok.json` | 同上 | 成功体含 `applied` / `pending_restart` **两数组**、**没有 `message`** |
| | `workspace-open-ok.json` / `workspace-remove-ok.json` | 进程内 TestClient（e2f6166b）：挑一个临时根下的已有目录走「新会议」；再从列表移除 | 成功体 = 通用信封 + `workspace` 键（`open` 给 `{dir, kind, suspected, first_time}`，`remove` 给 `{dir, removed}`） |
| | `fail-already-recording.json` | 录音中再 `start` | 409 + `detail` 三键 |
| | `fail-draining.json` | 失败停留档里再 `start` | 409 + `detail` 四键（含 `failed_steps`） |
| | `fail-capture-unavailable.json` | 进程内 TestClient（显式配 `capture.backend: null` 替身；默认 `auto` 已接真采集，`start` 会真开一场） | 503 + `detail` 三键（`{session_id, dir, reason}` 那一档） |
| | `fail-not-recording.json` | 空闲态 `stop` | 409 + **空对象** `detail` |
| | `fail-invalid-setting.json` | `start` 带 `mode: "burst"` | 400 + `detail` 三键（`{key, value, legal}`） |
| | `fail-invalid-setting-unknown-action.json` | 打一个不认识的动作名 | 400 + `detail` 两键（`{action, known}`，`known` 是**十九个动作名**） |
| | `fail-persist-failed.json` | `monkeypatch` 把 `write_config_values` 换成抛 `OSError` | 500 + `detail = {error}` |
| | `fail-action-unavailable.json` | 空闲态 `dev_inject`（非 dev 后端） | 404 + **空对象** `detail` |
| | `fail-transcript-unavailable.json` | 录音中把转写后端拿掉再 `summarize` | 503 + `detail = {health: "stopped"}` |
| | `fail-summary-failed-missing.json` | 录音中把只读配置视图换成一份缺端点的再 `summarize` | 502 + `detail = {missing: [...]}` |
| `summaries/` | `after-one.json` | 进程内 TestClient：按过一次 `summarize` 之后读 `GET /api/summaries` | `{session_id, entries}`、记录那一条七键（不含 `lines`）、时刻与那次应答逐字同值 |
| `devices/` | `all.json` | 进程内 TestClient（设置抽屉节点 `311f84f5`）：**设备枚举走假件**（`sources.py` 两个枚举函数换成固定端点表），读一次 `GET /api/devices` | `{mic, loopback}` 两路各一份、每项六格（`name` / `host_api` / `role` / `index` / `channels` / `is_default`）；名字与默认标记是假件的确定值；纯读，revision 不动 |
| `dirs/` | `listing.json` | 进程内 TestClient（目录选择器节点）：临时根下造两个子目录（空 / 杂物），读一次 `GET /api/dirs?dir=<临时根>` | `{home, cwd, up, here, dirs}`：`up` 到盘根才 `null`（掩后不可见，形状由采集用例断言）、`here` 是正在浏览这一层自己的名目四格、`dirs` 按名字小写序；路径掩 `<path>`，名字与 `kind` 是确定值 |
| | `create-ok.json` / `fail-exists.json` / `fail-invalid-name.json` | 同上：`POST /api/dirs?dir=<临时根>&name=新会`（成功体 + 同名再建）；非法名 `../逃逸` 一档 | 与 `materials/*.json` 同构的三件套（`method` + `dir` + `name`）+ 应答：成功体 = 通用信封 + `directory`（新建那一行的名目判读，刚建出来 `empty`）；`fail-exists` 是 409 + `code = dir_exists`、`detail = {dir, name}`；`fail-invalid-name` 是 400 + `invalid_setting`、`detail = {key: "name", value, legal}`——名字不清洗不改写，脏名字原样进样本。写头哨（`X-Meeting-Service: 1`）由采集驱动件自动带上 |
| `materials/` | `upload-ok.json` | 进程内 TestClient（资料节点 285bf58c）：临时根下造目的目录、`workspace_open` 登记，往 `/api/sessions/{id}/materials?name=报告.pdf` POST 一段固定字节 | 请求两件套（`method` + `name`）+ `status` + 掩码后的成功体：通用信封 + `material` 三格（`name` / `size_bytes` / `added_at`）；名字与字节数是确定值，`added_at` 走 `<iso>`、`revision` 保留真实值 |
| | `after-upload.json` | 同上：再传一次同名（样本里看得出 `报告 (2).pdf` 的序号后缀）与一个穿越加非法字符的脏名字（清洗后 `假报告_v__.pdf`），然后读 `GET …/materials` | `{dir, materials_dir, items:[{name, size_bytes, added_at}]}`：三格的清单形状、按名字排序的确定次序；路径走 `<path>` 掩码 |
| | `delete-ok.json` | 同上：`DELETE …/materials/报告 (2).pdf`（带头） | 成功体 = 通用信封 + `material: {name}` |
| | `missing.json` | 同上：删除之后再 GET 那同一个名字 | 404 + 失败信封，`code = material_missing`、`detail = {name}`（4.12 那一片自己的 `code`） |
| `sessions/` | `empty.json` / `after-open.json` | 进程内 TestClient（工作台收编节点 e2f6166b）：空注册表一份；再往临时根下造三个目的目录（空 / 带 `session.json` / 只有音频归档名的疑似档）逐个走 `workspace_open` 之后读 `GET /api/sessions`。清单按「最近在用」倒序（7058f2e1）——采集把登记时钟钉成三拍递增的固定值（时间戳是次序判据，不钉则两次再生成的次序会在跨秒时翻转，幂等红） | `{sessions, recent, post_jobs}`（在飞清单，`(种类, 场次)` 多槽：`offline` / `minutes` / `transcode`）、每行十二键（`dir` / `session_id` / `dir_exists` / `kind` / `suspected` / `state` / `date` / `mode` / `segments` / `has_offline` / `added_at` / `last_opened_at`；`state` 三档：`recording` 仅当这一行就是本进程此刻在录的活跃场次、盘上有 `session.json` 一律 `ended`、没有则 `null`——判据正本在 `docs/contract.md` 4.8）；清单次序是确定值：最近登记的排最前（疑似档 → 会议档 → 空目录）；路径与目录名掩成 `<path>` / `<session-id>` |
| | `status-after-run.json` | 进程内 TestClient（会后进度读数节点 `a9affaa8`；`speakers` 一格由人名节点 `91b6752e` 加进来）：往临时场次目录里按真实形状落逐块进度记录（两路各两块，一块静音、一块失败）与状态文件，登记进工作台之后读 `GET /api/sessions/{id}` | `dir` / `session_id` / `kind` / `state` / `date` / `mode` / `segments` / `has_offline` / `post` / `offline` / `transcode` / `minutes` / `speakers` 十三键；`offline` 里两路各一格（`source` / `done` / `total` / `remaining` / `current_chunk` / `counts` 四档计数）；`speakers` 是身份集。块号、块数、四档计数是确定值，路径与场次目录名掩成 `<path>` / `<session-id>`，墙钟戳掩 `<iso>`、走秒量掩 `0` / `null`。采的是**跨进程那一档**：`offline.state` 照盘上如实答 `done`、`offline.in_process` 如实答 `false` |
| | `status-interrupted.json` | 进程内 TestClient（打断会后节点 `b1c6a58e`）：落逐块进度记录（mic 两块里跑了一块）与一版 `interrupted` 状态文件，登记进工作台之后读 `GET /api/sessions/{id}` | 形状与 `status-after-run` 同体：`offline.state` 照盘答 **`interrupted`**（折档白名单里有它，不许折回 `running`）、`in_process` 如实 `false`；`sources` 两路各一格（mic `1/2`、loopback `0/null`——没开跑的那一路分母读不出如实 `null`）。掩码同上 |
| | `transcripts-both.json` / `transcripts-one-missing.json` / `transcripts-none.json` / `transcripts-dir-missing.json` | 进程内 TestClient（稿子正文端点，38817ecf）：四档逐份采——① `mode: both` 四份稿都在（正文逐字原样）；② `mode: mic` 只采一路（另一路两份稿落 `unrecorded`，会后稿 mic 落 `absent`——采了但没跑过）；③ 一份稿都没有（强杀现场，四路全部 `absent`）；④ 登记过的目录被移走（404 + `session_dir_missing`） | `GET /api/sessions/{id}/transcripts`：请求两件套（`method` + `dir`）+ 状态码 + 掩码后的应答体 `{dir, kinds:[{kind, sources:[{source, text\|reason}]}]}`。两份稿次序 `live → offline`、两路次序 `mic → loopback` 都是契约；在的那一格恰好 `source` + `text`，缺的那一格是 `source` + `reason`；正文行是确定值原样留；404 那份 `message` 里内嵌路径走掩码 |
| `heartbeat/` | `idle.json` | 进程内 TestClient（dev 替身） | 七字段、两字段 `null`、空洞记录为空 |
| | `recording.json` | 同上，等 `recording_alive is True` | 七字段、`recording_alive` 为 `true` |
| | `no-first-frame.json` | 同上，`stall` 先挂上（进程内直挂替身）再 `start`，开场窗口内等 `top_state` 为 `recording` 而 `recording_alive` 为 `null` 的那一读 | 七字段、`top_state` 为 `recording` 而 `recording_alive` 与 `seconds_since_last_tick` 同为 `null` —— **还没有第一帧**的未知档（de899128），界面不再把它等同于「停止」 |
| | `capture-gap.json` | 同上，正常开一场等 `recording_alive is True`，进程内直调 `runtime.apply_fact()` 打进两条区间重叠的 `FrameGap` 事实（同一次冻结、两路刻度线各报一条的真实形状）再读心跳 | 七字段、`recording_alive` 仍为 `true` 而 `capture_gaps.total_count` 为 **1**（两条合并为一条）—— **帧空洞留痕**那一档（gil-stall-hole）：历史事实与当下读数各管一头。`at` 走 `<iso>`、秒数走走秒掩码落 0，`total_count` 是确定值 |

**动作样本的形状**：每个 `actions/*.json` 是一份**请求三件套 + 应答**：

```json
{
  "action": "start",
  "args": {"mode": "both", "dir": "<path>"},
  "status": 200,
  "body": {"ok": true, "revision": 2}
}
```

`action` / `args` 是打过去的那份请求（两个必需请求头由驱动件自动带上），`status` 是 HTTP 状态码，`body` 是掩码后的应答体。**失败样本的 `message` 也逐字留在样本里** —— 它是给人看的整句，界面不该按它做字符串匹配，但契约要能看到它长什么样。

**`materials/*.json` 的形状**：与动作样本同构，但请求侧是**两件套**（`method` + `name`）而不是 `action` + `args` —— 会议资料那四条不是动作（不走 `/api/actions`），上传 / 删除的写头哨（`X-Meeting-Service: 1`）由采集驱动件自动带上，清单 / 取原文件不带头。样本里的 `name` 是打过去的那份请求名（脏名字原样进样本——清洗前后的对照正是契约要看的东西），应答体里的 `material.name` 是服务清洗 / 让位之后的存盘名。

**SSE 帧样本的形状**：`{event, id, data}`，`data` 掩码。`id` 保留线上形状的字符串（`levels` / `transcript` 不带 `id:`，这里就是 `null`）。

**`resync-snapshot.json` 的出处说明**：这一帧走**进程内构造**——用例注册一个真订阅者，把它 512 容量的队列灌满，再打一次 `broadcast_state()`，`offer()` 失败即走 `EventHub.resync()`（清队列 + 补快照帧），`dropped` 恰为 1，采集可复现。这条路径与线上那条是同一段代码，差别只在触发者是进程内的用例而非某个真的卡住的页面。

**`transcript.json` 的出处说明**：真帧来自一个**假转写后端**——一个定时产行的模块写进临时目录，经 `PYTHONPATH` + `transcriber.backend` 的 `module:Class` 语法挂进真服务子进程。行内容与 `ts` 取固定值（fixtures 要的是**帧形状**不是数据）。

## 2. 掩码规则

重写在 `tests/_contract.py` 的 `normalize()`。**确定性的全部目的**：fixtures 由真实服务采集，采集里的墙钟、路径、端口、电平值每次都不同；掩码之后，同一份代码连跑两次 `--regen-contract` 落盘的字节必须**逐字相同**。

### 2.1 占位符（全部自描述）

| 占位符 | 常量 | 掩什么 |
|---|---|---|
| `<iso>` | `ISO_PLACEHOLDER` | ISO 时间戳 |
| `<path>` | `PATH_PLACEHOLDER` | 路径（`log_path` / `dir` / `session_dir` / `materials_dir`，以及自由文本里内嵌的绝对路径） |
| `<session-id>` | `SESSION_ID_PLACEHOLDER` | **工作区目录名**（`session.id` 与动作失败 `detail.session_id`，人给的名字、形状任意） |
| `<port>` | `PORT_PLACEHOLDER` | 端口 |
| `<url>` | `URL_PLACEHOLDER` | 服务 URL |
| `<dbfs>` | `LEVEL_PLACEHOLDER` | 电平类取值 |

自描述的意思是**一眼看出这一格被人换过**：真值里不会出现 `<iso>` / `<path>` / `<session-id>` 这样的串（采集路径产出的是 ISO 时间戳、Windows 绝对路径、工作区目录名、整数端口、数字电平），因此比对时不会把占位符与真值混淆。

**占位符要形状诚实**：`<path>` 与 `<session-id>` 分开是吃过亏的判例——早先两者同掩成 `<path>`，而真服务给 `session.id` 的是**裸目录名**，「掩码通过」于是冒充了「真值通过」（前端按路径形状写 schema，线上快照整份解析失败）。两个占位符现在各说各的形状，读 fixtures 的人从占位符就能看出这一格该长什么样。

### 2.2 逐键规则

| 对象 | 判据 | 换成 |
|---|---|---|
| ISO 时间戳 | 整串匹配 `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z\|[+-]\d{2}:?\d{2})?$`（容忍带小数与 `Z` 的写法） | `<iso>` |
| 路径类 | 键名在 `PATH_KEYS` = `{log_path, dir, session_dir, materials_dir}` | `<path>` |
| 场次目录名 | 键名在 `SESSION_ID_KEYS` = `{session_id}`（动作失败的 `detail`），**或**键名是 `id` 且父键是 `session`（靠键路径区分，见下） | `<session-id>` |
| `url` | 键名是 `url`（字符串或数字都算） | `<url>` |
| `port` | 键名是 `port` | `<port>` |
| 走秒的量 | 键名在 `ZERO_KEYS` = `{elapsed_seconds, duration_seconds, seconds_since_last_tick, wall_seconds, total_seconds, seconds}`（含会中总结那次实测耗时、会后逐块的墙钟 `wall_seconds` 与帧空洞记录的秒数） | `0` |
| 电平 | 键名在 `LEVEL_KEYS` = `{level_dbfs, peak_dbfs, dbfs, peak, bar}` | `<dbfs>` |
| 包络数组 | 键名是 `envelope` | **只保结构与长度**：长度不变、逐根换成 `<dbfs>` |
| 自由文本内嵌路径 | 正则 `(?<![A-Za-z0-9])[A-Za-z]:[/\\][^\s，。；！？：、「」『』（）【】《》"']+`（lookbehind 挡掉 `http:` 这类 scheme） | 就地替换成 `<path>` |

### 2.3 为什么 `session.id` 要按键路径判

`id` 这个键名在两处出现，处理**恰好相反**：

- `session.id` 是**工作区目录名**（人给的名字，形状任意；目录名就是这一场会的名字，软件不按日期生成）→ **掩码 `<session-id>`**；
- `sources[*].id` 是**取值域字面量**（`mic` / `loopback`）→ **原样保留**，掩掉就看不出快照的形状了。

因此 `normalize()` 带着一条从根到当前格的 **键路径** `path`，判据是「键名是 `id` 且它上一级是 `session`」。动作失败 `detail.session_id` 那一格直接以键名 `session_id` 出现（`SESSION_ID_KEYS`），掩成同一个 `<session-id>` —— 服务在那两处给的是同一个值（`session.id`）。同理 `label`（`本地` / `远端`）、`health`、`mode`、`kind`、`phase`、`status`、`level` 这些取值域字面量**一律不掩**。

`alert.message` / `DrainStep.detail` / 失败的 `message` / `error` 是给人看的整句，**句式原样留着**，只有句子里内嵌的绝对路径被换掉。真实样本（`actions/fail-draining.json` 里那一条 `failed_steps[*].error`）：

```
会后流程启动失败：RuntimeError: 假会后钩子按要求抛错；可复制命令：meeting post <path>；log 路径：<path>
```

### 2.4 不掩码的东西

`null` / `true` / `false`、空数组 `[]`、空对象 `{}`、以及所有**取值域字面量**（动作名、`code`、`mode`、`health`、`phase`、`status`、`kind`、`level`、`source`、`sources[*].id` / `label`）逐字保留 —— 它们是语义值，掩掉等于把契约掩没了。

**`revision` 保留真实值**：它由固定的迁移与提交次数决定（`start` 之后是 2、`mute_session` 提交一次之后是 3），因此**同样确定**，不需要掩。`service.version`（`0.1.0`）、`settings` 的取值、`envelope` 的长度也照原样留着。SSE 帧**帧级**的那个 `id:`（`snapshot` / `state` 两帧带它，等于 `data.revision`）同样保留真实值 —— 它与 `session.id` 是两处不同的 `id`，别混。

### 2.5 结构断言在掩码前做

有一类性质**掩码之后看不出来**（电平值都换成 `<dbfs>` 了），因此用例在**掩码前的原始载荷**上做这些断言：

- 顶层九键（或重同步帧的十一键）恰好齐；
- `sources` 两项且次序是 `mic`、`loopback`；
- 录音态两路 `envelope` 长度恰 `ENVELOPE_BAR_COUNT`（15）；
- `levels` 帧每路 `ts is None`、`bar == envelope[-1]`；
- `heartbeat` 七键齐、`service == "meeting"`、`revision` 是 int；
- 快照四态等于该样本该有的那一态；
- **SSE 四帧的自报 `revision` 互相对得上**：`snapshot` / `state` 的帧级 `id:` 等于 `data.revision`，不带 `id:` 的 `levels` / `transcript` 的 `data.revision` 落在同一版上。读帧因此**不依赖到达次序**（`levels` 什么时候来由服务侧的广播节奏定），revision 的确定性由这条自洽性钉住。

这些断言写在 `tests/test_contract_fixtures/` 里，是 fixtures 形状的**第二道保险**。

## 3. 再生成与漂移比对

**默认模式 = 漂移测试**（服务侧形状一漂移就红）：

```powershell
python -m pytest tests/test_contract_fixtures -q
```

它重走一遍真实服务路径采集样本，与盘上 fixtures **逐文件比对**；缺文件或内容不一致即 fail，报出「fixtures 是什么、实时采集是什么」。改了服务行为（键改名、字段增删、`detail` 换档）就会在这里被拦下。

**再生成**（确认差异是有意改的之后）：

```powershell
python -m pytest tests/test_contract_fixtures --regen-contract -q
```

开关由 `tests/conftest.py` 的 `pytest_addoption` 注册（`--regen-contract`，默认关）。两种模式走**同一套采集**，只有最后那一步不同：给开关时重写文件，不给时比对。

**幂等是硬要求**：同一份代码**连跑两次** `--regen-contract`，落盘的字节必须逐字相同（`git status` 里 `contract/fixtures/` 应当没有变化）。第一次跑完之后再跑一次，如果 fixtures 又被改动了，说明采集里混进了没有掩干净的易变量 —— 那是掩码规则的**bug，要去 `tests/_contract.py` 补规则**，而不是反复重跑把抖动存进样本。

**落了盘之后的规矩**：

- fixtures 一律**被 git 跟踪**（`.gitignore` 里没有任何规则盖住 `contract/`）。fixtures 的 diff 就是契约的 diff，它要进版本历史。
- 改了服务行为**就要重跑 `--regen-contract` 并逐条复核差异**，再回写 `docs/contract.md` 的正文。
- **不要手改 fixtures**：它们是采集产物，手改会在下一次比对时被冲掉，还会让「漂移」这件事失去判据。
- 加一个新样本的做法是**在 `tests/test_contract_fixtures/` 里加一段真实采集 + `emit({...})`**，跑一次 `--regen-contract` 落盘，然后在上面第 1 章的清单表里补一行出处。

## 4. 相关文档

- 契约正文（端点、schema、帧形状、`code`/`detail` 表、心跳、客户端义务、保留登记）：`docs/contract.md`
- HTTP 面速查：`docs/api.md`
- 状态与推送的现状叙述：`docs/state.md`
- 索引：`docs/spec.md`
