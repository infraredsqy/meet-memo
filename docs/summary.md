# meeting · 会中总结

> 这份文档写会中总结那一层**已经落地**的那一块（`1987cb20`）：按一下之后从哪读、窗口怎么切、提示词与模型输出是什么形状、端点怎么配、超时与重试的预算落在哪、动作与记录端点各回什么。它是 R7 那一条「十到十五秒出结果」的现状。
>
> 内核在 `meeting/summary/`（`window.py` / `prompt.py` / `client.py` / `parse.py` / `settings.py` / `runner.py`）；服务接线在 `meeting/service/runtime_summary.py` 的 `summarize()` 与 `summaries_dict()`（`ServiceRuntime` 的方法体按职责拆在各 `runtime_*.py` mixin 模块里，`runtime.py` 是聚合壳）。契约正文（逐档 `detail` 与成功体全文）在 `docs/contract.md` 第 4.7 节，HTTP 面的速查在 `docs/api.md` 第 3 章。
>
> 别跟会后纪要那一层混：那一层在 `docs/minutes.md`，由「生成纪要」按钮或 `meeting post` 拉起。

## 1. 它是什么、什么时候跑

会中有人跟丢了，按一下「总结一下」，十到十五秒拿回三小段——**刚才在讲什么 / 讲到哪儿了 / 丢掉了什么**。几条就够，是一块救生板，看完就关。

- **手动触发**：这一层没有「什么时候该总结」这件事。V1 那套频率门控（每 10 段或 300 秒一档、50 段或 1500 秒一档、关闭档）留在 V1，没搬过来。
- **一批可注入的函数与类**：`meeting/summary/` 不注册路由、不碰快照、不写文件。转写行的 sink 把行喂进内存缓冲，动作端点把窗口与配置交给 `run_summary()`。
- **跟会后纪要层的分工**（两层的名字里都有「总结 / 纪要」，服务的是会议的两头）：

  | | 会中总结（这份） | 会后纪要（`docs/minutes.md`） |
  |---|---|---|
  | 什么时候跑 | 人按一下，随叫随到 | 人按才跑：「生成纪要」按钮或 `meeting post` |
  | 吃什么 | 内存里的实时转写窗口 | 盘上的两条稿子（会中稿 + 会后稿，都在时两份一起给） |
  | 模型怎么调 | 自己调 OpenAI 兼容端点 | 外包给一个 headless DSH 子会话 |
  | 预算 | 十到十五秒（默认 deadline 25 秒） | 四小时（`minutes.timeout_seconds`） |
  | 产物 | 几条，出现在应答体里，关掉即消失 | 一份 `meeting_final.md` 落在场次目录 |
  | 落文件 | **一处都不落** | 状态文件 + 纪要 + 中间产物 |

会后纪要那一层的输入如今还多一份**人名表**（`91b6752e`）：机主在工作台确认过的名字存这一场目录的
`meeting_speaker_names.json`，出纪要时随提示词带上、附录照抄——那是盘上的账，与本层的内存窗口无关。

## 2. 窗口：吃的是内存里的实时临时稿

**读的是内存里的一条追加缓冲，不读盘。** `TranscriptBuffer`（`meeting/summary/window.py`）收转写行，喂它的是 `bind_transcriber()` 包出来的那个 sink——收行先于 `call_soon_threadsafe`，`bind()` 调两次也只装一层。容器线程安全（写的在转写 worker 线程、读的在事件循环），`lines()` 交回副本，行数上限两万（到顶从头丢最老的那些，窗口取的是尾部，丢头不影响）。

盘上那份 `meeting_transcript_<source>.md` 只够人看：它的时间戳是分钟级的 `[HH:MM]`（秒被丢掉、同一分钟会撞成一个戳），也没有单调序号。要按时间切窗口就得自己收一份带完整墙钟 `ts` 的。

**四档取值**由用户挑，缺省 `15m`：

| 取值 | 含义 | 秒数 |
|---|---|---|
| `5m` / `15m` / `30m` | 最近 5 / 15 / 30 分钟 | 300 / 900 / 1800 |
| `full` | 整场 = 缓冲里的全部 | 无上限 |

**边界按行首时间戳切，不按行数切**：锚点是**缓冲末行的 `ts`**，`cutoff = 末行 ts − 窗口秒数`，从尾部向前收到 `ts < cutoff` 为止，取的是连续的一段尾巴。转写停转、说话变稀的时候，末行比此刻早多少都不影响窗口取到那几行（拿当前墙钟当锚点会把这种缓冲切成空窗口）。同一个 `ts` 上挤着一批行时**整批一起收或一起丢**，绝不从中间断开——事故那一场里时间戳曾经被压进同一分钟，按行数取窗口就会取错内容。缓冲为空那一格才用当前墙钟兜底，交回空窗口。

**装配成文本**（`render_window()`）：一行一句、原句逐字保留，形状是

    [HH:MM:SS] 正文 · 本地

时间是那一行的墙钟时刻（模型要能说清「讲到哪儿了」），来源标在行尾（`mic` → `本地` / `loopback` → `远端`）。识别失败的占位符（`[[识别失败: 超时 20s]]`）原样留在里面——它是一条信息，人正是想知道丢掉了什么，提示词负责讲清楚，这一层不替它改写。

## 3. 提示词与模型输出契约

`build_messages()` 组两条消息：system 是一段身份说明加**七条硬规则**，user 是窗口正文（`【最近 N 分钟的转写】` 或 `【本场转写（整场）】`）加一段任务说明（三件事各一到三条、本条的窗口范围与行数、会议开始时刻、缺口怎么讲、只输出 JSON）。组装方式照 V1 `mnotes_core/summarizer.py` 的 `_build_messages()`，内容换成会中这一档的三件事。窗口里的缺口单独有一句嘱咐：见到几个就在【丢掉什么】里写几条、说清哪一路、大概什么位置；**一个缺口都没有就写一条说明「这段窗口里没有识别失败的缺口」**，不许猜缺口里可能说了什么。

**模型输出契约照搬 V1 B15**，改一处：元素仍是字符串，但每条 bullet **以三个固定标签之一开头**，解析时按标签归到三小段。

- **强制合法 JSON，形状固定 `{"bullets": [...]}`**；system 里明文禁止 JSON 以外的任何文字、解释、注释、代码围栏。
- **围栏容忍**：`parse_bullets()` 三步走——`strip()`；整段以 ` ``` ` 开头就去掉首行、末行；两份候选（整段 + 正则 `\{.*\}` 带 `re.DOTALL` 抠出来的第一段）。只接受「顶层 dict、`bullets` 是 list、每个元素 strip 后是非空字符串」。
- **空数组合法**：`{"bullets": []}` 交回空列表，算**成功**（模型判断这一段没有可讲的要点），不是失败。
- **三小段的标签**：`【讲什么】` / `【讲到哪】` / `【丢掉什么】`，只认**行首**那一处（`我们讲到了【讲到哪】这一步` 这种算认不出的条目，原样落在第一段，免得「去掉标签」这一步把正文咬掉一块）。认出的归位并去掉标签，**个别认不出不算失败**。
- **一条标签都认不出来算解析失败**（触发重试）：三条都认不出，说明模型压根没按约定的形状答，那几条放进哪一段都是猜的。

**重试只由解析失败触发**（照 V1）：交回 `None`，或者三条标签一条都认不出，就再试一次；第 2 次起在 messages 末尾追加一条提示（「上一次的输出不是合法 JSON（或者一条固定标签都没用上）」加一份形状示例）。端点的四档错误**当场收工**，不再发下一次请求——理由与 V1 一样：同一个错误重放一遍没有意义。标签写在 `prompt.py`、认在 `parse.py`，两处同源。

## 4. 端点是怎么调的

`ModelClient`（`meeting/summary/client.py`）用**标准库 `urllib.request`**，运行时不引 `httpx` / `requests`。语义照搬 V1 的 `_call_api()`：

- `POST {base_url}/chat/completions`（`base_url` 末尾多余的斜杠收掉），两个头：`Authorization: Bearer {api_key}`、`Content-Type: application/json`；
- 请求体四格——`model` / `messages` / `temperature: 0.3` / `stream: false`；`extra_body` 非空就**原样并进去**（厂商特有字段）；
- 读 `choices[0].message.content`；thinking（`reasoning_content`）丢掉；
- 非 2xx **优先取**响应体里 `error.message` 的原文，读不出来就退回响应体原文。

**四档错误**（`SummaryError.reason`）：`timeout`（请求超时）、`bad_status`（连不上或非 2xx）、`bad_json`（响应体不是 JSON / 没有 `choices[0].message`）、`no_content`（content 是空的）。

`post` 是**注入点**：用例与探针都从这里进，两种写法都收（`post(url, json=…, headers=…, timeout=…)` 与 `post(HttpRequest)`），因此这一层的用例一条网络请求都不发。`temp` / `model` / 密钥是**构造那一刻的快照**，中途改配置不影响已经在飞的那一次。

## 5. 配置：九个键

六个键是**启动时读一次**、归 `pending_restart`（与 `minutes.*` 同档），改了要重启；三键收在
热读档，写盘那一刻生效值就换 —— 缺省档 `summary.default_window`（`311f84f5`，快照
`settings.summary_default_window` 当场换过去）与会中切模型两键 `summary.presets_json` /
`summary.active_preset`（换一套模型预设不用重启，会中可切）：

| 键 | 默认值 | 合法范围 / 形状 | 谁在用 |
|---|---|---|---|
| `summary.base_url` | `https://api.deepseek.com/v1` | 非空字符串 | 端点地址（这一层说的是 OpenAI 兼容协议，换厂商改这个键） |
| `summary.api_key` | `null`（= 没配） | 非空字符串，**可空键** | 请求头那一格；**可以走环境变量 `MEETING_SUMMARY_API_KEY`** |
| `summary.model` | `deepseek-chat` | 非空字符串 | 请求体里那个模型名 |
| `summary.extra_body_json` | `""`（= 不并） | **一段合法 JSON 对象的文本** | 厂商特有字段，原样并进请求体；空串与 `null` 同解 |
| `summary.presets_json` | `""`（= 没配，回落平铺四键） | **一段 JSON 数组的文本**：每套一个对象，`name` 必填（非空白、不重名），`base_url` / `api_key` / `model` / `extra_body_json` 可省 | 多套端点预设整装一个键（会中切模型）；套数不设上限，**预设里的密钥不进快照、不在界面回显** |
| `summary.active_preset` | `""`（= 不选预设，用平铺四键） | 空串，或某套预设的 `name` | 当前用哪一套的指针（会中切模型）；「名字在不在清单里」是 `settings_update` 的写前校验，不在当场拒 |
| `summary.deadline_seconds` | `25.0` | `1.0`–`3600.0` 秒 | 一次总结的**墙钟 deadline** |
| `summary.max_attempts` | `3` | 整数 `1`–`10` | 尝试次数**上限**（真正的刹车是 deadline） |
| `summary.default_window` | `15m` | `5m` / `15m` / `30m` / `full` | `args.window` 省略时用的那一档 |

- **密钥那一格走环境变量**：`MEETING_SUMMARY_API_KEY` 与环境变量那一层另外五个走同一条通路（装载时求值、映射到 `summary.api_key`），优先级仍是环境变量 > `config.json` > 代码默认值。密钥不进仓库：配置文件里写它，或者用环境变量压住它。**预设模式不经过这一格**：指针指到某套预设时，密钥取预设里那一格，平铺键（含环境变量）不参与。
- **`summary.api_key` 是可空键**：与 `devices.mic` / `devices.loopback` 同形——没配就是 `None`。动作层据此点名缺的键（`detail.missing` 报的是**配置键全名**，例如 `["summary.base_url", "summary.api_key"]`）。
- **`extra_body_json` 用一段文本承载**，不用嵌套段：V2 的配置层是「点号扁平键名的单层映射」、装载时明确拒收 dict 值，为这一个键放开那条硬约束不划算。写进去的不是合法 JSON 对象时，装载那一层当场拒掉。
- **预设两键的取值次序（会中切模型）**：`summary.active_preset` 指到清单里**有名有姓**的那一套 → 端点四件套整套装它的；其余一切（两个键没配、清单解析失败、指针空、指向的名字不在清单里）→ **回落平铺四键**，存量 `config.json` 一个字不用改。预算三件套与缺省档始终走平铺键 —— 它们不在预设里。实际用的是哪一套记在 `from_preset`（空串 = 平铺键），`missing` 因此能把「这套预设缺哪一格」说到点上：预设模式报 `summary.presets_json[名字].api_key` 这样的形状。切换走设置抽屉「快速总结模型」下拉（`settings_update` 写 `summary.active_preset`），两键都收热读档，会中换一套当场生效。
- 九个键的键名、默认值与取值域在 `meeting/config.py` 的四张表里（默认值 / 取值域 / 环境变量映射 / 三分表），读数与归一化在 `meeting/summary/settings.py`。

## 6. 墙钟 deadline 与重试

V1 的「3 次 × 硬编码 60 秒超时」（最坏 180 秒）**没搬**——它与「按一下，十到十五秒出结果」直接冲突。现在的口径是**一条墙钟 deadline**（`summary.deadline_seconds`，默认 25 秒）：

- `deadline = 开始时刻 + deadline_seconds`；**每一次请求的超时取「当时的剩余预算」**（25 → 15 → 5 这样的递减），剩余不足 0.05 秒就不再发下一次请求。
- `max_attempts`（默认 3）只是**上限**，预算用尽即停。
- 停下来时**如实报数**：`elapsed_seconds`（等了多久）与 `attempts`（试了几次）在成功与失败两条路上都填。

**三种收场**（`run_summary()` 的返回值 `SummaryOutcome`）：

| 收场 | `ok` | 内容 |
|---|---|---|
| 成功 | `true` | `bullets` / `what` / `where` / `lost` 都填上，`unlabeled` 记下漏标签的条数 |
| 窗口里一行都没有 | `true` | 三段都是空的，`lines` 为 0，**一个模型请求都不发** |
| 失败 | `false` | `error` 是一句人话、`detail` 是机器可读的那一格 |

**配置缺键挡在出门前**：三个端点键缺一个就当场收工，`detail = {"missing": [...]}`，**一个请求都不发**（判据是记账的假客户端读数为 0）。预设模式下报的是那套预设里缺的格（`summary.presets_json[名字].api_key` 这样的形状）。

## 7. 服务这一层：动作与记录端点

动作 `summarize`（`meeting/service/runtime_summary.py` 的 `summarize()`）的**判定次序是固定的**：判态 → 校验 `window` → 看转写健康 → 看模型端点配置 → 才动手。前面几档没过时**一个模型请求都不发出去、一条记录都不写**。

**请求**：

```json
{"action": "summarize", "args": {"window": "15m"}}
```

`window` 取 `5m` / `15m` / `30m` / `full`；键省略或给 `null` 时取配置的 `summary.default_window`；**其余任何取值（含空串、纯空白、大小写变体、数字）当场回 `invalid_setting`(400)**，不静默落缺省档。

**成功体**比通用信封多一个 `summary` 键，八格：

```json
{"ok": true, "revision": 12,
 "summary": {"window": "15m", "window_label": "15 分钟", "at": "<iso>",
             "elapsed_seconds": 9.4, "lines": 37,
             "what": ["..."], "where": ["..."], "lost": ["..."]}}
```

`window_label` 是人话标签（`5 分钟` / `15 分钟` / `30 分钟` / `整场`），`lines` 是窗口里的行数。**`ok` / `bullets` / `attempts` / `detail` 不进应答体**——失败那几档走失败信封的 `code` + `detail`。

**两个新的 `code`**（第 4.4 节那张表跟着长了）与逐档 `detail`：

| 情形 | `code` | HTTP | `detail` |
|---|---|---|---|
| 没有在录的场次（`idle` / `ended`） | `not_recording` | 409 | `{}` |
| `window` 取值非法 | `invalid_setting` | 400 | `{key, value, legal}` |
| 转写停了 / 没起过 | `transcript_unavailable` | 503 | `{"health": "stopped"}` |
| 模型端点没配 | `summary_failed` | 502 | `{"missing": ["summary.base_url", …]}`；预设模式报那套预设里缺的格（`summary.presets_json[名字].api_key`） |
| 预算用尽 / 端点报错 / 解析连试带败 | `summary_failed` | 502 | `{"elapsed_seconds": 25.0, "attempts": 3}` |
| 动手之后出的意外 | `summary_failed` | 502 | `{"error": "<异常类型>: <消息>"}` |

**一次模型调用跑在工作线程上**：`summarize()` 是一条协程，`run_summary()` 经一条专用 daemon 线程池（两条工作线程）提交，配一个 `asyncio.wait_for` 的墙钟 deadline。不这么做的话，25 秒的等待会把 `/api/state`、三个 SSE 通道、心跳与健康节拍一起按住，而设计章 4.5 对进行中那一档写的正是「总结区在转，**其余照常**」。到点时放弃的是**等待**，已经交出去的那次调用照跑到底。

**并发**：服务侧**不做闸、不排队**。同一次会里两个页面同时按下就是两次独立的总结，各自记一条；界面自己按「进行中」把按钮禁掉。

**本场总结记录**：`GET /api/summaries`，任何状态下都 `200`，应答体两键——

```json
{"session_id": "<场次目录名或 null>", "entries": [
  {"window": "15m", "window_label": "15 分钟", "at": "<iso>", "elapsed_seconds": 9.4,
   "what": ["..."], "where": ["..."], "lost": ["..."]}]}
```

记录那一条是**七格**（比动作成功体少一格 `lines`）。只记**成功产出**的那几次（失败没有「那几条」可记）、按时间先后；没有场次或一次都没按过时 `session_id` 为 `null`、`entries` 是空数组。

## 8. 结果不进快照、不落文件

**结果是一过性的视图产物**，这条口径落在三处：

- 走 `summarize` 动作的**应答体**，记录走 `GET /api/summaries` 的内存表，**不加 SSE 事件**；
- **不进快照**：快照是**会议的**权威状态。打一次总结前后各取一次 `/api/state`，`revision` 与顶层九键逐字相同；`summary` 这个键在快照里一次都不出现（前端契约层用 `.strict()` 与「顶层恰好九键」两道硬门钉住它）；
- **不落任何文件**：`meeting/summary/` 与接线这一层都不写盘。记录活在内存里、会话级——新的一场 `start` 把缓冲与记录**一起**清空，服务关停同样清掉。

## 9. 测试与验证

- 用例一律**注入假件**：客户端走 `post` 注入点，服务侧走 `runtime.summary_client`，转写后端走 `runtime.transcriber_backend` 加 `bind(runtime.bind_transcriber(...))`。**一条真实模型端点请求都不发**。
- 出口还有一道兜底：`tests/conftest.py` 的 `_no_real_model_call`（autouse）按目标地址判，本机回环放行、其余当场抛 `RealModelCallForbidden`——`summary.base_url` 的配置默认值就是真地址，忘了注入假客户端就会被它拦下。
- 真实端点与真实素材的实测走一次性脚本（`summary_probe.py`，四档 `--only window/fake/real/material`），**会花钱的两档收在 `--allow-network` 后面、默认拒跑**。这一档的实测结论：四档窗口（最短 5 分钟到整场 411 行）逐档 1.74–2.83 秒，全在预算内。脚本与它跑出来的证据属于一次性材料，不随仓库保留。
- 为什么这么办写在 `README.md` 的「测试与验证纪律」与 `docs/v1-lineage.md` 第 4 章。

## 10. 相关文档

- 请求 / 应答 / 逐档 `detail` 的正文：`docs/contract.md` 第 4.4、4.7 节
- HTTP 面速查（端点表、动作表与 `code` 表在第 3 章；九个键在 `settings_update` 那一张表里，第 4 章）：`docs/api.md`
- 服务现状（端点表、配置键、跑测试）：`docs/service.md`
- 前端这一侧（总结区纯逻辑、请求状态机、契约 schema）：`web/README.md`
- V1 里搬过来的那几件事与不搬的那几条：`docs/v1-lineage.md` 第 4 章
- 需求锚（R7 会中一键总结、R6 实时转写是它的前提）：`docs/requirements.md`
- 索引：`docs/spec.md`
