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
| `state.md` | **状态、场次与推送**：四态与迁移、收尾四步与共享预算、快照十键与取值域、`session.json` schema、心跳判据、告警判定、SSE 三通道与帧形状、dev 替身（采集 / 转写）怎么用 | 接这份数据、写前端或验收脚本时 |
| `api.md` | **HTTP 面**：五个端点、动作信封、动作表与 `code` 表、`settings_update` 的键态两分、端口与本地防线 | 打这套接口时 |
| `contract.md` | **双端契约**：快照十键的 schema、SSE 四事件与重连语义、动作信封与逐档 `detail`、心跳与客户端义务、定名未实现的保留登记；正文后接两章附录（fixtures 与掩码、复核手法）。每条都在 `contract/fixtures/` 有一个 golden 样本，跟代码行为漂移就红 | 写界面、写验收脚本，或改服务对外形状时 |
| `backend-protocols.md` | 三个后端协议的成员与数据结构、解析入口、`skipped` / `failed` 的判据、采集与转写两个真实现的落点、dev 替身（按 key 分流） | 往服务里插真后端时 |
| `legacy-assets.md` | V1（`C:\Users\<user>\tools\meeting-notes`）可复用资产清单与迁移边界 | 想知道哪块照抄 V1、哪块必须重写时 |

## 代码现状

| 路径 | 是什么 |
|---|---|
| `meeting/` | 服务本体（Python 包）：起停、端口与三道防线、配置、日志、四态状态机、场次目录与 `session.json`、告警判定、SSE 推送、六个动作、三个后端协议与两个替身 |
| `meeting/service/` | 应用装配、五条路由的唯一注册点、`ServiceRuntime`（唯一状态权威）、收尾编排与共享预算、信号处理与关停接线、SSE 广播中心、`/debug` 自检页 |
| `meeting/backends/` | 协议与数据结构、`null` 替身、dev 替身（采集合成电平 + **转写合成行**，按 key 分流）、采集后端 `capture.py`（设备枚举与试开）、转写后端 `transcribe.py`（`9e5db89c` phase2：把 `meeting/asr/pipeline.py` 那条流水线包成协议实现，重依赖全在函数体内惰性 import）、会后钩子 `post.py`（`7c6952e5`：收尾第 4 步的落点，起一条 daemon 线程跑 `meeting/offline/runner.py`，**返回即算启动成功**，线程里的异常落进状态文件）、后端解析入口 `registry.py` |
| `meeting/capture/` | 采集层：设备模型与挑选策略（`devices.py`，纯逻辑，骨架搬自 V1）、真实适配层（`sources.py`，惰性 import `sounddevice` / `soundcard`，含两路持续采集源）、电平换算（`levels.py`，纯函数）、归档与转码（`archive.py`：无缓冲 PCM 写入器 + PCM→MP3）、会话引擎（`engine.py`：两路读侧 / 写侧 / 0.5 秒电平刻度） |
| `meeting/asr/` | 实时转写的 **ASR 核心与流水线**（`9e5db89c`）：`guards.py` 是判定内核（token 级判环 `_LoopGuard`、墙钟超时 `_DeadlineStop`、文本级剥离 `_strip_repeat_loops` + `_loop_coverage`，**零重依赖**）、`glm.py` 是模型层（`GlmAsrTranscriber` 的惰性加载与 `transcribe_segment`、噪声预筛 `_is_noise_segment`、四层防护的挂载点、`_load_from_hub` 离线优先）、`pipeline.py` 是流水线层（游标尾读 `session_dir/meeting_audio_<source>.pcm` → VAD 切段 → **入队即盖采集时刻** → 单 worker 顺序识别 → 每路一个 `meeting_transcript_<source>.md` + 行出口；段队列无界、收尾预算按积压量自适应）。重依赖（torch / transformers / numpy / webrtcvad）一律在**函数体内**惰性 import：`import meeting.asr.glm` 一条都不拉（`tests/test_asr_glm.py` 在全新解释器里盯着这一条） |
| `meeting/offline/` | **会后离线转写与说话人聚类**（`7c6952e5`）：`audio.py` 用 PyAV 把归档音频解成 16k 单声道波形、`segment.py` 按会中同一套规则切段（30ms 帧、静默 2.0s 收段、丢掉 <500ms 的段，数值搬自 V1）、`engine.py` 是 MOSSTD 引擎的包装（`post.engine` 的 `auto` 落点，把 `post.dtype` 的档位名翻成 `torch.dtype`）、`speakers.py` 按本路首次出现顺序把引擎标签编号成 `S01`、`artifacts.py` 写四份产物并定字段契约、`runner.py` 串起整条链（逐路取归档 PCM 或 MP3 → 逐路调引擎 → 逐路编号 → 落产物 → 状态文件落终态）。四份产物都住场次目录：`meeting_offline_transcript_<source>.md`（每路一份的权威转写，一行 `[HH:MM] **S01**: 正文`）、`meeting_offline_speakers_<source>.json`（类编号与区间对照表）、`meeting_offline_report.md`、`meeting_offline_status.json`。**两路各跑各的、各落各的产物，文件名与限定名都带 `source`，两路不合并**；音频长过 `post.chunk_seconds` 秒时按秒数硬切成连续不重叠的块（**不挑静音点**）逐块跑，块内标签带 `cNN:` 命名空间，块与块之间不做映射。重依赖（`torch` / `transformers` / `av`）一律在函数体内惰性 import |
| `tests/` | 40 个测试文件、688 个用例（**口径**：用例数取 `python -m pytest -q` 的收集数与 passed 数，文件数取 `--collect-only` 输出里出现过 `tests/….py::` 的文件个数 —— 实测 `688 tests collected` / `688 passed`。`9e5db89c` 的相位基线是 31 个文件 / 459 个用例，那个节点逐轮加上 `test_asr_pipeline.py`（17 条）、`test_transcriber_backend.py`（20 条）与 `test_asr_glm.py` 里补的 1 条；`test_transcriber_backend.py` 与 `test_asr_pipeline.py` 的条数在四轮复审的修复中持续增长（详见该节点的 `phase2/report.md` 与 `fix1..fix4.md`）。`7c6952e5` 逐轮加上会后离线那七个文件：`test_offline_audio.py` / `test_offline_segment.py` / `test_offline_engine.py` / `test_offline_speakers.py` / `test_offline_artifacts.py` / `test_offline_runner.py` / `test_post_hook.py`）+ 测试支持（`_helpers.py`、`_service.py`、`_drain_fakes.py`、`_capture_faulty.py`、`_contract.py`、`conftest.py`、`__init__.py`、`_fake_webbrowser/`）；`tests/v1/` 是 V1 那套回归的**整份副本**（`fixtures_real_loops.py` 逐字照抄、`test_repeat_filter.py` 只改 import），那两条真实故障行因此不依赖会议目录就能复现；`conftest.py` 里有一道「单条用例 ≤ 5 秒」的耗时看门、`--regen-contract` 开关与两道 autouse 安全兜底（不许开真浏览器、不许碰真声卡），`_contract.py` 是 `contract/fixtures/` 的采集与掩码，`test_contract_fixtures.py` 是漂移比对（8 个用例） |
| `pyproject.toml` | 打包与依赖；运行时依赖 = `starlette` / `uvicorn` + 音频四件套（`numpy` / `sounddevice` / `soundcard` / `pydub`，`dca4539b` §5 的裁定：录音是产品主体，不放可选 extra）；`asr` extra 已填（`torch==2.9.1+cu128` / `transformers==5.15.0` / `webrtcvad-wheels==2.0.14`，`9e5db89c` phase1 定档的版本号；`torch` 那个是 CUDA 12.8 构建，PyPI 上没有这个 wheel，装的时候要带 `--extra-index-url https://download.pytorch.org/whl/cu128`，CPU 档写 `torch==2.9.1`）；`post` extra 只有 `av` 一条（`7c6952e5`：PyAV 解码，`torch` / `transformers` / `webrtcvad` 已经在 `asr` 那一格里，会后与会中共用同一套模型依赖）；`packages` 六个包标记含 `meeting.asr` / `meeting.offline`；**不装 `asr` / `post` 这两格照样起服务**（重依赖全在函数体内惰性 import） |
| `README.md`（仓库根） | 简版使用说明：怎么起、log 在哪、热读键有哪些、dev 替身怎么开、退出码表、前端开发 |
| `web/` | 界面工程（React 18 + TypeScript 严格模式 + Vite 6 + vitest 2 + zod）：`src/contract/`（契约层，zod schema 为源）、`src/api/`（SSE / 动作 / 心跳三个客户端）、`src/App.tsx`（壳页）；`tests/` 4 个文件 121 个用例。五个 npm 脚本（`dev` / `typecheck` / `build` / `preview` / `test`）：`npm run dev` 边改边看、`npm run build` 出 `web/dist` 由服务直发（运行时不需要 Node）。工程自己的说明在 `web/README.md` |
| `contract/` | 双端契约的可机读基准：25 个 golden 样本（`fixtures/`）+ 采集 / 掩码 / 再生成规则（`README.md`）。契约正文在 `docs/contract.md` |

## 分层读法

想快速建立全局印象，按这个顺序：

1. `service.md` 第 1 章起一次服务，第 3 章看它现在给什么；
2. `state.md` 第 1–2 章拿到四态与快照的形状；
3. `api.md` 第 2–4 章学动作怎么打；
4. 往后端里插东西时读 `backend-protocols.md`。

当前可用范围、明确不可用的部分与后续节点归属，见 `service.md` 第 8 章。
