# meeting · 服务骨架

> 这份文档写**服务骨架的现状**：怎么装、怎么起、命令面、配置键的正本、log 与退出码、怎么跑测试。
>
> 一句话边界：现在起得来的是**一个有会话、能推、能问、能真录音、能边录边出转写临时稿、收尾会转 MP3、会后动作等人按按钮的服务**。
>
> 别的面各归各的正本，本文不重复：HTTP 面与动作见 `docs/contract.md`（可机读基准在 `contract/README.md`）；四态状态机、快照、`session.json`、心跳判据、SSE 帧形状、dev 替身的语义见 `docs/state.md`；四个后端协议的成员与解析见 `docs/backend-protocols.md`；测试纪律见 `docs/rules.md`。

## 1. 怎么装、怎么起

- Python ≥ 3.13。运行时依赖：`starlette` / `uvicorn` / `anyio` 三条主干 + **音频四件套** `numpy` / `sounddevice` / `soundcard` / `pydub`（录音是产品主体，不放可选 extra）。转写的模型依赖在 `.[asr]` extra 里；`torch` 那格是 CUDA 12.8 构建，要带索引装：`pip install --extra-index-url https://download.pytorch.org/whl/cu128 "meeting[asr]"`（CPU 档写 `torch==2.9.1`、不用索引）。**不装这一格照样能起服务**：重依赖全在函数体内惰性 import。
- **`ffmpeg` 要在 PATH 上**（`pydub` 靠它转 MP3）。缺席时服务照常起、照常录，收尾转码如实记 `failed`，PCM 原样留在场次目录里。
- 开发与测试用的 dev 替身（非产品路径，默认关闭）：把 `capture.backend` 或 `transcriber.backend`
  写成 `dev`（前者也可走 `MEETING_CAPTURE=dev`）。替身给什么、三档故障怎么注入、恢复口径，
  见 `docs/state.md`。

命令面三个词（`meeting web` 走装好的 console script，或在仓库根 `python -m meeting web`，两种等价）：

| 命令 | 干什么 |
|---|---|
| `meeting web` | 起本地服务，前台跑到 `Ctrl+C` |
| `meeting post <场次目录>` | 只跑会后那一套（归档补齐 → 会后离线转写 → 纪要），跑完就退。`--force` 无视终态重跑两层；不给时按幂等闸：完成的不重跑、停在 `running` 拒跑 |
| `meeting replay <场次目录>` | 重放验证：素材截断复制进 `--out`，在拷贝上重跑会后离线链；只验证、不拉纪要。`--seconds N` / `--chunks N` / `--out` 三个开关，上限两种给其一 |

命令行的开关语义与用法错误行为以 `meeting/cli.py` 的实现为准；面向使用者的三条命令速查在仓库根 `README.md`。

起好之后终端打印 `URL：…` 与 `log 路径：…` 两行；端口用 `MEETING_PORT` 改，浏览器开关用 `MEETING_NO_BROWSER=1`。

### `Ctrl+C` 的分档

`SIGINT` 与 `SIGBREAK` 挂同一个处理函数；按下之后落在哪一档，看这一刻有没有收尾要管：

| 按下的时刻 | 做什么 | 退出码 |
|---|---|---|
| 正在录（`recording` / `alert`） | 走与 `stop` 同一条路径起收尾，等它落定再关停；收尾进度照常推 | 收尾干净 `0`；有步骤 `failed` `1` |
| 刚起、还没开过任何一场（`idle`） | 直接退出 | `0` |
| 上一场已收尾干净走完 | 直接退出，盘上那一版逐字节不动 | `0` |
| 收尾还在跑，或收尾失败后停在 `ended·draining` | 先落盘（`draining` + `interrupted: true`，见 `docs/state.md`）→ 再取消收尾协程 → 关停 | `130` |

被强制打断的场次，盘上留的是「收尾中 + 已打断」，重启与 `meeting post` 都接得上。

两条运行期行为，知道就行：

- **收尾有步骤 `failed` 时服务不自杀**：进程继续活着、页面还能看，新的「开始录音」一律回 `409 draining`；出路是 `meeting post <场次目录>` 或重启。
- **关停与换场会同步等转写收尾**（把队列里剩下的段在预算内做完），这一段里各端点暂不应答；预算上界见 `meeting/backends/transcribe.py`，收尾预算的公式见 `docs/state.md`。

### log

两层：一份归服务，一份归这一场。两份都用同一对轮转键。

| 层 | 位置 | 挂摘 |
|---|---|---|
| 服务级 | `<配置目录>/logs/meeting.log`（`log.dir` 可改；未设 `MEETING_CONFIG_DIR` 时即 `~/.config/meeting/logs`） | 起服务时建，进程活着就在 |
| 场次级 | `<场次目录>/meeting.log` | `start` 时挂上，收尾 `log` 步 flush 后摘掉 |

- 轮转按 `log.max_bytes`（默认 5 MiB）与 `log.backup_count`（默认 3 份，整份旧文件保留）构造；两键改完要重启。
- **主 log 不可写 → 拒绝启动**（退出码 `3`），不静默降级。
- **场次级 log 写失败不中断录音**（录音是唯一不可再生的东西），但如实上报：收尾 `log` 步落 `failed`、状态里看得见、主 log 里留一条。
- 这一场结束时，场次 log 的最后一行是结束标记：

      场次结束：state=<draining|done|failed>；收尾步骤：<每步状态>；服务级 log：<路径>

### 退出码

五个出口，常量在 `meeting/cli.py` 模块级（冻结项）：

| 码 | 什么时候 |
|---|---|
| `0` | 正常收场：空闲态按 `Ctrl+C`，或收尾干净；`meeting post` / `meeting replay` 那一趟干净跑完 |
| `1` | 收尾有步骤 `failed`；或 `meeting post` / `meeting replay` 那一趟没做成；或服务起步出意外 |
| `2` | 端口被占、用法错误、配置值非法 |
| `3` | 启动前提不成立（`config.json` 损坏、主 log 不可写） |
| `130` | 收尾中途第二次 `Ctrl+C`（立即中止，盘上绝不写 `done`） |

按 `Ctrl+C` 落在哪一档，见上面那张分档表。纪要那一层落在 `delivered`（已交付 DSH）按成功算。

## 2. 配置

- **位置**：`~/.config/meeting/config.json`（Windows 上即 `%USERPROFILE%\.config\meeting\config.json`）。V1 的 `~/.config/meeting_notes/` 不迁移。设 `MEETING_CONFIG_DIR` 时配置目录整组跟着走（取值口径见下方环境变量表）。
- **形状**：点号扁平键名的单层映射，例如 `{"web.port": 8766}`。嵌套对象不合法；写了键表里没有的键也不合法。
- **优先级**：环境变量 > `config.json` > 代码默认值。文件不存在 → 用默认值照常起。
- **`settings_update` 会写这个文件**（原子写：同目录临时文件 + `os.replace`），只写文件里已有的键 + 这次改动的键，不落默认值。
- **校验**：值级非法（类型、范围、键间规则）→ 退出码 `2`，报错写清键名、收到的值、合法范围；`config.json` 存在但读不出来或不是合法 JSON → 退出码 `3`。`settings_update` 的写前校验同口径，违规回 `invalid_setting`(400)，校验在写盘之前。
- **配置目录里的非配置文件**：工作区注册表 `workspaces.json`（归 `workspace_open` / `workspace_remove` 与 `GET /api/sessions` 管），不在键表里。

### 键与默认值（正本）

| 键 | 默认值 | 作用 | 生效 |
|---|---|---|---|
| `web.port` | `8765` | 服务绑定端口 | 重启 |
| `web.dist_dir` | `web/dist` | `GET /` 挂的构建产物目录 | 重启 |
| `log.level` | `INFO` | 日志级别 | 重启 |
| `log.dir` | `<配置目录>/logs`（未设 `MEETING_CONFIG_DIR` 时即 `~/.config/meeting/logs`） | 服务级 log 的目录 | 重启 |
| `log.max_bytes` | `5 MiB` | 日志轮转上限 | 重启 |
| `log.backup_count` | `3` | 轮转保留的备份份数 | 重启 |
| `alert.silence_dbfs` | `-60` | 低于此值算这一路在静默（判定阈值） | 重启 |
| `alert.silence_seconds` | `180` | 静默连续多久起致命级 | 重启 |
| `alert.recover_seconds` | `10` | 连续有信号多久算恢复 | 重启 |
| `alert.alarm_suppress_seconds` | `60` | 同一事件多久内不重复出声 | 重启 |
| `alarm.sound` | `true` | 本场出不出声的持久化开关 | **热读** |
| `heartbeat.interval_seconds` | `10`（合法 `1–60`） | **界面**发心跳的周期，进快照 `settings` | **热读** |
| `capture.backend` | `auto` | 采集后端选取（`auto` / `null` / `dev` / `module:Class`） | 重启 |
| `devices.mic` | `null`（= 按默认策略挑） | 麦克风那一路用哪个输入端点 | **热读**（空闲态打一次 `reprobe` 按新值挑；会中不换） |
| `devices.loopback` | `null`（= 按默认策略挑） | loopback 那一路用哪个 render 端点，同上 | **热读**（同上） |
| `transcriber.backend` | `auto` | 转写后端选取（`auto` 解析到真后端，`dev` 落转写替身） | 重启 |
| `post.hook` | `auto` | 会后钩子选取；`null` → 「生成会后精细稿」按钮 404 | 重启 |
| `post.engine` | `auto` | 会后离线引擎选取（`auto` 解析到 `MossOfflineEngine`） | 重启 |
| `post.model` | `OpenMOSS-Team/MOSS-Transcribe-Diarize` | 离线引擎加载哪个模型（HF 仓库 id 或本地路径） | 重启 |
| `post.dtype` | `bfloat16` | 引擎精度档位 | 重启 |
| `post.chunk_seconds` | `450`（合法 `0–86400`） | 每块音频的长度上限秒数，`0` = 关掉分块 | 重启 |
| `minutes.hook` | `auto` | 纪要钩子选取；`null` → 「生成纪要」按钮 404、`meeting post` 补纪要安静跳过 | 重启 |
| `minutes.chunk_lines` | `100`（合法 `10–1000`） | 每块行数，切块与行号区间表都用它 | 重启 |
| `minutes.max_segments` | `8`（合法 `1–20`） | 分段方案契约的每条 source 段数上限（`check` 子命令用） | 重启 |
| `minutes.timeout_seconds` | `14400`（合法 `60–86400`） | 一次纪要生成的墙钟上限 | 重启 |
| `minutes.dsh_url` | `http://127.0.0.1:3080` | `dsh web` 服务端地址；在跑就交付、没在跑才起 headless 子进程 | 重启 |
| `summary.base_url` | `https://api.deepseek.com/v1` | 会中总结的 OpenAI 兼容端点地址 | 重启 |
| `summary.api_key` | `null`（可空键） | 会中总结请求头密钥；也可走环境变量 `MEETING_SUMMARY_API_KEY` | 重启 |
| `summary.model` | `deepseek-chat` | 会中总结请求体里的模型名 | 重启 |
| `summary.extra_body_json` | `""`（= 不并） | 厂商特有字段，合法 JSON 对象的文本，原样并进请求体 | 重启 |
| `summary.presets_json` | `""`（= 回落平铺四键） | 多套端点预设整装一个键：JSON 数组文本，每套 `name` 必填不重名；预设密钥不进快照 | **热读** |
| `summary.active_preset` | `""`（= 用平铺四键） | 当前用哪套预设的指针；名字不在清单里被写前校验当场拒 | **热读** |
| `summary.deadline_seconds` | `25.0`（合法 `1.0–3600.0`） | 一次总结的墙钟 deadline | 重启 |
| `summary.max_attempts` | `3`（合法 `1–10`） | 尝试次数上限；真正的刹车是 deadline | 重启 |
| `summary.default_window` | `15m`（`5m`/`15m`/`30m`/`full`） | 总结窗口省略时的缺省档，进快照 `settings` | **热读** |
| `asr.max_segment_seconds` | `10` | 会中转写每段长度上限，进快照 `settings.asr` | **热读** |
| `asr.timeout_seconds` | `20` | 单段识别超时；必须大于 `asr.max_segment_seconds` | **热读** |
| `asr.max_new_tokens` | `400` | 单段生成 token 上限 | **热读** |
| `asr.repeat_max_unit` | `40` | 循环检测的周期上限 | **热读** |
| `asr.repeat_min_coverage` | `0.5` | 循环检测的覆盖率下限 | **热读** |

几点口径：

- `alert.*` 是判定阈值，`alarm.*` 只管出声。两个「心跳」不串线：`heartbeat.interval_seconds` 是**界面**发心跳的周期，服务侧的采集帧心跳窗口见 `docs/state.md`。
- `asr.*` 五个默认值沿用 V1 实测值；四个 `post.*` 键的取值依据见 `docs/asr-backends.md`。
- **热读键以 `meeting/config.py` 的 `HOT_KEYS` 一处为准**。

### 环境变量

| 变量 | 作用 |
|---|---|
| `MEETING_PORT` | → `web.port` |
| `MEETING_LOG_DIR` | → `log.dir` |
| `MEETING_CAPTURE` | → `capture.backend` |
| `MEETING_NO_BROWSER=1` | 关掉自动打开浏览器（不指向配置键） |
| `MEETING_CONFIG_DIR` | 配置目录整组重定向：`config.json`、`workspaces.json`、会后任务记录与日志默认落点都落到它指定的目录（pytest 之外的探针与手工跑一行隔离）；空串按没设；显式 `log.dir`（config.json 或 `MEETING_LOG_DIR`）仍以显式值为准。不指向配置键 |
| `MEETING_SUMMARY_API_KEY` | → `summary.api_key`（给密钥一个不进配置文件的来源；预设模式走预设里那一格，平铺键不参与） |

环境变量一律在启动时求值，改了不会热生效。

## 3. 跑测试

从仓库根跑（`pythonpath = ["."]`，工作目录一变 import 就不成立），不需要先装包：

```powershell
python -m pytest -q                        # 全量
python -m pytest -q -m "not slow"          # 快档
python -m pytest -q -m slow                # 慢档
python -m pytest -q -m "not slow" -n auto --dist loadfile   # 快档并行（每圈这条）
```

- **分档**：`slow` 标记（注册在 `pyproject.toml`，带 `--strict-markers`）归慢档。归位判据——一条用例满足任一条就进慢档：起真服务子进程；依赖真实墙钟等待；注入慢模型证明解耦或存活；单条实测超 2.0 秒。
- **慢档清单现算**：

      Get-ChildItem tests -Recurse -Filter test_*.py | Select-String -Pattern 'pytest\.mark\.slow'

  （慢档模块也可能住在 `tests\test_*\` 子包，须递归才数得全；`-Filter test_*.py` 这层滤网天然排除 `conftest.py` 与包内 `_common.py` 这类不参与收集的文件。文件头 `pytestmark` 与用例级装饰器两种写法都命中，清单与 `-m slow` 的收集口径一致。）用例数不存档，用 `python -m pytest -q --collect-only` 现算。
- **快档红线**：每圈并行 ≤ 10 秒；新加测试必须「无 sleep、无真子进程、无真墙钟等待、毫秒级纯逻辑或结构断言」才许进快档，达不到进慢档。用例看门与七道 autouse 安全兜底的实现见 `tests/conftest.py`；测试与验证纪律见 `docs/rules.md`。
- **契约看守**：`python -m pytest tests/test_contract_fixtures -q` 逐文件比对 `contract/fixtures/`；`--regen-contract` 重新生成，规则见 `contract/README.md`。
- 一个例外判例：`tests/test_transcript_hints.py` 不带 `slow` 标记，但**碰了转写提示级那一路时要单跑它**。

## 4. 现在能做什么

- 服务本体：起停、`Ctrl+C` 分档、收尾三步跑到终态、配置原子写与热读、端口与三道本地防线（细节见 `docs/contract.md`）。
- 真采集：两路各自独立、边采边落盘 PCM、0.5 秒电平帧、续接、暂停与逐路开关、收尾转 MP3（PCM 保留）。
- 真转写：两路各一本会中临时稿、四层退化防护（防护判据见 `docs/asr-backends.md`，读数不存档）。
- 会后离线：按钮或 `meeting post` 按需拉起，逐块进度落盘、数字静音门拦全零段、`meeting replay` 重放验证。
- 会后纪要：按钮或 `meeting post` 拉起，DSH 在跑就交付、没在跑才子进程。
- 会中总结：动作 `summarize` 十到十五秒回三小段，四档窗口，本场记录可回看。
- 前端：工作区首页与会中主视图，`web/dist` 由服务直发（`web/README.md`）。

其余按「能力还没进来」理解。真机真开一场会的设备枚举与真实电平是手工验收，自动化不替代它。

## 5. 相关文档

- 纪律（测试 / 文档 / 注释）：`docs/rules.md`
- 状态、场次与推送：`docs/state.md`
- HTTP 面与动作（端点、动作、`code` 表、端口与本地防线）：`docs/contract.md`；可机读基准：`contract/README.md`
- 会后纪要：`docs/minutes.md`；会中总结：`docs/summary.md`
- 后端协议：`docs/backend-protocols.md`；ASR 选型与参数依据：`docs/asr-backends.md`
- 界面：`docs/ui-design.md`；需求锚：`docs/requirements.md`（R1 / R2 / R5 / R15 / R16 / R17）
- 索引：`docs/AGENTS.md`
