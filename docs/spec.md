# meeting · 文档索引

> 这份文件是 `docs/` 的**入口**：只做导航，正文一律在各分册里。
> 规矩跟项目其余文档一致——写已经落地的东西；没实现的进 `.work/`，不进这里。

## 项目一句话

一个**录音器加一套会后处理**：会中把声音完整录下来、让人一眼看出它还活着，会后交给能慢慢跑的模型出一份说清共识与分歧的纪要。服务是本体，界面只是它的一个窗口。

## 文档分册

| 文件 | 写什么 | 什么时候读 |
|---|---|---|
| `requirements.md` | **需求锚**：R1–R17 用户要什么、为什么 | 拿不准取舍方向时回到这里 |
| `ui-design.md` | 界面设计的唯一权威说明（四态、迁移、告警分级、设计 token、视觉硬门槛） | 做界面、定事件契约时 |
| `ui/` | 设计稿本体（`*.html` + `tokens.css` / `ui.css` + `shots/*.png`）。它是 `ui-design.md` 的渲染载体，不进构建、不被服务引用 | 看视觉效果时 |
| `service.md` | **服务现状**：怎么起、起了之后有什么、配置键、log 在哪（含场次 log 的挂摘）、退出码五档、`Ctrl+C` 两阶段、收尾怎么跑与失败后的两条出路、测试怎么跑、还差什么 | 装起来用、或接手改服务时 |
| `state.md` | **状态、场次与推送**：四态与迁移、收尾三步与共享预算、转码独立工序、快照九键与取值域、`session.json` schema、心跳判据、告警判定、SSE 三通道与帧形状、dev 替身（采集 / 转写）怎么用 | 接这份数据、写前端或验收脚本时 |
| `api.md` | **HTTP 面**：十七条端点（十五条 `/api/*` + 静态首页 + `/debug`）、动作信封、动作表与 `code` 表、`settings_update` 的键态两分、端口与本地防线 | 打这套接口时 |
| `contract.md` | **双端契约**：快照九键的 schema、SSE 四事件与重连语义、动作信封与逐档 `detail`、心跳与客户端义务、定名未实现的保留登记；正文后接两章附录（fixtures 与掩码、复核手法）。每条都在 `contract/fixtures/` 有一个 golden 样本，跟代码行为漂移就红 | 写界面、写验收脚本，或改服务对外形状时 |
| `backend-protocols.md` | 四个后端协议的成员与数据结构、解析入口、`skipped` / `failed` 的判据、采集与转写两个真实现的落点、两个会后钩子（`post.hook` / `minutes.hook`）与它们各自的开关、dev 替身（按 key 分流） | 往服务里插真后端时 |
| `minutes.md` | **会后纪要生成**（`41465462`）：什么时候被谁拉起、吃什么输入、五个阶段各产出什么、怎么拉起 headless DSH 子会话、产物四条判据、四个配置键、三个手动子命令 | 想知道纪要怎么出来的、或手动跑一遍时 |
| `summary.md` | **会中总结**（`1987cb20`）：窗口从哪读、四档怎么切、提示词与模型输出契约、端点九个配置键（含多套模型预设与会中切换）、墙钟 deadline 与重试、动作 `summarize` 与 `GET /api/summaries`、结果不进快照也不落文件 | 想知道按一下之后发生了什么、或接手这一层时 |
| `v1-lineage.md` | **V1 出处总表**：V1（`C:\Users\<user>\tools\meeting-notes`）里被 V2 沿用的参数、结论与边界，沿用原清单的格子号（A / B / C / E / R 与纪律编号）编排。代码与文档里「见 … 的 C9」这类出处注释都来查这里 | 想追某个数值、某条纪律、某个坑的 V1 出处时 |
| `asr-backends.md` | **本地 ASR 选型与参数定档**：会中用哪套、会后用哪套、会后那套的运行时怎么裁决出来的、`post.dtype` 与 `post.chunk_seconds` 为什么取当下的值、本机量到了什么（逐块显存峰值 / 墙钟 / 实时率 / 99 分 03 秒那一趟）、会后链开跑前那次显存交接、会中防护五个参数的复核、3060 那台机器的待办与已知缺口。**值本身不在这里**（住在 `backend-protocols.md` 第 3 章末），这份写「为什么是这个值」 | 想知道这些键为什么是这个数、或接手这两层时 |

## 代码现状

| 路径 | 是什么 |
|---|---|
| `meeting/` | 服务本体（Python 包）：起停、端口与三道防线、配置、日志、四态状态机、场次目录与 `session.json`、**工作区注册表（`workspace.py`）**、告警判定、SSE 推送、十九个动作（含会后两个按钮 `post_offline` / `post_minutes`、停掉正在跑精细稿的 `post_stop`、转码独立工序的三只 `transcode_run` / `transcode_pause` / `transcode_resume`、会中那三个 `pause` / `resume` / `set_source` 与改名的 `speaker_rename`）、四个后端协议与两个替身、五档退出码与三条命令行入口（`meeting web` / `meeting post` / `meeting replay`）；`meeting post` 的会后编排（`cli.py` 的 `run_post()`，含幂等闸与 `--force`）、`meeting replay` 的重放验证（素材截断复制进自己的落地，只验证离线链） |
| `meeting/workspace.py` | **工作区注册表**（e2f6166b）：登记「我这些会」，文件住服务侧配置目录 `workspaces.json`、按规范化路径去重、原子写；`classify_workspace_dir()` 对一个目录判名目（空目录当新会 / 有会议材料（含疑似档）当收编 / 非空未知）供 `workspace_open` 与目录选择器共用；启动、读端点都不扫任何场次目录（`docs/design-intent.md` 第 4 条）。口径见 `docs/contract.md` 4.8 |
| `meeting/service/` | 应用装配、十六条路由的唯一注册点（十五条 `/api/*` + `/debug`）、`ServiceRuntime`（唯一状态权威）、收尾编排与共享预算、信号处理与关停接线、SSE 广播中心、`/debug` 自检页 |
| `meeting/backends/` | 协议与数据结构、`null` 替身、dev 替身（采集合成电平 + **转写合成行**，按 key 分流）、采集后端 `capture.py`（设备枚举与试开）、转写后端 `transcribe.py`（`9e5db89c` phase2：把 `meeting/asr/pipeline.py` 那条流水线包成协议实现，重依赖全在函数体内惰性 import）、会后钩子 `post.py`（`7c6952e5`：起一条 daemon 线程跑 `meeting/offline/runner.py`，**返回即算启动成功**，线程里的异常落进状态文件；收尾第 4 步不再拉起它，`post.hook` 在「生成会后精细稿」按钮那一侧只答「这一层接没接」，`null` → 按钮 404）、纪要钩子 `minutes.py`（`41465462` phase2：由 `meeting post` 的离线尾巴或「生成纪要」按钮拉起，起一条 daemon 线程跑 `meeting/minutes/runner.py`，同样返回即算启动成功；`65dd3e4c` 补的**在飞标记**：建线程之前先同步落一版 `running`，盘上已经停着一版时抛 `AlreadyRunningError` 拒跑）、后端解析入口 `registry.py` |
| `meeting/capture/` | 采集层：设备模型与挑选策略（`devices.py`，纯逻辑，骨架搬自 V1）、真实适配层（`sources.py`，惰性 import `sounddevice` / `soundcard`，含两路持续采集源）、电平换算（`levels.py`，纯函数）、归档与转码（`archive.py`：无缓冲 PCM 写入器 —— 静音块写等长数字零、`content_bytes` / `silence_bytes` 计数分两格 —— + PCM→MP3 逐段转）、会话引擎（`engine.py`：两路读侧 / 写侧 / 0.5 秒电平刻度；**按段落盘** `meeting_audio_<source>_<NNN>.pcm`（一段 = 一次录制）+ 引擎层的暂停/继续与「关一路」：写静音、不切段、关一路不报 `SourceLost`，`6ea32d8d`） |
| `meeting/asr/` | 实时转写的 **ASR 核心与流水线**（`9e5db89c`）：`guards.py` 是判定内核（token 级判环 `_LoopGuard`、墙钟超时 `_DeadlineStop`、文本级剥离 `_strip_repeat_loops` + `_loop_coverage`，**零重依赖**）、`glm.py` 是模型层（`GlmAsrTranscriber` 的惰性加载与 `transcribe_segment`、噪声预筛 `_is_noise_segment`、四层防护的挂载点、`_load_from_hub` 离线优先）、`pipeline.py` 是流水线层（按段序号尾读 `session_dir/meeting_audio_<source>_<NNN>.pcm`、逐段接续（续接那一场从盘上实有最大段号接起，旧段不重读）→ VAD 切段 → **入队即盖采集时刻** → 单 worker 顺序识别 → 每路一个 `meeting_transcript_<source>.md` + 行出口；段队列无界、收尾预算按积压量自适应）。重依赖（torch / transformers / numpy / webrtcvad）一律在**函数体内**惰性 import：`import meeting.asr.glm` 一条都不拉（`tests/test_asr_glm.py` 在全新解释器里盯着这一条） |
| `meeting/offline/` | **会后离线转写与说话人聚类**（`7c6952e5`）：`audio.py` 用 PyAV 把归档音频解成 16k 单声道波形、`segment.py` 按会中同一套规则切段（30ms 帧、静默 2.0s 收段、丢掉 <500ms 的段，数值搬自 V1）、`engine.py` 是 MOSSTD 引擎的包装（`post.engine` 的 `auto` 落点，把 `post.dtype` 的档位名翻成 `torch.dtype`）、`speakers.py` 按本路首次出现顺序把引擎标签编号成 `S01`、`artifacts.py` 写四份产物并定字段契约、`progress.py` 写与读那份**逐块进度记录**（`meeting_offline_progress.jsonl`，`a9affaa8` 落地：每跑完一块补一行，块内带 `cNN:` 标签与四档结果 `transcribed` / `silent` / `failed` / `unjudged`，进程内另留一份现场读数）、`runner.py` 串起整条链（逐路取**段列表**（归档 PCM 或 MP3，多段按序拼成一条时间轴、单趟多段拼临时 wav）→ **数字静音门**（样本全零的那一段不进引擎，**单趟与分块两条路都过**；门判不了的那几段照常送引擎、把「没判成」记进 `gate`，判据与留痕见 `docs/state.md`）→ 逐路调引擎 → **每块跑完把那块的文字与落点落一次盘** → 逐路编号 → 落产物 → 状态文件落终态）。四份产物都住场次目录：`meeting_offline_transcript_<source>.md`（每路一份的会后精细稿，一行 `[HH:MM] **S01**: 正文`）、`meeting_offline_speakers_<source>.json`（类编号与区间对照表，逐段带结论词 `reason`）、`meeting_offline_report.md`、`meeting_offline_status.json`。**两路各跑各的、各落各的产物，文件名与限定名都带 `source`，两路不合并**；音频长过 `post.chunk_seconds` 秒时按秒数硬切成连续不重叠的块（**不挑静音点**）逐块跑，块内标签带 `cNN:` 命名空间，块与块之间不做映射；`replay.py` 是重放验证工具（`a3b684bd`：素材截断复制进 `meeting replay` 自己的落地、直调同一条 `run_session(minutes=False)`，出实时率 / 逐块耗时 / 失败分布读数——只验证，不是第二条生产路径）。重依赖（`torch` / `transformers` / `av`）一律在函数体内惰性 import |
| `meeting/minutes/` | **会后纪要生成**（`41465462` phase2）：`lines.py` 解析两种行形的转写行、`chunk.py` 按行切块 + `00_index.md` 行号区间表（块文件只有原始行、重跑先清本源的旧块）、`contract.py` 校验 `segmentation.json`（连续性**按每条 source 各判一遍**，跨 source 不判）、`prompt.py` 把路径与档位填进 `prompts/meeting_final.md` 渲染出子会话读的那份提示词、`inputs.py` 找两条转写并写 `working/inputs.json`、`status.py` 读写 `meeting_minutes_status.json`（含**拉起之前那一版 `running`**：`mark_running()` 用一把瞬态 claim 锁把「读现状 + 写 running」裹成跨进程原子的一段，读到在飞就抛 `AlreadyRunningError`）、`launch.py` 拉起 headless DSH 子进程 + 产物四条判据、`runner.py` 把这条链串起来（再入闸 → 切块 → 渲染 → 拉起 → 校验 → 登记）、`__main__.py` 三个手动子命令（`check` / `chunk` / `prompt`）。**整包只用标准库**：`torch` / `transformers` / `numpy` / `av` / `webrtcvad` / `starlette` / `sounddevice` 一个都不碰（`tests/test_minutes_contract.py` 在全新解释器里盯着这一条） |
| `meeting/summary/` | **会中总结内核**（`1987cb20` phase1）：`window.py`（线程安全的内存追加缓冲 `TranscriptBuffer` + 按行首时间戳取连续尾部的 `select_window()` + `render_window()` 装配），`prompt.py`（system 七条硬规则 + user 分段写法），`client.py`（OpenAI 兼容端点，标准库 `urllib.request`，`post` 是注入点，四档 `SummaryError`），`parse.py`（强制 `{"bullets": [...]}` + 围栏容忍 + 空数组合法，按三个固定标签归三小段），`settings.py`（九个 `summary.*` 键的读数与归一化、预设清单与回落、`missing` 点名），`runner.py`（`run_summary()`：窗口 → 装配 → 调用 → 解析，守一条墙钟 deadline）。**整包只用标准库**，运行时不引 `httpx` / `requests`；不注册路由、不碰快照、不写文件。这一层的正文见 `docs/summary.md` |
| `prompts/meeting_final.md` | 会后纪要那一层的编排提示词模板（五个阶段：分割 / 分割方案 / 逐段总结 / 合并 / 只读检查；两条转写对读与说话人附录的诚实口径写在这里）。渲染器认识的七处占位符（六处必写、`PHASE_BODY` 可省略）与两块 `PHASE_BODY` 标记见 `meeting/minutes/prompt.py`，产物形状见 `docs/minutes.md` |
| `pyproject.toml` | 打包与依赖；运行时依赖 = `starlette` / `uvicorn` / `anyio` + 音频四件套（`numpy` / `sounddevice` / `soundcard` / `pydub`，裁定：录音是产品主体，不放可选 extra）；`asr` extra 装转写模型依赖（`torch` 的 CUDA 构建要带 PyPI 之外的索引，CPU 档不用——装法见 `docs/service.md` 第 1 章），`post` extra 只有 `av` 一条，会后与会中共用同一套模型依赖；`packages` 八个包标记含 `meeting.asr` / `meeting.offline` / `meeting.minutes` / `meeting.summary`；**不装 `asr` / `post` 这两格照样起服务**（重依赖全在函数体内惰性 import） |
| `tests/` | pytest 套件：用例一律**注入假件**，真实模型端点 / 真声卡 / 真 DSH 子会话一条请求都不发，出口另有七道 autouse 兜底；`tests/v1/` 是 V1 重复循环回归的整份副本。文件数与用例数**现算**（`python -m pytest -q --collect-only`，不进文档存数）；口径、七道兜底与快慢分档的归位判据见 `docs/service.md` 第 7 章 |
| `README.md`（仓库根） | 简版使用说明：怎么起、log 在哪、哪些配置改完要重启、dev 替身怎么开、退出码、前端开发 |
| `web/` | 界面工程（React 18 + TypeScript 严格模式 + Vite 6 + vitest 2 + zod）：`src/contract/`（契约层，zod schema 为源）、`src/api/`（SSE / 动作 / 心跳 / 本场总结记录 / 会场清单与目录现场等客户端）、`src/home/`（**工作区首页**：`HomeGate` 按闸分流、左侧清单、目录选择器、收编 / 删除确认弹窗、选中那一场详情里的**会后两个按钮**与会后进度区、说话人名字弹窗；纯逻辑在 `gate.ts` / `post.ts` / `model.ts` 等）、`src/App.tsx`（**会中主视图**：状态胶囊与时长、两路电平表、告警区、心跳本地态与「采集已停」处置、实时转写、总结区、收音四组合直切、告警出声开关；纯逻辑在 `src/live/`）、`src/styles/`（`tokens.css` 逐字节拷贝 + `live.css`）。五个 npm 脚本（`dev` / `typecheck` / `build` / `preview` / `test`）：`npm run dev` 边改边看、`npm run build` 出 `web/dist` 由服务直发（运行时不需要 Node）。工程自己的说明在 `web/README.md` |
| `contract/` | 双端契约的可机读基准：golden 样本（`fixtures/`，个数现算）+ 采集 / 掩码 / 再生成规则（`README.md`）。契约正文在 `docs/contract.md` |

## 分层读法

想快速建立全局印象，按这个顺序：

1. `service.md` 第 1 章起一次服务，第 3 章看它现在给什么；
2. `state.md` 第 1–2 章拿到四态与快照的形状；
3. `api.md` 第 2–4 章学动作怎么打；
4. 往后端里插东西时读 `backend-protocols.md`；
5. 想弄清会后那份纪要怎么出来的，读 `minutes.md`；
6. 想知道会中按一下「总结一下」之后发生了什么，读 `summary.md`；
7. 想弄清会中与会后这两套语音模型为什么是它们、那些 `post.*` 键为什么取当下的值，读 `asr-backends.md`。

当前可用范围与还接不上的部分，见 `service.md` 第 8 章。
