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
| `state.md` | **状态、场次与推送**：四态与迁移、收尾四步与共享预算、快照十键与取值域、`session.json` schema、心跳判据、告警判定、SSE 三通道与帧形状、dev 替身怎么用 | 接这份数据、写前端或验收脚本时 |
| `api.md` | **HTTP 面**：五个端点、动作信封、动作表与 `code` 表、`settings_update` 的键态两分、端口与本地防线 | 打这套接口时 |
| `contract.md` | **双端契约**：快照十键的 schema、SSE 四事件与重连语义、动作信封与逐档 `detail`、心跳与客户端义务、定名未实现的保留登记；每条都在 `contract/fixtures/` 有一个 golden 样本，跟代码行为漂移就红 | 写界面、写验收脚本，或改服务对外形状时 |
| `backend-protocols.md` | 三个后端协议的成员与数据结构、解析入口、`skipped` / `failed` 的判据、dev 采集替身 | 往服务里插真后端时 |
| `legacy-assets.md` | V1（`C:\Users\<user>\tools\meeting-notes`）可复用资产清单与迁移边界 | 想知道哪块照抄 V1、哪块必须重写时 |

## 代码现状

| 路径 | 是什么 |
|---|---|
| `meeting/` | 服务本体（Python 包）：起停、端口与三道防线、配置、日志、四态状态机、场次目录与 `session.json`、告警判定、SSE 推送、五个动作、三个后端协议与两个替身 |
| `meeting/service/` | 应用装配、五条路由的唯一注册点、`ServiceRuntime`（唯一状态权威）、收尾编排与共享预算、信号处理与关停接线、SSE 广播中心、`/debug` 自检页 |
| `meeting/backends/` | 协议与数据结构、`null` 替身、dev 采集替身、后端解析入口 `registry.py` |
| `tests/` | 20 个测试文件、273 个用例 + 测试支持（`_helpers.py`、`_service.py`、`_drain_fakes.py`、`_contract.py`、`conftest.py`、`__init__.py`、`_fake_webbrowser/`）；`conftest.py` 里有一道「单条用例 ≤ 5 秒」的耗时看门，`_contract.py` 是 `contract/fixtures/` 的采集与掩码 |
| `pyproject.toml` | 打包与依赖；运行时依赖只有 `starlette` 与 `uvicorn` |
| `README.md`（仓库根） | 简版使用说明：怎么起、log 在哪、热读键有哪些、dev 替身怎么开、退出码表、前端开发 |
| `web/` | 界面工程（React 18 + TypeScript 严格模式 + Vite 6 + vitest 2 + zod）：`src/contract/`（契约层，zod schema 为源）、`src/api/`（SSE / 动作 / 心跳三个客户端）、`src/App.tsx`（壳页）；`tests/` 4 个文件 118 个用例。`npm run dev` 开发、`npm run build` 出 `web/dist` 由服务直发（运行时不需要 Node） |
| `contract/` | 双端契约的可机读基准：25 个 golden 样本（`fixtures/`）+ 采集 / 掩码 / 再生成规则（`README.md`）。契约正文在 `docs/contract.md` |

## 分层读法

想快速建立全局印象，按这个顺序：

1. `service.md` 第 1 章起一次服务，第 3 章看它现在给什么；
2. `state.md` 第 1–2 章拿到四态与快照的形状；
3. `api.md` 第 2–4 章学动作怎么打；
4. 往后端里插东西时读 `backend-protocols.md`。

当前可用范围、明确不可用的部分与后续节点归属，见 `service.md` 第 8 章。
