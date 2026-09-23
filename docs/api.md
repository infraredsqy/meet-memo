# meeting · HTTP 面

> 这份文档写服务对外的那几条线：五个端点、动作的信封、动作表与 `code` 表、以及本地防线。
>
> 读者是接这套 API 的人：前端、验收脚本、命令行复核。四态状态机、快照字段、`session.json`、SSE 帧形状、告警判定写在 `docs/state.md`；怎么起服务写在 `docs/service.md`。
>
> 代码落点：路由只在 `meeting/service/routes.py` 注册一次；动作的实现与 `code` 表在 `meeting/service/runtime.py`。

## 章节目录

| 章 | 标题 |
|---|---|
| 1 | 五个端点 |
| 2 | 请求信封与应答形状 |
| 3 | 动作表与 `code` 表 |
| 4 | `settings_update`：写哪些键、什么时候生效 |
| 5 | 端口与本地最低防线 |
| 6 | 复核时怎么打 |

## 1. 五个端点

| 端点 | 给什么 |
|---|---|
| `GET /` | 构建产物（`web.dist_dir`，默认 `web/dist`）有内容就是那份 React 应用；目录不存在就给一页说明（**存在性按每请求判定**，产物一建好刷新即可见，不必重启服务）；**目录在但是空的回 404**。三档与说明页文案的全文见 `docs/contract.md` 第 1 章 |
| `GET /debug` | 骨架自检页：读原始快照、按开始 / 结束 / 静音、看事件流、看心跳原始应答。零依赖、无构建，页面内显式标注**非产品界面、不承担任何告警职责** |
| `GET /api/state` | 完整状态快照，`200`。body 与 SSE 首帧那份 `snapshot` 同源（同一个 `to_dict()`） |
| `GET /api/events` | SSE。**第一帧一定是 `snapshot`**，之后是 `state` / `levels` / `transcript` 三条通道 |
| `GET /api/heartbeat` | `{"service": "meeting", "server_time", "revision", "top_state", "recording_alive", "seconds_since_last_tick"}`，`200`，任何状态下都给。`service` 这个字面量是端口探测认亲用的，必须有 |
| `POST /api/actions` | 动作端点，见第 2–4 章 |

`/api/*` 的应答一律带 `Cache-Control: no-store`，并且**不发 `Access-Control-Allow-Origin`**（不开 CORS，前端与本服务同源）。

## 2. 请求信封与应答形状

请求：

```json
{"action": "start", "args": {"mode": "both"}}
```

- **两个必需请求头**：`Content-Type: application/json` **且** `X-Meeting-Service: 1`。缺任一条回 **403**（挡表单式简单请求）。这道校验在中间件里，先于路由匹配生效。
- 成功：`200 {"ok": true, "revision": N}`。
- 失败：`4xx {"ok": false, "code": "...", "message": "...", "detail": {...}}`。`code` 是机器可读的（界面据此选失败块的画法），`message` 直接能显示。

## 3. 动作表与 `code` 表

六个动作：`start` / `stop` / `mute_session` / `settings_update` / `reprobe` / `dev_inject`。七个 `code`：

| `code` | HTTP | 意思 | 谁会回它 |
|---|---|---|---|
| `already_recording` | 409 | 已经有一场在录 | `start` / `reprobe` |
| `draining` | 409 | 上一场还在收尾（或收尾失败后停留、或启动扫描到的那一场没收完），这场不能开 | `start` |
| `capture_unavailable` | 503 | 采集后端不可用：没接入、或设备起不来 | `start` |
| `not_recording` | 409 | 现在没有在录的场次 | `stop` / `mute_session` / `dev_inject` |
| `invalid_setting` | 400 | 参数或键名非法（校验都在动手之前） | 各动作 |
| `persist_failed` | 500 | 配置写盘失败，这次改动一处都没生效 | `settings_update` |
| `action_unavailable` | 404 | 这个后端没有这个动作 | `dev_inject`（非 dev 后端） |

### 逐动作 × 逐态的可用性

| 动作 | `idle` | `recording` | `alert` | `ended`·`done` | `ended`·`draining` |
|---|---|---|---|---|---|
| `start` | 200：建目录、写 `session.json`、进 `recording`；**扫到未收尾场次的 `idle` 例外 → 409 `draining`** | 409 `already_recording` | 409 `already_recording` | 200：从 `ended` 直接进 `recording`，顺手清掉上一场的 `ended` 摘要 | 409 `draining` |
| `stop` | 409 `not_recording` | 200：进 `ended·draining`，时长冻结 | 200：同上（进 `ended` 不必先回 `recording`） | 409 `not_recording` | 409 `not_recording` |
| `mute_session` | 409 `not_recording` | 200：`alerts.*.muted` 立刻变 `true` | 200：同上 | 409 `not_recording` | 409 `not_recording` |
| `settings_update` | 200 | 200 | 200 | 200 | 200 |
| `reprobe` | 200：重新枚举并试开设备，两路快照随之更新（`revision +1`） | 409 `already_recording` | 409 `already_recording` | 200：同上 | 200：同上（采集已经停了，重新探测不干扰在飞的东西） |
| `dev_inject` | 409 `not_recording` | 200（非 dev 后端 404） | 200（非 dev 后端 404） | 409 `not_recording` | 409 `not_recording` |

`settings_update` 写的是配置文件，与「这一场录到哪儿」无关，因此**任何状态下都可用**。`mute_session` 只在本场收尾之前可用 —— 收尾完成后本场已无可静音，复位发生在下一场 `start`。`reprobe` 只在**没有场次在跑**的时候可用（`recording` / `alert` 两态回 `already_recording`）：设备在开场前定下来，会中换设备是明文不做的那一档。

### 逐个动作的语义

**`start`** —— 失败顺序是固定的：先判态（`already_recording` / `draining`）→ 再校验 `args`（`mode` 非法 → `invalid_setting`(400)）→ 再判后端可用性（`capture_unavailable`）→ 最后才建目录写文件。**态不对或后端不可用时盘上不留任何目录。**

- `args.mode` 取值 `both` / `mic` / `loopback`，**缺省 `both`**。
- `189` 的拒绝路径 `detail` 带 `session_id` / `dir` / `elapsed_seconds`（场次目录名、场次目录路径、已录秒数），够界面画「已经有一场在录」。
- `draining` 的拒绝路径 `detail` 带三项：场次目录（`session_id` / `dir`）、失败步（`failed_steps`，每项 `{id, error}`，没有失败步就是空数组）、场次 log 路径（`log_path`）。

**`stop`** —— 进 `ended·draining`，时长就地冻结，然后服务按四步收尾跑到终态（四步与终态规则见 `docs/state.md` 第 1 章）。收尾成功档下 `top_state` 停在 `ended`、`ended.phase == "done"`、`session` 保留。

**`mute_session`** —— 本场不再出声：`alerts.*.muted` 与横幅上那句「本场已静音」一起变，`revision +1`。它**不改**持久化开关 `alarm.sound`，**下一场 `start` 自动复位**。静音期间新起的告警同样继承静音。

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

## 4. `settings_update`：写哪些键、什么时候生效

请求的 `args` 就是**「配置点号键名 → 值」的扁平映射**，只有这一层、不按键前缀分组：

```json
{"action": "settings_update", "args": {"asr.timeout_seconds": 30}}
```

应答是 `{"ok": true, "revision": N, "applied": [...], "pending_restart": [...]}` —— 两个数组里也是同一套点号键名。**成功体里没有 `message` 字段**，需要给人看的话由界面按两个数组自己组织。

**合法键名是逐条列出的 31 个**（不按前缀归组、没有前缀白名单）。表外的任何键名一律 `invalid_setting`(400)，`devices.mic_device` 这种没定名的名字也在表外。

| 归类 | 键 | 为什么 |
|---|---|---|
| `applied`（6 个） | `alarm.sound` / `asr.max_segment_seconds` / `asr.timeout_seconds` / `asr.max_new_tokens` / `asr.repeat_max_unit` / `asr.repeat_min_coverage` | 这六个是热读键，写盘成功就立刻生效。`alarm.sound` 收在这一档，于是勾选出声开关**当场**改运行期行为 |
| `pending_restart`（25 个） | `web.port` / `web.dist_dir` / `output.root` / `log.level` / `log.dir` / `log.max_bytes` / `log.backup_count` / `alert.silence_dbfs` / `alert.silence_seconds` / `alert.recover_seconds` / `alert.alarm_suppress_seconds` / `heartbeat.interval_seconds` / `capture.backend` / `transcriber.backend` / `post.hook` / `post.engine` / `post.model` / `post.dtype` / `post.chunk_seconds` / `minutes.hook` / `minutes.chunk_lines` / `minutes.max_segments` / `minutes.timeout_seconds` / `devices.mic` / `devices.loopback` | 这些值在启动时读一次，写盘成功但要重启才生效 |

- **`alarm.sound` 与 `asr.*` 一起当场生效**：写盘那一刻主 log 落一行「alarm.sound 收在热读档，本刻起出声开关 = …」，服务侧同时重取一次生效值视图并提交 —— 紧接着的 `GET /api/state` 里 `settings.alarm_sound` 就是新值，`revision` 涨一格，界面当场按新值决定响不响（设计稿 `docs/ui-design.md` 4.5 要的「勾选立即生效」在这里成立）。
- **会后那四个键**（`post.engine` / `post.model` / `post.dtype` / `post.chunk_seconds`）也归 `pending_restart`：`post.engine` 与三个后端选择键同形（`auto` / `null` / `module:Class`），`post.model` 收 HF 仓库 id 或本地快照路径，`post.dtype` 只认 `bfloat16` / `bf16` / `float16` / `fp16` / `float32` / `fp32` 这几个档位名，`post.chunk_seconds` 是每块音频的长度上限（整数 `0`–`86400`，默认 `450`，`0` = 关掉分块、整段一次跑）。键名与取值范围的判据在 `meeting/config.py`，档位名要跟 `meeting/offline/engine.py` 的 `DTYPE_NAMES` 对得上，写错的名字在写盘前就被拦下。
- **会后纪要那四个键**（`minutes.hook` / `minutes.chunk_lines` / `minutes.max_segments` / `minutes.timeout_seconds`）同样归 `pending_restart`：`minutes.hook` 与三个后端选择键同形（`auto` / `null` / `module:Class`），另外**收 JSON 的 `null`**（表示「离线跑完不产生纪要」）；`minutes.chunk_lines` 是每块行数（整数 `10`–`1000`）、`minutes.max_segments` 是每条 source 的分段上限（整数 `1`–`20`）、`minutes.timeout_seconds` 是一次生成的墙钟上限（整数 `60`–`86400`）。键名与取值范围在 `meeting/config.py`。这一层怎么跑见 `docs/minutes.md`。
- **配置文件本身的键形与请求同位**：`~/.config/meeting/config.json` 里也只有一层点号扁平键名，例如 `{"alert.silence_seconds": 3}`。嵌套形状不合法。
- **只写用户配置文件里已有的键 + 本次改动的键**，不把代码里的默认值落盘 —— 一次 `settings_update` 不会凭空长出一份写着全部默认值的配置文件。
- **校验在写盘之前**：至少落地方案点名的「超时必须大于段长」（`asr.timeout_seconds > asr.max_segment_seconds`），违规回 `invalid_setting`(400) 并说明是哪两个键。
- **被环境变量压着的键不改分类**：`asr.*` 照样进 `applied`、`output.root` 照样进 `pending_restart`；「被环境变量压着」这件事只在主 log 留一条 warning（键名 + 写盘的值 + 压住它的环境变量名），运行期仍由环境变量说话。
- **写盘失败回 `persist_failed`(500)**，`failed` 一处都不生效：要么全部写入并生效，要么原样不动。

## 5. 端口与本地最低防线

- 默认端口 **8765**，只监听 **`127.0.0.1`**，这一条不可配（没有 `web.host` 这类键）。
- 端口被占**不猜、不静默换端口**，两种情形都以退出码 `2` 结束：占用者是 meeting 服务（`GET /api/heartbeat` 应答里有 `"service": "meeting"`）→ 说「已经有一个 meeting 服务在跑」；占用者是别的程序 → 写明端口被谁占着、怎么改。
- 三道最低防线，挡的是「本机浏览器里的恶意页面往 `127.0.0.1:8765` 发请求」：
  1. **Host 校验**：`Host` 不是 `127.0.0.1[:port]` / `localhost[:port]` 就 `403`（挡 DNS rebinding）。
  2. **动作要自定义头**：`POST /api/actions` 的两个必需头，缺任一条 `403`。
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
curl.exe -s -X POST http://127.0.0.1:8765/api/actions `
  -H "Content-Type: application/json" -H "X-Meeting-Service: 1" `
  -d '{\"action\":\"start\"}'
```

默认 `capture.backend = auto` 解析到真采集后端：设备起得来时 `start` 真的开一场（设备全起不来才回 `503 capture_unavailable`，`detail.reason` 写明哪一路为什么）；显式配 `null` 替身时 `start` 回 `503 capture_unavailable`（如实报「采集后端未接入」，盘上不留任何场次目录）；dev 替身给开发与测试用（合成电平 + 故障注入，不写真实音频）。

## 7. 相关文档

- **双端契约（端点口径、快照 schema、SSE 帧形状、动作信封与 `code`/`detail` 表）：`docs/contract.md`**
- 四态、快照字段、`session.json`、心跳、告警、SSE 帧形状：`docs/state.md`
- 怎么起服务、配置、日志、退出码、跑测试：`docs/service.md`
- 后端协议与 dev 替身：`docs/backend-protocols.md`
- 前端工程怎么跑、怎么构建：`web/README.md`
- 契约的可机读基准（25 个 golden 样本与掩码规则）：`contract/README.md`
- 索引：`docs/spec.md`
