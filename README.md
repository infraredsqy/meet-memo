# meeting —— 本地会议录制服务（骨架）

敲 `meeting web` 起的是**服务本身**：探测设备、准备录音、开本地端口、把界面发出去、
前台跑到 `Ctrl+C`。服务与界面之间只有两条线：SSE 把状态推出去，POST 把操作收回来。

本节点是骨架：**不含真实采集与模型代码，也不含产品界面**。默认什么都不配时服务照常起来、
界面照常发、「开始录音」给出「采集后端未接入」的明确说明 —— 不假装能录。

---

## 1. 怎么起

    meeting web

- 默认地址 `http://127.0.0.1:8765`；**只监听 `127.0.0.1`，这一条不可配**（没有 `web.host` 这类键）。
- 端口可以改：配置键 `web.port`，或环境变量 `MEETING_PORT=8766 meeting web`。
  把端口设成 `0` 时由系统挑一个空闲端口，选中的端口照常打印在终端并落 log。
- **端口被占就拒绝启动**，两种情形的话不一样（退出码 `2`）：
  - 占用者是本服务：`已经有一个 meeting 服务在跑：http://127.0.0.1:8765（当前空闲）`
    —— 这句话的**形状**是 phase1 冻结的（`meeting/service/app.py` 的端口策略那一处），而括号
    里的内容在当前实现里是**字面写死的「当前空闲」**：它还没接上「那一场已录多久」这个值
    （`session.elapsed_seconds` 那一侧由 phase2 提供，端口探针这一处**尚未取用**）。所以现在
    无论那个实例在录还是空着，你看到的都是这四个字；**本节点不承诺它将来会变成别的样子**。
  - 占用者是别的程序：`端口 8765 被别的程序占着，没法起服务。换一个端口再来，
    例如：MEETING_PORT=8766 meeting web`。
- 起服务时会**打开浏览器**；`MEETING_NO_BROWSER=1` 关掉这一步（测试与无头场景一律设它）。
- `meeting post <场次目录>` 眼下是**桩**：打印「会后流程还没接入（65dd3e4c）」、退出码 `1`。
  它同时是**收尾失败后的出路之一** —— 音频与 `session.json` 都在盘上，重跑不欠这次收尾什么；
  实现的落地归 `65dd3e4c`。

启动成功后终端上会打三样东西（同样的三行也进服务级 log）：

    URL：http://127.0.0.1:8765
    输出根：C:\Users\<你>\Documents\meeting\out
    log 路径：C:\Users\<你>\.config\meeting\logs\meeting.log

---

## 2. log 在哪

| 层级 | 位置 | 说明 |
|---|---|---|
| 服务级 | `~/.config/meeting/logs/meeting.log` | `log.dir` / `MEETING_LOG_DIR` 可改 |
| 场次级 | `<场次目录>/meeting.log` | `start` 时挂上、收尾 `log` 步 flush 后摘掉 —— **按场查** |

- 轮转按两个配置键的生效值构造：`log.max_bytes`（默认 **5 MiB**）、`log.backup_count`
  （默认 **3** 份）；**整份旧文件保留**（V1 那种「超限保留后半、删掉前半」已经改掉）。
  这两个键都在「改完要重启」那一组里。
- **主 log 不可写 → 拒绝启动**（退出码 `3`），不静默降级。
- **场次级 log 写失败不中断录音**（录音是唯一不可再生的东西），但会如实上报：
  收尾 `log` 那一步落 `failed`、状态里看得见、主 log 里留一条。
- 这一场结束时，场次 log 的最后一行是**结束标记**：

      场次结束：state=<draining|done|failed>；收尾四步：<每步状态>；服务级 log：<路径>

---

## 3. 热读键有哪些

**只有 `asr.*` 那五个键**每段重新读，改完立刻生效：

    asr.max_segment_seconds / asr.timeout_seconds / asr.max_new_tokens /
    asr.repeat_max_unit / asr.repeat_min_coverage

其余键（`web.*` / `output.root` / `log.*` / `alert.*` / `alarm.*` / `heartbeat.*` /
`capture.*` / `transcriber.*` / `post.*`）**改完要重启**。这份「其余键」清单以
`meeting/config.py` 的键全集为准。

**环境变量一律在启动时求值**，改了不会热生效。本服务的环境变量一共五个：
`MEETING_PORT` / `MEETING_OUT_ROOT` / `MEETING_LOG_DIR` / `MEETING_CAPTURE` /
`MEETING_NO_BROWSER`。

---

## 4. dev 替身怎么开

    MEETING_CAPTURE=dev meeting web          # 或配置键 capture.backend = "dev"

**这是非产品路径**：替身只发两路合成电平、不写真实音频（`finalize()` / `transcode()` 只记账），
默认关闭，代码里也标注了。

故障注入走 HTTP（dev-only 动作）：

    POST /api/actions
    {"action": "dev_inject",
     "args": {"source": "loopback", "fault": "silence|stall|lost", "after_seconds": 0}}

- `silence`：这一路电平落到 −80 dBFS 以下并保持；
- `stall`：两路事实帧整体停发（HTTP 照常应答，于是 `/api/heartbeat` 的 `recording_alive` 变 `false`）；
- `lost`：直接来一条 `SourceLost`。

采集后端不是 dev 时，这个动作回 `action_unavailable`（`404`）。
另有自检页 `GET /debug`（骨架自检，不是产品界面）。

---

## 5. 退出码表

| 退出码 | 什么时候 |
|---|---|
| `0` | 空闲态按 `Ctrl+C`；或者收尾干净（四步都走到 `done` / `skipped`） |
| `1` | `meeting post` 桩；或者**收尾有步骤 `failed`**（这时终端上会打 log 路径与最后几行）；或者**服务起步这一步出意外**（组装应用 / 构造服务器对象的路上抛异常，终端上会打一行「服务起步失败」与 log 路径） |
| `2` | 端口被占（已有 meeting 服务 / 别的程序占着）；用法错误；配置值非法 |
| `3` | 启动前提不成立（`config.json` 损坏、主 log 不可写） |
| `130` | **收尾中途第二次 `Ctrl+C`**：立即中止，盘上 `interrupted: true`、**绝不写 `done`** |

`Ctrl+C` 的两阶段语义：第一次收尾（与 `stop` 同一条路径，页面看得见「收尾中」），
收尾落定后关停；第二次立即中止（先落盘、再 `cancel()` 收尾协程）。

按下第一次落在哪一档，看的是**这一刻有没有收尾在跑**：

- **正在录（`recording` / `alert`）**：起收尾、等它落定 → `0`（收尾有步骤 `failed` → `1`）；
- **刚起、还没开过任何一场（`idle`）**：直接退出 → `0`；
- **服务重启过、上一场没走完**（空闲态挂着「服务重启过。上一场的收尾没走完……」这条提示）：
  同样**直接退出 → `0`** —— 这一刻没有任何收尾在跑，重启留下的现场只拦「开始录音」
  （新场次一律回 `409 draining`），**盘上那份 `session.json` 一个字都不会被这次关停改写**
  （不写 `interrupted`、不写 `ended_at`），因此**不会被当成强制打断（`130`）**；
- **已经停在收尾中 / 收尾失败的现场**（`ended` 里还有收尾在跑，或收尾失败后停着）：
  第一次按下就等同第二次 → `130`。

**收尾有步骤 `failed` 时服务不自杀**：进程继续活着、页面还能看、
新的「开始录音」一律回 `409 draining`（带上失败那一步与 log 路径），
出路是 `meeting post <场次目录>` 或看清现场后重启服务。

---

## 6. 默认位置与回归入口

| 东西 | 默认位置 |
|---|---|
| 配置 | `~/.config/meeting/config.json`（点号扁平键名的单层映射，例如 `{"web.port": 8765}`；**不迁移** V1 的 `~/.config/meeting_notes/`） |
| 输出根 | `%USERPROFILE%\Documents\meeting\out`（`output.root` / `MEETING_OUT_ROOT` 可改） |
| 场次目录 | `<输出根>\YYMMDD`（同名已存在就让位到 `-2` / `-3`…） |
| 前端产物 | `web.dist_dir`（默认 `web/dist`；目录缺失时 `GET /` 给一张说明页、服务照常起，`/debug` 与 `/api/*` 照常可用） |

回归入口：

    python -m pytest -q

---

## 7. 两个「心跳」别串线

- **界面心跳**（`heartbeat.interval_seconds`，可配 1–60 秒，默认 10 秒）管**失联横幅**：
  界面按这个周期问一次服务，问不着就挂横幅。
- **服务侧采集帧心跳**（`max(2.0s, 3 × frame_interval)` = **2.0 秒**）管 **`recording_alive`**：
  它看的是采集帧还在不在来，用来报出「HTTP 通着但录音停了」。

两者互不参与对方的计算：收尾预算也不拿这两个周期做任何计算。
