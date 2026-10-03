# meeting —— 本地会议录制服务

敲 `meeting web` 起的是**服务本身**：探测设备、准备录音、开本地端口、把界面发出去、
前台跑到 `Ctrl+C`。服务与界面之间只有两条线：SSE 把状态推出去，POST 把操作收回来。

**真实采集与真实转写都已经接通**：默认什么都不配时 `capture.backend` 取 `auto`，解析到真采集后端 ——
开始录音就把两路（本地麦克风 + 系统内部声音）各录各的，边采边把 16k / 单声道 / int16 的裸
PCM 写进场次目录，停下来的收尾把两份 PCM 转成 MP3。`transcriber.backend` 同样取 `auto`，解析到真转写后端：
它从盘上的归档尾读、按静默切段、单 worker 顺序识别，把两路各写一本会中临时稿
（`meeting_transcript_mic.md` / `meeting_transcript_loopback.md`，行形如 `[HH:MM] 正文`），
同一批行也经 SSE 的 `transcript` 通道推给界面 —— 它是**临时稿**；会后那条带说话人编号的稿
另落一份，两份平级放在一起对拍，谁也不顶掉谁。
**会中总结已经接通**（跟丢时按一下，十到十五秒拿回「刚才在讲什么 / 讲到哪儿了 / 丢掉了什么」）；
会后那一头，收尾不自动跑任何会后动作（第 4 步那一格停在 `waiting`，等你动手）：
「生成会后精细稿」与「直接出纪要」两个按钮各拉起各自那一趟，正在跑的精细稿能停
（`post_stop`：当前块收尾之后停，状态写成「被打断」，重按按钮从头再跑）；
手动的 `meeting post <场次目录>` 入口同样接通（跑完就退，可反复重跑）。
服务在空闲态落地**工作区首页**：左侧清单列出工作区注册表（`~/.config/meeting/workspaces.json`）里
登记的那几场，点「开始录音」落在最近一场的那个目录（**录过的那一场按钮叫「接着录」**：
同一场会再录一段，旧的录音与稿子原样保留）；没有一场时空态给 logo 与开场指引，
落点从目录选择器里挑。
界面在 `web/`，构建产物由服务自己发出去。

---

## 0. 前置条件

| 东西 | 要求 | 缺席时会怎样 |
|---|---|---|
| Python | ≥ 3.13（本机实测 3.13.7） | 起不来 |
| `ffmpeg` | **在 `PATH` 上**（`pydub` 靠它把 PCM 转 MP3） | 服务照常起、照常录；收尾第 2 步 `transcode` 如实记 `failed`，**归档 PCM 原样留在场次目录里**（一个字节都不丢） |
| Node | ≥ 20，**只在开发界面时需要** | 不影响开会（服务直发 `web/dist` 的产物） |

音频四件套（`numpy` / `sounddevice` / `soundcard` / `pydub`）是**运行时依赖**，装包时一并装上 ——
它们不在可选 extra 里，因为「没装 extra」会退化成「一场会什么都没录」，
正是这个产品最不能犯的错。

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
- `meeting post <场次目录>` **只跑会后那一套**：音频归档补齐（PCM → MP3）→ 会后离线转写与说话人聚类
  → 会后纪要生成，跑完打印每层的结论与产物绝对路径就退出，不开界面、不起服务
  （这一趟活在命令行进程里，界面上的「停止」管不到它）。
  它是**收尾失败后的出路之一** —— 音频与 `session.json` 都在盘上，重跑不欠这次收尾什么。
- **重跑是安全的**：两层各有「已完成」判据（状态文件落 `done` + 产物齐），都收干净了这一趟一个引擎调用、
  一个子进程都不产生（终端打一句「无待办」）；只差一层就只补那一层；任一层停在 `running`
  （上一趟还在跑，或崩在半路）就**拒跑**并点名那个状态文件，让确认之后再 `meeting post <场次目录> --force`。
  被打断的那一场（状态 `interrupted`）是终态但不拦闸：下一趟**从头再跑**，不需要 `--force`。
  `--force` 是**唯一**会再次花钱的开关：它无视两层的终态、把离线段与纪要段都重跑一遍。
- **`meeting replay <场次目录> [--seconds N | --chunks N] [--out 目录]` 是重放验证工具**（`a3b684bd`）：
  把那一场的素材**截断复制**进 `--out` 指定的目录（缺省落系统临时目录，每遍给一个新目录；
  原目录一个字节不动），在拷贝上全速重跑会后离线转写链，跑完打印实时率（墙钟 ÷ 盘上实测
  音频时长）、每块耗时与失败原因分布（`transcribed` / `silent` / `failed` / `unjudged` 四档）。
  上限两种给其一：`--seconds N` 只跑前 N 秒、`--chunks N` 只跑每路前 N 块（按生效的
  `post.chunk_seconds` 换算，该值 `<= 0` 时这一档拒跑），都不给就是整场，同给两个拒绝。
  它**只验证离线链、绝不拉纪要**，那一场的产物一个字节不动。

启动成功后终端上会打两样东西（同样的两行也进服务级 log）：

    URL：http://127.0.0.1:8765
    log 路径：C:\Users\<你>\.config\meeting\logs\meeting.log

### 1.1 录一场会

**一场会的落点由它的工作区目录决定**：start 请求要显式给 `args.dir` —— 一条**已存在**的
目录的路径。本软件不建目录、不按日期命名、也没有默认落点（`docs/design-intent.md` 第 3、4
条）；目录名就是这场会的名字。落点的选择入口（工作区首页的目录选择器 / 左侧清单）已经接上，
也可以随时经服务端动作 / 接口按上面的形状开。
一场会开起来之后它做这些事：

- 开场前 **`probe()` 真探测**：枚举声卡端点、以 16k 试开一遍、**读回一小段样本**，两路各给
  一条结论（`ready` / `probe_failed`，没探测过是 `unprobed`）；
- 两路**各自独立**地读、各自独立地写盘（一路出事另一路照跑；一路起不来只给它自己发一条
  `SourceLost`，两路都起不来才拒绝开场）；
- 音频按段落盘：`<场次目录>\meeting_audio_<source>_<NNN>.pcm`（`meeting_audio_mic_001.pcm` /
  `meeting_audio_loopback_001.pcm`；NNN 三位零填充、从 1 起，一段 = 一次录制，
  16k / 单声道 / int16 小端裸 PCM，无头，**边采边落盘**）；
- 停下来的收尾：第 1 步停流落盘，第 2 步同目录同名**逐段**转出 `.mp3`（64k）。**转码成功之后
  PCM 照样留着** —— `session.json` 里 `sources[*].archives` 段文件名表声明的就是它们。

**对录过的目录再按一次开始 = 续接**：同一场会续下去 —— 新段接着已有段号编
（`meeting_audio_mic_002.pcm`…），旧段的 PCM / MP3 与两本临时稿一个字节不碰，
`session.json` 的段账与权威时长（各段在录时长之和）把各段加在一起。续接那一段开场，
两本临时稿各落一条「续接」注释行、界面上各推一条断口行 —— 两段之间那截没录的时间
在稿子与界面上都留着一道看得见的口子；转写从盘上最新的段接起，旧正文不会被抄第二遍。
续接开场要是没起来（设备被占之类），旧记录原样保留、本轮段文件收走，工作区回到按下去之前的样子。

录哪一路用哪个设备由两个配置键定（`devices.mic` / `devices.loopback`，不配就按默认策略挑）。
页面上那个「设置」抽屉就能换（`311f84f5`）：清单来自只读的 `GET /api/devices`（只枚举、
不开设备），选完写进 config、当场生效，空闲态再打一次 `reprobe` 就按新设备挑（会中
不换设备，那两格禁用）。**配置是「你的意图」，快照 `sources[*].device_name` 是「实际落到
的那个设备」** —— 配了却没命中时不会报错，只按回退顺序挑一条并在主 log 留一行 `WARNING`。

**会后纪要**：按「结束录音」之后**会后那一套不自动跑**（`docs/design-intent.md` 第 10 条）——
工作台上那一场的详情里有两个按钮：**生成会后精细稿**（走离线模型，慢，出带说话人编号的那份稿）
与 **直接出纪要**（拿这一场手上有的稿子交给 DSH；缺会后稿就按现有档位来）。两个都不按，
这一场就什么都不跑。正在跑的精细稿随时能停：进度区头行有一颗「停止」（这一场的精细稿在飞时
才出现）——当前块收尾之后停，状态写成「被打断」，重新按按钮从头再跑；会后在跑时按「新会议」
会先弹一句「启动新会议会打断正在跑的会后，是否继续」，确认才打断、取消就什么都不发。详情里还有一行**说话人名字…**入口：点开弹窗，这一场盘上每个
说话人身份一行，输入名字、按确认就写进这场目录的 `meeting_speaker_names.json`（当场生效；
两个编号叫同一个名不拦，编号是分开的）。下次出纪要，确认过的名字随提示词带进附录照抄，
没起名的编号照旧由模型推断；改名字不改已生成的纪要。
纪要的产物是**场次目录根**的 `meeting_final.md`
（纪要正文 + 说话人附录，附录里写清每个人名的依据与把握，猜不出的单列一节），
状态在 `meeting_minutes_status.json`（`running` / `done` / `failed` / `delivered` + 原因 + 档位 +
实际走的启动分支），中间产物与那一次运行的账在 `working/` 下（`inputs.json`、
`<source>/<kind>/chunks/`、`run_prompt.md`、`minutes-run.log`）。这一层跑砸了不影响离线那几份产物。
`minutes.hook` 决定这一层**由哪个实现接**（`auto` / `dev` / `module:Class`；写成 `null` 时
「直接出纪要」那个按钮当场回 404，如实说这台机器上没接这一层）；每块行数、分段上限、
一次运行的墙钟上限与那一台 `dsh web` 服务端的地址分别由 `minutes.chunk_lines` /
`minutes.max_segments` / `minutes.timeout_seconds` / `minutes.dsh_url` 定
（五个键都收在 `settings_update` 的 `pending_restart` 那一档里）。
拉起之前先探一次 `minutes.dsh_url`（默认 `http://127.0.0.1:3080`，就是 `dsh web` 的默认地址）：
**在跑**就把这一场交付给它 —— 经 `/api` 在场次目录对应的那个工作区里建会话、把提示词全文送进去，
**投递成功即算交付完成**，状态记 `delivered`，后面的活不归这一层管（它不等 DSH 那边写完、
也不校验产物；产物在 DSH 界面的那条会话里看得见）；**没在跑**才落回 headless 那一趟 ——
`dsh --profile headless` 的回合结束进程就退出，那一趟会等子进程退出、再按四条判据校验产物
（纪要存在、带附录、附录里真有数据行），验不过就记 `failed`，验过了才是 `done`。
它吃什么、经哪几步出一份纪要、怎么手动跑一遍（`python -m meeting.minutes chunk|prompt|check`），
见 `docs/minutes.md`。

**会中总结**：跟丢了的时候按页面上的「总结一下」，十到十五秒拿回三小段 ——
刚才在讲什么 / 讲到哪儿了 / 丢掉了什么。范围由人挑四档（最近 5 / 15 / 30 分钟、整场，缺省 15 分钟），
读的是**内存里那份实时临时稿**（不读盘、也不动快照），结果是一过性的：关掉即消失、不落文件。
本场按过的、成功产出的那几次可以从 `GET /api/summaries` 取回来回看（记录只活在内存里，
新的一场 `start` 清空）。模型端点由九个 `summary.*` 键配：多套预设整装在 `summary.presets_json`
（一段 JSON 数组串，每套 `name` + 端点四件套），`summary.active_preset` 指名当前用哪一套 ——
设置抽屉的「快速总结模型」下拉**会中就能换**，写盘即生效不必重启；不配预设就走平铺四键
（存量配置零迁移）。缺省档 `summary.default_window` 同样写完即生效，其余六键改完要重启。
密钥那一格可以走环境变量 `MEETING_SUMMARY_API_KEY`（压的是平铺那一格；预设用它自己的密钥）；
没配齐就当场说清缺哪几个键。
这一层怎么切窗口、提示词与模型输出是什么形状、超时与重试的预算落在哪，见 `docs/summary.md`。

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

**`asr.*` 那五个键每段重新读，`alarm.sound` 勾选那一刻就生效；设置抽屉那四项
（`311f84f5`：两个设备键位、`heartbeat.interval_seconds`、`summary.default_window`）
与会中切模型两键（`summary.presets_json` / `summary.active_preset`）
写盘那一刻快照就换新值 —— 这十二个合起来是热读键**：

    alarm.sound / heartbeat.interval_seconds / summary.default_window
    devices.mic / devices.loopback
    summary.presets_json / summary.active_preset
    asr.max_segment_seconds / asr.timeout_seconds / asr.max_new_tokens /
    asr.repeat_max_unit / asr.repeat_min_coverage

`alarm.sound`（本场出不出声的持久化开关）收在热读档，是为了让钩子上的「出声开关」勾选即生效：
`settings_update` 写盘成功那一刻快照里的 `settings.alarm_sound` 就换过去了。
设置抽屉那四项同理：设备写完 `reprobe` 按新值挑，心跳与缺省档的快照格当场换。
预设两键同理：设置抽屉换一套模型当场生效，会中也能切（每次总结都是现读配置、现造客户端）。

其余键（`web.*` / `log.*` / `alert.*` / `capture.*` / `transcriber.*` / `post.*` / `minutes.*`
与其余六个 `summary.*`）**改完要重启**。两档的划分以
`meeting/config.py` 的 `SETTING_KEY_GROUPS` 为准（热读 12 键 + 重启 28 键 = 键全集 40）。**没有 `output.root` 键**：一场会放哪由它的工作区目录给
（start 显式传 `args.dir`），配置清空也照样能开会。

**环境变量一律在启动时求值**，改了不会热生效。本服务的环境变量一共四个：
`MEETING_PORT` / `MEETING_LOG_DIR` / `MEETING_CAPTURE` /
`MEETING_NO_BROWSER`；另有 `MEETING_SUMMARY_API_KEY` 单独映射会中总结那一格的密钥
（它不改变哪几个键生效，只是给 `summary.api_key` 供一个不进配置文件的来源；预设模式走
预设里那一格密钥，这一格不参与）。

---

## 4. dev 替身怎么开（开发与测试用）

    MEETING_CAPTURE=dev meeting web          # 或配置键 capture.backend = "dev"

**这是非产品路径**，默认关闭：替身只发两路合成电平、不写真实音频（`finalize()` / `transcode()`
只记账），它的用处是「手上没有声卡 / 想复现某一路出事」时把界面与告警链路跑起来 ——
真机录制走第 1.1 节那条默认路径。

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
| `0` | 空闲态按 `Ctrl+C`；或者收尾干净（前三步走到 `done` / `skipped`，`post` 那一格停 `waiting` 也算收尾干净） |
| `1` | **收尾有步骤 `failed`**（这时终端上会打 log 路径与最后几行）；或者 **`meeting post` 那一趟没做成**（音频转不动、离线那一层 `failed`、纪要落 `failed` 或等它落终态等过了 `minutes.timeout_seconds`）；或者 **`meeting replay` 那一趟没做成**（整场落在 `failed`，或重放路上抛异常）；或者**服务起步这一步出意外**（组装应用 / 构造服务器对象的路上抛异常，终端上会打一行「服务起步失败」与 log 路径） |
| `2` | 端口被占（已有 meeting 服务 / 别的程序占着）；用法错误；配置值非法 |
| `3` | 启动前提不成立（`config.json` 损坏、主 log 不可写） |
| `130` | **收尾中途第二次 `Ctrl+C`**：立即中止，盘上 `interrupted: true`、**绝不写 `done`** |

纪要那一层落在 `failed` 才算 `1`；落在 **`delivered`**（已经交付给 DSH web 服务端）按成功算 ——
交付这一步做成了，后面的活不归这一层管。

`Ctrl+C` 的两阶段语义：第一次收尾（与 `stop` 同一条路径，页面看得见「收尾中」），
收尾落定后关停；第二次立即中止（先落盘、再 `cancel()` 收尾协程）。

按下第一次落在哪一档，看的是**这一刻有没有收尾在跑**：

- **正在录（`recording` / `alert`）**：起收尾、等它落定 → `0`（收尾有步骤 `failed` → `1`）；
- **刚起、还没开过任何一场（`idle`）**：直接退出 → `0`（启动也不读别的场次目录：盘上没走完
  收尾的那一场不被这次关停改写，也不再拦新场次 —— 场次的登记与"最近一场"由收编节点
  落在工作区注册表里，界面经 `GET /api/sessions` 读）；
- **已经停在收尾中 / 收尾失败的现场**（`ended` 里还有收尾在跑，或收尾失败后停着）：
  第一次按下就等同第二次 → `130`。

**收尾有步骤 `failed` 时服务不自杀**：进程继续活着、页面还能看、
新的「开始录音」一律回 `409 draining`（带上失败那一步与 log 路径），
出路是 `meeting post <场次目录>` 或看清现场后重启服务。

---

## 6. 默认位置与回归入口

| 东西 | 默认位置 |
|---|---|
| 配置 | `~/.config/meeting/config.json`（点号扁平键名的单层映射，例如 `{"web.port": 8765}`；**不迁移** V1 的 `~/.config/meeting_notes/`；**没有输出根键 —— 没有默认落点**） |
| 场次落点 | start 显式给的 `args.dir`（**已存在的目录**，软件不建目录；日期在 `session.json` 的 `date` 元信息字段里，不进目录名） |
| 前端产物 | `web.dist_dir`（默认 `web/dist`）。目录缺失时 `GET /` 给一张说明页、服务照常起，`/debug` 与 `/api/*` 照常可用；目录**在但是空的**回 404 —— 跑 `npm run build` 重新产出即可。目录存在性**按每请求判定**，产物一建好刷新即可见，不必重启服务 |

回归入口：

    python -m pytest -q -m "not slow"   # 快档：没改模型调用 / 采集 / 服务运行时的时候跑这一条
    python -m pytest -q -m slow         # 慢档：只跑真子进程与真墙钟那一批
    python -m pytest -q                 # 全量：提交前、或改了核心代码之后

**三档怎么选**：改文案、纯函数、离线产物这类用例走快档（一分钟上下）；动了模型调用、采集、
转写、服务运行时的代码，跑慢档那一批（`pytestmark = pytest.mark.slow` 的文件级标记，现在 24 个文件）；
拿不准就跑全量（两分半上下，不加 `-m` 时默认就是
它）。慢档比快档慢在真子进程与真墙钟等待上，跳过它不等于跳过覆盖率 —— 但**被测代码落在慢档
文件里时必须跑全量**。归位判据（一条用例什么时候算慢）与慢档文件的清单在
`docs/service.md` 第 7 章「快慢分档」，标记 `slow` 注册在 `pyproject.toml` 的
`[tool.pytest.ini_options]`（带 `--strict-markers`：代码里 `pytest.mark.xxx` 用到的标记名
必须先在 `markers` 里注册，没注册就当场报 unknown mark）。

### 6.1 测试与验证纪律

**打模型端点花的是机主的 token，跟跑代码只耗电是两回事。** 所以：

- `tests/` 里的用例一律**注入假件**，一条真实模型端点请求都不发；出口另有一道 autouse 兜底
  （`tests/conftest.py` 的 `_no_real_model_call`：本机回环放行，其余一律拦下）。
- 真实端点、真实会议素材的验证写成**一次性脚本**，跑一次、看完证据就完事，**不进 `tests/`**。
  会中总结那一层的那份脚本与四档证据当时就是这么跑的：会花钱的两档收在显式开关后面、默认拒跑。
  这类脚本与证据属一次性材料，不随仓库保留。
- `tests/` 要**便宜且确定**：单条用例 ≤5 秒、单个测试文件 ≤10 秒，等真墙钟的那几条走慢档。

---

## 7. 前端开发

界面工程在 `web/`（React 18 + TypeScript 严格模式 + Vite 6 + vitest 2 + zod）。
**前提：Node ≥ 20**（本机实测 node v26.9.0 / npm 11.18.0）。

    cd web
    npm install        # 装依赖（只需一次）

**改界面**（要边改边看）：服务照常跑着，另开一个终端起 vite：

    cd web
    npm run dev        # vite 起在 5173，/api 转发到 http://127.0.0.1:${MEETING_PORT ?? 8765}

然后开 `http://127.0.0.1:5173` —— 这条路上改一行文案走 HMR，不必刷新页面。
服务端口不是默认的 8765 时，起 vite 前把 `MEETING_PORT` 设成实际端口，转发才对得上。

**开会**（用真界面）：先构建，再由服务直发产物 —— **运行时不需要 Node**：

    cd web
    npm run build      # npm run typecheck && vite build，产物落 web/dist
    cd ..
    meeting web        # 从服务端口（如 http://127.0.0.1:8765）打开就是这份产物

产物目录的存在性**按每请求判定**：构建完不必重启服务，刷新即可见；产物被删掉、刷新回说明页。

自检：

    cd web
    npm test           # vitest run，21 个文件 480 个用例
    npm run typecheck  # 两条 tsc --noEmit -p，各查各的 project

`npm run build` 里已经含 `npm run typecheck`，所以构建过一次就不必再单独跑；只改类型不看产物时跑 `typecheck` 更快。契约正文在 `docs/contract.md`，可机读基准（35 个 golden 样本与掩码规则）在 `contract/`，前端工程的五个脚本（`dev` / `typecheck` / `build` / `preview` / `test`）与其余说明在 `web/README.md`。

---

## 8. 两个「心跳」别串线

- **界面心跳**（`heartbeat.interval_seconds`，可配 1–60 秒，默认 10 秒）管**失联横幅**：
  界面按这个周期问一次服务，问不着就挂横幅。
- **服务侧采集帧心跳**（`max(2.0s, 3 × frame_interval)` = **2.0 秒**）管 **`recording_alive`**：
  它看的是采集帧还在不在来，用来报出「HTTP 通着但录音停了」。

两者互不参与对方的计算：收尾预算也不拿这两个周期做任何计算。
